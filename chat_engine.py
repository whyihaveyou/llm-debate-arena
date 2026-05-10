import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path

from llm_client import LLMClient, is_thinking_token, strip_thinking_prefix

CONVERSATIONS_DIR = Path(__file__).parent / "conversations"

CHAT_SYSTEM_PROMPTS = {
    "zh": """你是一位知识渊博的助手，专精于数学、物理学和计算机科学（包括编程、算法、系统设计）。请用严谨、清晰的方式回答用户的问题。

格式要求：
- 数学公式使用 LaTeX（行内 $...$，独立公式 $$...$$）
- 代码使用 Markdown 代码块（标注语言）
- 推导过程要详细、逻辑要清晰""",
    "en": """You are a knowledgeable assistant specializing in mathematics, physics, and computer science (including programming, algorithms, and system design). Please answer questions in a rigorous and clear manner.

Formatting requirements:
- Use LaTeX for math formulas (inline $...$, display $$...$$)
- Use Markdown code blocks (with language tags)
- Provide detailed derivations with clear logic""",
}


class ChatSession:
    def __init__(self, model_key: str, model: LLMClient, name: str, user_id: int):
        self.id = uuid.uuid4().hex[:8]
        self.model_key = model_key
        self.model = model
        self.name = name
        self.owner_id = user_id
        self.mode = "chat"
        self.topic = ""
        self.status = "idle"
        self.status_detail = ""
        self.messages: list[dict] = [{"role": "system", "content": CHAT_SYSTEM_PROMPTS["zh"]}]
        self.history: list[dict] = []
        self.round = 0
        self.error_message = ""
        self.created_at = datetime.now().isoformat()
        self.token_usage = {}
        self._event_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._stop_flag = False
        self.lang = "zh"

    def set_lang(self, lang: str):
        self.lang = lang
        self.messages[0] = {"role": "system", "content": CHAT_SYSTEM_PROMPTS.get(lang, CHAT_SYSTEM_PROMPTS["zh"])}

    def _emit(self, event_type: str, data: dict):
        try:
            self._event_queue.put_nowait(_ChatEvent(event_type, data))
        except asyncio.QueueFull:
            try:
                self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._event_queue.put_nowait(_ChatEvent(event_type, data))
            except asyncio.QueueFull:
                pass

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "topic": self.topic,
            "model_a_key": self.model_key,
            "name_a": self.name,
            "name_b": "",
            "name_c": "",
            "mode": "chat",
            "status": self.status,
            "status_detail": self.status_detail,
            "round": self.round,
            "max_rounds": 0,
            "created_at": self.created_at,
            "history": self.history,
            "messages": self.messages,
            "token_usage": self.token_usage,
            "error_message": self.error_message,
            "owner_id": getattr(self, 'owner_id', None),
        }

    def save_to_disk(self):
        CONVERSATIONS_DIR.mkdir(exist_ok=True)
        path = CONVERSATIONS_DIR / f"{self.id}.json"
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def load_from_disk(filepath: Path) -> "ChatSession":
        data = json.loads(filepath.read_text(encoding="utf-8"))
        s = ChatSession.__new__(ChatSession)
        s.id = data["id"]
        s.model_key = data.get("model_a_key", "")
        s.name = data.get("name_a", "")
        s.model = None
        s.owner_id = data.get("owner_id")
        s.mode = "chat"
        s.topic = data.get("topic", "")
        s.status = data.get("status", "idle")
        s.status_detail = data.get("status_detail", "")
        s.round = data.get("round", 0)
        s.error_message = data.get("error_message", "")
        s.created_at = data.get("created_at", "")
        s.history = data.get("history", [])
        s.messages = data.get("messages", [])
        s.token_usage = data.get("token_usage", {})
        s._event_queue = asyncio.Queue(maxsize=1000)
        s._stop_flag = False
        return s

    async def send_message(self, user_message: str):
        self._stop_flag = False
        self.status = "running"
        self.status_detail = f"{self.name} 思考中..."
        self.messages.append({"role": "user", "content": user_message})
        if not self.topic:
            self.topic = user_message[:100]
        self.round += 1

        self.history.append({"round": self.round, "model": "User", "content": user_message})
        self._emit("user_message", {"round": self.round, "content": user_message})

        self._emit("content_start", {"round": self.round, "model": self.name})
        thinking_chars = 0
        parts = []
        try:
            async for token in self.model.chat_stream(self.messages):
                if self._stop_flag:
                    break
                if is_thinking_token(token):
                    thinking_chars += len(strip_thinking_prefix(token))
                    self._emit("thinking", {"content": strip_thinking_prefix(token)})
                else:
                    parts.append(token)
                    self._emit("token", {"content": token})
        except Exception as e:
            self.status = "idle"
            self.status_detail = f"出错: {str(e)[:50]}"
            self.error_message = str(e)
            self._emit("error", {"message": str(e)})
            self._emit("chat_turn_end", {"round": self.round})
            self.save_to_disk()
            return

        response_text = "".join(parts)
        self.messages.append({"role": "assistant", "content": response_text})
        self.history.append({"round": self.round, "model": self.name, "content": response_text})

        prompt_est = LLMClient.estimate_messages_tokens(self.messages)
        completion_est = LLMClient.estimate_tokens(response_text)
        thinking_est = thinking_chars // 3
        self._update_token_usage(prompt_est, completion_est, thinking_est)

        self.status = "idle"
        self.status_detail = ""
        self._emit("content_end", {})
        self._emit("chat_turn_end", {"round": self.round})
        self.save_to_disk()

    def _update_token_usage(self, prompt: int, completion: int, thinking: int):
        name = self.name
        if name not in self.token_usage:
            self.token_usage[name] = {"prompt": 0, "completion": 0, "thinking": 0, "total": 0, "calls": 0}
        u = self.token_usage[name]
        u["prompt"] += prompt
        u["completion"] += completion
        u["thinking"] += thinking
        u["total"] = u["prompt"] + u["completion"] + u["thinking"]
        u["calls"] += 1
        self._emit("token_usage", {**self.token_usage, "_total": u["total"]})

    def stop(self):
        self._stop_flag = True

    def get_events(self) -> asyncio.Queue:
        return self._event_queue

    def clear_queue(self):
        while not self._event_queue.empty():
            try:
                self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    # Compatibility with DebateSession attributes used by app.py
    @property
    def name_a(self):
        return self.name

    @property
    def name_b(self):
        return ""

    @property
    def name_c(self):
        return ""

    @property
    def max_rounds(self):
        return 0

    @property
    def memory_masking(self):
        return False

    @property
    def diversity_retention(self):
        return False

    @property
    def model_a_key(self):
        return self.model_key

    @property
    def report(self):
        return ""

    @property
    def report_model(self):
        return ""

    def save_markdown(self) -> str:
        CONVERSATIONS_DIR.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            f"# Chat: {self.topic}",
            "",
            f"**日期**: {timestamp}",
            f"**模型**: {self.name}",
            f"**轮数**: {self.round}",
            "",
            "---",
            "",
        ]
        for entry in self.history:
            label = "User" if entry["model"] == "User" else entry["model"]
            lines.append(f"## {label}")
            lines.append("")
            lines.append(entry["content"])
            lines.append("")
            lines.append("---")
            lines.append("")

        content = "\n".join(lines)
        return content


class _ChatEvent:
    def __init__(self, event_type: str, data: dict):
        self.type = event_type
        self.data = data

    def to_sse(self) -> str:
        import json
        return f"event: {self.type}\ndata: {json.dumps(self.data, ensure_ascii=False)}\n\n"
