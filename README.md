# QQ AI Group Agent

一个运行在 Windows 本机的 QQ 群聊/私聊 AI Agent：通过 QQNT + NapCat 的 OneBot 反向 WebSocket 接收消息，把当前会话的实时上下文、滚动摘要和有证据的长期记忆交给 OpenAI-compatible API，并让模型在隔离的会话工作目录中处理文件、媒体、网页和本地任务。

## Repository description

> Windows 本机 QQ 群/私聊 AI Agent：NapCat/OneBot + OpenAI-compatible API，支持长期摘要、证据链记忆、视觉与媒体工具、会话工作区命令，以及真人式自主参与。

English:

> A local Windows QQ group/private-chat AI agent powered by NapCat/OneBot and OpenAI-compatible APIs, with durable summaries, evidence-backed memory, multimodal tools, isolated workspaces, and human-like participation.

## 免责声明

本项目使用 QQNT + NapCat 的非官方接入方式，不是腾讯官方 QQ 机器人 SDK。使用前请自行评估 QQ 账号稳定性、平台规则、群成员知情同意，以及把群聊内容上传到第三方模型服务带来的隐私和合规风险。

项目不会收集或保存 QQ 密码；QQ 登录由 QQNT/NapCat 自己完成，通常使用扫码登录。

## 主要能力

- FastAPI + SQLite 本机服务，控制台默认只监听 `127.0.0.1`。
- 通过 OneBot 反向 WebSocket 接入 NapCat；新发现的群默认关闭，必须在控制台显式启用。
- 支持群聊和私聊，每个会话都有独立的 Agent 工作目录、摘要和长期记忆。
- 启用会话时读取最近最多 200 条消息，文本预算最多 50,000 字；最新约 50,000 字原文在模型请求中保持可见，更早内容才进入滚动摘要。
- 长期记忆按会话隔离，保存原消息证据、置信状态、版本和冲突关系，不依赖向量数据库或 embedding 模型。
- 支持 OpenAI-compatible Chat Completions、Responses，以及自定义完整 `base` 端点。
- 支持全局/群级 `reasoning_effort`：`off`、`minimal`、`low`、`medium`、`high`、`xhigh`。
- 模型可以自主决定是否参与普通群聊；直接 @、QQ 回复机器人、明确召唤和明确任务会优先回应，普通刷屏可以保持安静。
- 工具失败会保留结构化错误和审计记录；本地参数错误可以安全修正，结果不确定的 QQ 状态操作不会盲目重复。
- 图片会持久化到媒体目录并按容量预算淘汰最旧原图；事件、摘要、记忆和审计记录保留。
- 视频使用 ffmpeg 每 10 帧抽图，按约 300 KiB 分块提交视觉模型，再合并为完整总结。
- 音频使用 ffmpeg 转为 QQ `record` 语音，超过 50 秒自动拆分，不上传成普通文件。
- Bilibili/YouTube 视频使用 yt-dlp 下载，默认不超过 720P，并在发送前转为 QQ 可播放的 H.264/AAC MP4 视频卡片。
- Markdown 长文、代码、公式和题解可以按模型判断渲染为 MarkFlow 风格长图；短文本仍可直接发送。

## 架构概览

```text
QQNT + NapCat
      │  OneBot reverse WebSocket
      ▼
FastAPI service ── SQLite (events / summaries / memory / audits)
      │
      ├── OpenAI-compatible LLM API
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

启动器会检查端口占用、自动发现 `ffmpeg`/Node，并打印实际运行数据目录。如果服务已经在运行，会显示健康状态后退出；如果端口被其他程序占用，会报告具体进程。

## 首次配置

打开控制台的“模型与连接”页面并填写：

1. LLM 端点模式、URL、模型名和 API Key。
2. OneBot Token；它必须和 NapCat 反向 WebSocket 客户端的 Token 完全一致。
3. 请求超时、视觉开关、媒体容量和是否透传思考深度。
4. 等 NapCat 连接后，在“发现的群”中显式启用要监听的群。

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
| `Builtin_Websearch` | Google 公共网页搜索 |
| `Builtin_patch` | 抓取公开 HTTP(S) 链接正文 |
| `Builtin_querymessage` | 查询当前会话已记录的前文 |
| `Builtin_querymemory` | 查询当前会话有证据的长期记忆 |
| `Builtin_image_generation` | 调用配置的图片生成模型并发送图片 |
| `Builtin_render_markdown_image` | 使用 MarkFlow 风格渲染 Markdown 长图 |
| `Builtin_list_group_files` / `Builtin_download_group_file` | 查看、下载当前群文件 |
| `list_workspace_files` / `read_workspace_file` | 列出或读取当前会话工作目录 |
| `write_workspace_file` | 写入工作目录中的文本文件 |
| `execute_command` | 在当前会话工作目录执行本地命令；执行前会发送进度提示 |
| `send_group_file` | 发送工作区文件；视频转视频卡片，音频转 QQ 语音 |
| `Builtin_video_understanding` | 下载/读取视频、抽帧、视觉分块总结 |
| `Builtin_bilibili_download` | 下载并发送 Bilibili 视频 |
| `Builtin_youtube_download` | 下载并发送 YouTube 视频，支持 URL 或搜索词 |
| `Builtin_music_download` | 下载音乐并以 QQ 语音发送 |

模型一次处理最多执行 16 个工具调用，并有有限的 Agent 决策轮次。状态改变工具会写入审计表并使用幂等操作记录，避免网络超时后重复发送、撤回或上传。

### 文件、视频和音乐

每个群聊或私聊默认使用独立目录：

```text
D:\Workspace\<群号或私聊编号>
```

可使用 `QQ_AI_WORKSPACE_ROOT` 修改根目录。`execute_command` 没有通用沙箱；它以运行服务的 Windows 用户权限执行，因此不要为陌生群启用机器人，也不要把控制台暴露到局域网或公网。

视频下载默认不超过 720P，发送前会通过 ffmpeg 转成 H.264/AAC、`yuv420p`、fast-start MP4，再使用 OneBot 视频段发送。视频下载/处理的服务上限为 2 GiB；普通文档和音频仍有 100 MiB 工作区限制，QQ/NapCat 还可能施加自己的上传限制。

Bilibili cookies 放在运行数据目录的 `bilibili-cookies.txt`，YouTube cookies 放在 `youtube-cookies.txt`，均使用 Netscape cookie 格式。两者严格分开，cookie 文件绝不能提交到仓库。

视频理解流程为：

1. 从消息 URL、OneBot 文件 URL 或 NapCat 本地缓存获取视频。
2. 使用 ffmpeg 每 10 帧抽取截图。
3. 每约 300 KiB 截图数据提交一次视觉模型。
4. 合并分段总结，返回最多约 20,000 字的完整视频理解结果。

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
| `QQ_AI_DATA_DIR` | SQLite、媒体、配置和 cookies 的运行数据目录 |
| `QQ_AI_WORKSPACE_ROOT` | 会话工作目录根路径 |
| `FFMPEG_PATH` | `ffmpeg.exe` 的完整路径 |
| `NODE_PATH` | Node.js 可执行文件路径 |
| `QQ_AI_MARKFLOW_ROOT` | MarkFlow checkout 根目录 |
| `QQ_AI_MARKDOWN_RENDER_BROWSER` | Edge/Chromium 可执行文件路径 |

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
```

登录后自动启动任务：

```powershell
.\scripts\install-autostart.ps1
.\scripts\install-autostart.ps1 -NapCatExecutable 'C:\Path\To\QQ.exe'
.\scripts\install-autostart.ps1 -Remove
```

计划任务只启动当前 Windows 用户的 QQNT/NapCat 和本服务，不保存 QQ 密码。

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

- 没有提交 `data/`、SQLite、`api-key.json`、`bilibili-cookies.txt`、`youtube-cookies.txt` 或 `rules.md`。
- README 中的 URL、截图和命令不包含个人账号、群号、Token、API Key 或本机绝对路径。
- 已选择并添加明确的开源许可证；当前仓库未替你预设许可证。
- 已在干净的 Windows 环境中测试安装、NapCat 反向 WS、LLM 请求和最小工具流程。
- 已告知使用者：这是非官方 QQ 接入，群聊内容可能被发送到配置的第三方 API，命令工具以本机用户权限运行。

## 贡献

欢迎提交 Issue 和 Pull Request。建议在提交前运行完整测试，并在报告中附上脱敏后的服务日志、操作系统、Python/NapCat/QQNT 版本和使用的端点模式；不要粘贴 Token、API Key、cookies、群聊原文或临时 QQ 文件 URL。

## 许可证

本仓库当前未附带许可证。若要公开分发，请在发布前根据你的需求添加 MIT、Apache-2.0 或其他合适的许可证文件，并确认第三方依赖和 NapCat/QQNT 的使用规则。
