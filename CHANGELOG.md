# Changelog

## [0.3.0] - 2026-05-04

### 基础设施改进

- **数据库连接池**：`db.py` 替换每次 `get_db()` 创建新连接为共享连接 + `threading.Lock`。写操作序列化，读操作靠 WAL 并发。移除所有 `conn.close()` 调用。
- **事件队列内存泄漏**：`ChatSession` 和 `DebateSession` 的 `asyncio.Queue` 加 `maxsize=1000`，满时丢弃最旧事件。新增 `clear_queue()` 方法。删除会话和 WebSocket 断开时调用清理。
- **SQLite UPSERT 语法修复**：`ON CONFLICT SET` 改为 `ON CONFLICT DO UPDATE SET`，兼容所有 SQLite 版本。

### 新功能

- **DOMPurify XSS 防护**：前端引入 DOMPurify CDN，`marked.parse()` 输出经过 `DOMPurify.sanitize()` 消毒。
- **会话重命名**：新增 `PUT /api/debate/{session_id}` 端点。大厅卡片添加"重命名"按钮。中英文 i18n 支持。
- **辩论结束自动评分**：`DebateSession` 新增 `quality_score` 属性。辩论结束后后台异步触发自动评分（6 个维度 1-10 分）。评分结果持久化到磁盘并展示。
- **PDF 导出加载提示**：`exportPDF()` 改为 async，显示加载 toast，客户端 45s 超时提示。Chrome subprocess timeout 从 30s 提高到 45s。

### 测试基础设施

- 新增 `tests/` 目录：`conftest.py`（临时数据库 + auth fixtures）、`test_db.py`（22 个数据库测试）、`test_api.py`（22 个 API 测试）、`test_engine.py`（8 个引擎测试）
- 总计 **52 个测试**，覆盖：用户管理、会话管理、模型 CRUD、配额、SQL 注入防护、认证保护、IDOR 防护、文件上传大小限制、管理员权限、队列行为
- `requirements.txt` 新增 `pytest>=8.0.0`、`pytest-asyncio>=0.23.0`、`python-multipart>=0.0.6`

---

## [0.2.1] - 2026-05-04

### 安全修复

- **IDOR 漏洞**：修复会话端点缺少所有权校验的问题。新增 `_require_session_owner()` 辅助函数，所有会话相关端点（history, export, stop, delete, compile, analyze, score, WebSocket）均进行所有权验证。管理员可访问所有会话。
- **`_session_users` 持久化**：`load_persisted_debates()` 现在从磁盘加载时恢复 `_session_users` 映射。`DebateSession` / `ChatSession` 均在 `to_dict()` 中包含 `owner_id` 字段。重启服务器后 IDOR 防护仍然有效。
- **`list_debates` / `search_debates` 按用户过滤**：返回结果仅包含当前用户拥有的会话（无 owner_id 的旧会话对所有用户可见）。
- **`search_debates` 认证绕过修复**：`request: Request = None` 导致 FastAPI 不注入请求对象，认证检查被跳过。移除默认值。
- **`analyze_persuasion` / `score_debate` 所有权检查**：替换手动认证为 `_require_session_owner`。
- **文件上传认证**：`/api/upload` 需要登录。
- **`compile_debate_stream` 参数修复**：移除 `request = None` 默认值，避免 500 错误。
- **所有 API 端点添加认证检查**：此前部分端点缺少 `get_current_user()` 验证，现已全部补齐。
- **XSS 防护**：`_md_to_html` 转义 `<script>`/`<iframe>`/`<img>`/`<svg>`/`<input>`/`<form>`/`javascript:` 等危险内容。用户内容在插入 HTML 前进行转义。
- **文件上传大小限制**：新增 10MB 上传限制，超限返回 HTTP 413。
- **密码哈希升级**：`bcrypt` 替代 SHA-256，登录时自动迁移旧哈希。
- **会话过期**：30 天未活跃的会话自动失效，启动时清理过期会话。
- **SQL 注入防护**：`update_user_model()` 使用字段白名单。
- **WebSocket 认证**：WebSocket 连接通过 query param `token` 或 `Authorization` header 验证身份。

### Bug 修复

- **`_stop_flag` 不重置**：`ChatSession.send_message()` 和 `DebateSession.run()` 开始时重置 `_stop_flag`，修复停止后无法继续发送消息的问题。
- **聊天 WebSocket 连接顺序**：`openChatView()` 中先关闭旧 WS 再打开新 WS，修复聊天流式输出不工作的问题。
- **聊天重复发送**：添加 `chatSending` 标志防止用户在模型回复期间重复发送。
- **速率限制消息**：聊天模式下错误消息正确显示"聊天"而非"辩论"。

### 其他改进

- **健康检查**：仅 2xx 状态码视为健康。
- **速率限制分离**：聊天和辩论使用独立的速率限制配置。
- **编译提示去重**：提取 `_build_compile_prompt()` 和 `_get_compile_client()` 辅助函数。
- **PDF 导出跨平台**：`shutil.which()` 检测 Chrome/Chromium。
- **结构化日志**：添加 `logging` 模块，启动时记录日志。
- **配置安全**：`config.json` 加入 `.gitignore`。
- **Token 估算优化**：CJK 字符 1.5 tokens，ASCII 0.25 tokens/char。

---

## [0.2.0] - 2026-05-04

### 新功能

#### 单模型聊天模式 (Chat)
- 新增 `ChatSession` 类 (`chat_engine.py`)，支持单模型多轮对话
- 聊天视图：气泡式 UI，用户消息右对齐（靛蓝色），模型回复左对齐（灰色）
- 支持流式输出 + thinking 展示（与辩论模式一致的 SSE/WebSocket）
- 聊天记录自动保存到磁盘，支持历史加载
- 大厅卡片展示聊天会话，显示"单聊"标签

#### 聊天模式扩展功能
- **导出 Markdown / PDF**：复用辩论模式的导出逻辑
- **停止按钮**：正在回复时可手动停止
- **文件上传**：输入框旁附件按钮，支持拖拽，上传内容拼入消息

#### 用户模型健康检测
- 新增 `/api/user/models/health` 端点
- 检测用户 BYOK 模型的 base_url 可达性
- 结果缓存 5 分钟，模型管理弹窗中显示绿点/红点

#### 中英双语支持 (i18n)
- 前端：内联 `I18N` 字典（~80 个 key），`t()` 函数，`setLang()` 切换
- Header 添加语言切换按钮（中/EN），偏好存 `localStorage`
- 后端：system prompt 提供 zh/en 两个版本
- 辩论启动、聊天启动均接受 `lang` 参数
- `DebateSession.set_lang()` / `ChatSession.set_lang()` 动态切换 prompt

### Bug 修复

- **注册按钮不可点击**：Tailwind CDN 在国内可能加载失败，`class="hidden"` 不生效。改用内联 `style="display:none"` + `.hidden { display: none !important; }` CSS 规则 + JS 中使用 `style.display` 替代 `classList` 操作关键可见性元素
- **ChatSession 兼容性**：`list_debates()` / `get_history()` 等接口直接访问 `s.name_a` 等属性，ChatSession 没有这些属性。添加了 `@property` 兼容层（name_a, name_b, name_c, max_rounds, report, report_model, model_a_key, memory_masking, diversity_retention）
- **聊天历史不显示**：`openChatView()` 原来依赖内存数据，改为调用 `loadChatHistoryFromServer()` 从服务端加载
- **Token 用量显示 0**：`get_history()` 接口未计算 `_total`，补充了 token usage 聚合逻辑
- **debate_engine.py 意外截断**：i18n 改造时 Write 工具仅写了 148 行（原 679 行），丢失了所有辩论核心逻辑（run, run_blind, run_chain3, save_to_disk 等）。从 GitHub 恢复并合并 i18n 改动

### 文件变更

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `chat_engine.py` | **新增** | ChatSession 类 + 系统提示词 (zh/en) |
| `app.py` | 修改 | +174 行：聊天 API、用户模型健康端点、lang 参数传递 |
| `debate_engine.py` | 修改 | +55 行：i18n system prompt 字典、set_lang 方法、_get_system_prompt |
| `templates/index.html` | 修改 | +728 行：聊天 UI、i18n 系统、语言切换、模型健康状态、文件上传 |
| `CHANGELOG.md` | **新增** | 本文件 |

---

## [0.1.0] - 2026-05-02

### 初始版本

#### 功能
- 多模型辩论竞技场（DeepSeek、Kimi、MiMo、GLM）
- 三种辩论模式：交替审查 (Sequential)、独立辩论 (Blind)、三人链式 (Chain-of-3)
- BYOK (Bring Your Own Key)：用户自带 API Key，Fernet 加密存储
- 月度配额系统：共享模型有限额，自带 Key 无限制
- 速率限制：每小时 + 并发数限制
- 辩论后工具：整理推导 (Compile)、流式整理、说服力分析、评分
- 管理员仪表盘：使用统计
- 文件上传：支持 PDF / Markdown / 代码文件提取
- 搜索：辩论记录全文搜索
- 导出：Markdown / PDF

#### 技术栈
- 后端：FastAPI + SQLite + SSE/WebSocket
- 前端：原生 HTML/CSS/JS + Tailwind CDN + KaTeX + highlight.js + pdf.js
- 部署：Cloudflare Tunnel 公网访问
