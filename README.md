# QQ AI Group Agent

> 运行在 **Windows 本机** 的 QQ 群聊 / 私聊 AI Agent。通过 **QQNT + NapCat** 的 OneBot 反向 WebSocket 接收消息，把当前会话的实时上下文、滚动摘要和有证据的长期记忆交给 **OpenAI-compatible API**，并让模型在**隔离的会话工作目录**中处理文件、媒体、网页和本地任务。

English: A local Windows QQ group/private-chat AI agent powered by NapCat/OneBot and OpenAI-compatible APIs, with durable summaries, evidence-backed memory, multimodal tools, isolated per-conversation workspaces, and human-like participation.

---

## 目录

- [1. 项目定位](#1-项目定位)
- [2. 核心功能](#2-核心功能)
- [3. 技术栈](#3-技术栈)
- [4. 目录结构](#4-目录结构)
- [5. 快速开始](#5-快速开始)
- [6. 配置说明](#6-配置说明)
- [7. 架构与运行流程](#7-架构与运行流程)
- [8. Agent 工具集](#8-agent-工具集)
- [9. 长期记忆模型](#9-长期记忆模型)
- [10. 媒体与工作区](#10-媒体与工作区)
- [11. Google Flow 异步视频](#11-google-flow-异步视频)
- [12. 终端控制台](#12-终端控制台)
- [13. Web 控制台](#13-web-控制台)
- [14. 测试](#14-测试)
- [15. 常见问题与注意事项](#15-常见问题与注意事项)
- [16. 免责声明与开源发布](#16-免责声明与开源发布)

---

## 1. 项目定位

这不是一个"群聊关键词回复机器人"，而是一个**长期驻留在本机的会话 Agent**：

- 每个群聊 / 私聊是**独立会话**，拥有自己的 Agent 工作目录、滚动摘要、长期记忆、群级提示词覆盖和思考深度。
- 消息先落 SQLite 队列，再由**单一持久化 worker 按事件顺序**处理；断线重连后按游标补偿，不会重复发送或撤回。
- 模型可以**自主决定是否插话**，也可以多轮调用工具（搜索、抓网页、读文件、执行命令、看视频、下载并发送视频/音乐/图片、提交异步视频生成任务）。
- 工具边界由**服务端固定**作用于当前会话和当前工作目录；群聊原文、网页正文、搜索结果、文件内容、长期记忆都是**不可信数据**，无法改变工具范围或目标会话。
- 长期记忆**不依赖向量数据库或 embedding**，而是保存"主体 / 谓词 / 值 / 时间状态 / 置信状态 / 来源事件 ID / 原文引文"的可核验结构化事实，支持更正、撤回、冲突和软删除。
- 全过程只监听 `127.0.0.1`，Web 控制台、OneBot 端点和终端命令接口都做了 loopback + CSRF + Host 校验。

---

## 2. 核心功能

模型 API 的工具入口已融合（内部旧名称只保留用于兼容历史记录）：

- `Builtin_video_download`：`action=download`，`channel=youtube/bilibili` 选择平台；非 H.264 视频继续使用同一工具的 `action=confirm`，传 `confirmation_id` 和 `decision=approve/decline/unclear`。下载只写工作区，不自动发送。
- `Builtin_send`：`type=text` 配合 `text` 发送纯文本；`type=markdown` 配合 `markdown` 渲染并发送图片；`type=file` 配合 `path` 发送工作区文件，保留视频/音频兼容处理。
- `Builtin_video_generation`：`action=create/list/result` 对应创建、列表和结果查询。创建仍只允许 Omni Flash 1.1，状态查询仍限定当前会话。

普通模型请求现在公开 18 个工具；Chat Completions 和 Responses 使用相同入口。参数按所选操作验证，原有发送去重、跨会话限制和下载确认保护继续生效。

AI 可调用只读工具 `Builtin_settings` 获取自己的中文介绍文档，介绍身份、功能和能力边界；不会返回后台设置、密钥或内部提示词。文档位于 `app/assistant_introduction.md`，编辑后下一次工具调用即可读取最新内容。该工具在群聊和私聊均可用，不依赖 QQ 连接，不会自动发送消息。

| 能力 | 说明 |
| --- | --- |
| 本机服务 | FastAPI + SQLite（`agent.sqlite3`），控制台默认只监听 `127.0.0.1:8765` |
| QQ 接入 | OneBot v11 **反向 WebSocket**（`ws://127.0.0.1:8765/onebot/v11`），Token 双向校验 |
| 群聊 + 私聊 | 会话 ID：群用 `group_id`，私聊用 `private:<user_id>`；两者能力完全一致 |
| 全量监听开关 | 后台"自动加入监听"**默认开启**，覆盖每个会话的启用状态，并监听已知及新发现的会话 |
| 好友申请 | "自动通过好友申请"**默认关闭**，必须人工显式打开，不交给 AI 判断 |
| 上下文窗口 | 启用会话时读取最近最多 **200** 条历史消息；最新约 **150,000 字**原文在模型请求中保持可见，更早内容才进入滚动摘要 |
| 滚动摘要 | 旧内容按每批 **50,000 字**分批折叠；摘要带不可变快照，模型拒答或格式错误时可回滚到上一个可用状态 |
| 长期记忆 | 按会话隔离；保存原文证据、置信状态、版本与冲突关系；FTS5 全文检索，无向量库 |
| 实时图片 | 图片持久化到本地媒体目录，按容量预算淘汰最旧原图；事件 / 摘要 / 记忆 / 审计记录保留 |
| 视频理解 | ffmpeg 每 10 帧抽图 → 按约 **300,000 tokens** 视觉输入上限分块 → 合并为完整总结 |
| 音频发送 | ffmpeg 转 QQ `record` 语音，超过 **50 秒**自动切段（最多 120 段） |
| 视频发送 | 优先复用已兼容的 H.264/AAC MP4，否则 ffmpeg 转 H.264/AAC + `yuv420p` + fast-start，再发视频卡片 |
| 视频下载 | Bilibili / YouTube 用 yt-dlp 下载到**当前会话 workspace**，默认 ≤720P、优先 H.264；**不自动发送** |
| 异步视频生成 | AI 全程操作 Flow；页面确认后返回，生成等待时每 5 秒由模型判断，操作返回即执行，下载到工作区后转码并发送 |
| 音乐点播 | yt-dlp + ffmpeg 转 MP3 → 自动切段 → 以 QQ 语音（而非群文件）发送 |
| Markdown 长图 | 由模型判断是否调用 MarkFlow 风格渲染，输出可断行的长图片 |
| 网页检索 | Google → Bing → DuckDuckGo 回退，结果带 `provider` / `source_id` / URL 诊断 |
| 权限 / 安全 | 工具边界服务端固定；关机/重启/注销/休眠/电源命令硬拦截；CSRF + loopback + 记忆操作会话归属校验 |
| 幂等与审计 | 每个状态改变动作前有持久化操作槽（`tool_operations`），避免网络超时后重复发送/撤回/上传；全量写入 `tool_audits` |
| 本地管理员 | 后台"与 AI 对话"面板 + PowerShell 终端控制台；管理员 AI 只能通过固定 `rules.md` 工具写规则 |

---

## 3. 技术栈

### 运行时依赖（`pyproject.toml`）

| 依赖 | 版本约束 | 用途 |
| --- | --- | --- |
| `fastapi` | `>=0.110,<0.116` | Web 控制台 + OneBot WebSocket 端点 |
| `uvicorn[standard]` | `>=0.27,<0.35` | ASGI 服务器 |
| `httpx` | `>=0.27,<0.29` | LLM / Flow / 搜索 / 媒体下载的异步 HTTP |
| `jinja2` | `>=3.1,<4` | 单页控制台模板渲染 |
| `keyring` | `>=24,<26` | OneBot Token 存 Windows 凭据管理器 |
| `python-multipart` | `>=0.0.9,<0.1` | 控制台表单解析 |
| `pypdf` | `>=4,<7` | PDF 文字层提取 |
| `PyMuPDF` | `>=1.23,<1.27` | PDF 页面/渲染辅助 |
| `yt-dlp[default]` | `>=2025.1,<2027` | Bilibili / YouTube / 音乐下载 |

开发依赖：`pytest>=8,<9`、`pytest-asyncio>=0.23,<0.26`（`asyncio_mode = "auto"`）。

- Python：`>=3.10`（启动脚本推荐 3.12）
- 包名 / 版本：`qq-ai-group-agent` `0.2.0`（源码预览版）
- 控制台入口点：`qq-ai-bot = app.main:run`

### 外部工具（不由 pip 管理）

| 工具 | 必需性 | 说明 |
| --- | --- | --- |
| QQNT + NapCat | 必需 | 提供 OneBot v11 实现；本项目只用它的反向 WS |
| OpenAI-compatible API | 必需 | Chat Completions / Responses / 自定义 base 端点 |
| `ffmpeg` / `ffprobe` | 视频与音频功能必需 | 转码、抽帧、语音切段；`FFMPEG_PATH` 可指定 |
| Node.js 22+ | YouTube 下载建议 | yt-dlp 的 EJS challenge solver 需要 JS 运行时 |
| Edge / Chromium | Markdown 渲染可选 | `QQ_AI_MARKDOWN_RENDER_BROWSER` |
| MarkFlow checkout | Markdown 渲染可选 | 默认探测 `D:/项目/MarkFlow` |

---

## 4. 目录结构

```text
AI QQ机器人/
├── app/                       # 应用包（约 1.9 万行 Python）
│   ├── main.py                # uvicorn 入口：仅允许 --host 127.0.0.1，默认端口 8765
│   ├── web.py                 # FastAPI 控制面：路由、CSRF、loopback 守卫、Jinja2 渲染
│   ├── service.py             # 编排核心（8.2k 行）：事件 → 队列 → worker → LLM → 工具
│   ├── llm.py                 # ChatCompletionsClient / AdminConversationClient、提示词策略、工具循环
│   ├── db.py                  # SQLite 数据层（2.6k 行）：全部表、迁移、FTS5
│   ├── onebot.py              # OneBot 反向 WS 适配器：token 校验、action/echo 配对
│   ├── terminal.py            # 本机终端命令表（解析、帮助、执行）
│   ├── config.py              # AppSettings / LLMSettings / SecretStore + 默认系统提示词
│   ├── workspace.py           # 每会话工作目录、文件读取（多编码 + Office/PDF 提取）
│   ├── media.py               # 图片下载、落盘、容量预算淘汰
│   ├── markdown_render.py     # MarkFlow 渲染 + CDP 长截图分片
│   ├── music.py               # yt-dlp 音乐下载 + ffmpeg 转 MP3 + 语音切段
│   ├── webtools.py            # Google/Bing/DuckDuckGo 搜索回退 + 网页正文抓取
│   ├── flow_video.py          # 本机 Google Flow 视频 API 异步客户端
│   ├── memory.py              # 记忆归一化、证据结构、FTS 查询构造
│   ├── rules.py               # 全局 rules.md 读写（原子写 + 大小校验）
│   ├── templates/index.html   # 单页控制台（Jinja2）
│   └── static/app.css         # 控制台样式
├── scripts/
│   ├── start.ps1 / start.cmd          # 主启动器（端口检查、发现 ffmpeg/Node、拉起 Flow 守护）
│   ├── console.ps1 / console.cmd      # 本机 PowerShell 终端控制台
│   ├── flow-api-guard.ps1 / flow-api.cmd   # Google Flow 本机 API 守护与健康检查
│   ├── flow-login.ps1 / flow-login.cmd     # 人工登录 Flow 账号（独立 Chrome profile）
│   ├── install-autostart.ps1           # 注册 / 移除两个登录计划任务
│   ├── recover_sqlite.py               # 损坏 SQLite 只读恢复工具
│   └── env/                            # 兼容入口（旧包路径），转发到根目录脚本
├── tests/                      # Python 回归测试 + Node Flow 浏览器任务测试
├── data/                       # 默认数据目录（.gitignore 排除）
├── pyproject.toml
└── README.md
```

业务编排集中在 `service.py`；模型协议、工具入口与提示词分别位于 `llm.py` 和 `tool_facade.py`，Flow 浏览器任务位于 `scripts/flow-ai/`。

---

## 5. 快速开始

### 5.1 前置条件

- Windows 10/11。
- Python 3.10+（推荐 3.12）。
- 可正常运行的 QQNT + NapCat。
- 一个 OpenAI-compatible API、模型名和 API Key；视觉功能需要支持图片内容段的模型。

> 建议使用**专用 QQ 账号**，并只在自己明确管理、成员知情的测试群中启用。

### 5.2 安装依赖

在仓库根目录打开 PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

### 5.3 启动服务

```powershell
.\scripts\start.ps1
```

如果 PowerShell 禁止执行脚本，**不修改全局执行策略**，改用：

```powershell
.\scripts\start.cmd
# 或仅对本次进程放行：
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

默认地址：<http://127.0.0.1:8765>

启动器会：确定/创建运行数据目录 → 拉起 Flow API 守护 → 检查端口占用（已在运行则显示健康状态后退出；被其他程序占用则报告具体进程）→ 自动发现 `ffmpeg` 和 Node → 可选启动 QQNT/NapCat → 执行 `python -m uvicorn app.main:app --host 127.0.0.1 --port 8765`。

也可以不用脚本，直接跑：

```powershell
.\.venv\Scripts\python.exe -m app.main --port 8765
# 或安装后使用入口点：
qq-ai-bot --port 8765
```

### 5.4 NapCat 反向 WebSocket 配置

在 NapCat WebUI 中新建并启用 **WebSocket 客户端 / 反向 WS**（不要配成正向 WS 服务端）：

| NapCat 字段 | 值 |
| --- | --- |
| URL | `ws://127.0.0.1:8765/onebot/v11` |
| Token | 与控制台"模型与连接"页填写的一致 |
| 上报格式 | `array` |
| `reportSelfMessage` | `false` |
| 重连间隔 | `5000` ms |
| 心跳周期 | `30000` ms |

```json5
{
  "network": {
    "websocketClients": [{
      "name": "qq-ai-group-agent",
      "enable": true,
      "url": "ws://127.0.0.1:8765/onebot/v11",
      "messagePostFormat": "array",
      "reportSelfMessage": false,
      "reconnectInterval": 5000,
      "token": "与本地控制台相同的 Token",
      "heartInterval": 30000
    }]
  }
}
```

顶部状态显示"反向 WebSocket 已认证"后再启用群。官方参考：

- [NapCat 安装与启动](https://napneko.github.io/guide/install)
- [NapCat 基础配置](https://napneko.github.io/config/basic)
- [NapCat 事件文档](https://napneko.github.io/develop/event)
- [NapCat API 文档](https://napneko.github.io/onebot/api)

### 5.5 常用启动选项

```powershell
.\scripts\start.ps1 -Reload                  # 开发热重载
.\scripts\start.ps1 -KeepOpenOnError         # 出错时保留窗口
.\scripts\start.ps1 -Port 8877               # 换本机端口（同步改 NapCat URL）
.\scripts\start.ps1 -DataDir 'C:\QQAIGroupAgent\data'
.\scripts\start.ps1 -NapCatExecutable 'C:\Path\To\QQ.exe'   # 先拉起 QQNT/NapCat
.\scripts\start.ps1 -Console                 # 启动服务并直接进入终端控制台
.\scripts\start.ps1 -NoFlowApi               # 本次不拉起 Flow API 守护

# Google Flow 相关
.\scripts\start.ps1 -FlowProjectDir 'D:\path\to\google-flow-video-skill\mcp' -FlowApiPort 8787
.\scripts\flow-login.cmd 1                 # 在账号 1 的专用持久浏览器正常登录
.\scripts\flow-login.cmd 2                 # 在账号 2 的专用持久浏览器正常登录
```

### 5.6 登录后自动启动

```powershell
.\scripts\install-autostart.ps1
.\scripts\install-autostart.ps1 -NapCatExecutable 'C:\Path\To\QQ.exe'
.\scripts\install-autostart.ps1 -FlowProjectDir 'D:\path\to\google-flow-video-skill\mcp'
.\scripts\install-autostart.ps1 -Remove
```

会注册两个当前用户登录任务：`QQ 群 AI 代理` 和 `QQ 群 AI 代理 - Google Flow API`（后者带崩溃重启策略）。都不保存 QQ 密码。

---

## 6. 配置说明

### 6.1 控制台配置项（`POST /settings`，存 SQLite `settings.app_settings`）

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `llm_endpoint_mode` | `completions` | 端点模式，见下表 |
| `llm_base_url` | `""` | 按端点模式解释为根地址或完整 URL |
| `llm_model` | `""` | 对话 / 摘要 / Agent 决策使用的模型 |
| `image_model` | `gemini-3.1-flash-image` | 图片生成模型，与对话模型独立 |
| `llm_api_key` | 空 | 写入 `api-key.json`，**网页不回显** |
| `onebot_token` | 空 | 写入 **Windows 凭据管理器**，不写入 SQLite |
| `send_reasoning_effort` | `false` | 是否向供应商透传 `reasoning_effort` |
| `global_reasoning_effort` | `off` | `off`/`minimal`/`low`/`medium`/`high`/`xhigh`；群级可设 `inherit` |
| `vision_enabled` | `true` | 图片内容段总开关 |
| `timeout_seconds` | `120` | LLM 请求超时 |
| `global_prompt` | 内置 `DEFAULT_PROMPT` | 全局管理员提示词（可编辑） |
| `media_budget_gib` | `20` | 原图容量预算（GiB），超出淘汰最旧原图 |
| `flow_video_api_url` | `http://127.0.0.1:8787` | 本机 Flow 视频 API |
| `flow_video_poll_seconds` | `6` | 后台轮询间隔（2–60，默认每 6 秒） |
| `flow_video_auto_send` | `true` | 任务完成后自动发送视频卡片 |
| `auto_monitor_all_conversations` | `true` | 自动加入监听总开关 |
| `auto_accept_friend_requests` | `false` | 自动通过好友申请（外部账号动作，显式开启） |
| `friend_request_remark` | `""` | 通过好友申请时设置的备注（≤120 字符） |

### 6.2 LLM 端点模式

| 模式 | URL 填写方式 | 实际请求 | 请求协议 |
| --- | --- | --- | --- |
| `base` | 供应商给出的完整请求 URL | 原样 `POST` | Chat Completions 兼容格式 |
| `completions` | API 根地址，例如 `https://api.example.com/v1` | 追加 `/chat/completions` | Chat Completions |
| `responses` | API 根地址，例如 `https://api.openai.com/v1` | 追加 `/responses` | OpenAI Responses |

非法模式会在加载时被归一化为 `completions`。供应商拒绝 `reasoning_effort` 时，服务会自动去掉该字段重试一次并记录告警。

### 6.3 凭据存储

- **LLM API Key** → `<数据目录>\api-key.json`（明文 JSON，`{"version": 1, "api_key": "..."}`）。写入使用临时文件 + `os.replace` 原子替换，并尽力用 `chmod` / `icacls` 限制为当前用户读写。旧版本存在凭据管理器中的条目会在**首次启动时一次性迁移**并随后删除。
- **OneBot Token** → Windows 凭据管理器（服务名 `qq-ai-group-agent`，条目 `onebot-access-token`）。旧 SQLite 明文设置在首次启动时自动迁移。
- 两者都**不会**出现在 `public_settings()`、仪表盘状态或终端 `/config` 输出中。

### 6.4 数据目录

默认 `%LOCALAPPDATA%\QQAIGroupAgent\data`，优先级：`QQ_AI_DATA_DIR` 环境变量 > `create_app(data_dir=...)` 参数 > 该默认值。

```text
<数据目录>/
├── agent.sqlite3        # events / summaries / memories / audits / jobs …
├── api-key.json          # LLM API Key（本地明文 JSON）
├── rules.md              # 全局管理员规则
├── media/originals/      # 持久化原图（按预算淘汰）
├── logs/                 # Flow 守护日志
└── flow-browser-profiles/account-*/   # 各 Flow 账号的独立持久 Chrome 资料目录
```

站点的 Netscape Cookie 文件（严格分开）：`bilibili-cookies.txt`、`youtube-cookies.txt`、`google-search-cookies.txt`（兼容 `google-cookies.txt`）。

### 6.5 环境变量

| 变量 | 用途 |
| --- | --- |
| `QQ_AI_DATA_DIR` | 运行数据目录（SQLite / 媒体 / 配置 / cookies） |
| `QQ_AI_WORKSPACE_ROOT` | 会话工作目录根路径（默认 `D:/Workspace`） |
| `FFMPEG_PATH` | `ffmpeg.exe` 完整路径 |
| `NODE_PATH` | Node.js 可执行文件路径（yt-dlp EJS solver） |
| `QQ_AI_MARKFLOW_ROOT` | MarkFlow checkout 根目录（默认 `D:/项目/MarkFlow`） |
| `QQ_AI_MARKDOWN_RENDER_BROWSER` | Edge/Chromium 可执行文件路径 |
| `GOOGLE_SEARCH_COOKIES_FILE`（兼容 `GOOGLE_COOKIES_FILE`） | Google 搜索专用 Netscape Cookie；不设置时读 `<数据目录>/google-search-cookies.txt` |
| `FLOW_API_KEY` | 本机 Flow API 的 Bearer Token（两侧同名即启用鉴权） |
| `FLOW_VIDEO_PROJECT_DIR`（兼容 `GOOGLE_FLOW_VIDEO_PROJECT`） | Google Flow Video Skill 的 `mcp` 项目目录 |
| `FLOW_NODE_PATH` | Node.js 完整路径；未设置时从 PATH 查找 |
| `FLOW_PROFILE_ROOT` | 持久浏览器资料根目录，默认 `<数据目录>/flow-browser-profiles` |
| `FLOW_ACCOUNT_COUNT` | 持久浏览器账号数，默认 2，可配置 1–20；新增第 3/4/5 个账号时设为 5 |
| `FLOW_USER_DATA_DIR` / `FLOW_USER_DATA_DIR_2` … `_20` | 按账号编号覆盖资料目录，现有目录不迁移、不清空 |
| `FLOW_USER_DATA_DIRS` | 分号分隔的资料目录列表；也会自动扩展账号数，不接受认证导出文件 |
| `FLOW_PERSISTENT_PROFILES` | 固定为 `1`，不能关闭持久浏览器 |
| `FLOW_BROWSER_EXECUTABLE_PATH` | Chromium/Chrome/Edge 路径；守护会自动寻找 Chrome SxS |
| `FLOW_MAX_CREDITS` | Flow API **本机预检**额度上限，默认 `1050`（非 Google 实时余额） |
| `FLOW_TIMEOUT_MS` | 实际提交后等待上限；启动脚本固定 `1200000`（20 分钟），排队不计入 |
| `FLOW_API_PORT` | Flow API 端口，默认 `8787` |

> 把数据目录放在健康的 **NTFS** 磁盘上。SQLite 的 WAL 在 exFAT / 可移动介质上会退化或报 `disk I/O error`。

---

## 7. 架构与运行流程

```text
QQNT + NapCat
      │  OneBot v11 反向 WebSocket（/onebot/v11，Token 校验）
      ▼
FastAPI（127.0.0.1:8765）
      │
      ├── web.py        仪表盘 / 配置 / 记忆管理 / 管理员对话 / 终端命令接口
      ├── onebot.py     WS 适配器：事件分发 + action/echo 配对 + 超时/断线分类
      ▼
AgentService（service.py）
      │  ① 归一化事件 → 去重键 → SQLite events（pending=1）
      │  ② 每会话一个持久 worker，按事件顺序消费
      │  ③ 组装：规则 + 全局/群提示词 + 可信消息元数据 + 最近 150K 原文
      │           + 归档批次 + 长期记忆 + 图片段
      │  ④ llm.py 执行有界工具循环（最多 4 轮决策 / 16 次普通工具调用）
      │  ⑤ 状态改变动作先占 tool_operations 槽，再执行并写 tool_audits
      ▼
SQLite（agent.sqlite3）   ├── OpenAI-compatible LLM（chat / responses / 图片 / 记忆协议）
                          ├── 本机 Google Flow 视频 API（127.0.0.1:8787，异步任务）
                          ├── 媒体存储（media/originals，容量淘汰）
                          ├── 每会话工作目录（D:/Workspace/<会话ID>）
                          └── ffmpeg / ffprobe / yt-dlp / Node / MarkFlow + CDP
```

### 7.1 事件到回复

1. **接入**：`onebot.py` 校验 Host/Origin 为 loopback，再比对 `Authorization: Bearer <token>`；失败以 1008/1000 关闭，不泄露细节。
2. **归一化**：`normalise_onebot_event` 解析数组/字符串消息段，抽取文本、`at` 段、回复、图片、视频、文件；私聊会话 ID 为 `private:<user_id>`。
3. **落库**：`events` 表以 `dedupe_key` 唯一约束去重，标记 `pending=1`、`archived=0`、`memory_processed=0`。
4. **调度**：该会话无活跃 worker 时启动一个；同一会话**始终只有一个主 worker**，保证事件顺序。
5. **忙碌回复**：主 Agent 忙碌时，新消息可触发一个**只读、短小的辅助回复**子 Agent，它拿到受限安全快照，不改变主任务的工具决策或摘要游标。
6. **上下文装配**：
   - 实时触发批次（本轮 live 消息）
   - 最近约 **150,000 字**原文窗口（永不在这轮被摘要）
   - 归档批次（已自然滑出 150K 窗口的事件，每批 ≤50,000 字）→ 更新滚动摘要
   - 相关长期记忆（按本轮内容做 FTS5 检索，最多 1,000 条 / 80,000 字）
   - `rules.md` + 全局提示词 + 群级 `prompt_override`
   - 可信消息元数据行（`reply_to_message_id` 只能取自这里）
7. **可信互动标记**：服务端本地校验（本轮是否含实时 @、是否用 QQ 回复了机器人、是否是明确群内召唤、是否是明确任务），命中才追加对应的不可变提示；普通刷屏不标记，模型可选择沉默。
8. **工具循环**：普通状态工具每次决策最多 16 次、每轮共 16 次、最多 4 轮决策；`execute_command` 走**独立不受限**的本地诊断通道，直到模型给出最终文本或用户停止任务。
9. **收尾**：写 `turns` 记录、更新 `summaries` 与 `summary_snapshots`、推进 `memory_processed` 游标。

### 7.2 幂等与不重放

- `tool_operations` 以 `(会话, 本批事件 ID, 工具名, 参数)` 生成 `operation_key`，执行前占位，执行后写结果。中断后重放同一批会命中 `deduplicated` 而不重复发送。
- `sent_messages` 记录本应用自己发出的 `message_id`，`recall_own_message` 只能撤回其中的消息。
- 服务重启时：`cancel_running_turns` 取消上次的 running 轮次、`recover_video_download_confirmations` 把中断的视频确认标记为失败、`recover_flow_video_sending_states` 把中断的 Flow 投递标记为 unknown —— **都不会自动重放旧的 QQ 动作**。
- 工具失败会保留结构化错误与审计记录：本地参数错误返回 `retry_safe: true` 允许**一次**安全修正；结果不确定的 QQ 状态操作会被移出下一轮工具列表，绝不盲目重复。

### 7.3 摘要安全降级

若供应商因安全策略拒绝接收群聊原文（命中 `SUMMARY_SAFETY_REFUSAL_MARKERS`），本轮归档批次会切换为**仅事件元数据**模式：不带原文、图片、记忆散文和提示词，并且**不再上传被拒内容**；只允许自动重算一次，然后停止并提示更换模型。记忆游标不推进，避免凭空生成记忆。

---

## 8. Agent 工具集

工具**始终由服务端固定**作用于当前群/私聊和当前工作目录。群聊原文、网页、搜索结果、文件内容和记忆都不能改变工具边界。

### 8.1 消息与群文件

| 工具 | 作用 |
| --- | --- |
| `send_group_message` | 向当前群或私聊发文字，可选引用当前会话可信消息；QQ 单条硬上限 4,000 字符 |
| `recall_own_message` | 只撤回本应用自己记录发送的消息（`delete_msg`） |
| `Builtin_list_group_files` | 列出当前群文件（`get_group_root_files`） |
| `Builtin_download_group_file` | 下载群文件到工作区 |

### 8.2 检索与抓取

| 工具 | 作用 |
| --- | --- |
| `Builtin_Websearch` | Google → Bing → DuckDuckGo 公共网页搜索回退；Google 可读 `google-search-cookies.txt`；结果带 `provider` / `source_id` / URL |
| `Builtin_patch` | 抓取公开 HTTP(S) 链接正文（限制公网 IP，防 SSRF） |
| `Builtin_querymessage` | 检索当前会话已记录的前文（只读） |
| `Builtin_querymemory` | 检索当前会话有证据的长期记忆（只读，FTS5） |

### 8.3 生成与渲染

| 工具 | 作用 |
| --- | --- |
| `Builtin_image_generation` | 调用配置的 `image_model` 生成并发送图片 |
| `Builtin_render_markdown_image` | 用 MarkFlow 渲染 Markdown 长图（宽度 1100，最多 12 片，≤160,000 字） |

### 8.4 本地工作区与命令

| 工具 | 作用 |
| --- | --- |
| `list_workspace_files` | 列出当前会话工作目录 |
| `read_workspace_file` | 读取文件：UTF-8 / UTF-16 / GB18030 / Big5，DOCX / PPTX / XLSX / ODT 文本提取，PDF 文字层提取；扫描 PDF 自动按页交给视觉模型（≤64 页） |
| `write_workspace_file` | 写入工作目录中的文本文件 |
| `execute_command` | 以**当前服务账户权限**访问整台机器执行命令；默认 cwd 为会话工作区。唯一硬拦截：关机 / 重启 / 注销 / 休眠 / 电源切换（含编码 PowerShell）。QQ 进度提示只展示命令前 30 字符，同轮前 3 次逐次提示，之后每 3 次汇报累计次数；完整 stdout/stderr 只返回给 Agent，退出码非零时标 `agent_continue: true` |

### 8.5 媒体与下载

| 工具 | 作用 |
| --- | --- |
| `send_group_file` | 发送工作区文件：视频 → QQ 视频卡片（必要时 ffmpeg 转 H.264/AAC）；音频 → QQ 语音（>50 秒切段） |
| `Builtin_video_understanding` | 下载/读取视频 → 每 10 帧抽图 → 按约 300,000 tokens 分块视觉总结 → 合并 |
| `Builtin_bilibili_download` | yt-dlp 下载 Bilibili 视频到会话 workspace（默认 ≤720P，**不自动发送**） |
| `Builtin_youtube_download` | yt-dlp 下载 YouTube 视频到会话 workspace，支持 `url` 或 `query`（搜索词） |
| `Builtin_video_download_confirmation` | 源站没有 H.264 时，先向群成员确认；返回 `awaiting_confirmation=true` 与 `user_visible_text`，成员明确同意后凭 `confirmation_id` 调 `approve` / `decline` / `unclear` |
| `Builtin_music_download` | 下载音乐/音频 → MP3 → 切段 → 以 QQ 语音发送 |

### 8.6 Google Flow 异步视频

| 工具 | 作用 |
| --- | --- |
| `Builtin_video_generation_create` | 向本机 Flow API 提交任务；Flow 页面确认任务已启动后返回任务 ID，渲染继续在后台运行 |
| `Builtin_video_generation_list` | 查询当前会话自己的任务列表 |
| `Builtin_video_generation_result` | 查询任务真实状态 / 错误 / 发送结果，可按需补发 |

### 8.7 管理员专用

| 工具 | 作用 |
| --- | --- |
| `write_rules_md` | 仅本机后台"与 AI 对话"可用，写入固定的 `rules.md`（路径不接受模型输入） |

---

## 9. 长期记忆模型

不使用向量压缩模型，保存**可核验的结构化事实**。

### 9.1 三段式协议

1. **提取**：`MEMORY_PROPOSAL_SCHEMA`（JSON Schema，`additionalProperties: false`）——每条候选必须带本轮事件的真实 `event_id` 和**逐字 `quote`**，并声明 `memory_type`、`temporal_status`、`confidence`。
   `memory_type` ∈ `alias / identity / preference / relationship / commitment / project / decision / skill / routine / background / inside_joke / episodic`；
   `temporal_status` ∈ `ongoing / temporary / completed / unknown`；
   `operation` ∈ `remember / correct / retract`。
2. **核验**：`MEMORY_VERIFICATION_SCHEMA`——第二次模型调用逐项判断候选是否被所引原文直接蕴含，输出 `accept / reject / needs_more_evidence`。
3. **落库前的本地校验**：`_verify_memory_evidence` 确认引文确实出现在原事件中、群归属正确；不合格直接拒绝。

### 9.2 版本与冲突

- 新事实**不会无声覆盖**旧事实。`group_memories` 保留所有非活动行（`active=0`），记录 `confidence_status`、`valid_from` / `valid_until`、`superseded_by_memory_id`、`retraction_reason`。
- 冲突写入 `group_memory_conflicts`（`open` / `resolved`），每次变更写入 `group_memory_changes`（before/after JSON）。
- `group_memories_fts`（FTS5）提供按会话的全文检索；`group_memory_evidence` 按 `(memory_id, role, event_id, message_id, text)` 唯一去重。
- 控制台可对单条记忆执行 `confirm` / `correct` / `retract` / `delete`（更正 ≤8,000 字符，备注 ≤2,000 字符）；URL 里的 `memory_id` 会先在该会话的完整记忆列表中校验归属。
- "清空本群记忆与规则并重算历史"只删除该会话的**派生**记忆、摘要和群级规则，然后重新归档已有消息；**不会重放**旧的 QQ 发送、撤回、上传或下载动作。

### 9.3 `rules.md` ≠ 记忆

全局管理员 `rules.md` 描述机器人的**行为偏好**（放在数据目录，由管理员 AI 通过 `write_rules_md` 维护）；每会话的 `group_memories` 记录**该群中有证据的事实**。两者是不同数据。

---

## 10. 媒体与工作区

### 10.1 工作区

每个会话一个独立目录，目录名由持久会话 ID 派生，**不接受模型选择**：

```text
D:/Workspace/<群号 或 private:用户号>/
```

可用 `QQ_AI_WORKSPACE_ROOT` 改根路径。限制：

| 项 | 值 |
| --- | --- |
| 单文件大小 | 100 MiB（视频发送走独立的 2 GiB 上限） |
| 单次返回文本 | 200,000 字符 |
| PDF 页数 | 最多 2,000 页；视觉逐页读取最多 64 页 |
| zip 解压总量 | 200 MiB |

> `execute_command` **没有通用沙箱**，它以运行服务的 Windows 用户权限执行。不要为陌生群启用机器人，也不要把控制台暴露到局域网或公网。

### 10.2 媒体存储

- 单图最大 10 MiB，下载超时 20 秒，最多重定向 3 次，仅接受解析到全球可路由地址的 HTTP(S) URL（`allow_private_hosts` 默认关闭）。
- 默认容量预算 20 GiB（`media_budget_gib`），超出后按最旧淘汰原图；事件、摘要、记忆和审计记录始终保留。
- 每轮最多附带 **4** 张图片；历史图片在 150K 原文窗口中只保留文本占位符，不会每轮重复上传。

### 10.3 视频与音频转换

- 视频：已兼容的 H.264/AAC + `yuv420p` + fast-start MP4 直接发送；只有音频不兼容时只转音频；否则 ffmpeg 完整转码为 H.264/AAC 视频卡片。转码上限 900 秒。
- 音频：转 QQ `record` 语音，单段 ≤50 秒，最多 120 段。
- 视频理解：ffmpeg 每 10 帧抽图 → 按约 300,000 tokens 分块 → 合并为完整总结。
- 视频下载：先 yt-dlp 探测格式（≤120 秒），优先 `vcodec^=avc1/avc3/h264` 且 `height<=720`；源站无 H.264 时**先向群成员确认**，同意后才下载。

---

## 11. Google Flow 异步视频

视频生成与普通聊天是**两条独立链路**。唯一允许的 Flow 模型是 **Gemini Omni Flash 1.1**（API ID `gemini-omni-flash`），不会选择或回退到 Veo 3.1 等其他模型；HTTP 客户端本身也会拒绝其他模型 ID。

### 11.1 流程

1. 模型调用 `Builtin_video_generation(action=create)` → 本机 AI Flow 服务。浏览器操控（提交及 review）独立使用 **`gemini-3.5-flash-lite`** 读取实际页面截图和元素，创建新项目、选择 Omni 1.1 Flash、填写提示词并点击生成；普通回复模型单独在后台配置（例如 `gemini-3.8-flash-tiered`），修改回复模型不会改动浏览器操控模型。两者共用配置的 Antigravity API 地址、密钥和协议；操控模型不发送额外推理级别参数。这些是操控/回复 LLM，不是 Flow 的视频生成模型。
   - 必须切换到底部**普通视频模式**，看到实际时长、比例、数量后才允许提交。关闭 Flow 智能体侧栏不等于切换模式；不能把聊天发送或它的“正在思考”当作视频生成。
2. 程序核对实际模型、时长、比例、数量及提示词；只有 Flow 页面出现新结果、生成进度或取消生成控件等提交证据时才返回任务 ID，灰色播放占位卡片不算证据。配置阶段最多 4 分钟；点击生成后独立观察确认，不受模型推理延迟影响，最多等待 20 分钟，结果不确定时不重复提交。
3. 任务写入 `flow_video_jobs`，主 Agent 结束当前轮。浏览器 AI 继续操作**同一账号、同一 Page 对象**；生成等待时按 5 秒节奏采集真实截图并由模型判断。每次模型返回立即核验并执行，非等待操作后立即采集新页面、请求下一步，不再等待额外节拍。模型单次请求超过 5 秒时不并发追发、不另加 5 秒等待；仅保留一个模型请求，操作前验证目标仍存在、文字及开关状态未改变。
4. review AI 根据完成画面进入视频详情、打开下载菜单、点击视频下载选项；监听到实际下载事件后 `saveAs` 到该会话 workspace，并检查 MP4 文件头。下载结束前保持页面打开；未收到真实视频文件不能报告完成。
5. QQBot 的后台监视器独立读取任务状态，取得完成文件后转换为 QQ 兼容视频并按配置发到**发起请求的原会话**。期间新的 QQ 消息正常处理；成员询问进度时必须查工具返回的真实观察记录。

### 11.2 守护进程

`scripts/flow-api-guard.ps1` 自动启动本仓库的 `scripts/flow-ai/api.mjs`。外部 Flow 项目仅提供 Playwright 会话和账号库，视频工作流由本仓库的 AI 任务执行。`scripts/flow-ai-model.py` 在私有管道内读取 Bot 配置并调用模型，密钥不经过命令行、审计或模型上下文。

`/healthz` 必须显示 `runtime=qqbot-ai-browser`、`runtime_version=5`、`ai_browser_control=true`、`review_interval_seconds=5`、`action_execution=immediate`，以及 `browser_resources.limit=1`；只有进程存活不能证明功能完整。启动服务、`GET /healthz` 和 `GET /readyz` **均不会打开浏览器**；`/readyz` 仅返回逐账号缓存，未检测的资料明确显示 `checked=false`。需要实际核验时显式调用 `POST /v1/flow/accounts/<账号序号>/check`，只打开该账号并在检测结束后关闭；忙时不另开浏览器。生成和下载仍需要真实视频测试。日志写在 `<数据目录>/logs/flow-api.stderr.log`，包含任务 ID、AI 操作、模型、提交证据和下载大小。

首次使用或更换 Flow 项目时，先在 Flow 项目目录手动跑一次：

```powershell
cd 'D:\path\to\google-flow-video-skill\mcp'
npm install
npx playwright install chromium
# 不设置 Cookie/storage-state 文件；每个账号通过专用浏览器登录
cd 'D:\path\to\QQBot'
.\scripts\flow-api-guard.ps1 -Once -FlowProjectDir 'D:\path\to\google-flow-video-skill\mcp'
.\scripts\flow-login.cmd 1
.\scripts\flow-login.cmd 2
```

需要重新登录时，用系统方式打开普通 Chrome 窗口（不带 `--no-sandbox` / `--enable-automation` / 远程调试），正常登录并进入 Flow 后关闭整个窗口：

```powershell
.\scripts\flow-login.cmd 1
.\scripts\flow-login.cmd 2
```

### 11.3 约束

- 每个账号仅使用独立持久 Chrome 资料目录 `<数据目录>/flow-browser-profiles/account-*`，跨 API 重启保留浏览器自己的登录 / localStorage / 设备会话。启动脚本不找、不转换、不导入 Cookie 文件；运行时忽略所有旧 `FLOW_COOKIES_*` / `FLOW_STORAGE_STATE*` 环境配置，不注入认证导出文件。
- 资料目录按账号隔离，但**全局最多运行 1 个 Flow 浏览器**，人工登录也共用该限制。五个资料目录不代表同时启动五个 Chrome。仅在生成、显式检测或人工登录时按需打开，任务成功、失败或检测结束后关闭，其他任务排队；启动和健康查询不预加载浏览器。
- 提交前失败会在已配置账号间最多尝试 **3** 次；Generate 已成功点击后不会重复提交。
- 尚未点击生成的新任务，从入队起最多等待 19 分钟进行排队和配置，早于 Bot 客户端的 20 分钟请求上限。父请求断开、或该截止时间到达时，尚未点击生成的任务不得继续启动模型、浏览器或付费操作；已点击、已确认或结果不确定的任务继续保留原项目，不据此取消云端生成或重复提交。此截止时间不替代点击后的确认等待及已确认生成后的独立进度监视。

添加第 3、4、5 个持久浏览器资料：

```powershell
[Environment]::SetEnvironmentVariable('FLOW_ACCOUNT_COUNT', '5', 'User')
$env:FLOW_ACCOUNT_COUNT = '5'
# 空闲时重载 Flow 视频服务（不需要重启 QQBot），然后分别人工登录：
.\scripts\flow-login.cmd 3
.\scripts\flow-login.cmd 4
.\scripts\flow-login.cmd 5
```

专用窗口打开后正常登录，确认可以进入 Flow，再关闭该专用 Chrome 窗口并按 Enter 检测。资料保存在 `<数据目录>/flow-browser-profiles/account-3`、`account-4`、`account-5`，不复制其他账号的认证数据。
- `FLOW_MAX_CREDITS` 只是本机预检上限（默认 `1050`），**不是 Google Flow 账户的实时余额**；修改后需重启 Flow 守护。
- 每次审查都有实际页面观察时间和计数；真实百分比缺失时 `progress_observed=false`，不会把 0% 或等待时长解释为云端拥堵。AI 连续审查失败 3 次会报告实际错误。
- `<数据目录>/flow-ai-jobs/` 保存任务日志和**最新一张**截图。服务重启不会为了旧任务自动启动浏览器；已确认但未完成的任务标记为 `resume_required`，由显式恢复接口继续原账号、原项目的审查，不再次生成；中断时提交结果不确定的任务保留诊断并禁止自动重提。
- 已确认生成、但详情/下载审查失败的任务，可在本机对 `POST /v1/videos/<id>/resume-review` 恢复**同一项目的审查和下载**。若原任务已点击普通视频生成且参数证据完整、但确认中断，该接口只重新观察原项目 60 秒；没有真实进度或视频则明确失败。恢复不增加生成额度、不开新项目，也不会再次点击生成。
- 图片与视频默认设置分别核验；旧模型证明不能覆盖当前参数。多个输出按实际下载内容去重，不能把同一视频重复计为多个结果。
- 浏览器登录失效、跳转登录或出现验证码时，报告账号需要在专用持久浏览器人工登录，不要求 Cookie 导出，也不绕过验证。旧导出文件仅保留作历史备份，不参与 Flow 生成。

真实验收（会消耗 Flow 额度，每次只提交一个任务）：

```powershell
.\.venv\Scripts\python.exe scripts\flow-ai-smoke.py --data-dir 'C:\path\to\data' --conversation 'private:你的QQ号'
node --test tests\flow-ai.test.mjs
```

该脚本直接使用 Bot 的 `Builtin_video_generation` 工具执行器和真实任务数据库；运行中的 Bot 继续负责最终投递。审查证据可通过本机 `GET /v1/videos/<id>/observation` 查看，截图和 Cookie 都不进入公开发布包。

---

## 12. 终端控制台

```powershell
.\scripts\console.ps1                 # 服务已运行则自动附着
.\scripts\start.ps1 -Console          # 启动服务并直接进入控制台
```

`console.cmd` 与 `scripts\env\console.ps1` 是兼容入口。退出 `/quit` 只退出输入循环，**不会停止 QQ 服务**。控制台通过 `POST /terminal/command`（要求 `X-Qqai-Terminal: 1` 头 + loopback）执行 `app/terminal.py` 中的**固定命令表**，不提供任意 shell 执行。

### 命令表

| 命令 | 说明 |
| --- | --- |
| `/help [命令]` | 显示全部命令；带命令名时显示该命令用法 |
| `/status` | 服务、OneBot、积压事件、Flow 任务和媒体占用状态 |
| `/config` | 显示当前**非敏感**配置（不含 API Key / Token / Cookie） |
| `/groups` | 列出已发现的群聊和私聊，以及启用状态和积压数量 |
| `/monitor-all [on\|off]` | 切换所有已知及新发现会话的自动监听 |
| `/group <会话ID>` | 查看单个会话的状态、摘要和工作目录 |
| `/enable <会话ID>` | 启用会话（群聊尝试读历史，私聊立即监听） |
| `/disable <会话ID>` | 停止处理新消息（已保存消息不删除） |
| `/retry <会话ID>` | 重试积压消息或初始化历史 |
| `/reset-memory <会话ID> reset` | 清空摘要、长期记忆和会话规则并重算（必须输入 `reset`） |
| `/summary <会话ID> [字符数]` | 查看当前滚动摘要 |
| `/memory <会话ID> [数量]` | 查看长期记忆及证据数量 |
| `/events <会话ID> [数量]` | 查看最近的原始消息上下文 |
| `/turns <会话ID> [数量]` | 查看最近的 Agent 处理轮次和错误 |
| `/audits [数量]` | 查看最近的工具调用审计 |
| `/flow` | 查询本机 Flow API 健康状态和待处理任务数 |
| `/flow-jobs <会话ID> [状态] [数量]` | 查看某会话的视频生成任务（`queued`/`in_progress`/`completed`/`failed`） |
| `/sync` | 立即从已连接的 OneBot 同步群列表 |
| `/workspace <会话ID> [路径]` | 列出会话工作目录文件 |
| `/rules` | 显示全局 `rules.md` |
| `/admin <内容>` | 向后台管理员 AI 发一条消息 |
| `/clear` | 清屏（不影响服务和数据） |
| `/quit` | 退出控制台（不停止 QQ 服务） |

所有命令都支持中文别名，例如 `/状态`、`/群`、`/重置记忆`。

---

## 13. Web 控制台

<http://127.0.0.1:8765>，单页 Jinja2 模板 + 纯 CSS，无前端框架与构建步骤。面板：

| 面板 | 内容 |
| --- | --- |
| 运行概况 | 服务 / OneBot 连接状态、启用会话数、积压事件、媒体占用、Flow 任务 |
| 自动加入监听 | 全量监听总开关（默认开启） |
| 自动通过好友申请 | 显式开关（默认关闭）+ 备注设置 |
| 与 AI 对话 | 本机管理员对话历史（单次 ≤12,000 字符），可指示 AI 写 `rules.md` |
| `rules.md` | 全局规则的只读展示与路径提示 |
| 发现的群与私聊 | 每个会话的启用开关、`prompt_override`、`reasoning_effort`、重试、最近错误、群长期记忆（展开式折叠面板，支持确认 / 更正 / 撤回 / 软删除） |
| 模型与连接 | 全部 LLM / OneBot / Flow 配置项 |
| 最近事件 / 最近处理记录 / 工具审计 | 运行诊断，`tool_audits` 的参数与结果以美化 JSON 展开 |

### 安全边界

- `create_app()` 中间件对所有 HTTP 请求校验 `Host` 必须是 `127.0.0.1` / `localhost` / `::1`。
- 所有 POST 表单需要 `csrf_token`（启动时 `secrets.token_urlsafe(32)`，`hmac.compare_digest` 比对）；显式的外部 HTTP(S) `Origin` 直接 403。
- 所有修改都是 **POST → 303 重定向 → GET**，刷新不会重复触发 LLM 请求。
- WebSocket 端点 `/onebot/v11` 同样校验 loopback Host/Origin 与 OneBot Token。

---

## 14. 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --test tests/*.test.mjs
.\.venv\Scripts\python.exe -m compileall -q app scripts tests
```

`pyproject.toml` 已配置 `asyncio_mode = "auto"`、`testpaths = ["tests"]`。Python 测试覆盖 Bot 业务，Node 测试覆盖 Flow 操作、账号资料、资源限制及接口策略；以实际运行结果为准。

本版发布的是源码预览版，Flow 仍依赖独立安装的外部会话库。自动化回归通过不等于真实 Google 账号、模型服务或 QQ 投递验收；本次发布检查不消耗 Flow 额度，不自动重启正在运行的服务。新五秒即时执行链路仍应按 11 节通过实际账号验收。

覆盖范围（按文件）：

| 领域 | 测试文件 |
| --- | --- |
| OneBot 协议与事件 | `test_onebot.py`、`test_direct_mentions.py`、`test_private_conversations.py`、`test_qq_plain_text.py` |
| LLM 客户端与协议 | `test_llm.py`、`test_responses.py`、`test_llm_safety.py`、`test_endpoint_mode_settings.py`、`test_prompt_policy.py`、`test_context_and_tool_slots.py` |
| Agent 循环与工具 | `test_agent_integration.py`、`test_agent_runtime_extensions.py`、`test_multi_tools.py`、`test_tool_repair.py`、`test_tool_idempotency.py`、`test_tool_finalization_fallback.py`、`test_command_recovery.py`、`test_busy_reply_agent.py` |
| 长期记忆 | `test_memory_db.py`、`test_memory_llm_protocol.py`、`test_memory_service_integration.py`、`test_group_memory_web.py` |
| 摘要 | `test_summary_recovery.py`、`test_summary_send_gate.py` |
| 媒体 | `test_media.py`、`test_image_generation.py`、`test_music_voice.py`、`test_video_understanding.py`、`test_video_card_delivery.py`、`test_video_download_h264.py`、`test_youtube_download.py`、`test_markdown_render.py` |
| 工作区与文件 | `test_workspace_and_group_files.py` |
| Flow 视频 | `test_flow_video.py` |
| 服务与恢复 | `test_service_recovery.py`、`test_executable_discovery.py` |
| 控制台与终端 | `test_web.py`、`test_admin_chat_web.py`、`test_admin_llm.py`、`test_admin_conversation_storage.py`、`test_admin_service_and_reply_interaction.py`、`test_terminal_console.py`、`test_webtools.py`、`test_config.py` |
| 维护工具 | `test_recover_sqlite.py` |

---

## 15. 常见问题与注意事项

### PowerShell 报 `running scripts is disabled`

用 `scripts\start.cmd`，或只对当前进程放行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

### 8765 端口被占用

再次运行启动脚本时，占用者是本代理会直接显示"已在运行"；如果是其他程序，会报告具体进程名与 PID，关闭它或用 `-Port` 换端口，并同步修改 NapCat 的反向 WS URL。

### OneBot 403 / 未连接

- 确认 URL 是 `ws://127.0.0.1:8765/onebot/v11`；
- Token 与控制台逐字一致（注意大小写和前后空格）；
- NapCat 用的是 **WebSocket 客户端 / 反向 WS**，不是正向 WS 服务端；
- `messagePostFormat` 为 `array`；
- 控制台已配置 Token —— 未配置时服务会以 1011 关闭连接；
- 服务只绑定 `127.0.0.1`，改过 `-Port` 就要同步改 URL。

### 视频理解没有 API 流量

先看工具审计。若 `Builtin_video_understanding` 报"QQ 临时 URL 中途断开"，说明视觉模型**还没收到任何截图**（QQ 的临时文件 URL 会过期）。重新发送原视频以获取新的 URL。服务对可续传 URL 会自动 Range 重试，且**源文件完整下载后**才启动视觉请求。

### SQLite `disk I/O error`

停止服务 → **先备份**数据目录中的 SQLite 文件 → 检查目录权限、磁盘健康、是否位于 exFAT / 可移动 / 网络同步盘（这些介质上 WAL 会退化）。不要在未备份的损坏库上反复写入。仓库提供只读恢复辅助工具：

```powershell
.\.venv\Scripts\python.exe .\scripts\recover_sqlite.py --help
```

它以 immutable 只读方式打开源库，把表按稳定行序复制到目标库，`turns` 表额外走主键探测路径；只有行数、行内容、外键和完整 `integrity_check` 全部通过才接受目标。

### 机器人不回话

按顺序检查：① "自动加入监听"总开关；② 该会话自己的启用开关；③ OneBot 已认证；④ 群级 `reasoning_effort`；⑤ 该会话的"最近处理记录"是否有 `failed` 及错误详情；⑥ `/status` 里的积压事件数。

### 成员说"下载好了吗"但没有文件

服务有强制的**下载状态真实性规则**：只有本轮实际收到 `Builtin_bilibili_download` / `Builtin_youtube_download` / `Builtin_music_download` 的成功结果才能说"已下载"。视频下载工具的成功只代表文件进了工作区，**不代表已发送** —— 需要发送时 Agent 必须再显式调用 `send_group_file`。旧的 `.part` 文件和代理进程流量都不是证据。

### 下载任务停在"待确认"

说明源站没有 H.264 格式。服务已创建 `video_download_confirmations` 记录并返回 `user_visible_text`；成员明确同意后，Agent 用返回的 `confirmation_id` 调 `Builtin_video_download_confirmation(approve|decline|unclear)`。确认记录 600 秒过期，服务重启时未完成的确认会被标记为失败并**不自动重放**。

### 模型一直沉默

这是**设计行为**。模型自主决定是否插话：明确提问、追问、@、QQ 回复机器人、明确召唤和明确任务会优先回应；纯刷屏、单个表情、明显无关的内容可以保持安静，只更新内部摘要。补充触发条件用 `/admin` 或直接编辑 `rules.md`。

### 注意事项

- 本项目使用 QQNT + NapCat 的**非官方**接入方式，不是腾讯官方 QQ 机器人 SDK。
- **不收集也不保存 QQ 密码**；QQ 登录由 QQNT/NapCat 自己完成，通常是扫码登录。
- 群聊原文、图片和文件会被上传到你配置的第三方模型 API，请自行评估平台规则、群成员知情同意与合规风险。
- `execute_command` 以本机用户权限执行，**没有沙箱**。不要为陌生群启用，不要把控制台暴露到局域网或公网。
- API Key / Token / Cookie / `rules.md` / SQLite / 媒体全部属于本机凭据或运行状态，`.gitignore` 已排除 `data/`、`*.sqlite3`、`*cookie*.txt`、`*storage-state*.json`、`.env`，请勿提交或同步。

---

## 16. 免责声明与开源发布

### 免责声明

使用前请自行评估 QQ 账号稳定性、平台规则、群成员知情同意，以及把群聊内容上传到第三方模型服务带来的隐私和合规风险。

### 发布检查清单

发布到公开仓库前请确认：

- [ ] 没有提交 `data/`、SQLite、`api-key.json`、`bilibili-cookies.txt`、`youtube-cookies.txt`、`google-search-cookies.txt` 或 `rules.md`。
- [ ] README 中的 URL、截图和命令不包含个人账号、群号、Token、API Key 或本机绝对路径。
- [ ] 已选择并添加明确的开源许可证。
- [ ] 已在干净的 Windows 环境中测试安装、NapCat 反向 WS、LLM 请求和最小工具流程。
- [ ] 已告知使用者：这是非官方 QQ 接入，群聊内容可能被发送到配置的第三方 API，命令工具以本机用户权限运行。

### 贡献

欢迎提交 Issue 和 Pull Request。建议在提交前运行完整测试，并在报告中附上**脱敏后**的服务日志、操作系统、Python / NapCat / QQNT 版本和使用的端点模式；不要粘贴 Token、API Key、Cookie、群聊原文或临时 QQ 文件 URL。
