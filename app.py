import asyncio
import ipaddress
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse

from config import load_config, CONFIG_DIR, CONVERSATIONS_DIR
from debate_engine import DebateSession, LLMClient
from chat_engine import ChatSession, CHAT_SYSTEM_PROMPTS
from db import (init_db, create_user, verify_user, create_session, verify_session, delete_session,
    get_user_models, add_user_model, get_user_model, update_user_model, delete_user_model,
    get_quota_usage, increment_quota_usage, get_all_usage_stats, set_admin, get_all_users,
    cleanup_expired_sessions)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("arena")

UPLOAD_MAX_BYTES = 10 * 1024 * 1024  # 10 MB

app = FastAPI(title="AI Debate Arena")
config = load_config()
sessions: dict[str, DebateSession] = {}
model_health: dict[str, dict] = {}
_session_users: dict[str, int] = {}  # session_id -> user_id

# CORS
cors_cfg = config.get("cors", {})
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_cfg.get("allowed_origins", ["*"]),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request logging middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    path = request.url.path
    if path in ("/", "/favicon.ico") or path.startswith("/static"):
        return await call_next(request)
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = (time.monotonic() - start) * 1000
    if response.status_code == 429:
        response.headers["Retry-After"] = "60"
    log.info("%s %s → %d (%.0fms)", request.method, path, response.status_code, duration_ms)
    return response

# Rate limiting (in-memory, fine for small groups)
_rate_timestamps: dict[int, list[float]] = {}  # user_id -> timestamps
_rate_concurrent: dict[int, int] = {}  # user_id -> running count
_rate_lock = asyncio.Lock()

# SSRF protection
_ALLOWED_URL_SCHEMES = {"https", "http"}
_MAX_MESSAGE_CHARS = 32768  # ~32K chars


def _validate_url(url: str) -> str:
    """Validate a URL is safe to fetch — rejects private/internal IPs and non-HTTP schemes."""
    url = url.strip().rstrip("/")
    if not url:
        raise HTTPException(400, "URL 不能为空")
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_URL_SCHEMES:
        raise HTTPException(400, "仅支持 http/https 协议")
    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(400, "URL 格式无效")
    # Block private/loopback/link-local IPs
    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise HTTPException(400, "不允许访问内网地址")
    except ValueError:
        pass  # hostname is a domain, not an IP — check via DNS
    # Reject common internal hostnames
    internal_patterns = (
        r"^localhost$",
        r"^localhost\.local$",
        r"^127\.",
        r"^10\.",
        r"^172\.(1[6-9]|2\d|3[01])\.",
        r"^192\.168\.",
        r"^0\.",
        r"^\[::1\]",
    )
    for pat in internal_patterns:
        if re.match(pat, hostname, re.IGNORECASE):
            raise HTTPException(400, "不允许访问内网地址")
    return url


@app.on_event("startup")
async def startup():
    init_db()
    CONVERSATIONS_DIR.mkdir(exist_ok=True)
    cleanup_expired_sessions()
    await check_model_health()
    load_persisted_debates()
    log.info("Server started")


def get_current_user(request: Request) -> dict | None:
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        token = request.cookies.get("session_token", "")
    return verify_session(token)


def _require_admin(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "未登录")
    if not user.get("is_admin"):
        raise HTTPException(403, "需要管理员权限")
    return user


async def _check_rate_limit(user_id: int, is_chat: bool = False) -> tuple[bool, str]:
    async with _rate_lock:
        rl = config.get("rate_limit", {})
        max_per_hour = rl.get("max_chats_per_hour" if is_chat else "max_debates_per_hour", 10)
        max_concurrent = rl.get("max_concurrent_chats" if is_chat else "max_concurrent_debates", 3)
        now = time.time()
        hour_ago = now - 3600
        timestamps = [t for t in _rate_timestamps.get(user_id, []) if t > hour_ago]
        _rate_timestamps[user_id] = timestamps
        if len(timestamps) >= max_per_hour:
            label = "聊天" if is_chat else "辩论"
            return False, f"每小时最多创建 {max_per_hour} 次{label}，请稍后再试"
        concurrent = _rate_concurrent.get(user_id, 0)
        if concurrent >= max_concurrent:
            label = "聊天" if is_chat else "辩论"
            return False, f"最多同时进行 {max_concurrent} 场{label}，请等待当前结束"
        return True, ""


async def _record_debate_start(user_id: int):
    async with _rate_lock:
        _rate_timestamps.setdefault(user_id, []).append(time.time())
        _rate_concurrent[user_id] = _rate_concurrent.get(user_id, 0) + 1


async def _record_debate_end(user_id: int):
    async with _rate_lock:
        _rate_concurrent[user_id] = max(0, _rate_concurrent.get(user_id, 0) - 1)


async def check_model_health():
    import httpx
    for mid, m in config["models"].items():
        if not m.get("api_key"):
            model_health[mid] = {"ok": False, "error": "未配置 API Key"}
            continue
        try:
            auth_type = m.get("auth_type", "bearer")
            base_url = m["base_url"].rstrip("/")
            if auth_type == "api-key":
                headers = {"api-key": m["api_key"], "Content-Type": "application/json"}
            elif auth_type == "anthropic":
                headers = {"x-api-key": m["api_key"], "Content-Type": "application/json", "anthropic-version": "2023-06-01"}
            else:
                headers = {"Authorization": f"Bearer {m['api_key']}", "Content-Type": "application/json"}
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                if auth_type == "anthropic":
                    resp = await client.get(f"{base_url}", headers=headers)
                else:
                    resp = await client.get(f"{base_url}/models", headers=headers)
            model_health[mid] = {"ok": 200 <= resp.status_code < 300, "status": resp.status_code}
        except Exception as e:
            model_health[mid] = {"ok": False, "error": str(e)[:80]}


def load_persisted_debates():
    for f in CONVERSATIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("mode") == "chat":
                s = ChatSession.load_from_disk(f)
            else:
                s = DebateSession.load_from_disk(f)
            sessions[s.id] = s
            owner_id = getattr(s, "owner_id", None)
            if owner_id is not None:
                _session_users[s.id] = owner_id
        except Exception:
            pass


def _resolve_model(model_key: str, user_id: int = None) -> dict | None:
    """Resolve a model config from global config or user's custom models."""
    if model_key.startswith("global:"):
        mid = model_key[7:]
        return config["models"].get(mid)
    if model_key.startswith("user:"):
        raw_id = model_key[5:]
        try:
            mid = int(raw_id)
            if user_id:
                return get_user_model(user_id, mid)
        except ValueError:
            pass
        return None
    if user_id:
        try:
            mid = int(model_key)
            m = get_user_model(user_id, mid)
            if m:
                return m
        except ValueError:
            pass
    return config["models"].get(model_key)


def _require_session_owner(request: Request, session_id: str):
    """Return (user, session) if session exists and belongs to current user, else raise."""
    if session_id not in sessions:
        raise HTTPException(404, "会话不存在")
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "未登录")
    session = sessions[session_id]
    owner = _session_users.get(session_id) or getattr(session, "owner_id", None)
    # Admins can access any session; otherwise check ownership
    if owner is not None and owner != user["user_id"] and not user.get("is_admin"):
        raise HTTPException(403, "无权访问此会话")
    return user, session


def _resolve_session_model(session, which: str = "a") -> dict | None:
    """Resolve model config from session, using owner_id for user models."""
    key = session.model_a_key if which == "a" else session.model_b_key
    owner_id = getattr(session, 'owner_id', None)
    return _resolve_model(key, owner_id)


@app.get("/", response_class=HTMLResponse)
async def index():
    return (CONFIG_DIR / "templates" / "index.html").read_text(encoding="utf-8")


# ===== Auth =====

@app.post("/api/auth/register")
async def register(request: Request):
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or len(username) < 2:
        raise HTTPException(400, "用户名至少2个字符")
    if not password or len(password) < 4:
        raise HTTPException(400, "密码至少4个字符")
    if create_user(username, password):
        return {"ok": True, "message": "注册成功"}
    raise HTTPException(409, "用户名已存在")


@app.post("/api/auth/login")
async def login(request: Request):
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    user = verify_user(username, password)
    if not user:
        raise HTTPException(401, "用户名或密码错误")
    token = create_session(user["id"])
    return {"ok": True, "token": token, "username": user["username"]}


@app.post("/api/auth/logout")
async def logout(request: Request):
    user = get_current_user(request)
    if user:
        delete_session(user["token"])
    return {"ok": True}


@app.get("/api/auth/me")
async def get_me(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "未登录")
    return {"id": user["user_id"], "username": user["username"], "is_admin": bool(user.get("is_admin", 0))}


# ===== Config + Health =====

@app.get("/api/config")
async def get_config():
    return {
        "models": {
            mid: {
                "id": mid, "name": m["name"],
                "has_key": bool(m.get("api_key")),
                "health": model_health.get(mid, {}),
            }
            for mid, m in config["models"].items()
        },
        "quota": config.get("quota", {}),
    }


@app.get("/api/health")
async def get_health():
    return model_health


@app.post("/api/health/refresh")
async def refresh_health():
    await check_model_health()
    return model_health


# ===== User Models =====

@app.get("/api/user/models")
async def list_user_models(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "未登录")
    models = get_user_models(user["user_id"])
    return {"models": [{"id": m["id"], "name": m["name"], "base_url": m["base_url"],
                        "model": m["model"], "auth_type": m["auth_type"], "created_at": m["created_at"]}
                       for m in models]}


@app.post("/api/user/models")
async def add_custom_model(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "未登录")
    data = await request.json()
    name = data.get("name", "").strip()
    base_url = data.get("base_url", "").strip()
    api_key = data.get("api_key", "").strip()
    model = data.get("model", "").strip()
    auth_type = data.get("auth_type", "bearer")
    if not all([name, base_url, api_key, model]):
        raise HTTPException(400, "请填写所有必填项")
    _validate_url(base_url)
    mid = add_user_model(user["user_id"], name, base_url, api_key, model, auth_type)
    return {"ok": True, "id": mid}


@app.put("/api/user/models/{model_id}")
async def edit_custom_model(model_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "未登录")
    data = await request.json()
    fields = {}
    for k in ("name", "base_url", "api_key", "model", "auth_type"):
        if k in data:
            fields[k] = data[k]
    if "base_url" in fields:
        _validate_url(fields["base_url"])
    if not update_user_model(model_id, user["user_id"], **fields):
        raise HTTPException(404, "模型不存在")
    return {"ok": True}


@app.delete("/api/user/models/{model_id}")
async def remove_custom_model(model_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "未登录")
    delete_user_model(model_id, user["user_id"])
    return {"ok": True}


@app.get("/api/user/models/available")
async def get_available_models(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "未登录")
    result = {}
    for mid, m in config["models"].items():
        result[f"global:{mid}"] = {
            "id": f"global:{mid}", "name": m["name"],
            "has_key": bool(m.get("api_key")),
            "health": model_health.get(mid, {}),
            "source": "global",
        }
    if user:
        for m in get_user_models(user["user_id"]):
            result[f"user:{m['id']}"] = {
                "id": f"user:{m['id']}", "name": m["name"],
                "base_url": m["base_url"], "model": m["model"],
                "auth_type": m["auth_type"],
                "has_key": True,
                "source": "user",
            }
    return {"models": result}


# ===== Debates =====

@app.get("/api/debates")
async def list_debates(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "未登录")
    result = []
    for s in sessions.values():
        owner = _session_users.get(s.id) or getattr(s, "owner_id", None)
        if owner is not None and owner != user["user_id"] and not user.get("is_admin"):
            continue
        usage = dict(getattr(s, "token_usage", {}))
        if usage:
            usage["_total"] = sum(v.get("total", 0) for v in usage.values() if isinstance(v, dict))
        result.append({
            "id": s.id, "topic": s.topic,
            "name_a": s.name_a, "name_b": s.name_b,
            "name_c": getattr(s, "name_c", ""),
            "status": s.status, "status_detail": getattr(s, "status_detail", ""),
            "round": s.round, "mode": getattr(s, "mode", "sequential"),
            "max_rounds": s.max_rounds,
            "memory_masking": getattr(s, "memory_masking", False),
            "diversity_retention": getattr(s, "diversity_retention", False),
            "token_usage": usage,
            "created_at": s.created_at,
        })
    return {"debates": result}


@app.post("/api/debate/start")
async def start_debate(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "未登录")
    data = await request.json()
    topic = data.get("topic", "").strip()
    model_a_id = data.get("model_a")
    model_b_id = data.get("model_b")
    model_c_id = data.get("model_c")
    max_rounds = data.get("max_rounds", config.get("max_rounds", 20))
    disagreement_threshold = data.get("disagreement_threshold", config.get("disagreement_threshold", 5))
    mode = data.get("mode", "sequential")
    memory_masking = data.get("memory_masking", False)
    masking_model_id = data.get("masking_model")
    diversity_retention = data.get("diversity_retention", False)
    embedding_url = data.get("embedding_url", "").strip()
    embedding_key = data.get("embedding_key", "").strip()
    embedding_model_id = data.get("embedding_model", "").strip()
    if diversity_retention and embedding_url:
        _validate_url(embedding_url)

    if mode not in ("sequential", "blind", "chain3"):
        raise HTTPException(400, "无效的辩论模式")
    if not topic:
        raise HTTPException(400, "请输入讨论主题")
    topic_max = config.get("quota", {}).get("topic_max_length", 2000)
    if len(topic) > topic_max:
        raise HTTPException(400, f"主题长度不能超过 {topic_max} 个字符")

    # Rate limit
    allowed, rl_msg = await _check_rate_limit(user["user_id"])
    if not allowed:
        raise HTTPException(429, rl_msg)

    # Check if any model uses shared (global with key) — counts against quota
    uses_shared = False
    for mk in (model_a_id, model_b_id, model_c_id):
        if mk and mk.startswith("global:"):
            m = config["models"].get(mk[7:])
            if m and m.get("api_key"):
                uses_shared = True
                break

    if uses_shared:
        quota_limit = config.get("quota", {}).get("monthly_limit", 50)
        current_usage = get_quota_usage(user["user_id"])
        if current_usage >= quota_limit:
            raise HTTPException(429, f"本月共享模型辩论次数已达上限 ({quota_limit})，请下月再试或使用自带的模型")

    if model_a_id == model_b_id:
        raise HTTPException(400, "请选择两个不同的模型")
    if mode == "chain3" and not model_c_id:
        raise HTTPException(400, "三人模式需要选择三个模型")
    if mode != "chain3" and model_c_id:
        raise HTTPException(400, "仅三人模式支持第三个模型")
    if model_c_id and model_c_id in (model_a_id, model_b_id):
        raise HTTPException(400, "三个模型必须不同")
    max_rounds = max(1, min(int(max_rounds), 50))
    disagreement_threshold = max(1, min(int(disagreement_threshold), 20))

    ma_cfg = _resolve_model(model_a_id, user["user_id"])
    mb_cfg = _resolve_model(model_b_id, user["user_id"])
    if not ma_cfg:
        raise HTTPException(400, "模型 A 不存在")
    if not ma_cfg.get("api_key"):
        raise HTTPException(400, f"模型 A（{ma_cfg.get('name', model_a_id)}）未配置 API Key，请在模型管理中添加自己的 Key")
    if not mb_cfg:
        raise HTTPException(400, "模型 B 不存在")
    if not mb_cfg.get("api_key"):
        raise HTTPException(400, f"模型 B（{mb_cfg.get('name', model_b_id)}）未配置 API Key，请在模型管理中添加自己的 Key")

    # 3rd model
    mc_cfg = None
    if model_c_id:
        mc_cfg = _resolve_model(model_c_id, user["user_id"])
        if not mc_cfg:
            raise HTTPException(400, "模型 C 不存在")
        if not mc_cfg.get("api_key"):
            raise HTTPException(400, f"模型 C（{mc_cfg.get('name', model_c_id)}）未配置 API Key")

    # Masking model
    masking_client = None
    if memory_masking and masking_model_id:
        mm_cfg = _resolve_model(masking_model_id, user["user_id"])
        if mm_cfg:
            masking_client = LLMClient(mm_cfg["base_url"], mm_cfg["api_key"], mm_cfg["model"], mm_cfg.get("auth_type", "bearer"))

    kwargs = dict(
        topic=topic,
        model_a_key=model_a_id,
        model_b_key=model_b_id,
        model_a=LLMClient(ma_cfg["base_url"], ma_cfg["api_key"], ma_cfg["model"], ma_cfg.get("auth_type", "bearer")),
        model_b=LLMClient(mb_cfg["base_url"], mb_cfg["api_key"], mb_cfg["model"], mb_cfg.get("auth_type", "bearer")),
        name_a=ma_cfg["name"],
        name_b=mb_cfg["name"],
        max_rounds=max_rounds,
        disagreement_threshold=disagreement_threshold,
        mode=mode,
        memory_masking=memory_masking,
        masking_model=masking_client,
        diversity_retention=diversity_retention,
        embedding_url=embedding_url,
        embedding_key=embedding_key,
        embedding_model=embedding_model_id,
    )

    if mc_cfg:
        kwargs["model_c_key"] = model_c_id
        kwargs["model_c"] = LLMClient(mc_cfg["base_url"], mc_cfg["api_key"], mc_cfg["model"], mc_cfg.get("auth_type", "bearer"))
        kwargs["name_c"] = mc_cfg["name"]

    session = DebateSession(**kwargs)
    session.owner_id = user["user_id"]
    lang = data.get("lang", "zh")
    if lang in ("zh", "en"):
        session.set_lang(lang)
    sessions[session.id] = session
    _session_users[session.id] = user["user_id"]
    await _record_debate_start(user["user_id"])
    if uses_shared:
        increment_quota_usage(user["user_id"])

async def _auto_score_session(session):
    if not session.history:
        return
    try:
        model_cfg = _resolve_session_model(session, "a")
        if not model_cfg:
            return
        client = LLMClient(model_cfg["base_url"], model_cfg["api_key"], model_cfg["model"], model_cfg.get("auth_type", "bearer"))
        transcript = "\n\n".join(f"R{e['round']} — {e['model']}\n\n{e['content'][:3000]}" for e in session.history[-10:])
        prompt = (
            f"Rate this debate on '{session.topic}' ({session.round} rounds, {session.status}). "
            f"Output ONLY a JSON object: "
            f"{{\"logic\": <1-10>, \"evidence\": <1-10>, \"rebuttal\": <1-10>, "
            f"\"conclusion\": <1-10>, \"efficiency\": <1-10>, \"overall\": <1-10>}}\n\n"
            f"Transcript (last 10 entries):\n{transcript}"
        )
        result = await client.chat([{"role": "user", "content": prompt}])
        session.quality_score = result
        session.save_to_disk()
    except Exception:
        pass

    async def _run_and_cleanup():
        await session.run()
        await asyncio.sleep(0.5)
        uid = _session_users.pop(session.id, None)
        if uid:
            await _record_debate_end(uid)
        if session.history and getattr(session, 'mode', '') != 'chat' and session.status not in ("running", "idle", "error"):
            asyncio.create_task(_auto_score_session(session))
    asyncio.create_task(_run_and_cleanup())
    return {"session_id": session.id}


@app.get("/api/debate/stream/{session_id}")
async def stream_debate(session_id: str, request: Request):
    user, session = _require_session_owner(request, session_id)
    queue = session.get_events()

    async def event_generator():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield event.to_sse()
                    if event.type in ("report_end", "error"):
                        break
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.websocket("/ws/debate/{session_id}")
async def ws_debate(websocket: WebSocket, session_id: str):
    if session_id not in sessions:
        await websocket.close(code=4004, reason="会话不存在")
        return
    # Auth via query param or header
    token = websocket.query_params.get("token", "")
    if not token:
        token = websocket.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not verify_session(token):
        await websocket.close(code=4001, reason="未登录")
        return
    await websocket.accept()
    queue = sessions[session_id].get_events()
    # Send current history as initial batch
    session = sessions[session_id]
    # Verify ownership
    owner = _session_users.get(session_id) or getattr(session, "owner_id", None)
    if owner is not None:
        u = verify_session(token)
        if u and u["user_id"] != owner and not u.get("is_admin"):
            await websocket.close(code=4003, reason="无权访问此会话")
            return
    init_data = {
        "type": "init",
        "history": session.history,
        "status": session.status,
        "round": session.round,
        "token_usage": session.token_usage,
    }
    try:
        await websocket.send_json(init_data)
    except Exception:
        await websocket.close()
        return
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20)
                ws_msg = {"type": event.type, "data": event.data}
                await websocket.send_json(ws_msg)
                if event.type in ("report_end", "error"):
                    break
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        sessions[session_id].clear_queue()
    except Exception:
        pass


@app.post("/api/debate/stop/{session_id}")
async def stop_debate(session_id: str, request: Request):
    user, session = _require_session_owner(request, session_id)
    session.stop()
    uid = _session_users.pop(session_id, None)
    if uid:
        _record_debate_end(uid)
    return {"ok": True}


@app.delete("/api/debate/{session_id}")
async def delete_debate(session_id: str, request: Request):
    user, session = _require_session_owner(request, session_id)
    session.clear_queue()
    sessions.pop(session_id, None)
    uid = _session_users.pop(session_id, None)
    if uid:
        _record_debate_end(uid)
    for f in CONVERSATIONS_DIR.glob(f"{session_id}*"):
        try: f.unlink()
        except Exception: pass
    return {"ok": True}


@app.put("/api/debate/{session_id}")
async def rename_session(session_id: str, request: Request):
    user, session = _require_session_owner(request, session_id)
    data = await request.json()
    new_topic = data.get("topic", "").strip()
    if not new_topic or len(new_topic) > 500:
        raise HTTPException(400, "名称长度必须在 1-500 之间")
    session.topic = new_topic
    session.save_to_disk()
    return {"ok": True, "topic": new_topic}


@app.get("/api/debate/history/{session_id}")
async def get_history(session_id: str, request: Request):
    user, s = _require_session_owner(request, session_id)
    usage = dict(getattr(s, "token_usage", {}))
    if usage:
        usage["_total"] = sum(v.get("total", 0) for v in usage.values() if isinstance(v, dict))
    return {
        "id": s.id, "topic": s.topic, "name_a": s.name_a, "name_b": s.name_b,
        "name_c": getattr(s, "name_c", ""),
        "status": s.status, "status_detail": getattr(s, "status_detail", ""),
        "round": s.round, "mode": getattr(s, "mode", "sequential"),
        "history": s.history, "report": s.report, "report_model": s.report_model,
        "token_usage": usage,
        "quality_score": getattr(s, "quality_score", ""),
    }


@app.get("/api/debate/export/{session_id}")
async def export_debate(session_id: str, request: Request):
    user, session = _require_session_owner(request, session_id)
    return {"content": session.save_markdown()}


@app.get("/api/debate/export/pdf/{session_id}")
async def export_debate_pdf(session_id: str, request: Request):
    user, session = _require_session_owner(request, session_id)
    md_content = session.save_markdown()
    pdf_path = await _md_to_pdf(md_content, f"debate_{session_id}")
    return FileResponse(pdf_path, media_type="application/pdf", filename=f"debate_{session_id}.pdf")


def _build_compile_prompt(session) -> str:
    transcript = "\n\n".join(f"## 第 {e['round']} 轮 — {e['model']}\n\n{e['content']}" for e in session.history)
    model_count = "多个" if session.mode == "chain3" else "两个"
    return (
        f"基于以下{model_count}AI模型关于「{session.topic}」的讨论记录，"
        f"请整理出一份完整、严谨的分析。\n\n"
        f"要求：\n1. 综合模型达成共识的部分\n"
        f"2. 对于仍存在分歧的部分，给出你认为更正确的推导或实现\n"
        f"3. 数学/物理问题使用清晰的步骤和 LaTeX 公式；代码问题给出完整实现和分析\n"
        f"4. 标注关键结论\n\n"
        f"讨论记录：\n\n{transcript}"
    )


def _get_compile_client(session, use_model: str = "a"):
    model_cfg = _resolve_session_model(session, use_model)
    if not model_cfg:
        raise HTTPException(400, "模型配置未找到")
    return LLMClient(model_cfg["base_url"], model_cfg["api_key"], model_cfg["model"], model_cfg.get("auth_type", "bearer"))


@app.post("/api/debate/compile/pdf/{session_id}")
async def export_compile_pdf(session_id: str, request: Request):
    user, session = _require_session_owner(request, session_id)
    if not session.history:
        raise HTTPException(400, "对话记录为空")
    data = await request.json()
    client = _get_compile_client(session, data.get("model", "a"))
    result = await client.chat([{"role": "user", "content": _build_compile_prompt(session)}])
    pdf_path = await _md_to_pdf(result, f"derivation_{session_id}")
    return FileResponse(pdf_path, media_type="application/pdf", filename=f"derivation_{session_id}.pdf")


@app.post("/api/debate/compile/{session_id}")
async def compile_debate(session_id: str, request: Request):
    user, session = _require_session_owner(request, session_id)
    if not session.history:
        raise HTTPException(400, "对话记录为空")
    data = await request.json()
    client = _get_compile_client(session, data.get("model", "a"))
    result = await client.chat([{"role": "user", "content": _build_compile_prompt(session)}])
    return {"content": result}


@app.get("/api/debate/compile/stream/{session_id}")
async def compile_debate_stream(session_id: str, request: Request, model: str = "a"):
    user, session = _require_session_owner(request, session_id)
    if not session.history:
        raise HTTPException(400, "对话记录为空")
    client = _get_compile_client(session, model)
    prompt = _build_compile_prompt(session)

    async def generate():
        from llm_client import is_thinking_token
        async for token in client.chat_stream([{"role": "user", "content": prompt}]):
            if not is_thinking_token(token):
                yield token

    return StreamingResponse(
        generate(),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/debate/search")
async def search_debates(request: Request, q: str = ""):
    if not get_current_user(request):
        raise HTTPException(401, "未登录")
    if not q.strip():
        return {"results": []}
    q = q.strip().lower()
    user = get_current_user(request)
    results = []
    for s in sessions.values():
        owner = _session_users.get(s.id) or getattr(s, "owner_id", None)
        if owner is not None and owner != user["user_id"] and not user.get("is_admin"):
            continue
        score = 0
        if q in s.topic.lower():
            score += 10
        for entry in s.history:
            if q in entry["content"].lower():
                score += 1
        if score > 0:
            results.append({
                "id": s.id, "topic": s.topic,
                "name_a": s.name_a, "name_b": s.name_b,
                "status": s.status, "round": s.round,
                "mode": getattr(s, "mode", "sequential"),
                "max_rounds": s.max_rounds,
                "created_at": s.created_at, "score": score,
            })
    results.sort(key=lambda x: x["score"], reverse=True)
    return {"results": results}


@app.get("/api/debate/analyze/{session_id}")
async def analyze_persuasion(session_id: str, request: Request):
    """Use LLM to analyze persuasion dynamics after debate."""
    user, session = _require_session_owner(request, session_id)
    if not session.history or session.status == "running":
        raise HTTPException(400, "辩论尚未结束")
    model_cfg = _resolve_session_model(session, "a")
    if not model_cfg:
        model_cfg = config["models"].get(list(config["models"].keys())[0])
    if not model_cfg:
        raise HTTPException(400, "模型配置未找到")
    client = LLMClient(model_cfg["base_url"], model_cfg["api_key"], model_cfg["model"], model_cfg.get("auth_type", "bearer"))
    transcript = "\n\n".join(f"## 第 {e['round']} 轮 — {e['model']}\n\n{e['content']}" for e in session.history)
    model_count = "多个" if session.mode == "chain3" else "两个"
    prompt = (
        f"以下是{model_count}AI模型就「{session.topic}」的辩论记录。\n\n"
        f"请分析**说服力动态**，包括：\n\n"
        f"1. **立场变化追踪**：每个模型的核心观点在辩论过程中是否发生了变化？如果变化了，是什么导致的？\n"
        f"2. **说服力评分**：为每个模型给出 1-10 的说服力评分，并说明理由。考虑论据质量、逻辑严谨性、反驳有效性。\n"
        f"3. **关键转折点**：辩论中最重要的 2-3 个转折点是什么？哪次回应最有说服力？\n"
        f"4. **论证风格对比**：各模型的论证风格有什么不同？各有什么优劣势？\n\n"
        f"辩论记录：\n\n{transcript}"
    )
    result = await client.chat([{"role": "user", "content": prompt}])
    return {"content": result}


@app.get("/api/debate/score/{session_id}")
async def score_debate(session_id: str, request: Request):
    """Use LLM to score debate quality after it ends."""
    user, session = _require_session_owner(request, session_id)
    if not session.history or session.status == "running":
        raise HTTPException(400, "辩论尚未结束")
    model_cfg = _resolve_session_model(session, "b")
    if not model_cfg:
        model_cfg = config["models"].get(list(config["models"].keys())[0])
    if not model_cfg:
        raise HTTPException(400, "模型配置未找到")
    client = LLMClient(model_cfg["base_url"], model_cfg["api_key"], model_cfg["model"], model_cfg.get("auth_type", "bearer"))
    transcript = "\n\n".join(f"## 第 {e['round']} 轮 — {e['model']}\n\n{e['content']}" for e in session.history)
    prompt = (
        f"以下是AI模型就「{session.topic}」的辩论记录（{session.round} 轮，{session.status}）。\n\n"
        f"请对这场辩论进行**综合评分**，以 Markdown 表格形式呈现：\n\n"
        f"| 评分维度 | 得分 (1-10) | 说明 |\n|----------|-----------|------|\n"
        f"| 逻辑严谨性 | | |\n| 论据充分性 | | |\n| 反驳有效性 | | |\n| 结论可靠性 | | |\n| 讨论效率 | | |\n| **综合得分** | | |\n\n"
        f"请在表格下方补充：\n"
        f"1. **最佳论点**：整场辩论中最有说服力的论点是什么？\n"
        f"2. **关键缺陷**：双方讨论中最大的漏洞或不足是什么？\n"
        f"3. **最终判断**：基于讨论内容，你认为哪个模型在核心问题上更正确？\n\n"
        f"辩论记录：\n\n{transcript}"
    )
    result = await client.chat([{"role": "user", "content": prompt}])
    return {"content": result}


# ===== Quota & Admin =====

@app.get("/api/user/quota")
async def get_user_quota(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "未登录")
    limit = config.get("quota", {}).get("monthly_limit", 50)
    used = get_quota_usage(user["user_id"])
    return {"limit": limit, "used": used, "remaining": max(0, limit - used)}


@app.get("/api/admin/stats")
async def admin_stats(request: Request):
    _require_admin(request)
    return {
        "users": get_all_users(),
        "usage": get_all_usage_stats(),
        "running_debates": {uid: cnt for uid, cnt in _rate_concurrent.items() if cnt > 0},
    }


@app.post("/api/admin/set-admin")
async def admin_set_admin(request: Request):
    _require_admin(request)
    data = await request.json()
    user_id = data.get("user_id")
    is_admin = data.get("is_admin", False)
    if not user_id:
        raise HTTPException(400, "缺少 user_id")
    set_admin(user_id, is_admin)
    return {"ok": True}


# ===== User Model Health =====

_user_model_health: dict[int, dict] = {}
_user_health_cache_time: float = 0
USER_HEALTH_CACHE_TTL = 300  # 5 minutes

@app.get("/api/user/models/health")
async def check_user_models_health(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "未登录")
    import time as _time
    global _user_health_cache_time
    if _time.time() - _user_health_cache_time < USER_HEALTH_CACHE_TTL and _user_model_health:
        return {str(uid): h for uid, h in _user_model_health.items()}
    import httpx
    models = get_user_models(user["user_id"])
    result = {}
    for m in models:
        mid = m["id"]
        try:
            auth_type = m.get("auth_type", "bearer")
            base_url = m["base_url"].rstrip("/")
            if auth_type == "api-key":
                headers = {"api-key": m["api_key"], "Content-Type": "application/json"}
            elif auth_type == "anthropic":
                headers = {"x-api-key": m["api_key"], "Content-Type": "application/json", "anthropic-version": "2023-06-01"}
            else:
                headers = {"Authorization": f"Bearer {m['api_key']}", "Content-Type": "application/json"}
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                if auth_type == "anthropic":
                    resp = await client.get(f"{base_url}", headers=headers)
                else:
                    resp = await client.get(f"{base_url}/models", headers=headers)
            result[str(mid)] = {"ok": resp.status_code < 500, "status": resp.status_code}
        except Exception as e:
            result[str(mid)] = {"ok": False, "error": str(e)[:80]}
    _user_model_health = {int(k): v for k, v in result.items()}
    _user_health_cache_time = _time.time()
    return result


# ===== Chat =====

@app.post("/api/chat/start")
async def start_chat(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "未登录")
    data = await request.json()
    model_id = data.get("model")
    if not model_id:
        raise HTTPException(400, "请选择模型")

    allowed, rl_msg = await _check_rate_limit(user["user_id"], is_chat=True)
    if not allowed:
        raise HTTPException(429, rl_msg)

    uses_shared = False
    if model_id.startswith("global:"):
        m = config["models"].get(model_id[7:])
        if m and m.get("api_key"):
            uses_shared = True
    if uses_shared:
        quota_limit = config.get("quota", {}).get("monthly_limit", 50)
        if get_quota_usage(user["user_id"]) >= quota_limit:
            raise HTTPException(429, f"本月共享模型使用次数已达上限 ({quota_limit})")

    model_cfg = _resolve_model(model_id, user["user_id"])
    if not model_cfg:
        raise HTTPException(400, "模型不存在")
    if not model_cfg.get("api_key"):
        raise HTTPException(400, f"模型（{model_cfg.get('name', model_id)}）未配置 API Key")

    client = LLMClient(
        model_cfg["base_url"], model_cfg["api_key"],
        model_cfg["model"], model_cfg.get("auth_type", "bearer")
    )
    session = ChatSession(
        model_key=model_id, model=client,
        name=model_cfg["name"], user_id=user["user_id"]
    )
    lang = data.get("lang", "zh")
    if lang in CHAT_SYSTEM_PROMPTS:
        session.set_lang(lang)
    sessions[session.id] = session
    _session_users[session.id] = user["user_id"]
    await _record_debate_start(user["user_id"])
    if uses_shared:
        increment_quota_usage(user["user_id"])
    return {"session_id": session.id}


@app.post("/api/chat/send/{session_id}")
async def chat_send(session_id: str, request: Request):
    user, session = _require_session_owner(request, session_id)
    if session.mode != "chat":
        raise HTTPException(400, "非聊天会话")
    if session.status == "running":
        raise HTTPException(429, "模型正在回复中")

    data = await request.json()
    message = data.get("message", "").strip()
    if not message:
        raise HTTPException(400, "消息不能为空")
    if len(message) > _MAX_MESSAGE_CHARS:
        raise HTTPException(400, f"消息长度不能超过 {_MAX_MESSAGE_CHARS} 个字符")

    if not session.model:
        user_id = getattr(session, 'owner_id', None)
        model_cfg = _resolve_model(session.model_key, user_id)
        if not model_cfg or not model_cfg.get("api_key"):
            raise HTTPException(400, "模型配置已失效，请删除此会话并重新创建")
        session.model = LLMClient(
            model_cfg["base_url"], model_cfg["api_key"],
            model_cfg["model"], model_cfg.get("auth_type", "bearer")
        )

    asyncio.create_task(session.send_message(message))
    return {"ok": True}


@app.websocket("/ws/chat/{session_id}")
async def ws_chat(websocket: WebSocket, session_id: str):
    if session_id not in sessions:
        await websocket.close(code=4004, reason="会话不存在")
        return
    token = websocket.query_params.get("token", "")
    if not token:
        token = websocket.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not verify_session(token):
        await websocket.close(code=4001, reason="未登录")
        return
    session = sessions[session_id]
    # Verify ownership
    owner = _session_users.get(session_id) or getattr(session, "owner_id", None)
    if owner is not None:
        u = verify_session(token)
        if u and u["user_id"] != owner and not u.get("is_admin"):
            await websocket.close(code=4003, reason="无权访问此会话")
            return
    await websocket.accept()
    init_data = {
        "type": "init",
        "history": session.history,
        "messages": session.messages,
        "status": session.status,
        "round": session.round,
        "token_usage": session.token_usage,
        "mode": "chat",
    }
    try:
        await websocket.send_json(init_data)
    except Exception:
        await websocket.close()
        return
    try:
        while True:
            try:
                event = await asyncio.wait_for(session.get_events().get(), timeout=20)
                ws_msg = {"type": event.type, "data": event.data}
                await websocket.send_json(ws_msg)
                if event.type in ("chat_turn_end", "error"):
                    break
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        sessions[session_id].clear_queue()
    except Exception:
        pass


@app.post("/api/upload")
async def upload_file(request: Request):
    if not get_current_user(request):
        raise HTTPException(401, "未登录")
    from fastapi.datastructures import UploadFile as UF
    form = await request.form()
    file: UF = form.get("file")
    if not file:
        raise HTTPException(400, "未提供文件")
    if not file.filename:
        raise HTTPException(400, "文件名无效")
    content_bytes = await file.read()
    if len(content_bytes) > UPLOAD_MAX_BYTES:
        raise HTTPException(413, f"文件大小不能超过 {UPLOAD_MAX_BYTES // (1024*1024)} MB")
    try:
        text = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "仅支持 UTF-8 编码的文本文件")
    return {"filename": file.filename, "content": text, "size": len(content_bytes)}


# ===== PDF =====

async def _md_to_pdf(markdown_content: str, name_prefix: str) -> str:
    import re
    html_body = _md_to_html(markdown_content)
    full_html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/styles/github.min.css">
<style>
@page {{ margin: 1.5cm 2cm; }}
body {{ font-family: -apple-system, "PingFang SC", "Noto Sans SC", "Helvetica Neue", sans-serif; padding: 0; line-height: 1.7; color: #1a1a2e; max-width: 780px; margin: 0 auto; }}
h1, h2, h3 {{ color: #1e293b; margin-top: 1.5em; page-break-after: avoid; }}
h1 {{ border-bottom: 2px solid #e2e8f0; padding-bottom: 0.3em; }}
h2 {{ border-bottom: 1px solid #f1f5f9; padding-bottom: 0.2em; }}
code {{ font-size: 0.88em; background: #f1f5f9; padding: 0.15em 0.35em; border-radius: 4px; }}
pre {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 1em; overflow-x: auto; font-size: 0.85em; page-break-inside: avoid; }}
pre code {{ background: none; padding: 0; }}
table {{ border-collapse: collapse; width: 100%; margin: 1em 0; page-break-inside: avoid; }}
th, td {{ border: 1px solid #e2e8f0; padding: 0.5em 0.8em; text-align: left; }}
th {{ background: #f8fafc; }}
blockquote {{ border-left: 3px solid #818cf8; margin: 1em 0; padding: 0.5em 1em; color: #475569; background: #f8fafc; border-radius: 0 4px 4px 0; }}
hr {{ border: none; border-top: 1px solid #e2e8f0; margin: 2em 0; }}
.math-block {{ margin: 1em 0; text-align: center; page-break-inside: avoid; }}
</style>
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></scr"+"ipt>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></scr"+"ipt>
<script src="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/lib/highlight.min.js"></scr"+"ipt>
</head><body><div class="content">{html_body}</div>
<script>
document.querySelectorAll('.content pre code').forEach(b => hljs.highlightElement(b));
document.querySelectorAll('.math-block').forEach(el => {{
  try {{ katex.render(el.dataset.tex, el, {{displayMode:true, throwOnError:false}}); }} catch(e) {{ el.textContent = el.dataset.tex; }}
}});
</scr"+"ipt></body></html>"""
    with tempfile.NamedTemporaryFile(suffix=".html", prefix=name_prefix, delete=False, mode="w") as f:
        f.write(full_html)
        html_path = f.name
    pdf_path = html_path.replace(".html", ".pdf")
    try:
        chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser") or (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" if Path("/Applications/Google Chrome.app").exists() else None
        )
        if not chrome:
            raise RuntimeError("No Chrome/Chromium found for PDF generation")
        subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--no-sandbox",
             f"--print-to-pdf={pdf_path}", "--print-to-pdf-no-header", html_path],
            capture_output=True, text=True, timeout=45,
        )
    except Exception as e:
        try: Path(html_path).unlink(missing_ok=True)
        except Exception: pass
        raise HTTPException(500, f"PDF 生成失败: {e}")
    finally:
        try: Path(html_path).unlink(missing_ok=True)
        except Exception: pass
    if not Path(pdf_path).exists():
        raise HTTPException(500, "PDF 生成失败")
    # Schedule cleanup of temp PDF after response
    import atexit
    def _cleanup(p=pdf_path, h=html_path):
        try: Path(p).unlink(missing_ok=True)
        except Exception: pass
        try: Path(h).unlink(missing_ok=True)
        except Exception: pass
    atexit.register(_cleanup)
    return pdf_path


def _md_to_html(markdown_content: str) -> str:
    import re
    safe = markdown_content.replace("<script", "&lt;script").replace("</script>", "&lt;/script&gt;").replace("<iframe", "&lt;iframe").replace("<img", "&lt;img").replace("<svg", "&lt;svg").replace("<input", "&lt;input").replace("<form", "&lt;form").replace("javascript:", "&amp;javascript:")
    blocks = []
    def save_block(m):
        blocks.append(m.group(1))
        return f"%%MATHBLOCK{len(blocks)-1}%%"
    safe = re.sub(r'\\\[(.+?)\\\]', save_block, safe, flags=re.DOTALL)
    safe = re.sub(r'\$\$(.+?)\$\$', save_block, safe, flags=re.DOTALL)
    lines = safe.split('\n')
    html_parts = []
    in_code = False
    in_table = False
    table_rows = []
    for line in lines:
        if line.strip().startswith('```'):
            if in_code:
                html_parts.append('</code></pre>'); in_code = False
            else:
                html_parts.append('<pre><code>'); in_code = True
            continue
        if in_code:
            html_parts.append(line.replace('<', '&lt;').replace('>', '&gt;'))
            continue
        h_match = re.match(r'^(#{1,6})\s+(.*)', line)
        if h_match:
            html_parts.append(f'<h{len(h_match.group(1))}>{h_match.group(2).strip().replace("<","&lt;").replace(">","&gt;")}</h{len(h_match.group(1))}>')
            continue
        if '|' in line and line.strip().startswith('|'):
            cells = [c.strip() for c in line.split('|')[1:-1]]
            if all(set(c) <= set('- :') for c in cells): continue
            if not in_table: html_parts.append('<table>'); in_table = True
            tag = 'th' if not table_rows else 'td'
            _esc = lambda x: x.replace("<", "&lt;").replace(">", "&gt;")
            html_parts.append(f'<tr>{"".join(f"<{tag}>{_esc(c)}</{tag}>" for c in cells)}</tr>')
            table_rows.append(cells)
            continue
        elif in_table:
            html_parts.append('</table>'); in_table = False; table_rows = []
        if line.strip().startswith('---'): html_parts.append('<hr>'); continue
        if line.startswith('> '):
            html_parts.append(f'<blockquote><p>{line[2:].replace("<","&lt;").replace(">","&gt;")}</p></blockquote>'); continue
        if re.match(r'^\d+\.\s', line): html_parts.append(f'<li>{line.replace("<","&lt;").replace(">","&gt;")}</li>'); continue
        if line.startswith('- ') or line.startswith('* '): html_parts.append(f'<li>{line[2:].replace("<","&lt;").replace(">","&gt;")}</li>'); continue
        if line.strip():
            html_parts.append(f'<p>{line.replace("<","&lt;").replace(">","&gt;")}</p>')
    if in_table: html_parts.append('</table>')
    result = '\n'.join(html_parts)
    for i, tex in enumerate(blocks):
        result = result.replace(f"%%MATHBLOCK{i}%%", f'<div class="math-block" data-tex="{tex.replace(chr(34), "&quot;")}"></div>')
    return result


if __name__ == "__main__":
    import uvicorn
    server = config.get("server", {})
    uvicorn.run(app, host=server.get("host", "0.0.0.0"), port=server.get("port", 8765))
