# Changelog

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
