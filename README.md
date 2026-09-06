# QQ AI Group Agent

一个运行在 Windows 本机的 QQ 群聊/私聊 AI Agent：通过 QQNT + NapCat 的 OneBot 反向 WebSocket 接收消息，把当前会话的实时上下文、滚动摘要和有证据的长期记忆交给 OpenAI-compatible API，并让模型在隔离的会话工作目录中处理文件、媒体、网页和本地任务。

## Repository description

> Windows 本机 QQ 群/私聊 AI Agent：NapCat/OneBot + OpenAI-compatible API，支持长期摘要、证据链记忆、视觉与媒体工具、会话工作区命令，以及真人式自主参与。

English:

> A local Windows QQ group/private-chat AI agent powered by NapCat/OneBot and OpenAI-compatible APIs, with durable summaries, evidence-backed memory, multimodal tools, isolated workspaces, and human-like participation.

## 当前发布说明

当前 `main` 分支包含异步 Google Flow 视频任务、双账号切换、Google/Bing/DuckDuckGo 搜索诊断、工作区媒体下载、严格事实引用和可断行的 Markdown 表格渲染。视频任务提交后立即返回任务 ID，后台负责轮询和发送；聊天线程不会被长时间等待阻塞。

发布到公开仓库前，请只提交源代码、测试和文档。运行数据目录中的 SQLite、媒体、API Key、Flow storage-state、各站点 Cookie 和浏览器 profile 都属于本机凭据/运行状态，已通过 `.gitignore` 排除，必须留在本机并通过环境变量或启动参数接入。

## 免责声明

本项目使用 QQNT + NapCat 的非官方接入方式，不是腾讯官方 QQ 机器人 SDK。使用前请自行评估 QQ 账号稳定性、平台规则、群成员知情同意，以及把群聊内容上传到第三方模型服务带来的隐私和合规风险。

项目不会收集或保存 QQ 密码；QQ 登录由 QQNT/NapCat 自己完成，通常使用扫码登录。

## 主要能力

- FastAPI + SQLite 本机服务，控制台默认只监听 `127.0.0.1`。
- 通过 OneBot 反向 WebSocket 接入 NapCat；新发现的群默认关闭，必须在控制台显式启用。
- 支持群聊和私聊，每个会话都有独立的 Agent 工作目录、摘要和长期记忆。
- 启用会话时读取最近最多 200 条消息，文本预算最多 150,000 字；最新约 150,000 字原文在模型请求中保持可见，150,000 字之前的更早内容才进入滚动摘要。
- 长期记忆按会话隔离，保存原消息证据、置信状态、版本和冲突关系，不依赖向量数据库或 embedding 模型。
- 支持 OpenAI-compatible Chat Completions、Responses，以及自定义完整 `base` 端点。
- 支持全局/群级 `reasoning_effort`：`off`、`minimal`、`low`、`medium`、`high`、`xhigh`。
- 模型可以自主决定是否参与普通群聊；直接 @、QQ 回复机器人、明确召唤和明确任务会优先回应，普通刷屏可以保持安静。
- 工具失败会保留结构化错误和审计记录；本地参数错误可以安全修正，结果不确定的 QQ 状态操作不会盲目重复。
- 图片会持久化到媒体目录并按容量预算淘汰最旧原图；事件、摘要、记忆和审计记录保留。
- 视频使用 ffmpeg 每 10 帧抽图，按约 300,000 tokens 的单次视觉输入上限分块提交视觉模型，再合并为完整总结。
- 音频使用 ffmpeg 转为 QQ `record` 语音，超过 50 秒自动拆分，不上传成普通文件。
- Bilibili/YouTube 视频使用 yt-dlp 下载到当前会话 workspace，默认不超过 720P；工具不会自动发 QQ，Agent 核验 path 后需要发送时再调用 `send_group_file`，由发送工具转为 QQ 可播放的 H.264/AAC MP4 视频卡片。
- Markdown 长文、代码、公式和题解可以按模型判断渲染为 MarkFlow 风格长图；短文本仍可直接发送。

## 架构概览

```text
QQNT + NapCat
      │  OneBot reverse WebSocket
      ▼
FastAPI service ── SQLite (events / summaries / memory / audits)
      │
      ├── OpenAI-compatible LLM API
      ├── Google Flow local video API (async jobs)
      ├── local media store
      ├── per-conversation workspace
      └── ffmpeg / yt-dlp / Node.js (media tools)
```

同一会话始终只有一个持久化主 worker，按事件顺序处理。主 Agent 忙碌时，新消息可以获得一个只读、短小的状态回复，但不会改变主任务的工具决策或摘要游标。

## 前置条件

- Windows 10/11。
- Python 3.10 或更高版本。
- 可正常运行的 QQNT + NapCat。
- 一个 OpenAI-compatible API、模型名和 API Key；视觉功能需要支持图片内容段的模型。
- 可选的 Google Flow 视频 API：使用配套 `google-flow-video` Skill 的本机服务（默认 `http://127.0.0.1:8787`）。
- `ffmpeg`：视频转码、视频抽帧、音乐转 QQ 语音都需要。
- Node.js 22 或更高版本：YouTube 的 yt-dlp EJS challenge solver 需要。启动脚本会自动发现 Node 并传给 yt-dlp。
- 可选：Microsoft Edge 和一个 MarkFlow checkout，用于 Markdown 长图渲染。

建议使用专用 QQ 账号，并只在自己明确管理、成员知情的测试群中启用。

## 安装

在仓库根目录打开 PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

启动服务：

```powershell
.\scripts\start.ps1
```

如果 PowerShell 禁止执行脚本，可以不修改全局执行策略：

```powershell
.\scripts\start.cmd
```

或只对当前启动进程临时放行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

默认地址：<http://127.0.0.1:8765>

启动器会检查端口占用、自动发现 `ffmpeg`/Node，并打印实际运行数据目录。默认还会在后台启动并守护 Google Flow 本机视频 API（`127.0.0.1:8787`）；Flow 项目或 Node 尚未安装时只记录告警并持续重试，不会阻止 QQ 服务启动。如果服务已经在运行，会显示健康状态后退出；如果端口被其他程序占用，会报告具体进程。

### 终端控制台

需要在启动服务后直接从 PowerShell 查看状态、重试会话或让后台管理员 AI 执行规则维护时，运行：

```powershell
.\scripts\console.ps1
```

也可以让原启动脚本自动启动服务并进入控制台：

```powershell
.\scripts\start.ps1 -Console
```

`console.cmd` 和 `scripts\env\console.ps1` 是对应的兼容入口。服务已在运行时控制台会自动附着，不会重复启动；退出 `/quit` 只退出输入循环，不会停止 QQ 服务。输入 `/help` 可查看全部中文命令说明，常用命令包括 `/status`、`/groups`、`/retry <会话ID>`、`/reset-memory <会话ID> reset`、`/flow`、`/workspace <会话ID>` 和 `/admin <内容>`。终端控制台只接受本机 loopback 请求，命令表不提供任意 shell 执行。

## 首次配置

打开控制台的“模型与连接”页面并填写：

1. LLM 端点模式、URL、模型名和 API Key。
2. OneBot Token；它必须和 NapCat 反向 WebSocket 客户端的 Token 完全一致。
3. 请求超时、视觉开关、媒体容量和是否透传思考深度。
4. 等 NapCat 连接后，在“发现的群”中显式启用要监听的群。
5. 如需视频生成，将配套 `Google Flow Video Gen to AI Skill` 项目的 `mcp` 目录设置为 `FLOW_VIDEO_PROJECT_DIR`（或使用当前机器的默认目录）；启动脚本会自动拉起并守护本机 API，并在本页“Google Flow 异步视频”中确认地址和轮询间隔。

API Key 会保存到运行数据目录的 `api-key.json`，按照本项目当前设计以明文 JSON 持久化，网页不会回显它。OneBot Token 使用 Windows 凭据存储。请勿提交、同步或分享这些文件。

### LLM 端点模式

| 模式 | URL 填写方式 | 实际请求 | 请求协议 |
| --- | --- | --- | --- |
| `base` | 供应商给出的完整请求 URL | 原样 `POST` | Chat Completions 兼容格式 |
| `completions` | API 根地址，例如 `https://api.example.com/v1` | 追加 `/chat/completions` | Chat Completions |
| `responses` | API 根地址，例如 `https://api.openai.com/v1` | 追加 `/responses` | OpenAI Responses |

供应商拒绝 `reasoning_effort` 时，服务会自动去掉该字段重试一次，并在控制台记录告警。

## NapCat 反向 WebSocket

在 NapCat WebUI 中新建并启用 **WebSocket 客户端/反向 WS**，不要配置成正向 WS 服务端：

| NapCat 字段 | 值 |
| --- | --- |
| URL | `ws://127.0.0.1:8765/onebot/v11` |
| Token | 与本地控制台填写的 OneBot Token 相同 |
| 上报格式 | `array` |
| `reportSelfMessage` | `false` |
| 重连间隔 | `5000` ms |
| 心跳周期 | `30000` ms |

参考配置形状如下，实际字段名以你的 NapCat 版本为准，优先使用 WebUI 保存：

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

顶部状态显示“反向 WebSocket 已认证”后，再启用群。遇到 403 时优先检查 URL、Token、NapCat 是否仍在运行，以及服务是否真的监听在同一端口。

官方参考：

- [NapCat 安装与启动](https://napneko.github.io/guide/install)
- [NapCat 基础配置](https://napneko.github.io/config/basic)
- [NapCat 事件文档](https://napneko.github.io/develop/event)
- [NapCat API 文档](https://napneko.github.io/onebot/api)

## Agent 工具

工具始终由服务端固定作用于当前群/私聊和当前工作目录，群聊原文、网页、搜索结果、文件内容和记忆都不能改变工具边界。

| 工具 | 作用 |
| --- | --- |
| `send_group_message` | 向当前群或私聊发送文字，可选引用当前会话可信消息 |
| `recall_own_message` | 只撤回本应用自己记录发送的消息 |
| `Builtin_Websearch` | Google → Bing → DuckDuckGo 公共网页搜索回退；Google 可读取运行数据目录的 `google-search-cookies.txt`（仅用于 Google），结果带 provider/source_id/URL 诊断 |
| `Builtin_patch` | 抓取公开 HTTP(S) 链接正文 |
| `Builtin_querymessage` | 查询当前会话已记录的前文 |
| `Builtin_querymemory` | 查询当前会话有证据的长期记忆 |
| `Builtin_image_generation` | 调用配置的图片生成模型并发送图片 |
| `Builtin_render_markdown_image` | 使用 MarkFlow 风格渲染 Markdown 长图 |
| `Builtin_list_group_files` / `Builtin_download_group_file` | 查看、下载当前群文件 |
| `list_workspace_files` / `read_workspace_file` | 列出或读取当前会话工作目录 |
| `write_workspace_file` | 写入工作目录中的文本文件 |
| `execute_command` | 以当前服务账户权限访问整台机器执行命令；只有关机/重启/注销/休眠/电源切换会被拦截，命令失败会继续交给 Agent；前 3 次进度提示显示命令前 30 字符，之后每 3 次汇报累计次数 |
| `send_group_file` | 发送工作区文件；视频转视频卡片，音频转 QQ 语音 |
| `Builtin_video_understanding` | 下载/读取视频、抽帧、视觉分块总结 |
| `Builtin_bilibili_download` | 下载 Bilibili 视频到当前会话 workspace（不自动发送） |
| `Builtin_youtube_download` | 下载 YouTube 视频到当前会话 workspace（不自动发送，支持 URL 或搜索词） |
| `Builtin_music_download` | 下载音乐并以 QQ 语音发送 |
| `Builtin_video_generation_create` | 向本机 Google Flow 提交异步视频生成任务，立即返回任务 ID |
| `Builtin_video_generation_list` | 查询当前会话自己的视频生成任务列表 |
| `Builtin_video_generation_result` | 查询当前会话任务状态/错误/发送结果，可按需补发 |

普通状态工具一次处理最多执行 16 个调用，并有有限的 Agent 决策轮次；`execute_command` 是本地诊断/修复原语，不受这两个上限限制，直到 Agent 返回最终文本或用户停止任务。状态改变工具会写入审计表并使用幂等操作记录，避免网络超时后重复发送、撤回或上传。

### 文件、视频和音乐

每个群聊或私聊默认使用独立目录：

```text
D:\Workspace\<群号或私聊编号>
```

可使用 `QQ_AI_WORKSPACE_ROOT` 修改根目录。`execute_command` 没有通用沙箱；它以运行服务的 Windows 用户权限执行，因此不要为陌生群启用机器人，也不要把控制台暴露到局域网或公网。

视频下载默认不超过 720P，并先只保存到当前会话 workspace；Agent 核验返回的相对路径后，确需发送时再调用 `send_group_file`，该工具会通过 ffmpeg 转成 H.264/AAC、`yuv420p`、fast-start MP4，再使用 OneBot 视频段发送。视频下载/处理的服务上限为 2 GiB；普通文档和音频仍有 100 MiB 工作区限制，QQ/NapCat 还可能施加自己的上传限制。

Bilibili cookies 放在运行数据目录的 `bilibili-cookies.txt`，YouTube cookies 放在 `youtube-cookies.txt`，Google 搜索 cookies 放在 `google-search-cookies.txt`（也兼容 `google-cookies.txt`），均使用 Netscape cookie 格式。三者严格分开，cookie 文件绝不能提交到仓库。

视频理解流程为：

1. 从消息 URL、OneBot 文件 URL 或 NapCat 本地缓存获取视频。
2. 使用 ffmpeg 每 10 帧抽取截图。
3. 按约 300,000 tokens 的单次视觉输入上限分块提交截图（预留少量指令空间）。
4. 合并分段总结，返回最多约 20,000 字的完整视频理解结果。

### Google Flow 异步视频生成

视频生成与普通聊天是两条独立链路，唯一允许的 Flow 模型是 Gemini Omni Flash 1.1（API ID `gemini-omni-flash`），不会选择或回退到 Veo 3.1 等其他模型。模型调用 `Builtin_video_generation_create` 后只提交任务，不会在同一轮等待浏览器生成；服务把任务写入 SQLite，并由后台轮询 Flow API。Flow 返回 `completed` 后，服务下载每个 MP4 输出，使用 ffmpeg 转成 QQ 可播放的 H.264/AAC 视频卡片，再发送到创建任务的原群或私聊。任务生成期间新的 QQ 消息仍会进入正常 Agent worker；模型收到“做好了吗”等问题时使用 `Builtin_video_generation_list` 或 `Builtin_video_generation_result` 查询真实状态。

配套 Flow API 默认位于 `http://127.0.0.1:8787`。`scripts\start.ps1`、`scripts\start.cmd` 和登录计划任务都会自动启动 `scripts\flow-api-guard.ps1`：它负责启动 Node API、使用 `/healthz` 检查进程、使用 `/readyz` 实际验证每个账号的登录状态、Flow 工作区、输入框和生成按钮，并在进程崩溃后重启。守护进程使用隐藏窗口运行，日志写入 `%LOCALAPPDATA%\QQAIGroupAgent\data\logs\flow-api-guard.log`、`flow-api.stdout.log` 和 `flow-api.stderr.log`。

首次使用或更换 Flow 项目时，可先在其项目目录运行一次：

```powershell
cd 'D:\path\to\google-flow-video-skill\mcp'
npm install
npx playwright install chromium
# 先按该 Skill 的说明设置 FLOW_STORAGE_STATE 或 FLOW_COOKIES_FILE
$env:FLOW_API_HOST = '127.0.0.1'
$env:FLOW_API_PORT = '8787'
npm run api
```

也可以直接让 QQ 代理完成启动：

```powershell
$env:FLOW_VIDEO_PROJECT_DIR = 'D:\path\to\google-flow-video-skill\mcp'
.\scripts\start.ps1
```

如果项目放在其他位置，优先设置 `FLOW_VIDEO_PROJECT_DIR`（兼容别名 `GOOGLE_FLOW_VIDEO_PROJECT`）。Flow 登录状态可设置 `FLOW_STORAGE_STATE` / `FLOW_STORAGE_STATE_2`，也可以把两个 Google Flow 的 Cookie 导出文件通过 `-FlowCookiesFile` 与 `-FlowCookiesFile2` 传给启动脚本；守护会自动转换 Netscape 文件为本地 JSON。Cookie/storage-state 只负责首次迁移或主动换号，之后每个账号都使用 `%LOCALAPPDATA%\QQAIGroupAgent\data\flow-browser-profiles\account-*` 下的独立持久 Chrome 资料目录，Google 刷新的 Cookie、localStorage 和设备会话会跨 API 重启保留，旧导出文件不会在每次启动时覆盖新会话。每个有效账号使用独立 Chromium worker，因此两个账号可同时生成两个任务；更多任务保持 `queued`，实际取得 worker 后才变为 `in_progress`。提交前失败会在两个账号间最多尝试 3 次，Generate 已成功点击后不会重复提交。需要手动只启动守护时运行 `.\scripts\flow-api.cmd`。没有 Flow 项目时可用 `.\scripts\start.ps1 -NoFlowApi` 禁用自动守护。

如果严格检测提示账号需要重新登录，不要再反复导出同一份 Cookie。直接运行以下命令，服务会用系统方式打开该账号的普通 Chrome 窗口，不通过 Playwright 登录，因此没有 `--no-sandbox`、`--enable-automation` 或远程调试参数。正常完成 Google 登录并进入 Flow 后，关闭整个专用 Chrome 窗口；服务会接管同一个资料目录并执行严格检测。该窗口与日常 Chrome 资料目录隔离，不会遇到正在使用中的 profile 锁，也不会修改日常 Chrome 数据。

```powershell
.\scripts\flow-login.cmd 1
.\scripts\flow-login.cmd 2
```

`FLOW_MAX_CREDITS` 只是本机 API 的预检上限，不是 Google Flow 的实时余额；默认已设为 1050，Google Flow 账户自身的剩余额度/策略仍是最终限制。修改此值后需要重启 Flow API 守护进程。任务排队时间不计入生成时限；实际提交后的等待上限为 20 分钟（`FLOW_TIMEOUT_MS=1200000`）。轮询页面连续失败 3 次会提前判定浏览器失活，不会继续伪装成生成中。创建任务前还会使用一分钟内的严格 readiness 结果；没有任何可用账号时直接返回 503，不创建任务也不预留本地额度。

该 Flow API 的任务状态保存在 API 进程内存中；如果单独重启 Flow API，QQ 服务保留本地记录，但查询不到的上游任务会标记为失败并把原因告诉当前会话。需要鉴权时可在 Flow API 进程设置 `FLOW_API_KEY`，再在 QQ 服务进程设置同名变量；密钥不会写入提示词或 QQ 消息。

## 长期记忆和重算

长期记忆不使用向量压缩模型，而是保存可核验的结构化事实：主体、关系/属性、值、时态、置信状态、来源事件 ID 和证据片段。

- 新消息只能提出当前会话的记忆候选。
- 服务会验证候选的群归属和证据是否确实出现在原文中。
- 新事实不会无声覆盖旧事实；更正、失效、冲突和撤回都有版本记录。
- 控制台可以查看、确认、修改、撤回或软删除群记忆。
- “清空本群记忆与规则并重算历史”只删除该会话的派生记忆、摘要和群级规则，然后重新归档已有消息；不会重放旧的 QQ 发送、撤回、上传或下载动作。

全局管理员 `rules.md` 与每个群的事实记忆是两套不同数据：前者描述机器人的行为偏好，后者记录该会话中有证据的事实。

## 可选环境变量

| 变量 | 用途 |
| --- | --- |
| `QQ_AI_DATA_DIR` | SQLite、媒体、配置和各站点 cookies 的运行数据目录；Google 搜索默认读取其中的 `google-search-cookies.txt` |
| `QQ_AI_WORKSPACE_ROOT` | 会话工作目录根路径 |
| `FFMPEG_PATH` | `ffmpeg.exe` 的完整路径 |
| `NODE_PATH` | Node.js 可执行文件路径 |
| `QQ_AI_MARKFLOW_ROOT` | MarkFlow checkout 根目录 |
| `QQ_AI_MARKDOWN_RENDER_BROWSER` | Edge/Chromium 可执行文件路径 |
| `GOOGLE_SEARCH_COOKIES_FILE`（兼容 `GOOGLE_COOKIES_FILE`） | 可选，Google 搜索专用 Netscape Cookie 文件；不设置时读取 `QQ_AI_DATA_DIR/google-search-cookies.txt` |
| `FLOW_API_KEY` | 可选，本机 Google Flow API 的 Bearer Token；Flow API 同样设置此变量时启用鉴权 |
| `FLOW_VIDEO_PROJECT_DIR` | Google Flow Video Skill 的 `mcp` 项目目录；启动脚本据此拉起 Node API |
| `GOOGLE_FLOW_VIDEO_PROJECT` | `FLOW_VIDEO_PROJECT_DIR` 的兼容别名 |
| `FLOW_NODE_PATH` | 可选，Node.js 可执行文件完整路径；未设置时从 PATH 查找 |
| `FLOW_STORAGE_STATE` | Playwright storage-state JSON 的完整路径 |
| `FLOW_COOKIES_FILE` | Flow Cookie JSON 或 Netscape `.txt` 文件的完整路径；`.txt` 会在本机转换 |
| `FLOW_STORAGE_STATE_2` | 第二个 Flow 账号的 Playwright storage-state JSON 路径（可选） |
| `FLOW_COOKIES_FILE_2` | 第二个 Flow 账号的 Cookie JSON 或 Netscape `.txt` 路径（可选） |
| `FLOW_STORAGE_STATES` / `FLOW_COOKIES_FILES` | 兼容的分号分隔多账号写法；最多读取两个账号 |
| `FLOW_PROFILE_ROOT` | 两个 Flow 持久浏览器资料的根目录；QQ 守护默认放在运行数据目录的 `flow-browser-profiles` 下 |
| `FLOW_USER_DATA_DIR` / `FLOW_USER_DATA_DIR_2` | 可选，分别覆盖两个账号的专用持久浏览器资料目录；不要指向正在使用的日常 Chrome `User Data` |
| `FLOW_PERSISTENT_PROFILES` | 是否启用持久浏览器资料，默认 `1`；只有兼容旧部署时才设为 `0` |
| `FLOW_BROWSER_EXECUTABLE_PATH` | 可选，Chromium/Chrome/Edge 可执行文件；启动守护会自动寻找 Chrome SxS |
| `FLOW_MAX_CREDITS` | Flow API 本地预检额度上限，默认 `1050`；Google Flow 账户余额仍是最终限制 |
| `FLOW_TIMEOUT_MS` | 实际提交后浏览器等待上限；启动脚本固定为 `1200000`（20 分钟），排队时间不计入 |
| `FLOW_API_PORT` | Flow API 端口，默认 `8787`；启动脚本可用 `-FlowApiPort` 覆盖，改端口后请把控制台“Google Flow 异步视频”的 API 地址同步改成相同端口 |

默认数据目录为 `%LOCALAPPDATA%\QQAIGroupAgent\data`。将数据目录放在健康的 NTFS 磁盘上，不要把 SQLite、媒体、cookies、`api-key.json` 或 `rules.md` 上传到公开仓库。

## 常用启动选项

```powershell
# 开发热重载
.\scripts\start.ps1 -Reload

# 错误时保留窗口
.\scripts\start.ps1 -KeepOpenOnError

# 改用其他本机端口
.\scripts\start.ps1 -Port 8877

# 指定运行数据目录
.\scripts\start.ps1 -DataDir 'C:\QQAIGroupAgent\data'

# 启动 QQNT/NapCat 后再启动服务
.\scripts\start.ps1 -NapCatExecutable 'C:\Path\To\QQ.exe'

# 指定 Flow API 项目和端口
.\scripts\start.ps1 -FlowProjectDir 'D:\path\to\google-flow-video-skill\mcp' -FlowApiPort 8787

# 使用 Google Flow Netscape Cookie 导出文件（会自动转换为本地 JSON）
.\scripts\start.ps1 -FlowCookiesFile 'C:\Users\you\Downloads\labs.google_cookies.txt'

# 配置第二个账号；提交前失败时自动切换，最多共尝试 3 次
.\scripts\start.ps1 -FlowCookiesFile 'C:\Users\you\Downloads\labs.google_cookies.txt' -FlowCookiesFile2 'C:\Users\you\Downloads\cookies.json'

# 本次启动不拉起 Flow API（不会修改已存在的进程）
.\scripts\start.ps1 -NoFlowApi
```

登录后自动启动任务：

```powershell
.\scripts\install-autostart.ps1
.\scripts\install-autostart.ps1 -NapCatExecutable 'C:\Path\To\QQ.exe'
.\scripts\install-autostart.ps1 -FlowProjectDir 'D:\path\to\google-flow-video-skill\mcp'
.\scripts\install-autostart.ps1 -Remove
```

安装脚本会注册两个当前用户登录任务：QQ 服务，以及带崩溃重启策略的 Google Flow API 守护任务。两者都不保存 QQ 密码；使用 `-NoFlowApi` 安装可移除/禁用 Flow 任务。

## 故障排查

### PowerShell 报 running scripts is disabled

使用 `scripts\start.cmd`，或执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

### 8765 端口被占用

再次运行启动脚本时，若占用者是本代理，会直接显示“已在运行”；如果是其他程序，关闭它或使用 `-Port` 指定另一个本机端口，并同步修改 NapCat URL。

### OneBot 403 或未连接

检查反向 WS URL 是否为 `ws://127.0.0.1:8765/onebot/v11`，Token 是否逐字一致，NapCat 是否已启用 WebSocket 客户端，以及控制台是否仍只绑定本机地址。

### 视频理解没有 API 流量

先看工具审计：如果 `Builtin_video_understanding` 报 QQ 临时 URL 中途断开，视觉模型尚未收到任何截图；重新发送原视频以获取新的 QQ URL。服务会对可续传的 URL 自动进行 Range 重试，源文件完整下载后才会启动视觉请求。

### SQLite `disk I/O error`

停止服务，备份运行数据目录中的 SQLite 文件，再检查目录权限、磁盘健康和是否位于异常的同步/可移动文件系统。不要在未备份的损坏数据库上反复写入；仓库提供 `scripts\recover_sqlite.py` 作为人工恢复辅助工具。

## 测试

安装开发依赖后运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

测试覆盖 OneBot 事件、同群顺序、断线补偿、摘要/记忆、工具幂等、Chat Completions/Responses、视觉降级、视频卡片、音频切分、文件处理、CSRF 和 Web 控制台等路径。

## 开源发布检查清单

发布前请确认：

- 没有提交 `data/`、SQLite、`api-key.json`、`bilibili-cookies.txt`、`youtube-cookies.txt`、`google-search-cookies.txt` 或 `rules.md`。
- README 中的 URL、截图和命令不包含个人账号、群号、Token、API Key 或本机绝对路径。
- 已选择并添加明确的开源许可证；当前仓库未替你预设许可证。
- 已在干净的 Windows 环境中测试安装、NapCat 反向 WS、LLM 请求和最小工具流程。
- 已告知使用者：这是非官方 QQ 接入，群聊内容可能被发送到配置的第三方 API，命令工具以本机用户权限运行。

## 贡献

欢迎提交 Issue 和 Pull Request。建议在提交前运行完整测试，并在报告中附上脱敏后的服务日志、操作系统、Python/NapCat/QQNT 版本和使用的端点模式；不要粘贴 Token、API Key、cookies、群聊原文或临时 QQ 文件 URL。
