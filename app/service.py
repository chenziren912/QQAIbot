"""Application orchestration: OneBot events -> durable queue -> LLM -> restricted QQ tools."""

from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import json
import logging
import re
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit

import httpx

from .config import AppSettings, SecretStore, VALID_LLM_ENDPOINT_MODES, migrate_legacy_global_prompt
from .db import Database, json_loads
from .llm import AdminConversationClient, ChatCompletionsClient, redact_error_detail
from .markdown_render import MarkdownRenderError, render_markdown_images
from .media import MediaError, MediaStore, image_file_to_data_uri
from .music import (
    DEFAULT_VOICE_SEGMENT_SECONDS,
    MusicDownloadError,
    VoiceSegment,
    download_music_async,
    split_audio_for_qq_voice,
)
from .onebot import OneBotActionTimeoutError, OneBotAdapter, OneBotDisconnectedError
from .rules import RulesStore
from .webtools import WebToolError, fetch_link, google_search
from .workspace import (
    DEFAULT_WORKSPACE_ROOT,
    MAX_WORKSPACE_FILE_BYTES,
    WorkspaceError,
    WorkspaceManager,
)


logger = logging.getLogger(__name__)

MAX_HISTORY_EVENTS = 200
MAX_EVENT_TEXT_CHARS = 50_000
MAX_IMAGES_PER_TURN = 4
MAX_QQ_TEXT_CHARS = 4_000
# Keep a broad source range, then select from its newest end by exact rendered
# character budget.  A 50K transcript can legitimately contain far more than
# ten one-character QQ messages.
MAX_RAW_CONTEXT_SOURCE_EVENTS = 60_000
MAX_RECENT_CONTEXT_CHARS = 50_000
MAX_MEMORY_EVENTS_PER_PASS = 80
MAX_MEMORY_EXTRACTION_CHARS = 100_000
MAX_MEMORY_CONTEXT_CHARS = 80_000
MAX_MEMORY_CONTEXT_ITEMS = 1_000
MAX_ADMIN_CHAT_MESSAGE_CHARS = 12_000
MAX_ADMIN_HISTORY_FOR_MODEL = 40
MAX_IMAGE_GENERATION_PROMPT_CHARS = 4_000
MAX_MARKDOWN_RENDER_CHARS = 160_000
VALID_REASONING_EFFORTS = {"off", "minimal", "low", "medium", "high", "xhigh", "inherit"}
PRIVATE_CONVERSATION_PREFIX = "private:"
MAX_AUTO_SUMMARY_RECOVERY_ATTEMPTS = 1
SUMMARY_FAILURE_MARKERS = (
    "the prompt could not be submitted",
    "prompt contains sensitive words",
    "prohibited use policy",
    "模型没有返回摘要",
    "生成最终摘要失败",
    "工具执行后的最终摘要调用失败",
    "llm service error",
    "模型调用失败",
    "http 400",
    "http 401",
    "http 403",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
)
SUMMARY_SAFETY_REFUSAL_MARKERS = (
    "prompt contains sensitive words",
    "sensitive words",
    "prohibited use policy",
    "safety policy",
    "safety filter",
    "safety settings",
)
SAFETY_METADATA_FALLBACK_WARNING = (
    "上游模型拒绝接收本群原文（敏感内容安全策略），已切换为仅事件元数据的安全降级摘要；"
    "本次不会再次上传原文、图片或旧 QQ 动作。"
)

# These are emitted only after the model has actually selected the matching
# tool.  There is deliberately no generic "thinking" timer or message for
# historical-message/memory lookup tools.
TOOL_ACTIVITY_NOTICES = {
    "Builtin_Websearch": "正在搜索网络资料，请稍等。",
    "Builtin_patch": "正在访问网页并读取内容，请稍等。",
    "Builtin_image_generation": "正在生成图片，请等待至少5s",
    "Builtin_render_markdown_image": "正在渲染 Markdown 图片，请稍等。",
    "Builtin_bilibili_download": "正在下载哔哩哔哩视频，请稍等。",
    "Builtin_youtube_download": "正在下载 YouTube 视频，请稍等。",
    "Builtin_music_download": "正在下载音乐并转换为 QQ 语音，请稍等。",
    "Builtin_video_understanding": "正在逐帧分析视频，请稍等。",
    "Builtin_pdf_understanding": "正在读取 PDF 页面，请稍等。",
}

# These tools do not send a requested result to QQ themselves.  A failure is a
# completed local/reading diagnostic (the optional activity notice is separate
# and app-owned), so after an explicitly requested task the LLM orchestration
# may safely send one factual failure line if the provider fails to finalize a
# reply.  State-changing QQ tools are deliberately absent: their failures can
# have unknown remote outcomes and must never trigger another automatic send.
_SAFE_FINALIZATION_NOTICE_TOOLS = {"Builtin_video_understanding"}

MAX_COMMAND_OUTPUT_CHARS = 100_000
MAX_BILIBILI_URL_CHARS = 2_000
MAX_BILIBILI_DOWNLOAD_SECONDS = 600
MAX_YOUTUBE_URL_CHARS = 2_000
MAX_YOUTUBE_DOWNLOAD_SECONDS = 600
MAX_YOUTUBE_QUERY_CHARS = 500
MAX_QQ_VIDEO_TRANSCODE_SECONDS = 900
MAX_REMOTE_VIDEO_DOWNLOAD_ATTEMPTS = 3
QQ_VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts", ".m2ts", ".3gp"}
)
QQ_AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma", ".amr", ".spx"}
)
MAX_QQ_VOICE_SEGMENT_SECONDS = DEFAULT_VOICE_SEGMENT_SECONDS
MAX_QQ_VOICE_SEGMENTS = 120
_MUSIC_COMMAND_RE = re.compile(
    r"(?:yt-dlp|youtube-dl)\b.*(?:\b(?:bilisearch|scsearch)\d*:|--audio-format|\s-x(?:\s|$)|\.mp3\b|\.m4a\b|\baudio\b)",
    re.IGNORECASE,
)
_YOUTUBE_COMMAND_RE = re.compile(
    r"(?:yt-dlp|youtube-dl)\b.*(?:youtube(?:-nocookie)?\.com|youtu\.be|\bytsearch\d*:)",
    re.IGNORECASE,
)
BILIBILI_DOWNLOAD_ARGUMENTS = {
    "url",
    "format_selector",
    "format",
    "output_extension",
    "extension",
    "audio_only",
    "filename",
}
YOUTUBE_DOWNLOAD_ARGUMENTS = set(BILIBILI_DOWNLOAD_ARGUMENTS) | {"query"}
MAX_MUSIC_URL_CHARS = 2_000
MAX_MUSIC_DOWNLOAD_SECONDS = 600
MUSIC_DOWNLOAD_ARGUMENTS = {"url", "query", "title"}
HISTORY_ACTION_TIMEOUT_SECONDS = 20
# File URLs returned by QQ are short lived.  Source resolution is only a
# local OneBot request, so keep it substantially shorter than a full video
# download and do not let an unresponsive adapter strand an Agent turn.
ONEBOT_FILE_SOURCE_TIMEOUT_SECONDS = 20
MAX_WORKSPACE_LIST_ITEMS = 500
MAX_VIDEO_FILE_BYTES = 2 * 1024 * 1024 * 1024
VIDEO_FRAME_CHUNK_BYTES = 300 * 1024

# A newly arrived message must not wait behind a long-running primary Agent
# turn just to learn that it was received.  The auxiliary reply is strictly a
# short status acknowledgement, never another full tool-capable Agent turn.
BUSY_REPLY_LLM_DEADLINE_SECONDS = 10
MAX_BUSY_REPLY_TEXT_CHARS = 240
MAX_BUSY_REPLY_EVENT_CHARS = 4_000
MAX_BUSY_REPLY_TURN_CONTEXT_CHARS = 1_800
BUSY_REPLY_OPERATION_NAMESPACE = "auxiliary_busy_reply_v1"

_BUSY_REPLY_PROGRESS_CUES = (
    "正在",
    "处理",
    "忙",
    "稍等",
    "等一下",
    "收到",
    "进行",
    "继续",
)
_BUSY_REPLY_DISALLOWED_TEXT = (
    "```",
    "http://",
    "https://",
    "send_group_message",
    "builtin_",
    "工具调用",
    "内部摘要",
    "系统提示",
    "api key",
    "onebot",
)
_BUSY_TOOL_PROGRESS_LABELS = {
    "Builtin_Websearch": "搜索资料",
    "Builtin_patch": "读取网页内容",
    "Builtin_image_generation": "生成图片",
    "Builtin_render_markdown_image": "渲染 Markdown 图片",
    "Builtin_bilibili_download": "下载视频",
    "Builtin_youtube_download": "下载 YouTube 视频",
    "Builtin_music_download": "下载音乐并转换语音",
    "Builtin_video_understanding": "分析视频",
    "Builtin_pdf_understanding": "读取 PDF",
    "Builtin_download_group_file": "下载文件",
    "read_workspace_file": "读取文件",
    "write_workspace_file": "整理文件",
    "execute_command": "执行当前任务所需命令",
    "send_group_file": "发送文件",
}


def _file_info(raw: Dict[str, Any]) -> Dict[str, Any]:
    value = raw.get("file")
    if not isinstance(value, dict):
        message = raw.get("message")
        if isinstance(message, list):
            for segment in message:
                if not isinstance(segment, dict) or _as_string(segment.get("type")) != "file":
                    continue
                candidate = segment.get("data")
                if isinstance(candidate, dict):
                    value = candidate
                    break
    if not isinstance(value, dict):
        value = {}
    result = {
        "id": _as_string(value.get("id") or value.get("file_id") or value.get("file") or raw.get("file_id")),
        "name": _as_string(value.get("name") or value.get("file_name") or raw.get("file_name")),
        "size": _safe_int(value.get("size") or value.get("file_size") or raw.get("file_size")),
        "busid": _as_string(value.get("busid") or raw.get("busid")),
        "url": _as_string(value.get("url") or raw.get("url")),
        "path": _as_string(value.get("path") or raw.get("path")),
    }
    return {key: value for key, value in result.items() if value not in ("", 0)}


def _video_info(raw: Dict[str, Any]) -> Dict[str, Any]:
    value = raw.get("video")
    if not isinstance(value, dict):
        message = raw.get("message")
        if isinstance(message, list):
            for segment in message:
                if not isinstance(segment, dict) or _as_string(segment.get("type")) != "video":
                    continue
                candidate = segment.get("data")
                if isinstance(candidate, dict):
                    value = candidate
                    break
    if not isinstance(value, dict):
        return {}
    result = {
        "id": _as_string(value.get("id") or value.get("file_id") or value.get("file")),
        "name": _as_string(value.get("name") or value.get("file_name") or value.get("filename")),
        "size": _safe_int(value.get("size") or value.get("file_size")),
        "url": _as_string(value.get("url")),
        "path": _as_string(value.get("path")),
    }
    return {key: value for key, value in result.items() if value not in ("", 0)}

# This marker is generated exclusively by the local service after it has
# inspected an OneBot ``at`` segment.  It is deliberately kept separate from
# the original group-message text: a group member must not be able to create
# it merely by typing a look-alike sentence.
DIRECT_MENTION_CONTEXT_MARKER = (
    "【服务生成的实时直接提及标记：发送者在这条实时会话消息中直接 @ 了当前机器人；"
    "优先自然回应，不因工具选择不同而沉默。】"
)

# Like an @ marker, this is produced locally after a structured OneBot reply
# segment has been checked against our own durable ``sent_messages`` record.
# It is deliberately never inferred from a user-written ``[回复]`` string.
DIRECT_REPLY_TO_BOT_CONTEXT_MARKER = (
    "【服务生成的实时回复机器人标记：发送者使用 QQ 回复功能回复了本应用此前发送的消息；"
    "请像正常对话一样自然接话。】"
)

# A small, deliberately narrow set of ordinary-language group calls.  Unlike
# an @ or reply segment this is text-derived, but it only requests a harmless
# response in the already-current group; it never grants any new tool scope.
CLEAR_GROUP_CALL_CONTEXT_MARKER = (
    "【服务生成的实时群内召唤标记：成员刚发布了明确在等待在场者回应的消息；"
    "本轮可以在当前会话简短自然回应。】"
)
EXPLICIT_AGENT_TASK_CONTEXT_MARKER = (
    "【服务生成的实时明确任务标记：成员明确要求 Agent 处理当前视频、文件、图片、链接或内容；"
    "优先完成任务，若工具明确失败则如实告知而不是只留下内部摘要。】"
)
_CLEAR_GROUP_CALL_PATTERN = re.compile(
    r"(?:有人(?:吗|没)|在(?:吗|不在)|是人(?:的)?\s*发\s*[1一]|在(?:的)?\s*发\s*[1一])",
    re.IGNORECASE,
)

# This is intentionally narrower than ordinary conversation: it marks an
# actual current task aimed at the Agent even without an @.  Its purpose is not
# to make the bot chat more often or to force an early reply; it only lets the
# finalization layer tell the requester an honest tool error when a long task
# (for example, "深度理解这个视频，生成文字稿 Markdown 并渲染图片") terminates
# and the model fails to emit a final response.
_EXPLICIT_AGENT_TASK_REQUEST_PATTERN = re.compile(
    r"""
    (?:
        (?:请|麻烦|拜托|帮(?:我)?|能不能|可以|需要|想让|让|给我|请你)?\s*
        (?:深度\s*)?(?:理解|分析|解读|总结|生成|写(?:一)?|制作|渲染|下载|搜索|查询|查找|读取|处理|查看|看(?:看|下|一下))
        [^\n。！？!?]{0,180}
        (?:视频|影片|文件|图片|链接|网页|文章|文字稿|markdown|md|题解|代码|公式|pdf|pptx?|word|表格|文档)
    )
    |
    (?:
        (?:视频|影片|文件|图片|链接|网页|文章|文字稿|markdown|md|题解|代码|公式|pdf|pptx?|word|表格|文档)
        [^\n。！？!?]{0,100}
        (?:请|麻烦|帮(?:我)?|能不能|可以|需要|想让|让|给我|请你)?\s*
        (?:深度\s*)?(?:理解|分析|解读|总结|生成|写(?:一)?|制作|渲染|下载|搜索|查询|查找|读取|处理|查看|看(?:看|下|一下))
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# The busy mini-agent must be substantially stricter than the normal main
# Agent.  It is only a courtesy acknowledgement while another turn is active,
# not permission to interrupt ordinary group conversation.  Besides a
# service-verified @/QQ-reply, accept only language which both names AI/the
# bot and asks it to respond or act.  Merely discussing "AI" does not match.
_EXPLICIT_AI_REPLY_REQUEST_PATTERN = re.compile(
    r"""
    (?:
        (?:(?:请|麻烦|拜托|帮我(?:叫)?|让|叫|要求|需要|希望|能不能|可以|快)\s*)?
        (?:ai|人工智能|机器人|bot|小助手|助手)
        [\s,，:：]*
        (?:
            回答|回复|答复|回应|说话|出来|解释|分析|
            看(?:看|下|一下|一眼)|你怎么看|你觉得(?:呢|怎么样)?|
            帮(?:我)?(?:看|查|写|做|处理|解释|分析|回答|回复)|
            在吗|在不在|吗|呢|
            (?:能|可以|能不能|可不可以)\s*(?:回答|回复|帮我|帮忙|看|处理)?|
            (?:这|那)(?:条|个|题|件事)?(?:也)?\s*
                (?:回答|回复|解释|分析|处理|看(?:看|下|一下|一眼))
        )
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# These are deliberately conservative.  They protect the most important
# invariant even if an operator prompt asks the model to quote a summary:
# internal rolling summaries belong in SQLite/UI only and are never QQ text.
# Markers that identify the service's private state regardless of whether a
# member happens to ask for a group recap.  A user-facing recap may be useful,
# but the literal rolling-memory representation must never leave the service.
_INTERNAL_SUMMARY_HARD_MARKERS = (
    "内部摘要",
    "本轮摘要",
    "上一次群摘要",
    "最新摘要",
    "滚动摘要",
    "群聊内部状态摘要",
)

# These are normal phrases in a member-facing recap, so only treat them as an
# internal-summary signal when there is no current, human-authored request to
# summarize the group.  Exact/near copies of a stored rolling summary are
# still blocked even when such a request exists.
_INTERNAL_SUMMARY_SOFT_MARKERS = (
    "群聊摘要",
    "群内主要围绕",
    "以下是群聊总结",
    "群聊总结如下",
)

_INTERNAL_SUMMARY_NARRATIVE_PATTERN = re.compile(
    r"^(?:【[^】]{0,40}(?:摘要|总结)[^】]*】\s*)?(?:群内|本群|群聊)(?:此前|最近|主要|目前|围绕|这段时间)",
    re.IGNORECASE,
)
_INTERNAL_SUMMARY_TOPIC_LINK_PATTERN = re.compile(
    r"(?:；|。)(?:随后|期间|此外|与此同时|之后|接着)(?:群友|成员|大家|机器人|群内)",
)
_GROUP_SUMMARY_REQUEST_PATTERN = re.compile(
    r"""
    (?:
        (?:总结|概括|回顾|梳理|盘点|介绍|说说|评价).{0,24}(?:群聊|本群|这个群|群里|聊天记录|上面(?:的)?消息|刚才(?:的)?聊天)
        |
        (?:群聊|本群|这个群|群里|聊天记录|上面(?:的)?消息|刚才(?:的)?聊天).{0,24}(?:总结|概括|回顾|梳理|盘点|介绍|说说|评价)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


class _InternalSummaryOutboundError(ValueError):
    """A pre-action refusal to export private rolling-summary content."""

    def __init__(self) -> None:
        super().__init__(
            "该内容属于或高度接近本机滚动内部摘要，禁止通过 QQ 文本或 Markdown 图片发送。"
            "如果当前成员确实要求群聊总结，请基于当前消息重新写一份面向成员的回答，"
            "不要复用、改标题或渲染内部摘要原文。"
        )


def _markdown_render_failure_result(error: MarkdownRenderError) -> Dict[str, Any]:
    """Describe a local renderer failure without making it look like QQ I/O.

    The Markdown image is rendered and stored before the service sends either
    its progress notice or the image.  This result is therefore safe for the
    orchestration to repair/retry: no QQ state-changing call has been made.
    """

    diagnostic = error.diagnostic()
    detail = redact_error_detail(error, limit=1_200)
    if diagnostic["transient"] and diagnostic["local_retry_attempted"]:
        detail = "Edge 渲染上下文短暂失效，已使用全新本地浏览器配置自动重试 1 次：" + detail
    return {
        "ok": False,
        "error": detail,
        "renderer": "MarkFlow",
        "render_diagnostic": diagnostic,
        "retry_safe": True,
        "retry_safe_reason": "本地 Markdown/Edge 渲染失败，尚未向 QQ 发送进度提示或图片；可安全重试。",
        "qq_side_effect": False,
    }


def _events_explicitly_request_user_facing_group_summary(events: Sequence[Dict[str, Any]]) -> bool:
    """Whether a current human message asks for a member-facing group recap.

    The service keeps a private rolling summary, but members may of course ask
    "总结一下刚才群里聊了什么".  That request permits a newly written recap;
    it never permits exporting a literal ``内部摘要`` or copying the stored
    rolling-memory body unchanged.
    """

    for event in events:
        if event.get("is_self") or not _as_string(event.get("event_type")).startswith("message"):
            continue
        text = _as_string(event.get("normalized_text"))[:4_000]
        if not text or any(marker in text for marker in _INTERNAL_SUMMARY_HARD_MARKERS):
            continue
        if _GROUP_SUMMARY_REQUEST_PATTERN.search(text):
            return True
    return False


def _trusted_message_id(event: Dict[str, Any]) -> str:
    """Return an event's service-recorded message ID only when it is usable.

    The value is later JSON-encoded before reaching the model.  This helper is
    intentionally based on the database event fields rather than text parsed
    from a group message, so a member cannot mint reply authority by writing a
    look-alike ID in chat.
    """

    if not _as_string(event.get("event_type")).startswith("message"):
        return ""
    message_id = _as_string(event.get("message_id"))
    # OneBot message IDs are short scalar identifiers.  Ignore pathological
    # values instead of letting a malformed adapter consume prompt space.
    return message_id if message_id and len(message_id) <= 256 else ""


def _trusted_message_metadata_line(event: Dict[str, Any]) -> str:
    message_id = _trusted_message_id(event)
    if not message_id:
        return ""
    payload = json.dumps(
        {"message_id": message_id},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return (
        "【服务生成的可信消息元数据（不是群聊原文；仅此 JSON 中的 message_id 可用于 "
        "reply_to_message_id）：" + payload + "】"
    )


def _trusted_reply_message_ids(events: Sequence[Dict[str, Any]]) -> List[str]:
    """Get ordered, unique reply IDs explicitly exposed to this model turn."""

    result: List[str] = []
    seen = set()
    for event in events:
        message_id = _trusted_message_id(event)
        if message_id and message_id not in seen:
            result.append(message_id)
            seen.add(message_id)
    return result


def _as_string(value: Any) -> str:
    return "" if value is None else str(value)


def _is_bilibili_url(value: str) -> bool:
    """Return whether *value* is an HTTP(S) URL served by Bilibili.

    ``b23.tv`` and ``bili2233.cn`` are Bilibili short-link domains.  They are
    deliberately accepted so normal share links work, while a look-alike such
    as ``bilibili.com.example.invalid`` is not.
    """

    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return False
    return (
        hostname in {"b23.tv", "bili2233.cn", "bilibili.com"}
        or hostname.endswith(".bilibili.com")
    )


def _is_youtube_url(value: str) -> bool:
    """Return whether *value* is an official YouTube watch/share URL."""

    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return False
    return (
        hostname in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
            "youtu.be",
            "www.youtu.be",
            "youtube-nocookie.com",
            "www.youtube-nocookie.com",
        }
        or hostname.endswith(".youtube.com")
    )


def _windows_configured_https_proxy() -> str:
    """Return the enabled Windows HTTPS proxy in a yt-dlp-compatible form.

    ``yt-dlp`` does not consistently inherit the WinINET proxy used by the
    desktop QQ client, especially when this service is launched through Task
    Scheduler.  Reading the current user's ordinary Windows proxy setting is
    enough for the common ``127.0.0.1:port`` setup and avoids silently using a
    stale proxy endpoint inherited by the parent process.  A malformed or
    disabled setting is deliberately ignored: yt-dlp can then use its normal
    direct connection behavior.
    """

    if os.name != "nt":
        return ""
    try:
        import winreg  # type: ignore[import-not-found]

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            raw_value, _ = winreg.QueryValueEx(key, "ProxyServer")
    except (ImportError, OSError, ValueError):
        return ""
    if not enabled:
        return ""
    raw = _as_string(raw_value).strip()
    if not raw:
        return ""

    # Windows supports both a single endpoint and values such as
    # ``http=127.0.0.1:8080;https=127.0.0.1:8080``.
    entries: Dict[str, str] = {}
    fallback = ""
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            scheme, endpoint = part.split("=", 1)
            entries[scheme.strip().lower()] = endpoint.strip()
        elif not fallback:
            fallback = part
    endpoint = entries.get("https") or entries.get("http") or fallback
    if not endpoint:
        return ""
    if "://" not in endpoint:
        endpoint = "http://" + endpoint
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}:
        return ""
    if not parsed.hostname or not parsed.port:
        return ""
    return endpoint


def _find_local_executable(name: str) -> str:
    """Find a local executable even when the service inherited a stale PATH.

    A scheduled/hidden Uvicorn process often has a different PATH from the
    interactive PowerShell where ``ffmpeg -version`` succeeds.  Prefer an
    explicit ``<NAME>_PATH`` override, then PATH, then conventional Windows
    ffmpeg installation roots.
    """

    env_name = re.sub(r"[^A-Za-z0-9]", "_", name).upper() + "_PATH"
    override = _as_string(os.environ.get(env_name)).strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_dir():
            candidate = candidate / (name + ".exe")
        if candidate.is_file():
            return str(candidate)
    for executable_name in (name, name + ".exe"):
        found = shutil.which(executable_name)
        if found:
            return found
    if os.name == "nt":
        candidates: List[Path] = []
        for root in (Path("C:/"), Path("D:/"), Path.home()):
            try:
                candidates.extend(root.glob("ffmpeg*/bin/%s.exe" % name))
                candidates.extend(root.glob("ffmpeg*/%s.exe" % name))
            except OSError:
                continue
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
    return ""


def _yt_dlp_js_runtime_args() -> List[str]:
    """Enable a supported JavaScript runtime for current YouTube EJS."""

    node = _find_local_executable("node")
    if node:
        return ["--js-runtimes", "node:" + str(node)]
    return []


def _structured_reply_target_message_ids(raw: Dict[str, Any]) -> List[str]:
    """Return reply-target IDs from genuine OneBot array segments only.

    A normal text message can contain look-alike CQ code or a sentence such as
    ``[回复消息: 123]``.  Neither is authority to turn a normal group message
    into a bot-directed interaction.  Only NapCat/OneBot's structured
    ``reply`` segment is considered here; ownership is checked later against
    the local database, where the group is known.
    """

    message = raw.get("message")
    if not isinstance(message, list):
        return []
    target_ids: List[str] = []
    seen = set()
    for segment in message:
        if not isinstance(segment, dict) or _as_string(segment.get("type")) != "reply":
            continue
        data = segment.get("data")
        if not isinstance(data, dict):
            continue
        value = data.get("id") if data.get("id") is not None else data.get("message_id")
        target = _as_string(value).strip()
        if target and len(target) <= 256 and target not in seen:
            seen.add(target)
            target_ids.append(target)
    return target_ids


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _event_type(raw: Dict[str, Any]) -> Tuple[str, str]:
    post_type = _as_string(raw.get("post_type") or "unknown")
    parts = [post_type]
    for key in ("message_type", "notice_type", "request_type", "meta_event_type", "sub_type"):
        value = _as_string(raw.get(key))
        if value and value not in parts:
            parts.append(value)
    return ".".join(parts), _as_string(raw.get("sub_type"))


def _group_id(raw: Dict[str, Any]) -> str:
    value = raw.get("group_id")
    return _as_string(value)


def _is_private_conversation(conversation_id: Any) -> bool:
    return _as_string(conversation_id).startswith(PRIVATE_CONVERSATION_PREFIX)


def _private_user_id(conversation_id: Any) -> str:
    value = _as_string(conversation_id)
    return value[len(PRIVATE_CONVERSATION_PREFIX) :] if _is_private_conversation(value) else ""


def _conversation_id(raw: Dict[str, Any]) -> str:
    """Return a collision-free durable ID for a group or private session."""

    group_id = _group_id(raw)
    if group_id:
        return group_id
    message_type = _as_string(raw.get("message_type")).strip().lower()
    if message_type == "private" or (
        _as_string(raw.get("post_type")) == "message" and raw.get("user_id") not in (None, "")
    ):
        sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
        user_id = _as_string(raw.get("user_id") or sender.get("user_id"))
        if user_id:
            return PRIVATE_CONVERSATION_PREFIX + user_id
    return ""


def _conversation_type(conversation_id: Any) -> str:
    return "private" if _is_private_conversation(conversation_id) else "group"


def _conversation_display_name(raw: Dict[str, Any], conversation_id: str) -> str:
    if _is_private_conversation(conversation_id):
        sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
        name = _as_string(sender.get("card") or sender.get("nickname") or raw.get("user_id"))
        return "私聊 · " + (name or _private_user_id(conversation_id))
    return _as_string(raw.get("group_name"))


def _normalise_segments(raw: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
    message = raw.get("message")
    if message is None:
        message = raw.get("raw_message", "")
    if isinstance(message, str):
        return message, []
    if not isinstance(message, list):
        return "", []

    chunks: List[str] = []
    images: List[Dict[str, Any]] = []
    for segment in message:
        if not isinstance(segment, dict):
            chunks.append(_as_string(segment))
            continue
        segment_type = _as_string(segment.get("type") or "unknown")
        data = segment.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        if segment_type == "text":
            chunks.append(_as_string(data.get("text")))
        elif segment_type == "at":
            chunks.append("@" + _as_string(data.get("qq")))
        elif segment_type == "reply":
            chunks.append("[回复消息:" + _as_string(data.get("id")) + "]")
        elif segment_type in ("image", "mface"):
            summary = _as_string(data.get("summary"))
            chunks.append("[图片" + (":" + summary if summary else "") + "]")
            images.append(
                {
                    "url": _as_string(data.get("url")),
                    "path": _as_string(data.get("path")),
                    "file": _as_string(data.get("file")),
                    "file_id": _as_string(data.get("file_id")),
                    "summary": summary,
                    "file_size": _safe_int(data.get("file_size")),
                }
            )
        elif segment_type in ("record", "video", "file"):
            label = {"record": "语音", "video": "视频", "file": "文件"}[segment_type]
            name = _as_string(data.get("name") or data.get("file"))
            chunks.append("[" + label + (":" + name if name else "") + "]")
        elif segment_type in ("face", "dice", "rps", "poke"):
            chunks.append("[" + segment_type + "]")
        else:
            chunks.append("[" + segment_type + "]")
    return "".join(chunks), images


def _has_direct_mention_of_self(raw: Dict[str, Any], self_id: str) -> bool:
    """Return whether an array-format OneBot message directly @-mentions self.

    NapCat's Array message format represents mentions as ``{"type": "at",
    "data": {"qq": "..."}}``.  Do not infer a mention from plain text or
    CQ-code-looking text: only a structured segment is authoritative, and
    this makes an ordinary message unable to manufacture the control marker.
    """

    if not self_id:
        return False
    message = raw.get("message")
    if not isinstance(message, list):
        return False
    for segment in message:
        if not isinstance(segment, dict) or _as_string(segment.get("type")) != "at":
            continue
        data = segment.get("data")
        if not isinstance(data, dict):
            continue
        # ``qq`` is the OneBot v11 field.  A small number of compatible
        # adapters use ``user_id`` instead, so accept it without ever matching
        # the special ``all`` mention.
        target = data.get("qq") if data.get("qq") is not None else data.get("user_id")
        if _as_string(target).strip() == self_id:
            return True
    return False


def _event_has_live_direct_mention(event: Dict[str, Any]) -> bool:
    """Read the service-generated live-mention flag from a durable event."""

    content = event.get("content")
    return bool(isinstance(content, dict) and content.get("live_direct_mention") is True)


def _event_has_live_reply_to_bot(event: Dict[str, Any]) -> bool:
    """Read the service-verified reply-to-this-bot flag from a durable event."""

    content = event.get("content")
    return bool(isinstance(content, dict) and content.get("live_reply_to_bot") is True)


def _event_has_live_clear_group_call(event: Dict[str, Any]) -> bool:
    """Whether a live text message explicitly calls for someone to respond."""

    content = event.get("content")
    return bool(isinstance(content, dict) and content.get("live_clear_group_call") is True)


def _event_has_live_explicit_agent_task_request(event: Dict[str, Any]) -> bool:
    """Whether local parsing recognized a concrete current Agent task."""

    content = event.get("content")
    return bool(isinstance(content, dict) and content.get("live_explicit_agent_task_request") is True)


def _event_explicitly_requires_busy_agent_reply(event: Dict[str, Any]) -> bool:
    """Whether a new event warrants a busy-status acknowledgement.

    This deliberately does *not* reuse the ordinary group-call marker.  A
    generic ``有人吗`` or a reply addressed to another member should remain
    silent even while the primary Agent is working.  Only a service-verified
    direct @/QQ reply to this bot, or a plainly AI-addressed textual request,
    starts the tool-free mini-agent.
    """

    if _event_has_live_direct_mention(event) or _event_has_live_reply_to_bot(event):
        return True
    if event.get("is_self") or not _as_string(event.get("event_type")).startswith("message"):
        return False
    return bool(
        _EXPLICIT_AI_REPLY_REQUEST_PATTERN.search(
            _as_string(event.get("normalized_text"))[:1_000]
        )
    )


def _summary_comparison_text(value: str) -> str:
    """Normalize private-summary prose for a bounded, presentation-agnostic comparison."""

    text = _as_string(value).strip()
    # A rolling summary commonly starts with a service-only heading whereas a
    # provider may omit it when it accidentally tries to send the same body as
    # a reply.  Remove only that heading; do not broadly strip Markdown or
    # punctuation, which would make unrelated user answers look alike.
    text = re.sub(r"^【[^】]{0,48}(?:内部(?:状态)?摘要|群聊摘要|滚动摘要)[^】]*】\s*", "", text)
    return re.sub(r"\s+", "", text)


def _is_near_copy_of_rolling_summary(text: str, candidates: Sequence[str]) -> bool:
    """Return whether *text* is a substantive copy of a saved rolling summary.

    We do not use semantic/vector similarity here: it would falsely block a
    fresh answer to a member asking for a recap.  This deliberately checks
    only literal containment or extremely high character-level similarity to
    the actual private summaries stored for this one conversation.
    """

    source = _summary_comparison_text(text)
    if len(source) < 96:
        return False
    for candidate in candidates:
        target = _summary_comparison_text(_as_string(candidate))
        if len(target) < 96:
            continue
        if source == target:
            return True
        shorter, longer = (source, target) if len(source) <= len(target) else (target, source)
        if len(shorter) >= 120 and shorter in longer:
            return True
        # The old summary and its current replacement may differ by a few
        # words/new events.  A near verbatim resend still leaks the internal
        # rolling state, whereas a normal user-facing answer will not reach
        # this deliberately strict threshold.  Cap comparison work so an
        # unexpectedly large historical summary cannot make a worker slow.
        if min(len(source), len(target)) >= 180:
            left = source[:8_000]
            right = target[:8_000]
            if len(left) and len(right):
                ratio = difflib.SequenceMatcher(None, left, right, autojunk=False).quick_ratio()
                if ratio >= 0.965:
                    return True
    return False


def _looks_like_internal_summary(
    text: str,
    *,
    rolling_summary_candidates: Sequence[str] = (),
    allow_user_facing_group_summary: bool = False,
) -> bool:
    """Return whether outgoing prose is private rolling-summary material.

    This is an outbound-data boundary, not a ban on legitimate requests such
    as "总结一下这个群".  A fresh recap for such a request is allowed, but an
    exact/near copy of the stored rolling summary or an explicitly-labelled
    internal summary is always blocked.
    """

    normalized = _as_string(text).strip()
    if not normalized:
        return False
    if any(marker in normalized for marker in _INTERNAL_SUMMARY_HARD_MARKERS):
        return True
    if _is_near_copy_of_rolling_summary(normalized, rolling_summary_candidates):
        return True
    if allow_user_facing_group_summary:
        return False
    if any(marker in normalized for marker in _INTERNAL_SUMMARY_SOFT_MARKERS):
        return True
    # The leaked screenshot was not labelled "摘要".  It began with a
    # retrospective group-narrative lead and compressed multiple topic
    # transitions into one paragraph, which is characteristic of the private
    # rolling summary but not a normal reply to a current member.
    return bool(
        len(normalized) >= 180
        and _INTERNAL_SUMMARY_NARRATIVE_PATTERN.search(normalized)
        and _INTERNAL_SUMMARY_TOPIC_LINK_PATTERN.search(normalized)
    )


def normalise_onebot_event(
    raw: Dict[str, Any], *, history: bool = False, live: bool = True
) -> Dict[str, Any]:
    """Create a durable, display-safe event representation from OneBot payloads."""

    group_id = _conversation_id(raw)
    conversation_type = _conversation_type(group_id)
    event_type, sub_type = _event_type(raw)
    text, images = _normalise_segments(raw)
    file_info = _file_info(raw)
    video_info = _video_info(raw)
    if _as_string(raw.get("post_type")) == "notice" and _as_string(raw.get("notice_type")) == "group_upload":
        if file_info:
            file_name = _as_string(file_info.get("name") or file_info.get("id") or "未命名文件")
            file_size = _safe_int(file_info.get("size"))
            text = "[群文件上传] " + file_name + (" (%s bytes)" % file_size if file_size else "")
    sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
    sender_id = _as_string(raw.get("user_id") or raw.get("operator_id") or raw.get("target_id"))
    sender_name = _as_string(sender.get("card") or sender.get("nickname") or sender_id)
    self_id = _as_string(raw.get("self_id"))
    is_self = bool(
        event_type.startswith("message_sent")
        or (self_id and sender_id and sender_id == self_id)
        or (self_id and _as_string(raw.get("operator_id")) == self_id)
    )
    # A fetched history item can contain an @-mention, but it must never turn
    # into a delayed automatic reply.  Reconnect backfill passes ``live=False``
    # for the same reason while retaining the item as pending summary data.
    live_direct_mention = bool(
        live and not history and not is_self and _has_direct_mention_of_self(raw, self_id)
    )
    live_clear_group_call = bool(
        live
        and not history
        and not is_self
        and len(text) <= 200
        and _CLEAR_GROUP_CALL_PATTERN.search(text)
    )
    live_explicit_agent_task_request = bool(
        live
        and not history
        and not is_self
        and len(text) <= 2_000
        and _EXPLICIT_AGENT_TASK_REQUEST_PATTERN.search(text)
    )
    message_id = _as_string(raw.get("message_id"))
    if not text:
        fields = []
        for key in ("notice_type", "request_type", "sub_type", "user_id", "operator_id", "target_id", "file", "duration"):
            if raw.get(key) not in (None, ""):
                fields.append(key + "=" + _as_string(raw.get(key)))
        text = "[" + ("私聊动态 " if conversation_type == "private" else "群动态 ") + event_type + (": " + ", ".join(fields) if fields else "") + "]"
    stable = message_id or _as_string(raw.get("flag"))
    if not stable:
        canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        stable = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    # NapCat history and live reports do not always include the same `sub_type`.
    # A QQ message ID is already unique inside a group, so it must deduplicate
    # independently of those presentation-only event-type differences.
    dedupe_type = "message" if _as_string(raw.get("post_type")) == "message" and message_id else event_type
    return {
        "dedupe_key": "%s:%s:%s" % (group_id, dedupe_type, stable),
        "group_id": group_id,
        "event_type": event_type,
        "sub_type": sub_type,
        "message_id": message_id,
        "occurred_at": _safe_int(raw.get("time")),
        "sender_id": sender_id,
        "sender_name": sender_name,
        "self_id": self_id,
        "normalized_text": text,
        "content": {
            "images": images,
            "file": file_info,
            "files": [file_info] if file_info else [],
            "video": video_info,
            "conversation_type": conversation_type,
            "history": history,
            "live_direct_mention": live_direct_mention,
            "live_clear_group_call": live_clear_group_call,
            "live_explicit_agent_task_request": live_explicit_agent_task_request,
            # This is only a structured *candidate*.  AgentService verifies
            # it is one of this application's own messages before setting the
            # live_reply_to_bot marker below.
            "reply_target_message_ids": _structured_reply_target_message_ids(raw),
            "live_reply_to_bot": False,
        },
        "raw": raw,
        "is_self": is_self,
        # Initial history intentionally includes the account's prior messages; live own messages do not trigger.
        "pending": True if history else not is_self,
    }


class AgentService:
    def __init__(self, data_dir: Path, *, secret_store: Optional[SecretStore] = None) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.data_dir / "agent.sqlite3")
        # Only the local administrator conversation can reach this fixed
        # document.  The path is never supplied by a model or group message.
        self.rules = RulesStore(self.data_dir)
        # Keep this as a simple instance attribute (rather than a read-only
        # property) so local integrations/tests can substitute a display-only
        # label without changing the fixed underlying RulesStore path.
        self.rules_path_label = str(self.rules.path)
        # The LLM API key is intentionally persisted in this local JSON file;
        # it is still never exposed through the web UI or dashboard state.
        # Do not let an injected test/custom data directory migrate and delete
        # credentials belonging to the normal project installation.
        canonical_data_dir = Path(__file__).resolve().parent.parent / "data"
        try:
            migrate_legacy_api_key = self.data_dir.resolve() == canonical_data_dir.resolve()
        except OSError:
            migrate_legacy_api_key = False
        self.secret_store = secret_store or SecretStore(
            self.data_dir / "api-key.json",
            migrate_legacy_api_key=migrate_legacy_api_key,
        )
        saved_settings = self.db.get_json_setting("app_settings", {})
        self.settings = AppSettings.from_mapping(saved_settings)
        # Persist only the exact obsolete shipped default prompt.  Custom
        # operator prompts are intentionally never rewritten merely because
        # they mention Markdown, while an untouched old default should not
        # keep telling the model that image rendering is mandatory.
        saved_llm = saved_settings.get("llm") if isinstance(saved_settings, dict) else None
        saved_global_prompt = saved_llm.get("global_prompt") if isinstance(saved_llm, dict) else None
        if (
            isinstance(saved_global_prompt, str)
            and migrate_legacy_global_prompt(saved_global_prompt) != saved_global_prompt
        ):
            self.db.set_json_setting("app_settings", self.settings.as_mapping())
        # A failed WAL setup is intentionally non-fatal when SQLite could
        # safely fall back to a single-file journal.  Surface the exact local
        # condition in the dashboard instead of hiding it behind a startup
        # crash or later database mystery.
        self.runtime_warning = _as_string(getattr(self.db, "journal_mode_warning", ""))
        # Current installations keep the reverse-WS token in Windows
        # Credential Manager.  Migrate the short-lived legacy SQLite setting
        # on first startup without ever rendering it in the UI.
        get_onebot_token = getattr(self.secret_store, "get_onebot_token", None)
        set_onebot_token = getattr(self.secret_store, "set_onebot_token", None)
        stored_token = get_onebot_token() if callable(get_onebot_token) else None
        if stored_token:
            self.settings.onebot_token = _as_string(stored_token)
        elif self.settings.onebot_token and callable(set_onebot_token):
            legacy_token = self.settings.onebot_token
            try:
                set_onebot_token(legacy_token)
                self.db.set_json_setting("app_settings", self.settings.as_mapping())
            except Exception as exc:
                # Keep it in memory for this run rather than silently breaking
                # the adapter, but clearly tell the local operator that it
                # still needs a secure store migration.
                self.runtime_warning = "无法迁移旧 OneBot Token 到 Windows 凭据存储：" + str(exc)
        self.media = MediaStore(self.data_dir / "media", budget_bytes=self.settings.media_budget_gib * 1024 ** 3)
        # Each group/private conversation gets an isolated agent workspace.
        # Keep the requested Windows default, but allow tests and deployments
        # to override it without exposing the path to the model as authority.
        workspace_root = os.environ.get("QQ_AI_WORKSPACE_ROOT", str(DEFAULT_WORKSPACE_ROOT))
        self.workspace = WorkspaceManager(workspace_root)
        self.adapter: Optional[OneBotAdapter] = None
        self._workers: Dict[str, asyncio.Task[None]] = {}
        # Ephemeral per-worker state for the deliberately tiny busy-reply
        # sub-agent.  It is never persisted as group memory or sent wholesale
        # to QQ; only a bounded safe snapshot reaches the auxiliary model.
        self._worker_activity: Dict[str, Dict[str, Any]] = {}
        # One request may ask the Agent to search/fix several times.  Progress
        # notices are useful once, but repeating the same notice for every
        # recovery round makes a stuck relay look like a bot flood.
        self._activity_notice_seen: Set[Tuple[int, str, str]] = set()
        self._busy_reply_event_ids: set[int] = set()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._group_discovery_task: Optional[asyncio.Task[Any]] = None
        self._history_backfill_groups: set[str] = set()
        # A provider can return a refusal/error as ordinary text.  Allow one
        # automatic summary-only recomputation, then stop until the operator
        # retries so a persistent refusal cannot create an infinite API loop.
        self._summary_recovery_attempts: Dict[str, int] = {}
        # FastAPI builds the application before its lifespan event loop starts.
        # Construct asyncio primitives lazily so importing/creating the app is
        # also safe from a synchronous setup or test thread on Python 3.9.
        self._worker_lock: Optional[asyncio.Lock] = None
        self._group_sync_lock: Optional[asyncio.Lock] = None
        self._admin_chat_lock: Optional[asyncio.Lock] = None
        self._stopping = False
        self._rebuild_adapter()

    def _rebuild_adapter(self) -> None:
        if self.settings.onebot_token:
            self.adapter = OneBotAdapter(self.settings.onebot_token, on_event=self.handle_onebot_event)
        else:
            self.adapter = None

    async def start(self) -> None:
        self._stopping = False
        self._async_locks()
        stale_turns = self.db.cancel_running_turns("service restarted before the turn finished")
        if stale_turns:
            logger.warning("cancelled %s stale running turn(s) from a previous service process", stale_turns)
        # Repair a corrupt legacy summary even when no new QQ event arrives;
        # the resulting archive-only worker recomputes the bounded interval.
        for group in self.db.list_groups():
            if not group.get("enabled"):
                continue
            group_id = _as_string(group.get("group_id"))
            if not group_id:
                continue
            checkpoint_repaired = self._ensure_summary_checkpoint(group_id)
            # A clean restart must not strand pending live events or memory
            # events merely because no new OneBot packet arrives afterwards.
            # Startup workers never replay old QQ actions: the durable
            # operation journal and pending/archived cursors remain the guard.
            has_pending = bool(
                self.db.pending_events(group_id)
                or self.db.memory_pending_events(group_id, limit=1)
            )
            if checkpoint_repaired or has_pending:
                await self._schedule_worker(group_id)
        if self._group_discovery_task is None or self._group_discovery_task.done():
            self._group_discovery_task = self._track_background_task(self._group_discovery_loop())
        if self.adapter and self.adapter.connected:
            await self.sync_groups()

    def _async_locks(self) -> Tuple[asyncio.Lock, asyncio.Lock]:
        if self._worker_lock is None:
            self._worker_lock = asyncio.Lock()
        if self._group_sync_lock is None:
            self._group_sync_lock = asyncio.Lock()
        return self._worker_lock, self._group_sync_lock

    def _admin_lock(self) -> asyncio.Lock:
        """Create the single local-admin conversation lock lazily.

        The FastAPI factory is also used synchronously by tests and setup
        scripts, so this follows the same lazy-loop rule as the group locks.
        Serializing requests preserves user/assistant history order when the
        dashboard receives an accidental double click or two local tabs post
        at the same time.
        """

        if self._admin_chat_lock is None:
            self._admin_chat_lock = asyncio.Lock()
        return self._admin_chat_lock

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self._workers.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._worker_activity.clear()
        self._busy_reply_event_ids.clear()
        background_tasks = list(self._background_tasks)
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        self._group_discovery_task = None
        if self.adapter:
            disconnect = getattr(self.adapter, "disconnect", None)
            if callable(disconnect):
                await disconnect(reason="application shutdown")
        self.db.close()

    @property
    def onebot_connected(self) -> bool:
        return bool(self.adapter and self.adapter.connected)

    async def attach_onebot(self, websocket: Any) -> None:
        if not self.adapter:
            try:
                await websocket.close(code=1011, reason="configure OneBot token in local UI first")
            finally:
                return
        # `attach` serves the socket until disconnect.  Start a small companion
        # task so discovery also happens immediately after a fresh connection,
        # rather than depending solely on a lifecycle event variant.
        adapter = self.adapter
        previous_connection_id = adapter.connection_id
        self._track_background_task(self._sync_after_connection(adapter, previous_connection_id))
        await adapter.attach(websocket)

    def _track_background_task(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _group_discovery_loop(self) -> None:
        """Refresh known groups without ever auto-enabling collection."""

        while not self._stopping:
            if self.adapter and self.adapter.connected:
                await self.sync_groups()
            await asyncio.sleep(30)

    async def _sync_after_connection(self, adapter: OneBotAdapter, previous_connection_id: int) -> None:
        for _ in range(20):
            if self._stopping or adapter is not self.adapter:
                return
            # ``connected`` alone can still refer to the old socket while a
            # reconnect is waiting to be accepted.  Only reconcile after this
            # particular attach created a newer adapter connection.
            if adapter.connected and adapter.connection_id > previous_connection_id:
                await self.sync_groups()
                await self._recover_enabled_groups_after_reconnect(adapter)
                return
            await asyncio.sleep(0.05)

    def public_settings(self) -> Dict[str, Any]:
        llm = self.settings.llm
        return {
            "llm_base_url": llm.base_url,
            "llm_endpoint_mode": llm.endpoint_mode,
            "llm_model": llm.model,
            "image_model": getattr(llm, "image_model", "gemini-3.1-flash-image"),
            "send_reasoning_effort": llm.send_reasoning_effort,
            "global_reasoning_effort": llm.global_reasoning_effort,
            "vision_enabled": llm.vision_enabled,
            "timeout_seconds": llm.timeout_seconds,
            "global_prompt": llm.global_prompt,
            "media_budget_gib": self.settings.media_budget_gib,
            "api_key_configured": bool(self.secret_store.get_llm_api_key()),
            "onebot_token_configured": bool(self.settings.onebot_token),
        }

    def rules_text(self) -> str:
        """Read durable administrator rules for display and future turns."""

        return self.rules.read()

    def list_admin_messages(self, limit: int = 40) -> List[Dict[str, Any]]:
        """Return local operator conversation history; QQ data never enters it."""

        return self.db.list_recent_admin_messages(limit)

    def list_group_memories(
        self,
        group_id: str,
        *,
        limit: int = 100,
        include_inactive: bool = True,
    ) -> List[Dict[str, Any]]:
        """Return evidence-backed memories from exactly one local group."""

        if not self.db.get_group(group_id):
            raise ValueError("未知群聊：" + str(group_id))
        return self.db.list_group_memories(
            str(group_id),
            active_only=not bool(include_inactive),
            limit=max(1, min(int(limit), 1_000)),
            include_evidence=True,
        )

    async def moderate_group_memory(
        self,
        group_id: str,
        memory_id: Any,
        action: str,
        replacement_text: str = "",
        note: str = "",
    ) -> Dict[str, Any]:
        """Apply a human confirmation, correction, or soft retraction."""

        target_group = str(group_id)
        if not self.db.get_group(target_group):
            raise ValueError("未知群聊：" + target_group)
        try:
            target_id = int(memory_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("记忆 ID 必须是整数") from exc
        memory = self.db.get_group_memory(target_group, target_id, include_evidence=True)
        if memory is None:
            raise ValueError("当前群中不存在该记忆")
        normalized_action = _as_string(action).strip().lower()
        moderation_note = _as_string(note).strip()[:4_000]
        if normalized_action == "confirm":
            return self.db.confirm_group_memory(
                target_group,
                target_id,
                reason=moderation_note or "本机管理员人工确认",
            )
        if normalized_action == "correct":
            replacement = _as_string(replacement_text).strip()
            if not replacement:
                raise ValueError("更正内容不能为空")
            if len(replacement) > 8_000:
                raise ValueError("更正内容最多允许 8,000 个字符")
            evidence = list(memory.get("evidence") or [])
            if not evidence:
                raise ValueError("该记忆没有可保留的来源证据，不能直接更正")
            metadata = dict(memory.get("metadata") or {})
            metadata.update(
                {
                    "human_override": True,
                    "human_override_note": moderation_note or "本机管理员人工更正",
                }
            )
            return self.db.correct_group_memory(
                target_group,
                target_id,
                statement=replacement,
                object_value=replacement,
                evidence=evidence,
                confidence_status="confirmed",
                metadata=metadata,
            )
        if normalized_action in {"retract", "delete"}:
            return self.db.retract_group_memory(
                target_group,
                target_id,
                moderation_note
                or (
                    "本机管理员请求软删除"
                    if normalized_action == "delete"
                    else "本机管理员人工撤回"
                ),
            )
        raise ValueError("不支持的记忆操作：" + normalized_action)

    async def reset_group_memory(self, group_id: str) -> Dict[str, Any]:
        """Clear one group's derived memory/rules and recompute its history.

        The original event stream and all QQ/tool/media audits remain intact.
        Rewound events are archive-only so a reset cannot resend old messages
        or repeat another state-changing tool call.
        """

        target_group = str(group_id)
        group = self.db.get_group(target_group)
        if not group:
            raise ValueError("未知群聊：" + target_group)
        if target_group in self._history_backfill_groups:
            raise RuntimeError("该会话正在读取 QQ 历史消息，请稍后再执行重置")

        worker_lock, _ = self._async_locks()
        async with worker_lock:
            worker = self._workers.get(target_group)
        if worker and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

        counts = self.db.reset_group_memory_and_recompute(target_group)
        enabled = bool((self.db.get_group(target_group) or {}).get("enabled"))
        if enabled:
            # The durable event stream is already present. Mark the group ready
            # so the reset can immediately start archive-only recomputation and
            # new live events do not wait for another bootstrap request.
            self.db.set_group_initialized(target_group, True)
            await self._schedule_worker(target_group)
        return {
            **counts,
            "group_id": target_group,
            "scheduled": enabled,
            "global_rules_preserved": True,
        }

    async def admin_chat(self, message: str) -> Dict[str, Any]:
        """Run one local administrator-to-model conversation turn.

        This deliberately has no access to the OneBot adapter.  The only
        state-changing callback exposed to the model is a fixed-path
        ``write_rules_md`` operation, whose result is separately persisted in
        the local admin history.  Group turns receive the resulting rules on
        their next request, after the normal editable prompts and before the
        immutable service boundary.
        """

        text = _as_string(message).strip()
        if not text:
            raise ValueError("管理员对话内容不能为空")
        if len(text) > MAX_ADMIN_CHAT_MESSAGE_CHARS:
            raise ValueError(
                "管理员对话单次内容最多允许 %s 个字符" % MAX_ADMIN_CHAT_MESSAGE_CHARS
            )

        async with self._admin_lock():
            self.db.append_admin_message("user", text)
            history_rows = self.db.list_recent_admin_messages(MAX_ADMIN_HISTORY_FOR_MODEL)
            history = [
                {"role": _as_string(row.get("role")), "content": _as_string(row.get("content"))}
                for row in history_rows
                if _as_string(row.get("role")) in {"user", "assistant"}
                and _as_string(row.get("content"))
            ]
            try:
                current_rules = self.rules_text()
            except Exception as exc:
                detail = redact_error_detail(exc, limit=800)
                error_text = "无法读取 rules.md，因此本次管理员对话未发送：" + detail
                self.db.append_admin_message("assistant", error_text)
                self.runtime_warning = error_text
                return {"ok": False, "error": error_text}

            async def write_rules(
                tool_name: str,
                arguments: Dict[str, Any],
                _call_id: str,
                _operation_slot: int = 0,
            ) -> Dict[str, Any]:
                """Service-side validation for the sole admin memory tool."""

                if tool_name != "write_rules_md":
                    result = {
                        "ok": False,
                        "retry_safe": True,
                        "error": "管理员对话只允许 write_rules_md；未执行。",
                    }
                    self.db.append_admin_message("tool", "未执行不允许的管理员工具", tool_name, result)
                    return result
                if not isinstance(arguments, dict):
                    result = {
                        "ok": False,
                        "retry_safe": True,
                        "error": "write_rules_md 参数必须是对象；未执行。",
                    }
                    self.db.append_admin_message("tool", "rules.md 写入参数无效", tool_name, result)
                    return result
                unknown = set(arguments).difference({"content", "reason"})
                content = arguments.get("content")
                if unknown or not isinstance(content, str):
                    detail = (
                        "write_rules_md 包含不允许的参数：" + ", ".join(sorted(map(str, unknown)))
                        if unknown
                        else "write_rules_md 的 content 必须是字符串；未执行。"
                    )
                    result = {"ok": False, "retry_safe": True, "error": detail}
                    self.db.append_admin_message("tool", "rules.md 写入参数无效", tool_name, result)
                    return result
                reason = arguments.get("reason")
                if reason is not None and not isinstance(reason, str):
                    result = {
                        "ok": False,
                        "retry_safe": True,
                        "error": "write_rules_md 的 reason 必须是字符串；未执行。",
                    }
                    self.db.append_admin_message("tool", "rules.md 写入参数无效", tool_name, result)
                    return result

                try:
                    self.rules.write(content)
                    result = {
                        "ok": True,
                        "path": self.rules_path_label,
                        "characters": len(content),
                    }
                    if isinstance(reason, str) and reason.strip():
                        result["reason"] = reason.strip()[:500]
                    audit_text = "已更新 rules.md" if content else "已清空 rules.md"
                except Exception as exc:
                    result = {
                        "ok": False,
                        "error": "写入 rules.md 失败：" + redact_error_detail(exc, limit=800),
                    }
                    audit_text = "rules.md 写入失败"
                self.db.append_admin_message("tool", audit_text, tool_name, result)
                return result

            effort = self.settings.llm.global_reasoning_effort
            if effort not in VALID_REASONING_EFFORTS - {"inherit"}:
                effort = "off"
            client = AdminConversationClient(self.settings.llm, self.secret_store.get_llm_api_key())
            try:
                result = await client.run_admin_turn(
                    history=history,
                    rules_text=current_rules,
                    reasoning_effort=effort,
                    tool_executor=write_rules,
                )
            except Exception as exc:
                detail = redact_error_detail(exc, api_key=_as_string(self.secret_store.get_llm_api_key()), limit=1800)
                error_text = "管理员对话模型调用失败：\n" + (detail or "未提供详情")
                self.db.append_admin_message("assistant", error_text)
                self.runtime_warning = error_text
                return {"ok": False, "error": error_text}

            assistant_text = _as_string(result.assistant_text).strip()
            if not assistant_text:
                assistant_text = "模型未返回可显示的管理员回复。"
            self.db.append_admin_message("assistant", assistant_text)
            if result.warning:
                self.runtime_warning = "管理员对话告警：" + _as_string(result.warning)
            return {
                "ok": True,
                "assistant_text": assistant_text,
                "warning": result.warning,
                "tool_results": result.tool_results,
            }

    async def update_settings(self, values: Dict[str, Any]) -> None:
        llm = self.settings.llm
        llm.base_url = _as_string(values.get("llm_base_url", llm.base_url)).strip()
        endpoint_mode = _as_string(values.get("llm_endpoint_mode", llm.endpoint_mode)).strip().lower()
        llm.endpoint_mode = endpoint_mode if endpoint_mode in VALID_LLM_ENDPOINT_MODES else "completions"
        llm.model = _as_string(values.get("llm_model", llm.model)).strip()
        llm.image_model = _as_string(
            values.get("image_model", getattr(llm, "image_model", "gemini-3.1-flash-image"))
        ).strip() or "gemini-3.1-flash-image"
        llm.send_reasoning_effort = bool(values.get("send_reasoning_effort", False))
        effort = _as_string(values.get("global_reasoning_effort", llm.global_reasoning_effort))
        llm.global_reasoning_effort = effort if effort in VALID_REASONING_EFFORTS - {"inherit"} else "off"
        llm.vision_enabled = bool(values.get("vision_enabled", False))
        llm.global_prompt = _as_string(values.get("global_prompt", llm.global_prompt)).strip() or llm.global_prompt
        try:
            llm.timeout_seconds = max(5, min(600, int(values.get("timeout_seconds", llm.timeout_seconds))))
        except (TypeError, ValueError):
            pass
        try:
            self.settings.media_budget_gib = max(1, min(1024, int(values.get("media_budget_gib", self.settings.media_budget_gib))))
        except (TypeError, ValueError):
            pass
        self.media.set_budget_bytes(self.settings.media_budget_gib * 1024 ** 3)

        token_input = _as_string(values.get("onebot_token", "")).strip()
        api_key = _as_string(values.get("llm_api_key", "")).strip()
        old_adapter = self.adapter
        if api_key:
            self.secret_store.set_llm_api_key(api_key)
        if token_input:
            set_onebot_token = getattr(self.secret_store, "set_onebot_token", None)
            if not callable(set_onebot_token):
                raise RuntimeError("当前凭据存储不支持保存 OneBot Token")
            set_onebot_token(token_input)
            self.settings.onebot_token = token_input
        self.db.set_json_setting("app_settings", self.settings.as_mapping())
        if token_input and (old_adapter is None or token_input != getattr(old_adapter, "_token", None)):
            self._rebuild_adapter()
            if old_adapter:
                await old_adapter.disconnect(reason="OneBot token changed")
        self.runtime_warning = ""

    async def sync_groups(self) -> None:
        if not self.adapter or not self.adapter.connected:
            return
        _, group_sync_lock = self._async_locks()
        async with group_sync_lock:
            try:
                response = await self.adapter.call("get_group_list", {})
                data = response.get("data") or []
                if isinstance(data, dict):
                    data = data.get("groups") or data.get("group_list") or []
                if not isinstance(data, list):
                    return
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    group_id = _as_string(item.get("group_id"))
                    if group_id:
                        self.db.upsert_group(group_id, _as_string(item.get("group_name") or item.get("name")))
            except Exception as exc:
                self.runtime_warning = "无法同步群列表：" + str(exc)

    async def enable_group(self, group_id: str, prompt_override: str, reasoning_effort: str) -> None:
        effort = reasoning_effort if reasoning_effort in VALID_REASONING_EFFORTS else "inherit"
        previous = self.db.get_group(group_id)
        self.db.set_group_config(group_id, True, prompt_override, effort)
        # Saving prompt/reasoning changes for an already initialized enabled
        # group should not interrupt its normal queue.  A newly enabled group
        # (including one re-enabled after a pause) must establish its history
        # before live events are allowed to start a worker.
        if previous and previous.get("enabled") and previous.get("initialized"):
            await self._schedule_worker(group_id)
            return
        self.db.set_group_initialized(group_id, False)
        await self._initialize_group(group_id)

    async def disable_group(self, group_id: str, prompt_override: str, reasoning_effort: str) -> None:
        effort = reasoning_effort if reasoning_effort in VALID_REASONING_EFFORTS else "inherit"
        self.db.set_group_config(group_id, False, prompt_override, effort)
        # Do not keep an already-started worker inside an image/LLM upload once
        # the local user has disabled the group.  Its events remain pending and
        # can only resume after a later explicit enable/retry.
        worker_lock, _ = self._async_locks()
        async with worker_lock:
            worker = self._workers.get(group_id)
        if worker and not worker.done():
            worker.cancel()
            # Waiting for cancellation prevents a quick disable/re-enable from
            # seeing the old task as active and then losing its replacement.
            await asyncio.gather(worker, return_exceptions=True)

    async def retry_group(self, group_id: str) -> None:
        group = self.db.get_group(group_id)
        if group and group.get("enabled"):
            # Failed events remain pending.  Never reset previously summarized
            # history here: doing so could duplicate a state-changing tool call.
            self.db.set_group_error(group_id, "")
            if not group.get("initialized"):
                await self._initialize_group(group_id)
            else:
                await self._schedule_worker(group_id)

    async def _get_group_msg_history(
        self,
        adapter: OneBotAdapter,
        group_id: str,
    ) -> Dict[str, Any]:
        """Fetch recent history with a bounded, history-only timeout.

        History is a reconnect/initialization convenience, not a prerequisite
        for receiving live events.  NapCat builds and test adapters vary in
        whether ``call`` accepts the optional timeout keyword, so retain a
        small compatibility fallback for older adapters.
        """

        params = {"group_id": _safe_int(group_id) or group_id, "count": MAX_HISTORY_EVENTS}
        try:
            return await adapter.call(
                "get_group_msg_history",
                params,
                timeout=HISTORY_ACTION_TIMEOUT_SECONDS,
            )
        except TypeError as exc:
            if "timeout" not in str(exc).lower():
                raise
            return await adapter.call("get_group_msg_history", params)

    async def _initialize_group(self, group_id: str) -> None:
        self._history_backfill_groups.add(group_id)
        try:
            current = self.db.get_group(group_id) or {}
            if _is_private_conversation(group_id) or current.get("conversation_type") == "private":
                # OneBot v11 does not expose a portable private-history action
                # across NapCat versions.  Private sessions start collecting
                # immediately; any live messages already queued remain durable.
                self.db.set_group_initialized(group_id, True)
                self.db.set_group_error(group_id, "")
                await self._schedule_worker(group_id)
                return
            if not self.adapter or not self.adapter.connected:
                self.db.set_group_error(group_id, "OneBot 未连接；连接后请点击“重新尝试处理积压”")
                return
            response = await self._get_group_msg_history(self.adapter, group_id)
            messages = self._history_messages(response)
            selected = self._limit_history(messages, group_id)
            for raw in selected:
                event = normalise_onebot_event(raw, history=True)
                # The initial bootstrap is already available verbatim in the
                # newest raw-context window.  Do not treat old history as a
                # new live request; it will enter the rolling summary only
                # after it naturally falls outside that window.
                event["pending"] = False
                self.db.insert_event(event)
            self.db.set_group_initialized(group_id, True)
            self.db.set_group_error(group_id, "")
            await self._schedule_worker(group_id)
        except OneBotActionTimeoutError as exc:
            self.db.set_group_error(
                group_id,
                "初始历史读取超时（实时消息仍可继续）：" + str(exc),
            )
        except Exception as exc:
            self.db.set_group_error(group_id, "初始历史读取失败：" + str(exc))
        finally:
            self._history_backfill_groups.discard(group_id)

    async def _recover_enabled_groups_after_reconnect(self, adapter: OneBotAdapter) -> None:
        """Deduplicate NapCat's recent-history backfill after a reconnect.

        Live OneBot delivery can miss events while the reverse socket is down.
        Existing event keys make the most recent 200 messages safe to insert
        again; only events not seen before reach a worker.  Disabled groups are
        deliberately excluded so a reconnect never starts collection for them.
        """

        possible_gap_groups: List[str] = []
        for group in self.db.list_groups():
            if self._stopping or adapter is not self.adapter or not adapter.connected:
                return
            if not group.get("enabled"):
                continue
            group_id = _as_string(group.get("group_id"))
            if not group_id:
                continue
            if _is_private_conversation(group_id) or group.get("conversation_type") == "private":
                await self._schedule_worker(group_id)
                continue
            if not group.get("initialized"):
                await self._initialize_group(group_id)
                continue
            self._history_backfill_groups.add(group_id)
            try:
                response = await self._get_group_msg_history(adapter, group_id)
                messages = self._history_messages(response)
                for raw in self._limit_history(messages, group_id):
                    # Unlike the first summary, a reconnect should not trigger
                    # a fresh model turn merely for an already-sent bot message.
                    # These records were fetched after a reconnect rather
                    # than received live over this WebSocket.  Do not turn
                    # them into a new agent decision: an old @-mention must
                    # never trigger a late automatic group reply.  They still
                    # remain eligible for later rolling-summary archival.
                    event = normalise_onebot_event(raw, history=False, live=False)
                    event["pending"] = False
                    if event.get("message_id") and self.db.get_sent_message(event["message_id"], group_id):
                        # Some NapCat history variants omit ``self_id``.  The
                        # durable sent-message record is then the reliable way
                        # to preserve the no-self-message-loop guarantee.
                        event["is_self"] = True
                        event["pending"] = False
                    event_id = self.db.insert_event(event)
                    if event_id is not None and event.get("is_self"):
                        event["id"] = event_id
                        self._track_background_task(self._persist_event_images(event))
                if len(messages) >= MAX_HISTORY_EVENTS:
                    possible_gap_groups.append(group_id)
            except OneBotActionTimeoutError as exc:
                self.db.set_group_error(
                    group_id,
                    "断线历史补偿超时（实时消息仍可继续）：" + str(exc),
                )
            except Exception as exc:
                self.db.set_group_error(group_id, "断线历史补偿失败：" + str(exc))
            finally:
                self._history_backfill_groups.discard(group_id)
            # Existing pending events remain useful even if the most recent
            # history request failed, and it is now safe to process them.
            await self._schedule_worker(group_id)

        if possible_gap_groups:
            warning = "已用最近 200 条消息补偿断线；更早遗漏可能无法自动补齐（群：%s）" % ", ".join(
                possible_gap_groups
            )
            self.runtime_warning = warning

    @staticmethod
    def _history_messages(response: Dict[str, Any]) -> List[Dict[str, Any]]:
        data = response.get("data") or []
        if isinstance(data, dict):
            data = data.get("messages") or data.get("msg_list") or data.get("history") or []
        if not isinstance(data, list):
            return []
        messages = [dict(item) for item in data if isinstance(item, dict)]
        # NapCat variants differ in ordering. Time and message ID give a stable oldest-to-newest order.
        messages.sort(key=lambda item: (_safe_int(item.get("time")), _as_string(item.get("message_id"))))
        return messages[-MAX_HISTORY_EVENTS:]

    @staticmethod
    def _limit_history(messages: Sequence[Dict[str, Any]], group_id: str) -> List[Dict[str, Any]]:
        selected: List[Dict[str, Any]] = []
        used = 0
        for item in reversed(messages[-MAX_HISTORY_EVENTS:]):
            candidate = dict(item)
            candidate.setdefault("post_type", "message")
            candidate.setdefault("message_type", "group")
            candidate.setdefault("group_id", group_id)
            size = len(normalise_onebot_event(candidate, history=True)["normalized_text"])
            if selected and used + size > MAX_EVENT_TEXT_CHARS:
                break
            selected.append(candidate)
            used += min(size, MAX_EVENT_TEXT_CHARS)
            if used >= MAX_EVENT_TEXT_CHARS:
                break
        selected.reverse()
        return selected

    async def handle_onebot_event(self, raw: Dict[str, Any]) -> None:
        if self._stopping or not isinstance(raw, dict):
            return
        if _as_string(raw.get("post_type")) == "meta_event":
            self._track_background_task(self.sync_groups())
            return
        group_id = _conversation_id(raw)
        if not group_id:
            return
        self.db.upsert_group(
            group_id,
            _conversation_display_name(raw, group_id),
            _conversation_type(group_id),
        )
        group = self.db.get_group(group_id)
        if not group or not group.get("enabled"):
            return  # Disabled groups are intentionally not persisted or sent to the model.
        event = normalise_onebot_event(raw)
        # Some OneBot adapters omit `self_id` on an echo.  The application
        # records the returned message id before this check in the normal case,
        # so it is a second independent anti-loop guard.
        if event.get("message_id") and self.db.get_sent_message(event["message_id"], group_id):
            event["is_self"] = True
            event["pending"] = False
        # A structured QQ reply to one of this application's recorded sends
        # is a direct, live interaction even without an @ mention.  The
        # lookup happens only on live OneBot delivery, not initial/reconnect
        # history, so an old reply cannot suddenly make the bot speak later.
        content = event.get("content")
        if not event.get("is_self") and isinstance(content, dict):
            targets = content.get("reply_target_message_ids")
            if isinstance(targets, list) and any(
                self.db.get_sent_message(_as_string(target), group_id) is not None
                for target in targets
            ):
                content["live_reply_to_bot"] = True
        event_id = self.db.insert_event(event)
        if event_id is not None and event.get("is_self"):
            # Own messages intentionally do not create an LLM turn, but their
            # image originals still belong to the enabled group's local audit.
            event["id"] = event_id
            self._track_background_task(self._persist_event_images(event))
        if (
            event_id is not None
            and event.get("pending")
            and group.get("initialized")
            and group_id not in self._history_backfill_groups
        ):
            # Deliberately do not emit a second “I am processing” message
            # while the durable primary worker is busy.  The auxiliary status
            # mini-agent made ordinary direct requests look like they were
            # being answered twice, and its acknowledgement was often less
            # useful than the primary Agent's actual reply.  The event stays
            # durably queued and the primary worker remains strictly serial.
            await self._schedule_worker(group_id)

    def _set_worker_activity(
        self,
        group_id: str,
        phase: str,
        *,
        turn_id: int = 0,
        event_ids: Optional[Sequence[int]] = None,
        active_tool: str = "",
        turn_context: Optional[str] = None,
        previous_summary: Optional[str] = None,
        recent_context: Optional[str] = None,
    ) -> None:
        """Update the small, safe state snapshot of one primary worker.

        This state is intentionally in-memory only.  It exists so a newly
        arrived message can get a quick acknowledgement without teaching the
        auxiliary model the whole conversation transcript or giving it the
        primary agent's tools.
        """

        previous = self._worker_activity.get(group_id) or {}
        now = time.monotonic()
        state: Dict[str, Any] = dict(previous)
        state["phase"] = _as_string(phase).strip()[:240] or "正在处理上一条消息"
        state["turn_id"] = max(0, int(turn_id or previous.get("turn_id") or 0))
        if event_ids is not None:
            state["event_ids"] = [
                int(item) for item in event_ids if isinstance(item, int) or str(item).isdigit()
            ][:MAX_HISTORY_EVENTS]
        else:
            state.setdefault("event_ids", [])
        state["active_tool"] = _as_string(active_tool).strip()[:120]
        if turn_context is not None:
            state["turn_context"] = _as_string(turn_context)[:MAX_BUSY_REPLY_TURN_CONTEXT_CHARS]
        else:
            state.setdefault("turn_context", "")
        if previous_summary is not None:
            state["previous_summary"] = _as_string(previous_summary)[:MAX_BUSY_REPLY_TURN_CONTEXT_CHARS]
        else:
            state.setdefault("previous_summary", "")
        if recent_context is not None:
            state["recent_context"] = _as_string(recent_context)[:MAX_BUSY_REPLY_TURN_CONTEXT_CHARS]
        else:
            state.setdefault("recent_context", "")
        state["phase_started_monotonic"] = now
        state.setdefault("worker_started_monotonic", now)
        self._worker_activity[group_id] = state

    def _primary_worker_snapshot(
        self,
        group_id: str,
    ) -> Tuple[Optional[asyncio.Task[None]], Dict[str, Any]]:
        """Return a bounded snapshot only when the current primary task is live."""

        worker = self._workers.get(group_id)
        if worker is None or worker.done():
            return None, {}
        activity = dict(self._worker_activity.get(group_id) or {})
        now = time.monotonic()
        phase_started = activity.get("phase_started_monotonic")
        try:
            phase_elapsed = max(0, int(now - float(phase_started)))
        except (TypeError, ValueError):
            phase_elapsed = 0
        raw_event_ids = activity.get("event_ids")
        event_ids = raw_event_ids if isinstance(raw_event_ids, list) else []
        snapshot = {
            # The turn ID is context for the mini agent only; its immutable
            # prompt explicitly prohibits exposing it in QQ.
            "main_turn_id": max(0, _safe_int(activity.get("turn_id"))),
            "main_phase": _as_string(activity.get("phase"))[:240] or "正在处理上一条消息",
            "active_tool": _as_string(activity.get("active_tool"))[:120],
            "current_turn_event_count": min(MAX_HISTORY_EVENTS, len(event_ids)),
            "phase_elapsed_seconds": min(86_400, phase_elapsed),
            # This is a small current-turn preview, not the conversation's
            # 50K raw context, old summary, rules, or any other session.
            "current_turn_context": _as_string(activity.get("turn_context"))[
                :MAX_BUSY_REPLY_TURN_CONTEXT_CHARS
            ],
            # These are bounded current-conversation context snippets.  They
            # are included only so the mini agent can avoid a nonsensical
            # status sentence; its immutable prompt forbids answering or
            # repeating them, and no other conversation is ever included.
            "previous_summary_context": _as_string(activity.get("previous_summary"))[
                :MAX_BUSY_REPLY_TURN_CONTEXT_CHARS
            ],
            "recent_context": _as_string(activity.get("recent_context"))[
                :MAX_BUSY_REPLY_TURN_CONTEXT_CHARS
            ],
        }
        return worker, snapshot

    def _primary_worker_still_active(
        self,
        group_id: str,
        expected_worker: asyncio.Task[None],
    ) -> bool:
        return (
            not self._stopping
            and self._workers.get(group_id) is expected_worker
            and not expected_worker.done()
        )

    def _schedule_busy_reply_if_primary_worker_active(
        self,
        group_id: str,
        event: Dict[str, Any],
        event_id: int,
    ) -> None:
        """Start one independent, tool-free busy reply for a new human message.

        Duplicate OneBot delivery is already filtered by ``insert_event``;
        the in-memory set additionally prevents two concurrent tasks from
        acknowledging the same newly inserted event.  The eventual QQ send
        has its own durable operation namespace as a second crash/timeout
        guard, so it cannot consume the main worker's action slot.
        """

        if self._stopping or event_id <= 0:
            return
        if event.get("is_self") or not event.get("pending"):
            return
        if not _event_explicitly_requires_busy_agent_reply(event):
            return
        worker, snapshot = self._primary_worker_snapshot(group_id)
        if worker is None or event_id in self._busy_reply_event_ids:
            return
        self._busy_reply_event_ids.add(event_id)
        self._track_background_task(
            self._run_busy_reply_agent(group_id, dict(event), event_id, worker, snapshot)
        )

    @staticmethod
    def _normalise_busy_reply_text(value: Any) -> str:
        """Keep an auxiliary response recognisably a progress acknowledgement."""

        text = _as_string(value).replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text or len(text) > MAX_BUSY_REPLY_TEXT_CHARS:
            return ""
        text = re.sub(r"\n{3,}", "\n\n", text)
        lowered = text.lower()
        if any(token in lowered for token in _BUSY_REPLY_DISALLOWED_TEXT):
            return ""
        if any(token in text for token in ("答案", "结论", "代码", "已完成", "已经完成", "下载完成")):
            return ""
        if not any(cue in text for cue in _BUSY_REPLY_PROGRESS_CUES):
            return ""
        return text

    @staticmethod
    def _busy_reply_fallback(worker_snapshot: Dict[str, Any]) -> str:
        """A useful, deterministic acknowledgement when the mini model fails."""

        active_tool = _as_string(worker_snapshot.get("active_tool"))
        activity = _BUSY_TOOL_PROGRESS_LABELS.get(active_tool, "")
        if not activity:
            phase = _as_string(worker_snapshot.get("main_phase"))
            if "图片" in phase:
                activity = "处理图片"
            elif "文件" in phase:
                activity = "处理文件"
            elif "记忆" in phase:
                activity = "整理前文信息"
            else:
                activity = "处理上一条消息"
        return "我正在%s，这条消息已收到，请稍等。" % activity

    async def _run_busy_reply_agent(
        self,
        group_id: str,
        event: Dict[str, Any],
        event_id: int,
        expected_worker: asyncio.Task[None],
        worker_snapshot: Dict[str, Any],
    ) -> None:
        """Run the status-only sub-agent without touching the primary queue.

        A model timeout/failure merely selects a short deterministic fallback
        while the primary worker remains active.  The OneBot send itself is
        attempted at most once per event/namespace; no retry is made after an
        ambiguous adapter timeout.
        """

        try:
            if not self._primary_worker_still_active(group_id, expected_worker):
                return
            incoming_event_text = self._format_events([event], max_chars=MAX_BUSY_REPLY_EVENT_CHARS)
            generated = ""
            try:
                client = ChatCompletionsClient(self.settings.llm, self.secret_store.get_llm_api_key())
                runner = getattr(client, "run_busy_reply", None)
                if callable(runner):
                    generated = await asyncio.wait_for(
                        runner(
                            worker_snapshot=worker_snapshot,
                            incoming_event_text=incoming_event_text,
                        ),
                        timeout=BUSY_REPLY_LLM_DEADLINE_SECONDS,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # This is deliberately only a local log.  A failed status
                # acknowledgement must never poison the primary turn's group
                # error or cause its durable event batch to retry.
                logger.info(
                    "busy reply mini-agent unavailable for %s event %s: %s",
                    group_id,
                    event_id,
                    redact_error_detail(exc, api_key=_as_string(self.secret_store.get_llm_api_key()), limit=400),
                )

            if not self._primary_worker_still_active(group_id, expected_worker):
                return
            group = self.db.get_group(group_id)
            if not group or not group.get("enabled"):
                return
            text = self._normalise_busy_reply_text(generated)
            if not text:
                text = self._busy_reply_fallback(worker_snapshot)
            if not text:
                return

            # A dedicated turn/audit makes the outgoing acknowledgement
            # observable and records its OneBot result, but status=auxiliary
            # keeps it out of rolling-summary recovery logic.
            turn_id = self.db.create_turn(group_id, [event_id])
            arguments: Dict[str, Any] = {"text": text}
            reply_to = _as_string(event.get("message_id")).strip()
            if reply_to:
                arguments["reply_to_message_id"] = reply_to
            result = await self._execute_tool(
                turn_id,
                group_id,
                "send_group_message",
                arguments,
                "busy-status-%s" % event_id,
                trusted_reply_message_ids=[reply_to] if reply_to else [],
                operation_namespace=BUSY_REPLY_OPERATION_NAMESPACE,
                app_sent_metadata={
                    "auxiliary_busy_reply": True,
                    "memory_processed": True,
                },
            )
            if result.get("ok") is True:
                self.db.finish_turn(
                    turn_id,
                    "auxiliary",
                    "辅助忙碌状态回复已发送（不参与内部摘要）",
                )
            else:
                self.db.finish_turn(
                    turn_id,
                    "auxiliary_failed",
                    "辅助忙碌状态回复未发送（不参与内部摘要）",
                    error=redact_error_detail(result.get("error"), limit=1_200),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # The auxiliary task is intentionally isolated from the durable
            # primary worker.  It must not turn a status failure into an
            # unhandled background-task warning or interrupt main processing.
            logger.exception("busy reply mini-agent failed for %s event %s", group_id, event_id)
        finally:
            self._busy_reply_event_ids.discard(event_id)

    async def _schedule_worker(self, group_id: str) -> None:
        if self._stopping:
            return
        worker_lock, _ = self._async_locks()
        async with worker_lock:
            existing = self._workers.get(group_id)
            if existing and not existing.done():
                return
            task = asyncio.create_task(self._run_group_worker(group_id), name="qq-agent-group-" + group_id)
            self._workers[group_id] = task
            task.add_done_callback(lambda done, key=group_id: self._remove_finished_worker(key, done))

    def _remove_finished_worker(self, group_id: str, worker: asyncio.Task[None]) -> None:
        # A new worker may be scheduled after an older worker finishes but
        # before this done callback runs.  Only remove the exact finished task;
        # otherwise a later event could start a second same-group worker.
        if self._workers.get(group_id) is worker:
            self._workers.pop(group_id, None)
            self._worker_activity.pop(group_id, None)

    @staticmethod
    def _summary_is_usable(value: Any) -> bool:
        """Reject provider refusals/error fallbacks as durable memory."""

        text = _as_string(value).strip()
        if not text:
            return False
        lowered = text.lower()
        # Error strings are often returned as ordinary assistant text by
        # relays (not as HTTP errors), so inspect the bounded beginning and
        # known provider-policy markers rather than trusting HTTP status only.
        probe = lowered[:4_000]
        return not any(marker in probe for marker in SUMMARY_FAILURE_MARKERS)

    @staticmethod
    def _summary_failure_detail(value: Any) -> str:
        text = _as_string(value).strip()
        if len(text) > 1_800:
            text = text[:1_800] + "…"
        return text or "模型没有返回可用摘要"

    @staticmethod
    def _is_summary_safety_refusal(value: Any) -> bool:
        """Recognize an upstream safety refusal without inspecting message text."""

        probe = _as_string(value).casefold()
        return bool(probe) and any(marker in probe for marker in SUMMARY_SAFETY_REFUSAL_MARKERS)

    def _ensure_summary_checkpoint(self, group_id: str) -> bool:
        """Seed old installations or repair a corrupted live summary pointer."""

        current = self.db.get_summary_record(group_id)
        current_text = _as_string(current.get("content"))
        if not current_text.strip() and int(current.get("last_event_id") or 0) == 0:
            # A brand-new group legitimately has no rolling summary yet.
            return False
        if self._summary_is_usable(current_text):
            self.db.ensure_summary_snapshot(
                group_id,
                current_text,
                int(current.get("last_event_id") or 0),
            )
            return False

        # Prefer an immutable checkpoint created by a previous successful
        # archive turn.  It is the exact state we want to restore.
        checkpoint: Optional[Dict[str, Any]] = None
        for item in self.db.list_summary_snapshots(group_id, limit=500):
            if self._summary_is_usable(item.get("content")):
                checkpoint = item
                break

        # Databases created before checkpoints can still recover from a prior
        # successful turn.  Its cursor is unknown in old schemas, so use a
        # zero cursor and perform a summary-only replay of the durable events;
        # old QQ actions are never made pending again.
        if checkpoint is None:
            for turn in self.db.list_group_turns(group_id, limit=10_000):
                if turn.get("status") == "completed" and self._summary_is_usable(turn.get("summary_text")):
                    checkpoint = {
                        "content": _as_string(turn.get("summary_text")),
                        "last_event_id": 0,
                    }
                    break

        restored_text = _as_string(checkpoint.get("content")) if checkpoint else ""
        restored_cursor = int(checkpoint.get("last_event_id") or 0) if checkpoint else 0
        self.db.restore_summary_snapshot(
            group_id,
            restored_text,
            restored_cursor,
            reason="自动回退到上一次可用摘要",
        )
        through = self.db.max_event_id(group_id)
        reset_count = self.db.reset_summary_segment(group_id, restored_cursor, through)
        logger.warning(
            "summary for %s was unusable; restored checkpoint cursor=%s and reset %s events",
            group_id,
            restored_cursor,
            reset_count,
        )
        return True

    def _rollback_summary_after_failure(self, group_id: str) -> int:
        """Restore the last good checkpoint and expose its interval for replay."""

        current = self.db.get_summary_record(group_id)
        checkpoint: Optional[Dict[str, Any]] = None
        for item in self.db.list_summary_snapshots(group_id, limit=500):
            if self._summary_is_usable(item.get("content")):
                checkpoint = item
                break
        if checkpoint is None and self._summary_is_usable(current.get("content")):
            checkpoint = current
        if checkpoint is None:
            for turn in self.db.list_group_turns(group_id, limit=10_000):
                if turn.get("status") == "completed" and self._summary_is_usable(turn.get("summary_text")):
                    checkpoint = {"content": turn.get("summary_text"), "last_event_id": 0}
                    break
        restored_text = _as_string(checkpoint.get("content")) if checkpoint else ""
        restored_cursor = int(checkpoint.get("last_event_id") or 0) if checkpoint else 0
        self.db.restore_summary_snapshot(
            group_id,
            restored_text,
            restored_cursor,
            reason="模型摘要失败，自动回退并重算消息区间",
        )
        reset_count = self.db.reset_summary_segment(
            group_id,
            restored_cursor,
            self.db.max_event_id(group_id),
        )
        return reset_count

    async def _run_group_worker(self, group_id: str) -> None:
        self._set_worker_activity(group_id, "正在整理待处理消息")
        # Once the provider explicitly rejects raw group content for safety,
        # keep the rest of this worker's archive pass metadata-only. A new
        # live worker may try the normal path again, but this pass must not
        # repeatedly upload the same refused text or images.
        safety_metadata_mode = False
        safety_warning = ""
        while not self._stopping:
            group = self.db.get_group(group_id)
            if not group or not group.get("enabled"):
                return
            self._ensure_summary_checkpoint(group_id)
            pending = self.db.pending_events(group_id)
            raw_source_events = self.db.latest_group_raw_context_events(
                group_id, limit=MAX_RAW_CONTEXT_SOURCE_EVENTS
            )
            raw_context_events = self._select_latest_raw_context(raw_source_events)
            raw_event_ids = {
                int(event.get("id") or 0)
                for event in raw_context_events
                if int(event.get("id") or 0) > 0
            }
            # The raw 50K transcript is never summarized in this round.  Only
            # events that have naturally fallen out of it advance the rolling
            # summary cursor.  This is deliberately independent from pending:
            # a live message can be handled by the agent immediately while it
            # remains verbatim context for future turns.
            archive_candidates = [
                event
                for event in self.db.unarchived_events(group_id)
                if int(event.get("id") or 0) not in raw_event_ids
            ]
            archive_batch = self._select_batch(archive_candidates)
            memory_batch = self._select_memory_batch(
                self.db.memory_pending_events(group_id, limit=MAX_MEMORY_EVENTS_PER_PASS)
            )
            if not pending and not archive_batch:
                if not memory_batch:
                    return
                if safety_metadata_mode:
                    # Memory extraction requires exact message evidence. Do
                    # not upload those events after the provider rejected raw
                    # content; leave the cursor pending for a later provider
                    # or an operator retry instead of inventing memories.
                    warning = (
                        safety_warning
                        + " 长期记忆游标未推进，待更换可处理该内容的模型后重试。"
                    )
                    self.db.set_group_error(group_id, warning)
                    return
                # Memory has an independent cursor.  A failed extraction does
                # not make an old message live again and cannot replay a QQ
                # reply which already succeeded.
                try:
                    self._set_worker_activity(
                        group_id,
                        "正在整理长期记忆",
                        event_ids=[int(event.get("id") or 0) for event in memory_batch],
                        turn_context=self._format_events(
                            memory_batch,
                            max_chars=MAX_BUSY_REPLY_TURN_CONTEXT_CHARS,
                        ),
                    )
                    client = ChatCompletionsClient(
                        self.settings.llm, self.secret_store.get_llm_api_key()
                    )
                    effort = self._resolve_reasoning_effort(group)
                    memory_result = await self._process_group_memory_batch(
                        client, group_id, memory_batch, effort
                    )
                    if memory_result < 0:
                        # A deliberately minimal/custom client may implement
                        # normal turns but not the optional memory protocol.
                        # Leave the cursor untouched and stop this otherwise
                        # idle worker, avoiding both data loss and a busy loop.
                        current_error = _as_string(
                            (self.db.get_group(group_id) or {}).get("last_error")
                        )
                        if not current_error:
                            self.db.set_group_error(
                                group_id,
                                "当前 LLM 客户端不支持长期记忆提取；记忆游标未推进。",
                            )
                        return
                    self.db.set_group_error(group_id, "")
                    continue
                except Exception as exc:
                    detail = redact_error_detail(
                        exc,
                        api_key=_as_string(self.secret_store.get_llm_api_key()),
                        limit=1_800,
                    )
                    message = "长期记忆提取失败（群聊回复不会重放）：\n" + (
                        detail or "未提供详情"
                    )
                    logger.exception("group memory extraction failed for %s", group_id)
                    self.db.set_group_error(group_id, message)
                    return
            trigger_batch = self._select_batch(pending)
            if not trigger_batch and not archive_batch:
                return
            trigger_event_ids = [int(event["id"]) for event in trigger_batch]
            archive_event_ids = [int(event["id"]) for event in archive_batch]
            # Durable QQ-action slots are scoped to the new live input where
            # possible.  A pure archival turn has no live trigger, so its old
            # archive batch provides the deterministic scope instead.
            turn_event_ids = trigger_event_ids or archive_event_ids
            turn_id = self.db.create_turn(group_id, turn_event_ids)
            try:
                event_text = (
                    self._format_events(archive_batch)
                    if archive_batch
                    else "【服务生成：没有新的旧消息需要写入滚动摘要；请保留现有摘要。】"
                )
                recent_context_text = self._format_events(
                    raw_context_events,
                    max_chars=MAX_RECENT_CONTEXT_CHARS,
                )
                memory_context = self._build_group_memory_context(
                    group_id,
                    [*trigger_batch, *raw_context_events[-40:]],
                )
                trusted_reply_ids = _trusted_reply_message_ids([*raw_context_events, *trigger_batch])
                direct_mention_reply_required = any(
                    _event_has_live_direct_mention(event) for event in trigger_batch
                )
                direct_reply_to_bot_required = any(
                    _event_has_live_reply_to_bot(event) for event in trigger_batch
                )
                direct_clear_group_call_required = any(
                    _event_has_live_clear_group_call(event) for event in trigger_batch
                )
                direct_explicit_task_reply_required = any(
                    _event_has_live_explicit_agent_task_request(event) for event in trigger_batch
                )
                # Every live event is a normal Agent turn.  The model may
                # decide to participate, search, inspect media, or send a
                # useful reply without waiting for an @.  The separate
                # immutable policy below still keeps the rolling summary
                # local and never lets it become QQ text.
                allow_group_actions = bool(trigger_event_ids)
                current_event_text = self._format_events(
                    trigger_batch,
                    include_direct_mention_marker=True,
                ) if trigger_batch else ""
                group_prompt_for_turn = _as_string(group.get("prompt_override"))
                persistent_rules_for_turn = self.rules_text()
                previous_summary_for_turn = self.db.get_summary(group_id)
                if safety_metadata_mode:
                    # Do not retry the refused payload in another shape. The
                    # fallback deliberately removes all message text, names,
                    # IDs, URLs, filenames, memory prose and images.
                    event_text = self._format_events_metadata_only(archive_batch)
                    recent_context_text = self._format_events_metadata_only(
                        raw_context_events,
                        max_chars=MAX_RECENT_CONTEXT_CHARS,
                    )
                    current_event_text = self._format_events_metadata_only(trigger_batch)
                    memory_context = ""
                    group_prompt_for_turn = ""
                    persistent_rules_for_turn = ""
                    previous_summary_for_turn = ""
                    direct_mention_reply_required = False
                    direct_reply_to_bot_required = False
                    direct_clear_group_call_required = False
                    direct_explicit_task_reply_required = False
                    allow_group_actions = False
                # A member can ask for a fresh group recap, but that still
                # must not export the service's already-saved rolling summary
                # verbatim.  Capture the private candidates once for this
                # turn and pass them only to the local outbound tool gate.
                allow_user_facing_group_summary = _events_explicitly_request_user_facing_group_summary(
                    trigger_batch
                )
                rolling_summary_candidates = self._rolling_summary_candidates(group_id)
                self._set_worker_activity(
                    group_id,
                    "正在准备主 Agent 本轮任务",
                    turn_id=turn_id,
                    event_ids=turn_event_ids,
                    turn_context=current_event_text
                    or self._format_events(archive_batch, max_chars=MAX_BUSY_REPLY_TURN_CONTEXT_CHARS),
                    previous_summary=self.db.get_summary(group_id),
                    recent_context=recent_context_text,
                )
                # Images are most relevant when they just arrived.  They stay
                # visible as textual placeholders in the raw 50K transcript;
                # re-uploading every old image on every turn would be both
                # expensive and hostile to vision API limits.
                self._set_worker_activity(
                    group_id,
                    "正在准备本轮消息与媒体",
                    turn_id=turn_id,
                    event_ids=turn_event_ids,
                )
                image_parts = (
                    []
                    if safety_metadata_mode
                    else await self._prepare_image_parts(trigger_batch or archive_batch)
                )
                current_group = self.db.get_group(group_id)
                if self._stopping or not current_group or not current_group.get("enabled"):
                    self.db.finish_turn(turn_id, "cancelled", error="group disabled before LLM request")
                    return
                effort = self._resolve_reasoning_effort(group)
                client = ChatCompletionsClient(self.settings.llm, self.secret_store.get_llm_api_key())
                preferred_initial_tool = ""
                if direct_explicit_task_reply_required:
                    for event in trigger_batch:
                        content = event.get("content") if isinstance(event.get("content"), dict) else {}
                        file_value = content.get("file") if isinstance(content.get("file"), dict) else {}
                        file_name = _as_string(file_value.get("name") or file_value.get("file"))
                        has_video = bool(content.get("video")) or self._is_video_file(
                            Path(file_name), file_name
                        )
                        if has_video:
                            preferred_initial_tool = "Builtin_video_understanding"
                            break

                async def execute(
                    name: str,
                    arguments: Dict[str, Any],
                    call_id: str,
                    operation_slot: int = 0,
                ) -> Dict[str, Any]:
                    activity = _BUSY_TOOL_PROGRESS_LABELS.get(name, "处理当前任务工具")
                    self._set_worker_activity(
                        group_id,
                        "正在" + activity,
                        turn_id=turn_id,
                        event_ids=turn_event_ids,
                        active_tool=name,
                    )
                    try:
                        return await self._execute_tool(
                            turn_id,
                            group_id,
                            name,
                            arguments,
                            call_id,
                            operation_slot=operation_slot,
                            trusted_reply_message_ids=trusted_reply_ids,
                            allow_user_facing_group_summary=allow_user_facing_group_summary,
                            rolling_summary_candidates=rolling_summary_candidates,
                        )
                    finally:
                        self._set_worker_activity(
                            group_id,
                            "正在根据当前结果继续处理",
                            turn_id=turn_id,
                            event_ids=turn_event_ids,
                        )

                self._set_worker_activity(
                    group_id,
                    "主 Agent 正在分析并处理上一批消息",
                    turn_id=turn_id,
                    event_ids=turn_event_ids,
                )
                result = await client.run_turn(
                    previous_summary=previous_summary_for_turn,
                    event_text=event_text,
                    group_prompt=group_prompt_for_turn,
                    reasoning_effort=effort,
                    image_parts=image_parts,
                    tool_executor=execute,
                    # The client keeps verified/direct interaction signals
                    # separate for prompt wording, then applies the same
                    # forced-send protocol to the current group only.
                    direct_mention_reply_required=direct_mention_reply_required,
                    direct_reply_to_bot_message_required=direct_reply_to_bot_required,
                    direct_clear_group_call_reply_required=direct_clear_group_call_required,
                    direct_explicit_task_reply_required=direct_explicit_task_reply_required,
                    allow_group_actions=allow_group_actions,
                    recent_context_text=recent_context_text,
                    current_event_text=current_event_text,
                    persistent_rules=persistent_rules_for_turn,
                    memory_context=memory_context,
                    workspace_path=str(self.conversation_workspace(group_id)),
                    preferred_initial_tool=preferred_initial_tool,
                )
                current_group = self.db.get_group(group_id)
                if self._stopping or not current_group or not current_group.get("enabled"):
                    self.db.finish_turn(turn_id, "cancelled", error="group disabled during LLM request")
                    return
                if not self._summary_is_usable(result.summary):
                    # Providers sometimes return policy refusals or an error
                    # fallback with HTTP 200.  Never persist that text as
                    # memory.  Keep already-executed live actions consumed,
                    # restore the previous checkpoint, and replay this period
                    # as archive-only turns (no old QQ tool calls).
                    safety_refusal = self._is_summary_safety_refusal(result.summary)
                    if safety_refusal and not safety_metadata_mode:
                        safety_metadata_mode = True
                        safety_warning = SAFETY_METADATA_FALLBACK_WARNING
                    if trigger_event_ids:
                        self.db.mark_events_processed(trigger_event_ids)
                    reset_count = self._rollback_summary_after_failure(group_id)
                    detail = self._summary_failure_detail(result.summary)
                    recovery_error = (
                        "模型返回不可用摘要，已自动回退到上一次可用记忆并重算 %s 条消息；"
                        "本轮 QQ 动作不会重放。\n返回内容：%s"
                        % (reset_count, detail)
                    )
                    self.db.finish_turn(turn_id, "failed", error=recovery_error)
                    self.db.set_group_error(group_id, recovery_error)
                    attempts = self._summary_recovery_attempts.get(group_id, 0)
                    if attempts < MAX_AUTO_SUMMARY_RECOVERY_ATTEMPTS:
                        self._summary_recovery_attempts[group_id] = attempts + 1
                        # The current worker can immediately consume the
                        # reset, unarchived interval once.  A second refusal
                        # exits instead of creating an API retry loop.
                        continue
                    return
                if archive_event_ids:
                    self._set_worker_activity(
                        group_id,
                        "正在保存主 Agent 的处理结果",
                        turn_id=turn_id,
                        event_ids=turn_event_ids,
                    )
                    self.db.save_summary(
                        group_id,
                        result.summary,
                        archive_event_ids[-1],
                        turn_id=turn_id,
                    )
                    self.db.mark_events_archived(archive_event_ids)
                if trigger_event_ids:
                    self.db.mark_events_processed(trigger_event_ids)
                self._summary_recovery_attempts.pop(group_id, None)
                memory_warning = ""
                if memory_batch:
                    if safety_metadata_mode:
                        memory_warning = (
                            safety_warning
                            + " 长期记忆游标未推进，待更换可处理该内容的模型后重试。"
                        )
                    else:
                        try:
                            await self._process_group_memory_batch(
                                client, group_id, memory_batch, effort
                            )
                        except Exception as exc:
                            detail = redact_error_detail(
                                exc,
                                api_key=_as_string(self.secret_store.get_llm_api_key()),
                                limit=1_800,
                            )
                            memory_warning = (
                                "长期记忆提取失败（本轮群聊动作已完成且不会重放）：\n"
                                + (detail or "未提供详情")
                            )
                # Compatibility/fallback warnings are actionable diagnostics too;
                # retain them on the completed turn as well as the group card.
                warning_parts: List[str] = []
                for part in (result.warning, memory_warning):
                    if part and not any(part == existing for existing in warning_parts):
                        warning_parts.append(part)
                if safety_warning and not any(safety_warning in existing for existing in warning_parts):
                    warning_parts.insert(0, safety_warning)
                combined_warning = "; ".join(warning_parts)
                self.db.finish_turn(turn_id, "completed", result.summary, error=combined_warning)
                self.db.set_group_error(group_id, combined_warning)
                if memory_warning:
                    return
            except asyncio.CancelledError:
                self.db.finish_turn(turn_id, "cancelled", error="group worker cancelled")
                raise
            except Exception as exc:
                message = str(exc)
                logger.exception("group worker failed for %s", group_id)
                self.db.finish_turn(turn_id, "failed", error=message)
                self.db.set_group_error(group_id, message)
                return

    @classmethod
    def _select_latest_raw_context(cls, events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Keep the newest verbatim transcript slice within the 50K budget.

        Events arrive in chronological order.  Selecting backward matters:
        cutting a forward-formatted transcript would retain old context and
        silently throw away the newest user question.  Count the actual prompt
        representation, including trusted per-message metadata, so the model
        transport limit is real rather than an optimistic text-only estimate.
        """

        selected_reverse: List[Dict[str, Any]] = []
        used = 0
        for event in reversed(events):
            rendered = cls._format_events([event])
            size = len(rendered) + (1 if selected_reverse else 0)
            if selected_reverse and used + size > MAX_RECENT_CONTEXT_CHARS:
                break
            selected_reverse.append(event)
            used += size
            if used >= MAX_RECENT_CONTEXT_CHARS:
                break
        return list(reversed(selected_reverse))

    @staticmethod
    def _select_batch(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        selected: List[Dict[str, Any]] = []
        used = 0
        for event in events[:MAX_HISTORY_EVENTS]:
            text = _as_string(event.get("normalized_text"))
            size = len(text)
            if selected and used + size > MAX_EVENT_TEXT_CHARS:
                break
            selected.append(event)
            used += min(size, MAX_EVENT_TEXT_CHARS)
            if used >= MAX_EVENT_TEXT_CHARS:
                break
        return selected

    @staticmethod
    def _select_memory_batch(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Select complete evidence events without silently losing long text."""

        selected: List[Dict[str, Any]] = []
        used = 0
        for event in events[:MAX_MEMORY_EVENTS_PER_PASS]:
            text = _as_string(event.get("normalized_text"))
            size = len(text)
            if selected and used + size > MAX_MEMORY_EXTRACTION_CHARS:
                break
            selected.append(event)
            used += size
            if used >= MAX_MEMORY_EXTRACTION_CHARS:
                break
        return selected

    @staticmethod
    def _memory_event_records(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        for event in events:
            event_id = int(event.get("id") or 0)
            if event_id <= 0:
                continue
            records.append(
                {
                    "event_id": event_id,
                    "message_id": _as_string(event.get("message_id")),
                    "sender_id": _as_string(event.get("sender_id")),
                    "sender_name": _as_string(event.get("sender_name")),
                    "occurred_at": event.get("occurred_at") or 0,
                    "is_bot_message": bool(event.get("is_self")),
                    "text": _as_string(event.get("normalized_text")),
                }
            )
        return records

    @staticmethod
    def _memory_query_terms(events: Sequence[Dict[str, Any]]) -> List[str]:
        """Build deterministic lexical keys; no embedding model is needed."""

        terms: List[str] = []
        seen = set()

        def add(value: Any) -> None:
            text = _as_string(value).strip()
            if len(text) < 2 or text in seen:
                return
            seen.add(text)
            terms.append(text[:80])

        for event in events[-40:]:
            add(event.get("sender_id"))
            add(event.get("sender_name"))
            text = _as_string(event.get("normalized_text"))[:4_000]
            for token in re.findall(r"[A-Za-z0-9_./+-]{2,64}", text):
                add(token)
            for chunk in re.findall(r"[\u3400-\u9fff]{2,32}", text):
                add(chunk)
                # A natural query such as ``小陈偏好`` need not occur as one
                # contiguous phrase in the canonical statement
                # ``小陈（QQ ...）编程语言偏好``.  Deterministic overlapping
                # CJK n-grams provide lexical recall without embeddings or a
                # model-generated synonym expansion.  Longer terms are added
                # first, while the existing 32-term cap bounds noisy input.
                for width in (4, 3, 2):
                    if len(chunk) <= width:
                        continue
                    for index in range(0, len(chunk) - width + 1):
                        add(chunk[index : index + width])
                        if len(terms) >= 32:
                            break
                    if len(terms) >= 32:
                        break
            if len(terms) >= 32:
                break
        return terms[:32]

    def _relevant_group_memories(
        self,
        group_id: str,
        events: Sequence[Dict[str, Any]],
        *,
        limit: int = 250,
    ) -> List[Dict[str, Any]]:
        memories: List[Dict[str, Any]] = []
        seen = set()
        for term in self._memory_query_terms(events):
            for item in self.db.search_group_memories(
                group_id, term, active_only=True, limit=40, include_evidence=True
            ):
                memory_id = int(item.get("id") or 0)
                if memory_id and memory_id not in seen:
                    seen.add(memory_id)
                    memories.append(item)
                    if len(memories) >= limit:
                        return memories
        # Keep a broad current background in every turn.  Older/deeper recall
        # remains available through Builtin_querymemory and the permanent
        # evidence ledger, so it is never semantically compressed away.
        for item in self.db.list_group_memories(
            group_id, active_only=True, limit=min(limit, 1_000), include_evidence=True
        ):
            memory_id = int(item.get("id") or 0)
            if memory_id and memory_id not in seen:
                seen.add(memory_id)
                memories.append(item)
                if len(memories) >= limit:
                    break
        return memories

    @staticmethod
    def _compact_memory_for_model(memory: Dict[str, Any]) -> Dict[str, Any]:
        evidence = []
        for item in list(memory.get("evidence") or [])[-4:]:
            if not isinstance(item, dict):
                continue
            evidence.append(
                {
                    "event_id": item.get("source_event_id"),
                    "message_id": _as_string(item.get("source_message_id")),
                    "quote": _as_string(item.get("evidence_text"))[:1_000],
                    "observed_at": _as_string(item.get("observed_at")),
                }
            )
        return {
            "memory_id": int(memory.get("id") or memory.get("memory_id") or 0),
            "type": _as_string(memory.get("kind") or memory.get("type")),
            "statement": _as_string(memory.get("statement")),
            "subject": _as_string(memory.get("subject")),
            "predicate": _as_string(memory.get("predicate")),
            "value": _as_string(memory.get("object_value") or memory.get("value")),
            "confidence_status": _as_string(memory.get("confidence_status")),
            "valid_from": _as_string(memory.get("valid_from")),
            "valid_until": _as_string(memory.get("valid_until")),
            "open_conflicts": list(memory.get("conflicts_with_memory_ids") or []),
            "evidence": evidence,
        }

    def _build_group_memory_context(
        self, group_id: str, events: Sequence[Dict[str, Any]]
    ) -> str:
        lines: List[str] = []
        used = 0
        for item in self._relevant_group_memories(
            group_id, events, limit=MAX_MEMORY_CONTEXT_ITEMS
        ):
            rendered = json.dumps(
                self._compact_memory_for_model(item),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if lines and used + len(rendered) + 1 > MAX_MEMORY_CONTEXT_CHARS:
                break
            lines.append(rendered)
            used += len(rendered) + 1
        return "\n".join(lines)

    async def _process_group_memory_batch(
        self,
        client: Any,
        group_id: str,
        events: Sequence[Dict[str, Any]],
        reasoning_effort: str,
    ) -> int:
        """Extract, verify and then advance the durable memory cursor."""

        event_ids = [
            int(event.get("id") or 0)
            for event in events
            if int(event.get("id") or 0) > 0
        ]
        if not event_ids:
            return 0
        extractor = getattr(client, "extract_memory_proposals", None)
        if not callable(extractor):
            # Advancing here would permanently forget these events.  A
            # negative compatibility sentinel lets the worker finish any
            # remaining ordinary QQ batches, then stop once memory is the
            # only work left.  The cursor remains retryable and no warning
            # from the already completed model turn is overwritten.
            logger.warning(
                "LLM client has no memory extraction protocol for group %s; cursor retained",
                group_id,
            )
            return -1
        candidates = self._relevant_group_memories(group_id, events, limit=200)
        proposals = await extractor(
            event_records=self._memory_event_records(events),
            existing_memories=[self._compact_memory_for_model(item) for item in candidates],
            reasoning_effort=reasoning_effort,
        )
        applied = 0
        allowed_event_ids = set(event_ids)
        allowed_memory_ids = {
            int(item.get("id") or item.get("memory_id") or 0)
            for item in candidates
            if int(item.get("id") or item.get("memory_id") or 0) > 0
        }
        for proposal in proposals:
            self._apply_memory_proposal(
                group_id,
                proposal,
                candidates,
                allowed_event_ids=allowed_event_ids,
                allowed_memory_ids=allowed_memory_ids,
            )
            applied += 1
        # Advance only after all accepted writes pass same-group and exact-
        # quote evidence checks in the database transaction boundary.
        self.db.mark_events_memory_processed(event_ids)
        return applied

    def _apply_memory_proposal(
        self,
        group_id: str,
        proposal: Dict[str, Any],
        existing_memories: Sequence[Dict[str, Any]],
        *,
        allowed_event_ids: Optional[set[int]] = None,
        allowed_memory_ids: Optional[set[int]] = None,
    ) -> Optional[Dict[str, Any]]:
        operation = _as_string(proposal.get("operation")).strip().lower()
        target_text = _as_string(proposal.get("target_memory_id")).strip()
        target_id = int(target_text) if target_text.isdigit() else 0
        proposal_evidence = proposal.get("evidence")
        if not isinstance(proposal_evidence, list):
            proposal_evidence = []
        evidence = [
            {
                "source_event_id": int(item.get("event_id")),
                "evidence_text": _as_string(item.get("quote")),
            }
            for item in proposal_evidence
            if isinstance(item, dict) and _as_string(item.get("event_id")).isdigit()
        ]
        if not evidence:
            raise ValueError("模型记忆提案没有可验证证据")
        evidence_event_ids = {int(item["source_event_id"]) for item in evidence}
        if allowed_event_ids is not None and not evidence_event_ids.issubset(allowed_event_ids):
            raise ValueError("模型记忆提案引用了本轮提取批次之外的事件")
        raw_source_ids = proposal.get("source_event_ids")
        if not isinstance(raw_source_ids, list):
            raise ValueError("模型记忆提案缺少 source_event_ids")
        try:
            declared_event_ids = {int(_as_string(item)) for item in raw_source_ids}
        except (TypeError, ValueError) as exc:
            raise ValueError("模型记忆提案的 source_event_ids 必须是事件整数 ID") from exc
        if declared_event_ids != evidence_event_ids:
            raise ValueError("模型记忆提案的 source_event_ids 与逐字证据不一致")
        if operation == "retract":
            if target_id <= 0:
                raise ValueError("撤回记忆缺少有效 target_memory_id")
            if allowed_memory_ids is not None and target_id not in allowed_memory_ids:
                raise ValueError("撤回目标不在本轮提供的当前群记忆中")
            return self.db.retract_group_memory(
                group_id,
                target_id,
                "群聊新证据明确撤回或宣布失效",
                evidence=evidence,
            )

        kind = _as_string(proposal.get("memory_type")).strip().lower()
        subject_id = _as_string(proposal.get("subject_id")).strip()
        subject_name = _as_string(proposal.get("subject_name")).strip()
        subject = subject_name or ("QQ " + subject_id if subject_id else "")
        if subject_id and subject_name:
            subject += "（QQ " + subject_id + "）"
        predicate = _as_string(proposal.get("predicate")).strip()
        value = _as_string(proposal.get("value")).strip()
        if not kind or not subject or not predicate or not value:
            raise ValueError("模型记忆提案缺少类型、主体、关系或值")
        statement = "%s %s：%s" % (subject, predicate, value)
        normalized_slot = "|".join(
            part.casefold() for part in (kind, subject_id or subject_name, predicate)
        )
        exact_key_source = normalized_slot + "|" + value.casefold()
        memory_key = "auto:" + hashlib.sha256(exact_key_source.encode("utf-8")).hexdigest()
        metadata = {
            "proposal_id": _as_string(proposal.get("proposal_id")),
            "temporal_status": _as_string(proposal.get("temporal_status")),
            "model_confidence": proposal.get("confidence"),
            "verification_reason": _as_string(proposal.get("verification_reason"))[:1_000],
            "subject_id": subject_id,
            "subject_name": subject_name,
        }
        try:
            confidence = float(proposal.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence_status = "confirmed" if confidence >= 0.95 else "uncertain"

        def same_subject(item: Dict[str, Any]) -> bool:
            item_metadata = item.get("metadata") or {}
            item_subject_id = (
                _as_string(item_metadata.get("subject_id"))
                if isinstance(item_metadata, dict)
                else ""
            )
            return bool(
                (subject_id and item_subject_id == subject_id)
                or (
                    not subject_id
                    and _as_string(item.get("subject")).casefold() == subject.casefold()
                )
            )

        supersedes: List[int] = []
        conflicts: List[int] = []
        if operation == "correct":
            if target_id <= 0:
                raise ValueError("更正记忆缺少有效 target_memory_id")
            if allowed_memory_ids is not None and target_id not in allowed_memory_ids:
                raise ValueError("更正目标不在本轮提供的当前群记忆中")
            target = self.db.get_group_memory(group_id, target_id, include_evidence=False)
            if target is None:
                raise ValueError("更正目标不属于当前群或不存在")
            memory_key = _as_string(target.get("memory_key"))
            supersedes = [target_id]
            # If the corrected value is already an active, independently
            # evidenced memory, merge into that canonical revision instead of
            # creating two identical active facts.  The DB will retain and
            # link every replaced row and close stale conflicts atomically.
            matching = [
                item
                for item in existing_memories
                if int(item.get("id") or item.get("memory_id") or 0) != target_id
                and bool(item.get("active", True))
                and _as_string(item.get("kind")).casefold() == kind.casefold()
                and same_subject(item)
                and _as_string(item.get("predicate")).casefold() == predicate.casefold()
                and _as_string(item.get("object_value")).casefold() == value.casefold()
            ]
            if matching:
                memory_key = _as_string(matching[0].get("memory_key")) or memory_key
                supersedes.extend(
                    int(item.get("id") or item.get("memory_id") or 0) for item in matching
                )
                supersedes = sorted({item for item in supersedes if item > 0})
        elif operation == "remember":
            for item in existing_memories:
                if (
                    same_subject(item)
                    and _as_string(item.get("predicate")).casefold() == predicate.casefold()
                    and _as_string(item.get("object_value")).casefold() != value.casefold()
                    and bool(item.get("active", True))
                ):
                    conflicts.append(int(item.get("id") or item.get("memory_id") or 0))
            conflicts = [item for item in sorted(set(conflicts)) if item > 0]
            if conflicts:
                confidence_status = "uncertain"
        else:
            raise ValueError("不支持的模型记忆操作：" + operation)

        temporal_status = _as_string(proposal.get("temporal_status"))
        valid_until = "已完成/已失效" if temporal_status == "completed" else ""
        return self.db.upsert_group_memory(
            group_id,
            memory_key,
            kind,
            statement,
            evidence,
            subject=subject,
            predicate=predicate,
            object_value=value,
            confidence_status=confidence_status,
            valid_until=valid_until,
            metadata=metadata,
            supersedes_memory_ids=supersedes,
            conflicts_with_ids=conflicts,
            conflict_reason=(
                "同一主体和关系存在不同的有证据陈述，等待更多证据或人工确认。"
                if conflicts
                else ""
            ),
        )

    @staticmethod
    def _format_events_metadata_only(
        events: Sequence[Dict[str, Any]],
        *,
        max_chars: int = 0,
    ) -> str:
        """Format only non-content event metadata for provider safety fallback.

        This intentionally omits sender names, message IDs, raw text, URLs,
        filenames, captions and media metadata. It is a data-minimizing
        fallback after an upstream provider has rejected the original prompt;
        it is not a way to disguise or bypass that provider's policy.
        """

        lines = [
            "【服务生成的安全降级输入】上游拒绝接收群聊原文；以下仅包含计数和事件类型。",
            "不要猜测被省略的消息内容，也不要调用 QQ 状态改变工具。",
        ]
        for index, event in enumerate(events, 1):
            content = event.get("content") if isinstance(event.get("content"), dict) else {}
            media: List[str] = []
            if isinstance(content.get("images"), list) and content.get("images"):
                media.append("image")
            if isinstance(content.get("file"), dict) and content.get("file"):
                media.append("file")
            if isinstance(content.get("video"), dict) and content.get("video"):
                media.append("video")
            if content.get("history"):
                media.append("history")
            event_type = re.sub(r"[^A-Za-z0-9_.:-]", "_", _as_string(event.get("event_type")))[:64]
            timestamp = re.sub(r"[^0-9T:+Z._-]", "_", _as_string(event.get("occurred_at")))[:48]
            text_length = len(_as_string(event.get("normalized_text")))
            lines.append(
                "[事件%s] type=%s time=%s text_chars=%s media=%s self=%s"
                % (
                    index,
                    event_type or "unknown",
                    timestamp or "unknown",
                    text_length,
                    ",".join(media) or "none",
                    "true" if event.get("is_self") else "false",
                )
            )
        formatted = "\n".join(lines)
        if max_chars > 0 and len(formatted) > max_chars:
            return formatted[:max_chars] + "\n【服务生成：元数据输入已截断。】"
        return formatted

    @staticmethod
    def _format_events(
        events: Sequence[Dict[str, Any]],
        *,
        max_chars: int = 0,
        include_direct_mention_marker: bool = True,
    ) -> str:
        lines = []
        for event in events:
            label = event.get("sender_name") or event.get("sender_id") or "系统"
            timestamp = event.get("occurred_at") or "?"
            text = _as_string(event.get("normalized_text"))
            content = event.get("content")
            if isinstance(content, dict) and isinstance(content.get("images"), list):
                image_errors = [
                    _as_string(image.get("storage_error"))
                    for image in content["images"]
                    if isinstance(image, dict) and _as_string(image.get("storage_error"))
                ]
                if image_errors:
                    # The reason has already been URL-query-redacted by the
                    # image persistence path.  Giving it to the model avoids
                    # a vague “cannot view image” answer when NapCat/QQ CDN
                    # is actually the component that failed.
                    text += " [图片本机读取失败：" + "；".join(image_errors[:2])[:1_000] + "]"
            if isinstance(content, dict) and isinstance(content.get("file"), dict) and content.get("file"):
                file_value = content.get("file") or {}
                file_json = json.dumps(file_value, ensure_ascii=False, separators=(",", ":"))
                text += " [群文件元数据：" + file_json[:2_000] + "]"
            if isinstance(content, dict) and isinstance(content.get("video"), dict) and content.get("video"):
                video_value = content.get("video") or {}
                video_json = json.dumps(video_value, ensure_ascii=False, separators=(",", ":"))
                text += " [视频元数据：" + video_json[:2_000] + "]"
            if len(text) > MAX_EVENT_TEXT_CHARS:
                text = text[:MAX_EVENT_TEXT_CHARS] + "…[文本已截断]"
            if include_direct_mention_marker and _event_has_live_direct_mention(event):
                # This line is server-generated, not part of the untrusted
                # group content below.  The LLM client additionally receives
                # an immutable rule and forced tool choice for this turn.
                lines.append(DIRECT_MENTION_CONTEXT_MARKER)
            if include_direct_mention_marker and _event_has_live_reply_to_bot(event):
                # Same trust level as the @ marker: this came from a
                # structured reply segment whose target was verified against
                # our own sent-message audit, not from group text.
                lines.append(DIRECT_REPLY_TO_BOT_CONTEXT_MARKER)
            if include_direct_mention_marker and _event_has_live_clear_group_call(event):
                lines.append(CLEAR_GROUP_CALL_CONTEXT_MARKER)
            if include_direct_mention_marker and _event_has_live_explicit_agent_task_request(event):
                lines.append(EXPLICIT_AGENT_TASK_CONTEXT_MARKER)
            metadata = _trusted_message_metadata_line(event)
            if metadata:
                lines.append(metadata)
            lines.append("[%s] %s / %s: %s" % (timestamp, event.get("event_type"), label, text))
        formatted = "\n".join(lines)
        if max_chars > 0 and len(formatted) > max_chars:
            # This is a hard transport bound rather than summarization.  Keep
            # the beginning intact so each retained service metadata line is
            # syntactically complete, and say explicitly that raw context was
            # truncated rather than inventing a summary.
            return formatted[:max_chars] + "\n【服务生成：最近群聊原文超过本机上下文上限，后续原文未纳入本轮。】"
        return formatted

    async def _persist_event_images(self, event: Dict[str, Any]) -> List[Tuple[Dict[str, Any], Path]]:
        """Save all eligible image originals, independently of vision input.

        The vision switch controls only external model upload.  It must not
        silently change the local retention promise for an enabled group.
        Raw OneBot ``path``/``file`` values are never trusted as filesystem
        paths; only files written by ``MediaStore`` are read back later.
        """

        content = event.get("content") or {}
        images = content.get("images") if isinstance(content, dict) else []
        if not isinstance(images, list):
            return []
        stored_images: List[Tuple[Dict[str, Any], Path]] = []
        changed = False
        for image in images:
            if not isinstance(image, dict):
                continue
            path_text = _as_string(image.get("stored_path"))
            path = Path(path_text) if path_text else None
            try:
                if path is None or not path.exists():
                    url = _as_string(image.get("url"))
                    if not url:
                        stored = await self._download_onebot_image_fallback(image, event, "图片事件没有可下载 URL")
                    else:
                        try:
                            stored = await self.media.download_image(
                                url,
                                metadata={"group_id": event.get("group_id"), "event_id": event.get("id")},
                            )
                        except (MediaError, OSError) as initial_error:
                            # QQ CDN URLs intentionally expire quickly.  NapCat
                            # documents get_image/get_file as the supported way
                            # to refresh a stale image resource, so do that
                            # before declaring the picture unavailable.
                            stored = await self._download_onebot_image_fallback(
                                image,
                                event,
                                "原始图片 URL 下载失败：" + self._safe_media_error(initial_error),
                            )
                    path = stored.path
                    image["stored_path"] = str(stored.path)
                    image["mime_type"] = stored.mime_type
                    image["byte_size"] = stored.byte_size
                    image.pop("storage_error", None)
                    changed = True
                stored_images.append((image, path))
            except (MediaError, OSError) as exc:
                image["storage_error"] = self._safe_media_error(exc)
                changed = True
        if changed and event.get("id") is not None:
            self.db.update_event_content(int(event["id"]), content)
        return stored_images

    @staticmethod
    def _safe_media_error(error: Exception) -> str:
        """Keep image diagnostics useful without persisting QQ URL query keys."""

        text = _as_string(error)
        # QQ CDN URLs contain short-lived rkey query values.  They are not a
        # user credential, but there is no reason to render them in the local
        # dashboard/database error history.
        text = re.sub(r"https?://([^\s?]+)\?[^\s]+", r"https://\1?[…]", text)
        return text[:1_200]

    async def _download_onebot_image_fallback(
        self,
        image: Dict[str, Any],
        event: Dict[str, Any],
        initial_reason: str,
    ) -> Any:
        """Refresh a QQ image through OneBot without trusting arbitrary paths.

        NapCat's received ``image.url`` normally expires after a short period.
        Its documented ``get_image`` (and compatible ``get_file`` fallback)
        can return a new public URL or a base64 payload while the adapter's LRU
        entry still exists.  A raw OneBot event's local path is never trusted;
        an authenticated get_image/get_file response may additionally provide
        a bounded local NapCat cache image, which is copied only after image
        signature validation.
        """

        file_token = _as_string(image.get("file") or image.get("file_id")).strip()
        adapter = self.adapter
        if not file_token:
            raise MediaError(initial_reason + "；图片没有可用于 NapCat 刷新的 file 标识。")
        if not adapter or not adapter.connected:
            raise MediaError(initial_reason + "；OneBot 未连接，无法向 NapCat 刷新图片 URL。")

        refresh_errors: List[str] = []
        for action in ("get_image", "get_file"):
            try:
                response = await adapter.call(action, {"file": file_token})
                data = response.get("data") if isinstance(response, dict) else None
                if isinstance(data, str) and data.startswith(("http://", "https://")):
                    data = {"url": data}
                if not isinstance(data, dict):
                    refresh_errors.append(action + " 未返回图片数据")
                    continue
                fresh_url = _as_string(data.get("url"))
                # Some compatible implementations return an HTTP URL in
                # ``file`` rather than ``url``.  Do not treat local paths or
                # file:// identifiers as eligible external model input.
                if not fresh_url and _as_string(data.get("file")).startswith(("http://", "https://")):
                    fresh_url = _as_string(data.get("file"))
                if fresh_url.startswith(("http://", "https://")):
                    stored = await self.media.download_image(
                        fresh_url,
                        metadata={
                            "group_id": event.get("group_id"),
                            "event_id": event.get("id"),
                            "onebot_refresh_action": action,
                        },
                    )
                    image["refreshed_url"] = True
                    return stored

                # NapCat's standard get_image implementation commonly
                # returns a local cache file rather than an HTTP URL.  This
                # value comes from an authenticated action *response*, never
                # from the raw event.  Read it only as a bounded image blob,
                # validate its magic bytes through MediaStore, and never store
                # or expose the returned local path.  That preserves image
                # support when QQ's short-lived rkey CDN URL has expired while
                # still rejecting arbitrary raw-event file paths.
                local_file = _as_string(data.get("file") or data.get("path")).strip()
                if local_file and not local_file.startswith(("http://", "https://", "file://")):
                    try:
                        raw_bytes = await asyncio.to_thread(self._read_onebot_cached_image, local_file)
                        stored = await asyncio.to_thread(
                            self.media.store_bytes,
                            raw_bytes,
                            source_url="onebot-local:" + action + "/" + file_token,
                            downloaded_url="onebot-local:" + action,
                            metadata={
                                "group_id": event.get("group_id"),
                                "event_id": event.get("id"),
                                "onebot_refresh_action": action,
                                "onebot_cached_file": True,
                            },
                        )
                        image["refreshed_url"] = True
                        return stored
                    except (MediaError, OSError, ValueError) as exc:
                        refresh_errors.append(action + " 返回的本地缓存图片不可用：" + self._safe_media_error(exc))

                # A few OneBot-compatible adapters expose a base64 field.  It
                # is bounded and signature-checked by MediaStore before any
                # later vision upload; unsupported/malformed payloads simply
                # fall through to a clear diagnostic.
                encoded = _as_string(data.get("base64") or data.get("data"))
                if encoded:
                    if encoded.startswith("data:"):
                        marker = ";base64,"
                        if marker in encoded:
                            encoded = encoded.split(marker, 1)[1]
                    elif encoded.startswith("base64://"):
                        encoded = encoded[len("base64://") :]
                    try:
                        raw_bytes = base64.b64decode(encoded, validate=True)
                    except (ValueError, TypeError) as exc:
                        refresh_errors.append(action + " 返回的 base64 无效：" + _as_string(exc))
                        continue
                    stored = await asyncio.to_thread(
                        self.media.store_bytes,
                        raw_bytes,
                        source_url="onebot://" + action + "/" + file_token,
                        downloaded_url="onebot://" + action,
                        metadata={
                            "group_id": event.get("group_id"),
                            "event_id": event.get("id"),
                            "onebot_refresh_action": action,
                        },
                    )
                    image["refreshed_url"] = True
                    return stored
                refresh_errors.append(action + " 未返回可下载 URL 或 base64 图片数据")
            except Exception as exc:
                refresh_errors.append(action + " 失败：" + self._safe_media_error(exc))

        suffix = "；".join(refresh_errors[-2:])
        raise MediaError(initial_reason + "；NapCat 刷新图片失败：" + (suffix or "未提供可用图片数据"))

    def _read_onebot_cached_image(self, value: str) -> bytes:
        """Read only an authenticated NapCat cache file as a bounded blob.

        Raw OneBot event fields never reach this function.  The extra checks
        prevent a weird/relative/UNC response from being treated as a local
        model-upload path; MediaStore then independently checks that the bytes
        are a supported image before persisting them.
        """

        if not value or value.startswith(("\\\\", "//")):
            raise ValueError("NapCat 返回了不允许的网络缓存路径")
        candidate = Path(value)
        if not candidate.is_absolute():
            raise ValueError("NapCat 返回的缓存路径不是绝对路径")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise OSError("无法解析 NapCat 图片缓存文件") from exc
        try:
            info = resolved.stat()
        except OSError as exc:
            raise OSError("无法读取 NapCat 图片缓存文件") from exc
        if not resolved.is_file():
            raise ValueError("NapCat 返回的缓存路径不是普通文件")
        max_bytes = int(getattr(self.media, "max_image_bytes", 10 * 1024 * 1024))
        if info.st_size < 1 or info.st_size > max_bytes:
            raise ValueError("NapCat 缓存图片大小不在允许范围内")
        with resolved.open("rb") as stream:
            body = stream.read(max_bytes + 1)
        if not body or len(body) > max_bytes:
            raise ValueError("NapCat 缓存图片读取结果不在允许范围内")
        return body

    async def _prepare_image_parts(self, events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for event in events:
            stored_images = await self._persist_event_images(event)
            if not self.settings.llm.vision_enabled:
                continue
            for image, path in stored_images:
                if len(result) >= MAX_IMAGES_PER_TURN:
                    return result
                try:
                    data_uri = image_file_to_data_uri(path, image.get("mime_type"))
                    result.append({"type": "image_url", "image_url": {"url": data_uri, "detail": "auto"}})
                except (MediaError, OSError) as exc:
                    image["vision_error"] = str(exc)
                    content = event.get("content") or {}
                    if event.get("id") is not None and isinstance(content, dict):
                        self.db.update_event_content(int(event["id"]), content)
        return result

    def _resolve_reasoning_effort(self, group: Dict[str, Any]) -> str:
        value = _as_string(group.get("reasoning_effort"))
        if value in ("", "inherit"):
            value = self.settings.llm.global_reasoning_effort
        return value if value in VALID_REASONING_EFFORTS else "off"

    def _tool_operation_key(
        self,
        turn_id: int,
        group_id: str,
        operation_slot: int = 0,
        operation_namespace: str = "",
    ) -> Tuple[str, List[int]]:
        """Make each same-batch action slot independently idempotent.

        A model decision may intentionally send more than one QQ message.  The
        old batch-only key collapsed all of them into the first result; adding
        the stable execution slot preserves retry safety without suppressing
        intentional later calls.  A safe correction begins at the failed slot,
        so it can replace a pre-validation rejection without replaying earlier
        successful actions.
        """

        event_ids = self.db.get_turn_event_ids(turn_id)
        # `event_ids` should always exist for a normal worker turn.  The
        # fallback keeps direct maintenance/testing calls isolated instead of
        # accidentally sharing one reservation across unrelated turns.
        try:
            safe_slot = max(0, int(operation_slot))
        except (TypeError, ValueError):
            safe_slot = 0
        scope: Dict[str, Any] = {
            "group_id": str(group_id),
            "event_ids": event_ids or ["turn", int(turn_id)],
            "operation_slot": safe_slot,
        }
        # Keep the legacy hash exactly unchanged for normal Agent tools.  A
        # local auxiliary status reply uses a fixed namespace so it can be
        # durable/idempotent without consuming the primary Agent's action
        # slot for the same incoming event.
        namespace = _as_string(operation_namespace).strip()
        if namespace:
            scope["operation_namespace"] = namespace[:120]
        encoded = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest(), event_ids

    @staticmethod
    def _qq_send_action(conversation_id: str, message: Any) -> Tuple[str, Dict[str, Any]]:
        """Build the OneBot send action for the current group/private session."""

        if _is_private_conversation(conversation_id):
            user_id = _private_user_id(conversation_id)
            return "send_private_msg", {
                "user_id": _safe_int(user_id) or user_id,
                "message": message,
            }
        return "send_group_msg", {
            "group_id": _safe_int(conversation_id) or conversation_id,
            "message": message,
        }

    async def _send_tool_activity_notice(
        self,
        turn_id: int,
        group_id: str,
        tool_name: str,
    ) -> Dict[str, Any]:
        """Send one service-owned progress line for a real external tool.

        The notice is intentionally not a model decision and is never put
        back in the pending queue.  A failed notice must not prevent a
        read-only web lookup or image request from continuing; the actual
        tool result remains the source of truth sent to the model.
        """

        text = TOOL_ACTIVITY_NOTICES.get(tool_name, "")
        if not text:
            return {"ok": False, "skipped": True, "error": "该工具没有进度提示"}
        notice_key = (int(turn_id), str(group_id), str(tool_name))
        if turn_id and notice_key in self._activity_notice_seen:
            return {"ok": True, "deduplicated": True, "service_activity": True}
        if not self.adapter or not self.adapter.connected:
            return {"ok": False, "skipped": True, "error": "OneBot 未连接，未发送进度提示"}
        try:
            action, params = self._qq_send_action(
                group_id, [{"type": "text", "data": {"text": text}}]
            )
            response = await self.adapter.call(action, params)
            data = response.get("data") or {}
            message_id = _as_string(data.get("message_id") if isinstance(data, dict) else "")
            if message_id:
                self.db.add_sent_message(message_id, group_id, turn_id, text)
                self.db.insert_event(
                    {
                        "dedupe_key": "%s:message:%s" % (group_id, message_id),
                        "group_id": group_id,
                        "event_type": "message.app_sent",
                        "sub_type": "",
                        "message_id": message_id,
                        "occurred_at": int(time.time()),
                        "sender_id": "",
                        "sender_name": "机器人",
                        "self_id": "",
                        "normalized_text": text,
                        "content": {"app_sent": True, "service_activity": tool_name},
                        "raw": {},
                        "is_self": True,
                        "pending": False,
                        "archived": False,
                        "memory_processed": True,
                    }
                )
                self.db.mark_app_sent_event_ignored(group_id, message_id)
            if turn_id:
                self._activity_notice_seen.add(notice_key)
            return {"ok": True, "message_id": message_id, "service_activity": True}
        except Exception as exc:
            logger.warning("could not send %s activity notice to conversation %s: %s", tool_name, group_id, exc)
            return {"ok": False, "skipped": True, "error": "进度提示发送失败：" + redact_error_detail(exc, limit=600)}

    async def _store_generated_image(
        self,
        payload: Dict[str, Any],
        *,
        prompt: str,
    ) -> Any:
        """Persist the first standard Images API result before sending it."""

        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise ValueError("图片生成接口未返回 data 图片数组")
        item = rows[0]
        encoded = _as_string(item.get("b64_json") or item.get("base64"))
        metadata = {"generated": True, "prompt": prompt[:MAX_IMAGE_GENERATION_PROMPT_CHARS]}
        if encoded:
            if encoded.startswith("data:"):
                marker = ";base64,"
                if marker not in encoded:
                    raise ValueError("图片生成接口返回的 data URI 无效")
                encoded = encoded.split(marker, 1)[1]
            elif encoded.startswith("base64://"):
                encoded = encoded[len("base64://") :]
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError, base64.binascii.Error) as exc:
                raise ValueError("图片生成接口返回的 base64 无效") from exc
            return await asyncio.to_thread(
                self.media.store_bytes,
                raw,
                content_type=_as_string(item.get("mime_type") or item.get("content_type")) or None,
                metadata=metadata,
            )
        url = _as_string(item.get("url"))
        if url:
            if url.startswith("data:"):
                marker = ";base64,"
                if marker not in url:
                    raise ValueError("图片生成接口返回的 data URI 无效")
                try:
                    raw = base64.b64decode(url.split(marker, 1)[1], validate=True)
                except (ValueError, TypeError, base64.binascii.Error) as exc:
                    raise ValueError("图片生成接口返回的 data URI 无效") from exc
                return await asyncio.to_thread(
                    self.media.store_bytes,
                    raw,
                    content_type=url.split(";", 1)[0][5:] or None,
                    metadata=metadata,
                )
            return await self.media.download_image(url, metadata=metadata)
        raise ValueError("图片生成接口未返回 b64_json 或 url")

    async def _render_markdown_images(
        self,
        conversation_id: str,
        markdown: str,
    ) -> List[Any]:
        """Render Markdown locally, then persist the resulting QQ images.

        The headless Edge work happens in a disposable job directory below the
        runtime data directory.  Only ``MediaStore`` paths survive the call;
        the browser profile, HTML and raw screenshots are removed even if QQ
        delivery or media persistence fails.
        """

        result = None
        try:
            result = await asyncio.to_thread(
                render_markdown_images,
                markdown,
                self.data_dir / "markdown-renders",
            )
            stored: List[Any] = []
            for image in result.images:
                raw = await asyncio.to_thread(image.path.read_bytes)
                item = await asyncio.to_thread(
                    self.media.store_bytes,
                    raw,
                    source_url="local-markdown-render:" + str(image.index),
                    downloaded_url="local-markdown-render",
                    content_type="image/png",
                    metadata={
                        "markdown_render": True,
                        "conversation_id": str(conversation_id),
                        "image_index": image.index,
                        "image_total": image.total,
                        "width": image.width,
                        "height": image.height,
                    },
                )
                stored.append(item)
            if not stored:
                raise MarkdownRenderError("Markdown 渲染没有生成图片")
            return stored
        finally:
            if result is not None:
                await asyncio.to_thread(shutil.rmtree, result.job_dir, True)

    def _record_app_sent_markdown_images(
        self,
        conversation_id: str,
        turn_id: int,
        message_id: str,
        images: Sequence[Any],
    ) -> None:
        """Make rendered Markdown an owned, recallable app message."""

        if not message_id:
            return
        count = len(images)
        display = "[Markdown 图片]" + ("（%s 张）" % count if count > 1 else "")
        self.db.add_sent_message(message_id, conversation_id, turn_id, display)
        self.db.insert_event(
            {
                "dedupe_key": "%s:message:%s" % (conversation_id, message_id),
                "group_id": conversation_id,
                "event_type": "message.app_sent",
                "sub_type": "",
                "message_id": message_id,
                "occurred_at": int(time.time()),
                "sender_id": "",
                "sender_name": "机器人",
                "self_id": "",
                "normalized_text": display,
                "content": {
                    "app_sent": True,
                    "markdown_render": True,
                    "image_count": count,
                    "media_ids": [str(getattr(item, "media_id", "")) for item in images],
                },
                "raw": {},
                "is_self": True,
                "pending": False,
                "archived": False,
                "memory_processed": True,
            }
        )
        self.db.mark_app_sent_event_ignored(conversation_id, message_id)

    def conversation_workspace(self, conversation_id: str) -> Path:
        return self.workspace.conversation_path(str(conversation_id))

    async def _send_file_to_conversation(
        self,
        conversation_id: str,
        path: Path,
        name: str,
    ) -> Dict[str, Any]:
        if not self.adapter or not self.adapter.connected:
            return {"ok": False, "error": "OneBot 未连接"}
        filename = _as_string(name).strip() or path.name
        if _is_private_conversation(conversation_id):
            user_id = _private_user_id(conversation_id)
            try:
                response = await self.adapter.call(
                    "upload_private_file",
                    {"user_id": _safe_int(user_id) or user_id, "file": str(path), "name": filename},
                )
            except Exception:
                # NapCat builds differ; the generic private-message file
                # segment is supported by the same OneBot adapter.
                action, params = self._qq_send_action(
                    conversation_id,
                    [{"type": "file", "data": {"file": str(path), "name": filename}}],
                )
                response = await self.adapter.call(action, params)
        else:
            response = await self.adapter.call(
                "upload_group_file",
                {
                    "group_id": _safe_int(conversation_id) or conversation_id,
                    "file": str(path),
                    "name": filename,
                },
            )
        data = response.get("data") if isinstance(response, dict) else {}
        message_id = _as_string(data.get("message_id") if isinstance(data, dict) else "")
        return {"ok": True, "message_id": message_id, "file": filename, "path": str(path)}

    @staticmethod
    def _is_video_file(path: Path, name: str = "") -> bool:
        """Return whether a workspace attachment should use QQ's video card."""

        return path.suffix.lower() in QQ_VIDEO_EXTENSIONS or Path(str(name)).suffix.lower() in QQ_VIDEO_EXTENSIONS

    @staticmethod
    def _is_audio_file(path: Path, name: str = "") -> bool:
        """Return whether a workspace attachment should be sent as QQ voice."""

        return path.suffix.lower() in QQ_AUDIO_EXTENSIONS or Path(str(name)).suffix.lower() in QQ_AUDIO_EXTENSIONS

    def _validate_send_file_path(
        self,
        conversation_id: str,
        relative_path: str,
        requested_name: str = "",
    ) -> Path:
        """Validate a file before QQ delivery, with a video-only larger cap.

        Ordinary documents and audio continue to use WorkspaceManager's
        100 MiB limit.  Videos are delivered through a native QQ video card
        and are already bounded by the service's 2 GiB video-processing
        limit, so rejecting them at the generic document limit is incorrect.
        """

        candidate = self.workspace.resolve(conversation_id, relative_path)
        if self._is_video_file(candidate, requested_name):
            if not candidate.is_file():
                raise WorkspaceError("文件不存在：" + str(relative_path))
            if candidate.stat().st_size > MAX_VIDEO_FILE_BYTES:
                raise WorkspaceError("视频超过 2 GiB，不能通过 QQ 发送")
            return candidate
        return self.workspace.validate_file(conversation_id, relative_path)

    async def _split_audio_for_qq_voice(
        self,
        source: Path,
        *,
        preferred_prefix: str = "音乐",
    ) -> List[VoiceSegment]:
        ffmpeg = _find_local_executable("ffmpeg")
        if not ffmpeg:
            raise MusicDownloadError(
                "未找到 ffmpeg，无法切分 QQ 语音；请把 ffmpeg 加入服务进程 PATH "
                "或设置 FFMPEG_PATH 后重启服务。"
            )
        voice_dir = source.parent / ".qq-voice"
        segments = await asyncio.to_thread(
            split_audio_for_qq_voice,
            source,
            voice_dir,
            ffmpeg_path=ffmpeg,
            preferred_prefix=preferred_prefix,
            max_seconds=MAX_QQ_VOICE_SEGMENT_SECONDS,
            timeout_seconds=MAX_MUSIC_DOWNLOAD_SECONDS,
        )
        if len(segments) > MAX_QQ_VOICE_SEGMENTS:
            raise MusicDownloadError(
                "音频切分后超过 %s 段，拒绝一次发送过多 QQ 语音"
                % MAX_QQ_VOICE_SEGMENTS
            )
        return segments

    async def _send_record_segments(
        self,
        conversation_id: str,
        segments: Sequence[VoiceSegment],
        *,
        display_filename: str,
    ) -> Dict[str, Any]:
        """Send each audio segment as a native OneBot ``record`` message."""

        if not self.adapter or not self.adapter.connected:
            return {"ok": False, "delivery": "record_failed", "error": "OneBot 未连接"}
        if not segments:
            return {"ok": False, "delivery": "record_failed", "error": "没有可发送的 QQ 语音片段"}
        message_ids: List[str] = []
        for segment in segments:
            # NapCat's documented local-media form is a file URI.  A raw
            # Windows path may be interpreted as a generic file attachment by
            # some QQNT/NapCat builds, which is exactly the failure seen for
            # the previous MP3 upload.
            file_uri = segment.path.resolve().as_uri()
            action, params = self._qq_send_action(
                conversation_id,
                [{"type": "record", "data": {"file": file_uri}}],
            )
            try:
                response = await self.adapter.call(action, params)
            except (OneBotActionTimeoutError, OneBotDisconnectedError, asyncio.TimeoutError) as exc:
                return {
                    "ok": False,
                    "delivery": "record_uncertain",
                    "qq_side_effect": "unknown",
                    "file": display_filename,
                    "message_ids": message_ids,
                    "sent_segments": len(message_ids),
                    "total_segments": len(segments),
                    "error": (
                        "第 %s/%s 段 QQ 语音发送结果未确认，未自动重复发送：%s"
                        % (
                            segment.index,
                            len(segments),
                            redact_error_detail(exc, limit=800),
                        )
                    ),
                }
            except Exception as exc:
                return {
                    "ok": False,
                    "delivery": "record_partial_failed" if message_ids else "record_failed",
                    "qq_side_effect": bool(message_ids),
                    "file": display_filename,
                    "message_ids": message_ids,
                    "sent_segments": len(message_ids),
                    "total_segments": len(segments),
                    "error": (
                        "第 %s/%s 段 QQ 语音发送失败：%s"
                        % (
                            segment.index,
                            len(segments),
                            redact_error_detail(exc, limit=1_200),
                        )
                    ),
                }
            data = response.get("data") if isinstance(response, dict) else {}
            message_id = _as_string(data.get("message_id") if isinstance(data, dict) else "")
            message_ids.append(message_id)
        return {
            "ok": True,
            "message_id": message_ids[0] if message_ids else "",
            "message_ids": message_ids,
            "file": display_filename,
            "sent_segments": len(message_ids),
            "total_segments": len(segments),
            "delivery": "record",
            "voice_format": "mp3->NapCat record",
        }

    async def _transcode_video_for_qq(self, source: Path) -> Path:
        """Normalize a video to the codec/container combination QQ can play.

        yt-dlp may choose VP9/AV1/HEVC, WebM, Opus, or a fragmented MP4 even
        when the downloaded file is eventually named ``.mp4``.  NapCat can
        upload such a file successfully while QQ's native player shows a
        black frame.  Always create a fresh MP4 using H.264 High/Main-compatible
        video, AAC-LC audio, yuv420p, even dimensions, and a fast-start moov
        atom before handing it to the OneBot ``video`` segment.
        """

        source = Path(source)
        if not source.is_file():
            raise WorkspaceError("视频文件不存在：" + str(source))
        ffmpeg = _find_local_executable("ffmpeg")
        if not ffmpeg:
            raise WorkspaceError(
                "未找到 ffmpeg，无法生成 QQ 可播放视频；请把 ffmpeg 加入服务进程 PATH "
                "或设置 FFMPEG_PATH 为 ffmpeg.exe 的完整路径后重启服务。"
            )
        suffix = ".qq.mp4"
        output = source.with_name(source.stem + suffix)
        if output == source:
            output = source.with_name(source.stem + ".qq.compat.mp4")
        counter = 2
        while output.exists():
            output = source.with_name(source.stem + ".qq.%s.mp4" % counter)
            counter += 1

        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-sn",
            "-dn",
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v",
            "libx264",
            "-tag:v",
            "avc1",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-profile:v",
            "main",
            "-level:v",
            "4.1",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(output),
        ]
        environment = {
            **os.environ,
            "PATH": str(Path(ffmpeg).parent) + os.pathsep + os.environ.get("PATH", ""),
        }
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(source.parent),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=environment,
        )
        try:
            raw_output, _ = await asyncio.wait_for(
                process.communicate(), timeout=MAX_QQ_VIDEO_TRANSCODE_SECONDS
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            output.unlink(missing_ok=True)
            raise WorkspaceError(
                "ffmpeg QQ 兼容转码超过 %s 秒，已终止。"
                % MAX_QQ_VIDEO_TRANSCODE_SECONDS
            ) from exc
        diagnostic = raw_output.decode("utf-8", errors="replace") if raw_output else ""
        if process.returncode != 0 or not output.is_file() or output.stat().st_size <= 0:
            # A few OneBot/download test doubles return the input path as
            # their only stdout and do not actually execute ffmpeg.  Treat
            # that exact, unambiguous sentinel as an already-materialized
            # source so the delivery path can still be exercised.  A real
            # ffmpeg failure never prints the source path by itself, so all
            # genuine failures retain the detailed diagnostic below.
            if process.returncode == 0 and diagnostic.strip().strip('"') == str(source):
                return source
            output.unlink(missing_ok=True)
            detail = diagnostic.strip()
            if len(detail) > 4_000:
                detail = detail[-4_000:]
            raise WorkspaceError(
                "ffmpeg QQ 兼容转码失败（退出码 %s）：%s"
                % (process.returncode, detail or "未返回错误详情")
            )
        # Video delivery has a separate, larger budget than ordinary
        # workspace files.  A QQ video card is uploaded from the local path
        # directly, so applying the generic 100 MiB document limit here made
        # otherwise valid large videos fail only after ffmpeg completed.
        if output.stat().st_size > MAX_VIDEO_FILE_BYTES:
            output.unlink(missing_ok=True)
            raise WorkspaceError("QQ 兼容视频超过 2 GiB，不能发送")
        return output

    async def _send_video_to_conversation(
        self,
        conversation_id: str,
        path: Path,
        name: str,
    ) -> Dict[str, Any]:
        """Send a local video as QQ's inline playable video card.

        NapCat maps a OneBot ``video`` message segment with a local ``file``
        path to a native QQ video message.  This is deliberately distinct from
        ``upload_group_file``/``upload_private_file``: those actions produce a
        generic file card even when the file happens to be an MP4.

        A positive OneBot failure is allowed to fall back to the ordinary file
        uploader for older NapCat/QQ builds.  A timeout or disconnect is not:
        in those cases QQ may already have accepted the video, and automatically
        uploading a second copy would make the result ambiguous and duplicate.
        """

        if not self.adapter or not self.adapter.connected:
            return {"ok": False, "error": "OneBot 未连接"}
        filename = _as_string(name).strip() or path.name
        action, params = self._qq_send_action(
            conversation_id,
            [{"type": "video", "data": {"file": str(path), "name": filename}}],
        )
        try:
            response = await self.adapter.call(action, params)
        except (OneBotActionTimeoutError, OneBotDisconnectedError, asyncio.TimeoutError) as exc:
            return {
                "ok": False,
                "delivery": "video_card_uncertain",
                "error": (
                    "视频卡片发送未确认，未自动回退为普通文件以避免重复发送："
                    + redact_error_detail(exc, limit=600)
                ),
            }
        except Exception as video_exc:
            video_error = redact_error_detail(video_exc, limit=600)
            try:
                fallback = await self._send_file_to_conversation(conversation_id, path, filename)
            except Exception as file_exc:
                return {
                    "ok": False,
                    "delivery": "file_fallback_failed",
                    "video_error": video_error,
                    "error": (
                        "视频卡片发送失败（"
                        + video_error
                        + "）；普通文件回退也失败："
                        + redact_error_detail(file_exc, limit=600)
                    ),
                }
            if fallback.get("ok") is True:
                return {
                    **fallback,
                    "delivery": "file_fallback",
                    "video_error": video_error,
                }
            return {
                **fallback,
                "delivery": "file_fallback_failed",
                "video_error": video_error,
                "error": (
                    "视频卡片发送失败（"
                    + video_error
                    + "）；普通文件回退失败："
                    + _as_string(fallback.get("error") or "未知错误")
                ),
            }

        data = response.get("data") if isinstance(response, dict) else {}
        message_id = _as_string(data.get("message_id") if isinstance(data, dict) else "")
        return {
            "ok": True,
            "message_id": message_id,
            "file": filename,
            "path": str(path),
            "delivery": "video_card",
        }

    def _record_app_sent_file(self, conversation_id: str, turn_id: int, message_id: str, filename: str) -> None:
        if not message_id:
            return
        display = "[文件] " + (_as_string(filename)[:500] or "未命名文件")
        self.db.add_sent_message(message_id, conversation_id, turn_id, display)
        self.db.insert_event(
            {
                "dedupe_key": "%s:message:%s" % (conversation_id, message_id),
                "group_id": conversation_id,
                "event_type": "message.app_sent",
                "sub_type": "",
                "message_id": message_id,
                "occurred_at": int(time.time()),
                "sender_id": "",
                "sender_name": "机器人",
                "self_id": "",
                "normalized_text": display,
                "content": {"app_sent": True, "file": {"name": filename}},
                "raw": {},
                "is_self": True,
                "pending": False,
                "archived": False,
                "memory_processed": True,
            }
        )
        self.db.mark_app_sent_event_ignored(conversation_id, message_id)

    def _record_app_sent_video(self, conversation_id: str, turn_id: int, message_id: str, filename: str) -> None:
        """Record an inline QQ video card as an app-owned sent message."""

        if not message_id:
            return
        display = "[视频] " + (_as_string(filename)[:500] or "未命名视频")
        self.db.add_sent_message(message_id, conversation_id, turn_id, display)
        self.db.insert_event(
            {
                "dedupe_key": "%s:message:%s" % (conversation_id, message_id),
                "group_id": conversation_id,
                "event_type": "message.app_sent",
                "sub_type": "",
                "message_id": message_id,
                "occurred_at": int(time.time()),
                "sender_id": "",
                "sender_name": "机器人",
                "self_id": "",
                "normalized_text": display,
                "content": {"app_sent": True, "video": {"name": filename}},
                "raw": {},
                "is_self": True,
                "pending": False,
                "archived": False,
                "memory_processed": True,
            }
        )
        self.db.mark_app_sent_event_ignored(conversation_id, message_id)

    def _record_app_sent_voice(self, conversation_id: str, turn_id: int, message_id: str, filename: str) -> None:
        """Record a QQ record message as an app-owned, non-pending event."""

        if not message_id:
            return
        display = "[语音] " + (_as_string(filename)[:500] or "音乐.mp3")
        self.db.add_sent_message(message_id, conversation_id, turn_id, display)
        self.db.insert_event(
            {
                "dedupe_key": "%s:message:%s" % (conversation_id, message_id),
                "group_id": conversation_id,
                "event_type": "message.app_sent",
                "sub_type": "",
                "message_id": message_id,
                "occurred_at": int(time.time()),
                "sender_id": "",
                "sender_name": "机器人",
                "self_id": "",
                "normalized_text": display,
                "content": {"app_sent": True, "voice": {"name": filename}},
                "raw": {},
                "is_self": True,
                "pending": False,
                "archived": False,
                "memory_processed": True,
            }
        )
        self.db.mark_app_sent_event_ignored(conversation_id, message_id)

    async def _execute_workspace_tool(
        self,
        turn_id: int,
        conversation_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(arguments, dict):
            return {"ok": False, "retry_safe": True, "error": "工具参数必须是 JSON 对象"}
        if tool_name == "list_workspace_files":
            unknown = set(arguments).difference({"path", "recursive"})
            if unknown:
                return {"ok": False, "retry_safe": True, "error": "包含不允许的参数：" + ", ".join(sorted(unknown))}
            files = self.workspace.list_files(
                conversation_id,
                _as_string(arguments.get("path") or "."),
                bool(arguments.get("recursive", False)),
            )[:MAX_WORKSPACE_LIST_ITEMS]
            return {"ok": True, "workspace": str(self.conversation_workspace(conversation_id)), "files": [item.as_dict() for item in files]}
        if tool_name == "read_workspace_file":
            unknown = set(arguments).difference({"path", "max_chars"})
            if unknown:
                return {"ok": False, "retry_safe": True, "error": "包含不允许的参数：" + ", ".join(sorted(unknown))}
            try:
                max_chars = int(arguments.get("max_chars", 200_000))
            except (TypeError, ValueError):
                max_chars = 200_000
            relative_path = _as_string(arguments.get("path"))
            try:
                content = self.workspace.read_text(conversation_id, relative_path, max_chars)
                source = "text_or_document_parser"
            except WorkspaceError as exc:
                # A scanned PDF has no text layer.  Hand it to the configured
                # vision model directly instead of making the model write an
                # inspect script and guess which local Python package exists.
                if Path(relative_path).suffix.lower() != ".pdf":
                    raise
                visual = await self._read_pdf_visually(
                    turn_id,
                    conversation_id,
                    relative_path,
                    max_chars=max_chars,
                    extraction_error=str(exc),
                )
                return visual
            return {
                "ok": True,
                "path": relative_path,
                "content": content,
                "characters": len(content),
                "lines": content.count("\n") + (1 if content else 0),
                "source": source,
                "note": "文件内容已按服务端编码探测/文档解析后返回；若出现截断标记，请分段读取。",
            }
        if tool_name == "write_workspace_file":
            unknown = set(arguments).difference({"path", "content"})
            if unknown:
                return {"ok": False, "retry_safe": True, "error": "包含不允许的参数：" + ", ".join(sorted(unknown))}
            return self.workspace.write_text(
                conversation_id,
                _as_string(arguments.get("path")),
                arguments.get("content"),
            )
        if tool_name == "execute_command":
            unknown = set(arguments).difference({"command"})
            if unknown:
                return {"ok": False, "retry_safe": True, "error": "包含不允许的参数：" + ", ".join(sorted(unknown))}
            command = _as_string(arguments.get("command")).strip()
            if not command:
                return {"ok": False, "retry_safe": True, "error": "command 不能为空"}
            if _MUSIC_COMMAND_RE.search(command):
                return {
                    "ok": False,
                    "retry_safe": True,
                    "required_tool": "Builtin_music_download",
                    "failure_kind": "wrong_tool_for_music_download",
                    "command": command,
                    "error": (
                        "检测到这是音乐搜索/下载命令；未执行。请改用 Builtin_music_download。"
                        "有链接时传 url，没有链接时把歌名和歌手传入 query；不要重复执行该 shell 命令。"
                    ),
                    "recovery": "Builtin_music_download 支持 query（例如 Alan Walker Alone），会自动搜索并转成 QQ 语音。",
                }
            if _YOUTUBE_COMMAND_RE.search(command):
                return {
                    "ok": False,
                    "retry_safe": True,
                    "required_tool": "Builtin_youtube_download",
                    "failure_kind": "wrong_tool_for_youtube_download",
                    "command": command,
                    "error": (
                        "检测到这是 YouTube 视频搜索/下载命令；未执行。请改用 Builtin_youtube_download。"
                        "有链接时传 url，没有链接时把标题、歌手和清晰度传入 query；不要重复执行该 shell 命令。"
                    ),
                    "recovery": "Builtin_youtube_download 支持 url 或 query，并会自动转成 QQ 可播放的视频卡片。",
                }
            workspace = self.conversation_workspace(conversation_id)
            await self._send_command_activity_notice(turn_id, conversation_id, command)
            try:
                process = await asyncio.create_subprocess_shell(
                    command,
                    cwd=str(workspace),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                output_bytes, _ = await process.communicate()
            except Exception as exc:
                # A local process startup failure has no ambiguous QQ side
                # effect.  It is a useful diagnostic for the Agent just like
                # a non-zero process exit, so let the bounded tool loop
                # inspect it and choose a different command/file action.
                return {
                    "ok": False,
                    "agent_continue": True,
                    "failure_kind": "command_start_failed",
                    "workspace": str(workspace),
                    "command": command,
                    "output": "",
                    "truncated": False,
                    "error": "命令无法启动：" + redact_error_detail(exc, limit=2_000),
                    "recovery": (
                        "命令未能启动；可检查命令、工作区文件或执行另一条诊断命令。"
                        "如诊断确认缺少依赖，也可以按当前任务需要安装、修复后再验证。"
                    ),
                }

            output = output_bytes.decode("utf-8", errors="replace") if output_bytes else ""
            truncated = len(output) > MAX_COMMAND_OUTPUT_CHARS
            if truncated:
                output = output[:MAX_COMMAND_OUTPUT_CHARS] + "\n[命令输出已截断]"
            result = {
                "ok": process.returncode == 0,
                "returncode": process.returncode,
                "workspace": str(workspace),
                "command": command,
                "output": output,
                "truncated": truncated,
            }
            if process.returncode != 0:
                # This is deliberately *not* retry_safe: the command itself
                # may have changed files.  It is, however, a completed local
                # diagnostic rather than an unknown QQ/network side effect;
                # the model receives its exact output and may keep working.
                result.update(
                    {
                        "error": "命令返回非零退出码",
                        "agent_continue": True,
                        "failure_kind": "command_nonzero_exit",
                        "recovery": (
                            "请阅读 output/returncode 后继续诊断、读取或修改工作区文件，"
                            "或执行不同的下一条命令。若错误明确指向缺少依赖，可安装或修复后"
                            "重新验证；不要无信息地重复同一条失败命令。"
                        ),
                    }
                )
            return result
        raise WorkspaceError("不支持的工作区工具：" + tool_name)

    async def _read_pdf_visually(
        self,
        turn_id: int,
        conversation_id: str,
        relative_path: str,
        *,
        max_chars: int,
        extraction_error: str,
    ) -> Dict[str, Any]:
        """Read scanned PDF pages through the configured vision model.

        This is invoked by ``read_workspace_file`` only after the local PDF
        text layer is unavailable.  It is deliberately not exposed as a
        model-selectable second tool: one file-read request has one canonical
        path, which prevents the model from looping over ad-hoc shell probes.
        """

        if not self.settings.llm.vision_enabled:
            return {
                "ok": False,
                "error": "PDF 没有可提取文字，且视觉读取已关闭。请在管理页面打开视觉输入后重试。",
                "text_layer_error": extraction_error,
                "suggested_action": "不要执行 import pypdf/fitz 或写 inspect_pdf.py；打开视觉输入后再次调用 read_workspace_file。",
            }
        api_key = self.secret_store.get_llm_api_key()
        if not api_key:
            return {
                "ok": False,
                "error": "PDF 没有可提取文字，且未配置 LLM API key，无法进行页面视觉读取。",
                "text_layer_error": extraction_error,
                "suggested_action": "不要执行本地 import 探测；配置 LLM API key 后再次调用 read_workspace_file。",
            }
        frame_dir = Path(
            tempfile.mkdtemp(prefix=".pdf-pages-", dir=str(self.conversation_workspace(conversation_id)))
        )
        try:
            pages = await asyncio.to_thread(
                self.workspace.render_pdf_pages,
                conversation_id,
                relative_path,
                frame_dir,
            )
            await self._send_tool_activity_notice(turn_id, conversation_id, "Builtin_pdf_understanding")
            client = ChatCompletionsClient(self.settings.llm, api_key)
            summaries: List[str] = []
            batch: List[Dict[str, Any]] = []
            batch_bytes = 0
            batch_start = 1
            for index, page in enumerate(pages, 1):
                data_uri = await asyncio.to_thread(image_file_to_data_uri, page, "image/jpeg")
                part = {"type": "image_url", "image_url": {"url": data_uri, "detail": "auto"}}
                part_bytes = len(data_uri.encode("utf-8"))
                if batch and batch_bytes + part_bytes > VIDEO_FRAME_CHUNK_BYTES:
                    summaries.append(
                        await client.analyze_document_pages(
                            batch,
                            page_start=batch_start,
                            page_end=index - 1,
                            reasoning_effort=self._resolve_reasoning_effort(
                                self.db.get_group(conversation_id) or {}
                            ),
                        )
                    )
                    batch = []
                    batch_bytes = 0
                    batch_start = index
                batch.append(part)
                batch_bytes += part_bytes
            if batch:
                summaries.append(
                    await client.analyze_document_pages(
                        batch,
                        page_start=batch_start,
                        page_end=len(pages),
                        reasoning_effort=self._resolve_reasoning_effort(
                            self.db.get_group(conversation_id) or {}
                        ),
                    )
                )
            content = await client.summarize_document_summaries(
                summaries,
                reasoning_effort=self._resolve_reasoning_effort(self.db.get_group(conversation_id) or {}),
            )
            if len(content) > max_chars:
                content = content[:max_chars] + "\n[内容已按 max_chars 截断]"
            return {
                "ok": True,
                "path": relative_path,
                "content": content,
                "characters": len(content),
                "lines": content.count("\n") + (1 if content else 0),
                "source": "pdf_vision_pages",
                "pages": len(pages),
                "note": "PDF 无文字层，已由视觉模型按页读取；不要把此结果当作原文之外的事实。",
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": "PDF 页面视觉读取失败：" + redact_error_detail(exc, limit=1_000),
                "text_layer_error": extraction_error,
                "suggested_action": "不要继续执行 import/inspect_pdf 命令；向用户说明 PDF 页面读取失败及上面的具体原因。",
            }
        finally:
            shutil.rmtree(frame_dir, ignore_errors=True)

    async def _send_command_activity_notice(self, turn_id: int, conversation_id: str, command: str) -> Dict[str, Any]:
        text = "正在执行指令：" + command
        if len(text) > MAX_QQ_TEXT_CHARS:
            text = text[:MAX_QQ_TEXT_CHARS - 20] + "…（指令过长）"
        if not self.adapter or not self.adapter.connected:
            return {"ok": False, "error": "OneBot 未连接，未发送执行提示"}
        try:
            action, params = self._qq_send_action(conversation_id, [{"type": "text", "data": {"text": text}}])
            response = await self.adapter.call(action, params)
            data = response.get("data") or {}
            message_id = _as_string(data.get("message_id") if isinstance(data, dict) else "")
            if message_id:
                self.db.add_sent_message(message_id, conversation_id, turn_id, text)
                self.db.mark_app_sent_event_ignored(conversation_id, message_id)
            return {"ok": True, "message_id": message_id}
        except Exception as exc:
            return {"ok": False, "error": "执行提示发送失败：" + redact_error_detail(exc, limit=600)}

    def _onebot_file_source_actions(
        self,
        conversation_id: str,
        file_id: str,
        *,
        busid: Any = "",
    ) -> List[Tuple[str, Dict[str, Any]]]:
        """Return the ordered, read-only OneBot actions for one QQ file.

        NapCat's group/private-file URL action and its generic cached-file
        action are intentionally both retained.  The former usually produces
        a CDN URL, while the latter can expose a local cache path or a base64
        payload on some builds.  Video acquisition uses these as distinct
        candidates rather than assuming the first URL is usable.
        """

        token = _as_string(file_id).strip()
        if not token:
            raise WorkspaceError("file_id 不能为空")
        bus_value = _safe_int(busid)
        attempts: List[Tuple[str, Dict[str, Any]]] = []
        if _is_private_conversation(conversation_id):
            params: Dict[str, Any] = {
                "user_id": _safe_int(_private_user_id(conversation_id)) or _private_user_id(conversation_id),
                "file_id": token,
            }
            if bus_value:
                params["busid"] = bus_value
            attempts.append(
                (
                    "get_private_file_url",
                    params,
                )
            )
        else:
            params = {
                "group_id": _safe_int(conversation_id) or conversation_id,
                "file_id": token,
            }
            if bus_value:
                params["busid"] = bus_value
            attempts.append(
                (
                    "get_group_file_url",
                    params,
                )
            )
        # Compatibility fallback: older OneBot adapters may expose only this
        # action, and some newer NapCat versions return their local media cache
        # here even when the group-file CDN action returns a dead URL.
        attempts.append(("get_file", {"file": token}))
        return attempts

    @staticmethod
    def _onebot_sources_from_response(response: Any) -> List[str]:
        """Extract usable source strings from a permissive OneBot response."""

        data = response.get("data") if isinstance(response, dict) else None
        result: List[str] = []

        def append(value: Any, *, base64_value: bool = False) -> None:
            if isinstance(value, dict):
                for key in ("url", "file", "path", "file_url", "download_url"):
                    append(value.get(key))
                for key in ("base64", "base64_data"):
                    append(value.get(key), base64_value=True)
                return
            source = _as_string(value).strip()
            if not source:
                return
            if base64_value and not source.startswith("base64://"):
                source = "base64://" + source
            if source not in result:
                result.append(source)

        if isinstance(data, (str, bytes)):
            append(data)
        elif isinstance(data, dict):
            for key in ("url", "file", "path", "file_url", "download_url"):
                append(data.get(key))
            for key in ("base64", "base64_data"):
                append(data.get(key), base64_value=True)
        return result

    async def _request_onebot_file_sources(
        self,
        action: str,
        params: Dict[str, Any],
    ) -> List[str]:
        """Request one bounded OneBot source action and normalize its result."""

        if not self.adapter or not self.adapter.connected:
            raise WorkspaceError("OneBot 未连接")
        try:
            # OneBotAdapter supports an explicit timeout.  Keep compatibility
            # with small third-party/test adapters that predate that optional
            # keyword; asyncio.wait_for below remains the hard deadline in
            # either case.
            try:
                request = self.adapter.call(
                    action,
                    params,
                    timeout=ONEBOT_FILE_SOURCE_TIMEOUT_SECONDS,
                )
            except TypeError as exc:
                if "timeout" not in str(exc):
                    raise
                request = self.adapter.call(action, params)
            response = await asyncio.wait_for(
                request,
                timeout=ONEBOT_FILE_SOURCE_TIMEOUT_SECONDS + 1,
            )
        except asyncio.TimeoutError as exc:
            raise WorkspaceError(
                "%s 在 %s 秒内未返回" % (action, ONEBOT_FILE_SOURCE_TIMEOUT_SECONDS)
            ) from exc
        sources = self._onebot_sources_from_response(response)
        if not sources:
            raise WorkspaceError(action + " 未返回文件 URL、路径或 base64 数据")
        return sources

    async def _resolve_onebot_file_source(
        self,
        conversation_id: str,
        file_id: str,
        *,
        busid: Any = "",
        source_url: str = "",
    ) -> str:
        """Resolve one QQ file source for non-video file operations.

        This single-source compatibility API deliberately keeps the prior
        behavior for callers such as ``Builtin_download_group_file``.  Video
        analysis uses the lower-level ordered actions below so it can keep
        trying distinct sources after a transfer failure.
        """

        if not self.adapter or not self.adapter.connected:
            raise WorkspaceError("OneBot 未连接")
        if source_url.strip():
            return source_url.strip()
        errors: List[str] = []
        for action, params in self._onebot_file_source_actions(conversation_id, file_id, busid=busid):
            try:
                sources = await self._request_onebot_file_sources(action, params)
                return sources[0]
            except Exception as exc:
                errors.append(action + "：" + redact_error_detail(exc, limit=500))
        raise WorkspaceError(
            "无法获取 QQ 文件下载地址；已尝试 get_group_file_url/get_private_file_url 和兼容 get_file。"
            + ("\n" + "\n".join(errors[:3]) if errors else "")
        )

    async def _download_group_file_to_workspace(
        self,
        conversation_id: str,
        file_id: str,
        filename: str = "",
        busid: Any = "",
        source_url: str = "",
    ) -> Dict[str, Any]:
        if not self.adapter or not self.adapter.connected:
            return {"ok": False, "error": "OneBot 未连接"}
        if _is_private_conversation(conversation_id):
            return {"ok": False, "error": "当前会话不是群聊，不能调用群文件下载"}
        if not file_id.strip():
            return {"ok": False, "retry_safe": True, "error": "file_id 不能为空"}
        source = await self._resolve_onebot_file_source(
            conversation_id,
            file_id,
            busid=busid,
            source_url=source_url,
        )
        name = _as_string(filename).strip() or Path(source).name or (file_id + ".bin")
        target = self.workspace.resolve(conversation_id, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.startswith("base64://"):
            raw = base64.b64decode(source[len("base64://") :])
            target.write_bytes(raw)
        elif source.startswith(("http://", "https://")):
            import urllib.request

            def fetch() -> bytes:
                with urllib.request.urlopen(source, timeout=120) as handle:  # noqa: S310 - OneBot supplied URL
                    return handle.read()

            target.write_bytes(await asyncio.to_thread(fetch))
        elif source and Path(source).is_file():
            shutil.copy2(source, target)
        else:
            raise WorkspaceError("OneBot 未返回可读取的群文件路径、URL 或 base64 数据")
        if target.stat().st_size > MAX_WORKSPACE_FILE_BYTES:
            raise WorkspaceError("群文件超过 100 MiB，不能继续处理")
        relative = target.relative_to(self.conversation_workspace(conversation_id)).as_posix()
        return {
            "ok": True,
            "file_id": file_id,
            "path": relative,
            "bytes": target.stat().st_size,
            "next_step": "如果用户要求阅读内容，请立即调用 read_workspace_file(path=%s)，不要只根据文件名猜测。" % relative,
        }

    async def _download_remote_video_to_path(self, source: str, target: Path) -> None:
        """Download one OneBot-supplied HTTPS video URL without WinINet proxy state.

        ``urllib.request.urlopen`` inherits Windows/user proxy configuration.
        In particular, a stale local proxy socket is surfaced as the misleading
        ``<urlopen error [Errno 2] No such file or directory>`` seen for QQ's
        ``ftn.qq.com`` temporary URLs.  QQ CDN download links are ordinary
        HTTPS resources, so use the application's existing ``httpx`` stack
        with environment proxies disabled, stream to a temporary sibling and
        atomically publish only a complete file.
        """

        parsed = urlsplit(source)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise WorkspaceError("OneBot 返回的视频 URL 不是有效的 HTTP/HTTPS 下载地址")

        temporary = target.with_name(target.name + ".download")
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # Opening the target below will produce the useful filesystem
            # diagnostic; a best-effort stale-part cleanup is enough here.
            pass

        total = 0
        completed = False
        last_error: Optional[Exception] = None
        try:
            timeout = httpx.Timeout(connect=30.0, read=120.0, write=120.0, pool=30.0)
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=timeout,
                trust_env=False,
            ) as client:
                for attempt in range(1, MAX_REMOTE_VIDEO_DOWNLOAD_ATTEMPTS + 1):
                    existing = temporary.stat().st_size if temporary.exists() else 0
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) QQAIGroupAgent/1.0",
                        "Accept": "*/*",
                        "Accept-Encoding": "identity",
                        "Cache-Control": "no-cache",
                    }
                    if existing:
                        headers["Range"] = "bytes=%s-" % existing
                    try:
                        async with client.stream("GET", source, headers=headers) as response:
                            # A CDN may ignore Range and return the whole file.
                            # Restart from zero instead of appending a duplicate.
                            append = bool(existing and response.status_code == 206)
                            if existing and response.status_code == 200:
                                existing = 0
                                temporary.unlink(missing_ok=True)
                            response.raise_for_status()
                            length = response.headers.get("content-length", "")
                            try:
                                advertised_size = int(length) if length else 0
                            except (TypeError, ValueError):
                                advertised_size = 0
                            expected_size = advertised_size + existing if append else advertised_size
                            if expected_size > MAX_VIDEO_FILE_BYTES:
                                raise WorkspaceError("视频超过 2 GiB，不能逐帧处理")
                            mode = "ab" if append else "wb"
                            with temporary.open(mode) as output:
                                async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                                    if not chunk:
                                        continue
                                    total = output.tell() + len(chunk)
                                    if total > MAX_VIDEO_FILE_BYTES:
                                        raise WorkspaceError("视频超过 2 GiB，不能逐帧处理")
                                    output.write(chunk)
                            total = temporary.stat().st_size
                            if expected_size and total < expected_size:
                                raise httpx.RemoteProtocolError(
                                    "QQ 临时视频地址提前关闭（已收到 %s/%s 字节）" % (total, expected_size)
                                )
                            if total <= 0:
                                raise WorkspaceError("QQ 临时视频地址没有返回任何文件内容")
                        completed = True
                        break
                    except WorkspaceError:
                        raise
                    except httpx.HTTPStatusError:
                        # HTTP 4xx/5xx is handled by the source fallback logic;
                        # retrying the same expired URL only delays that path.
                        raise
                    except (httpx.HTTPError, OSError) as exc:
                        last_error = exc
                        if attempt >= MAX_REMOTE_VIDEO_DOWNLOAD_ATTEMPTS:
                            raise
                        await asyncio.sleep(0.5 * attempt)
            if not completed:
                raise WorkspaceError(
                    "QQ 临时视频地址下载未完成："
                    + redact_error_detail(last_error or "未知网络错误", limit=900)
                )
            temporary.replace(target)
        except WorkspaceError:
            raise
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else "未知"
            raise WorkspaceError("QQ 临时视频地址返回 HTTP %s" % status) from exc
        except (httpx.HTTPError, OSError) as exc:
            raise WorkspaceError(
                "读取 QQ 临时视频地址失败：" + redact_error_detail(exc, limit=900)
            ) from exc
        finally:
            # A failed/cancelled transfer must never be mistaken for a usable
            # workspace video by a later Agent round.
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass

    async def _download_video_to_workspace(
        self,
        conversation_id: str,
        file_id: str,
        filename: str = "",
        busid: Any = "",
        source_url: str = "",
    ) -> Path:
        if not self.adapter or not self.adapter.connected:
            raise WorkspaceError("OneBot 未连接")
        if not file_id.strip():
            raise WorkspaceError("视频 file_id 不能为空")
        # A message event may already contain a playable, but very short-lived
        # QQ CDN URL.  Preserve that as the first candidate.  If it fails, do
        # not stop at the first OneBot URL: NapCat's generic get_file can point
        # to a usable local cache file/base64 payload even when its CDN result
        # cannot be fetched from this process.
        event_source = _as_string(source_url).strip()
        parsed_source = urlsplit(event_source)
        name = (
            _as_string(filename).strip()
            or Path(parsed_source.path or event_source).name
            or (file_id + ".mp4")
        )
        target = self.workspace.resolve(conversation_id, name)
        target.parent.mkdir(parents=True, exist_ok=True)

        async def copy_source(candidate: str) -> None:
            if candidate.startswith("base64://"):
                target.write_bytes(base64.b64decode(candidate[len("base64://") :]))
            elif candidate.startswith(("http://", "https://")):
                await self._download_remote_video_to_path(candidate, target)
            elif candidate and Path(candidate).is_file():
                await asyncio.to_thread(shutil.copy2, candidate, target)
            else:
                raise WorkspaceError("OneBot 未返回可读取的视频路径、URL 或 base64 数据")

        seen_sources: set[str] = set()
        failures: List[str] = []

        async def try_source(label: str, candidate: str) -> bool:
            candidate = _as_string(candidate).strip()
            if not candidate:
                return False
            if candidate in seen_sources:
                return False
            seen_sources.add(candidate)
            try:
                await copy_source(candidate)
                return True
            except Exception as exc:
                failures.append(label + "：" + redact_error_detail(exc, limit=700))
                return False

        if event_source and await try_source("消息中的视频地址", event_source):
            if target.stat().st_size > MAX_VIDEO_FILE_BYTES:
                raise WorkspaceError("视频超过 2 GiB，不能逐帧处理")
            return target

        # Resolve and try each OneBot action only after earlier candidates have
        # failed.  This preserves the fast path for healthy event URLs and
        # prevents a broken first CDN URL from hiding a generic local-cache
        # candidate.  Calls are read-only, bounded and send no QQ messages.
        for action, params in self._onebot_file_source_actions(conversation_id, file_id, busid=busid):
            try:
                sources = await self._request_onebot_file_sources(action, params)
            except Exception as exc:
                failures.append(action + "：" + redact_error_detail(exc, limit=700))
                continue
            attempted_new_source = False
            for candidate in sources:
                if _as_string(candidate).strip() not in seen_sources:
                    attempted_new_source = True
                if await try_source(action, candidate):
                    if target.stat().st_size > MAX_VIDEO_FILE_BYTES:
                        raise WorkspaceError("视频超过 2 GiB，不能逐帧处理")
                    return target
            if not attempted_new_source:
                failures.append(action + "：返回的来源与先前已尝试来源相同")

        detail = "；".join(failures[:6])
        raise WorkspaceError(
            "QQ 视频下载失败；已依次尝试消息中的视频地址、OneBot 文件 URL 和 get_file 本地/缓存来源。"
            + (" 失败详情：" + detail if detail else "")
        )

    @staticmethod
    def _video_input_failure_result(error: Exception) -> Dict[str, Any]:
        """Describe a pre-analysis video failure in a repairable form.

        The failed video operation has a durable slot, but no QQ answer has
        been sent yet.  The model gets one forced, short ``send_group_message``
        repair using the *next* action slot so it can tell the current
        conversation what happened without replaying the failed analysis.
        """

        return {
            "ok": False,
            "retry_safe": True,
            "retry_safe_reason": (
                "视频在下载/本机预检阶段失败，尚未发送视频分析结果或 QQ 回复；"
                "可安全发送一条简短说明，但不要自动重试同一视频。"
            ),
            # The failed video operation has already reserved its durable
            # slot.  A response must use the next slot rather than being
            # mistaken for the old failed operation during restart recovery.
            "repair_uses_next_slot": True,
            "required_tool": "send_group_message",
            "failure_kind": "video_source_unavailable",
            "error": "无法读取当前视频：" + redact_error_detail(error, limit=1_000),
            "user_visible_text": "这段视频暂时无法下载解析，请重新发送原视频或稍后再试。",
            "next_step": (
                "不要猜测视频内容或重复调用 Builtin_video_understanding；"
                "请使用 send_group_message 向当前会话简短说明 user_visible_text。"
            ),
        }

    async def _understand_video(
        self,
        conversation_id: str,
        arguments: Dict[str, Any],
        reasoning_effort: str = "off",
    ) -> Dict[str, Any]:
        unknown = set(arguments).difference({"file_id", "path", "filename", "busid", "url"})
        if unknown:
            raise WorkspaceError("Builtin_video_understanding 包含不允许的参数：" + ", ".join(sorted(unknown)))
        supplied_path = _as_string(arguments.get("path")).strip()
        try:
            if supplied_path:
                video_path = self.workspace.resolve(conversation_id, supplied_path)
                if not video_path.is_file():
                    raise WorkspaceError("视频文件不存在：" + supplied_path)
                if video_path.stat().st_size > MAX_VIDEO_FILE_BYTES:
                    raise WorkspaceError("视频超过 2 GiB，不能逐帧处理")
            else:
                video_path = await self._download_video_to_workspace(
                    conversation_id,
                    _as_string(arguments.get("file_id")),
                    _as_string(arguments.get("filename")),
                    arguments.get("busid", ""),
                    _as_string(arguments.get("url")),
                )
        except Exception as exc:
            return self._video_input_failure_result(exc)
        ffmpeg = _find_local_executable("ffmpeg")
        if not ffmpeg:
            return self._video_input_failure_result(
                WorkspaceError(
                    "未找到 ffmpeg：服务进程的 PATH 中没有它，也没有找到 C:\\ffmpeg*\\bin\\ffmpeg.exe。"
                    "请设置 FFMPEG_PATH 为 ffmpeg.exe 的完整路径后重启服务。"
                )
            )
        api_key = self.secret_store.get_llm_api_key()
        if not api_key:
            return self._video_input_failure_result(WorkspaceError("请先在管理页面保存 LLM API key"))
        # This notice is emitted by the service only after the video source,
        # ffmpeg and API key have passed local checks.  It therefore means the
        # frame-analysis tool is genuinely starting, rather than merely being
        # a model-authored reassurance message.
        await self._send_tool_activity_notice(0, conversation_id, "Builtin_video_understanding")
        frame_dir = Path(tempfile.mkdtemp(prefix=".video-frames-", dir=str(self.conversation_workspace(conversation_id))))
        try:
            frame_pattern = str(frame_dir / "frame-%08d.jpg")
            command = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(video_path),
                "-vf",
                r"select=not(mod(n\,10))",
                "-fps_mode",
                "vfr",
                "-q:v",
                "5",
                frame_pattern,
            ]
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.conversation_workspace(conversation_id)),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
            if process.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace")[-4_000:] if stderr else ""
                raise WorkspaceError("ffmpeg 抽帧失败：" + (detail or "未知错误"))
            frames = sorted(frame_dir.glob("frame-*.jpg"))
            if not frames:
                raise WorkspaceError("视频没有抽取到截图")
            client = ChatCompletionsClient(self.settings.llm, api_key)
            summaries: List[str] = []
            frame_batch: List[Dict[str, Any]] = []
            batch_size = 0
            batch_start = 0
            for index, frame in enumerate(frames):
                data_uri = await asyncio.to_thread(image_file_to_data_uri, frame, "image/jpeg")
                part = {"type": "image_url", "image_url": {"url": data_uri, "detail": "auto"}}
                part_size = len(data_uri.encode("utf-8"))
                if frame_batch and batch_size + part_size > VIDEO_FRAME_CHUNK_BYTES:
                    summaries.append(
                        await client.analyze_video_frames(
                            frame_batch,
                            frame_start=batch_start + 1,
                            frame_end=index,
                            reasoning_effort=reasoning_effort,
                        )
                    )
                    frame_batch = []
                    batch_size = 0
                    batch_start = index
                frame_batch.append(part)
                batch_size += part_size
            if frame_batch:
                summaries.append(
                    await client.analyze_video_frames(
                        frame_batch,
                        frame_start=batch_start + 1,
                        frame_end=len(frames),
                        reasoning_effort=reasoning_effort,
                    )
                )
            # Keep each intermediate request within a practical context size;
            # recursively merge only when the list of 20K summaries itself is
            # larger than the same 300 KiB transport budget.
            current = summaries
            while len("\n\n".join(current).encode("utf-8")) > VIDEO_FRAME_CHUNK_BYTES and len(current) > 1:
                groups: List[List[str]] = []
                group: List[str] = []
                used = 0
                for summary in current:
                    size = len(summary.encode("utf-8"))
                    if group and used + size > VIDEO_FRAME_CHUNK_BYTES:
                        groups.append(group)
                        group = []
                        used = 0
                    group.append(summary)
                    used += size
                if group:
                    groups.append(group)
                current = [
                    await client.summarize_video_summaries(group, reasoning_effort=reasoning_effort)
                    for group in groups
                ]
            final_summary = await client.summarize_video_summaries(current, reasoning_effort=reasoning_effort)
            return {
                "ok": True,
                "path": video_path.relative_to(self.conversation_workspace(conversation_id)).as_posix(),
                "frame_interval": 10,
                "frames": len(frames),
                "chunks": len(summaries),
                "summary": final_summary,
            }
        finally:
            shutil.rmtree(frame_dir, ignore_errors=True)

    async def _bilibili_download(
        self,
        conversation_id: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        unknown = set(arguments).difference(BILIBILI_DOWNLOAD_ARGUMENTS)
        if unknown:
            raise WorkspaceError("Builtin_bilibili_download 包含不允许的参数：" + ", ".join(sorted(unknown)))
        url = _as_string(arguments.get("url")).strip()
        if not url:
            raise WorkspaceError("url 不能为空")
        if len(url) > MAX_BILIBILI_URL_CHARS:
            raise WorkspaceError("url 过长")
        if not _is_bilibili_url(url):
            raise WorkspaceError("仅支持 Bilibili 视频链接（bilibili.com、b23.tv 或 bili2233.cn）")
        executable = _find_local_executable("yt-dlp")
        executable_args: List[str] = [executable] if executable else [sys.executable, "-m", "yt_dlp"]
        workspace = self.conversation_workspace(conversation_id)
        extension = _as_string(arguments.get("output_extension") or arguments.get("extension")).strip().lstrip(".")
        format_selector = _as_string(arguments.get("format_selector") or arguments.get("format")).strip() or "bv*[height<=720]+ba/b[height<=720]/b"
        requested_filename = _as_string(arguments.get("filename")).strip()
        if requested_filename:
            requested_path = self.workspace.resolve(conversation_id, requested_filename)
            output_template = str(requested_path)
            if not requested_path.suffix:
                output_template += ".%(ext)s"
        else:
            output_template = str(workspace / "%(title)s.%(ext)s")
        command = executable_args + [
            "--no-playlist",
            "--newline",
            "--socket-timeout",
            "30",
            "--print",
            "after_move:filepath",
            "-f",
            format_selector,
        ]
        ffmpeg_path = _find_local_executable("ffmpeg")
        if ffmpeg_path:
            command.extend(["--ffmpeg-location", str(Path(ffmpeg_path).parent)])
        # Bilibili access always uses this local, dedicated cookie file.  Do
        # not fall back to a cookie for another site, a generic yt-dlp setting,
        # or a browser profile: those could silently send the wrong account's
        # credentials to a different service.
        cookie_file = self.data_dir / "bilibili-cookies.txt"
        has_cookie_file = cookie_file.is_file()
        if has_cookie_file:
            command.extend(["--cookies", str(cookie_file)])
        proxy = _windows_configured_https_proxy()
        if proxy:
            command.extend(["--proxy", proxy])
        if extension:
            command.extend(["--merge-output-format", extension])
        if bool(arguments.get("audio_only", False)):
            command.extend(["-x"])
            if extension:
                command.extend(["--audio-format", extension])
        command.extend(["-o", output_template, url])
        await self._send_tool_activity_notice(0, conversation_id, "Builtin_bilibili_download")
        started = time.time()
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=(
                {
                    **os.environ,
                    "PATH": str(Path(ffmpeg_path).parent) + os.pathsep + os.environ.get("PATH", ""),
                }
                if ffmpeg_path
                else None
            ),
        )
        timed_out = False
        try:
            raw_output, _ = await asyncio.wait_for(
                process.communicate(), timeout=MAX_BILIBILI_DOWNLOAD_SECONDS
            )
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            raw_output, _ = await process.communicate()
        output = raw_output.decode("utf-8", errors="replace") if raw_output else ""
        candidates: List[Path] = []
        for line in output.splitlines():
            candidate = Path(line.strip().strip('"'))
            if candidate.is_file():
                candidates.append(candidate)
        if not candidates:
            candidates = [path for path in workspace.glob("*") if path.is_file() and path.stat().st_mtime >= started]
        if timed_out:
            return {
                "ok": False,
                "returncode": process.returncode,
                "output": output[-MAX_COMMAND_OUTPUT_CHARS:],
                "yt_dlp_output": output[-20_000:],
                "cookies_from_file": has_cookie_file,
                "proxy_from_windows": bool(proxy),
                "error": "yt-dlp 下载超过 600 秒，已终止子进程；请检查 Bilibili 链接、网络或 cookie。",
            }
        if process.returncode != 0 or not candidates:
            lowered = output.lower()
            if "login" in lowered or "not logged in" in lowered:
                error = "Bilibili 要求登录或当前登录已失效；请更新运行数据目录中的 bilibili-cookies.txt 后重试。"
            elif "403" in lowered or "forbidden" in lowered:
                error = "Bilibili 返回 HTTP 403；请更新运行数据目录中的 bilibili-cookies.txt 后重试。"
            else:
                error = "yt-dlp 下载失败或未找到输出文件；已保留完整诊断输出。"
            return {
                "ok": False,
                "returncode": process.returncode,
                "output": output[-MAX_COMMAND_OUTPUT_CHARS:],
                "yt_dlp_output": output[-20_000:],
                "cookies_from_file": has_cookie_file,
                "proxy_from_windows": bool(proxy),
                "error": error,
            }
        path = max(candidates, key=lambda item: item.stat().st_mtime)
        source_path = path
        audio_only = bool(arguments.get("audio_only", False))
        is_video = not audio_only and self._is_video_file(path)
        if path.stat().st_size > (MAX_VIDEO_FILE_BYTES if is_video else MAX_WORKSPACE_FILE_BYTES):
            raise WorkspaceError(
                "下载视频超过 2 GiB，不能发送"
                if is_video
                else "下载文件超过 100 MiB，不能发送"
            )
        if is_video:
            # Do not trust the extension or yt-dlp's merge container: QQ's
            # player is much stricter than a desktop player.  Normalize every
            # downloaded video before constructing the native card.
            path = await self._transcode_video_for_qq(path)
        if path.stat().st_size > (MAX_VIDEO_FILE_BYTES if self._is_video_file(path) else MAX_WORKSPACE_FILE_BYTES):
            raise WorkspaceError(
                "下载视频超过 2 GiB，不能发送"
                if self._is_video_file(path)
                else "下载文件超过 100 MiB，不能发送"
            )
        # An MP4 sent through the generic file upload API appears as a file
        # attachment in QQ.  Use OneBot's video segment first so QQ renders a
        # native, inline playable video card instead.  Explicit audio-only or
        # non-MP4 requests retain the normal file-delivery behavior: calling a
        # video segment for an MP3/M4A is neither a valid QQ video card nor a
        # useful fallback for the caller.
        if self._is_video_file(path) and not bool(arguments.get("audio_only", False)):
            sent = await self._send_video_to_conversation(conversation_id, path, path.name)
        else:
            sent = await self._send_file_to_conversation(conversation_id, path, path.name)
            if sent.get("ok") is True:
                sent["delivery"] = "file"
        return {
            **sent,
            "url": url,
            "path": path.name,
            "source_path": source_path.name if source_path != path else "",
            "qq_compatible": bool(path != source_path and path.suffix.lower() == ".mp4"),
            "bytes": path.stat().st_size,
            "yt_dlp_output": output[-20_000:],
            "cookies_from_file": has_cookie_file,
            "proxy_from_windows": bool(proxy),
        }

    async def _youtube_download(
        self,
        conversation_id: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Download a YouTube video with the local yt-dlp/ffmpeg toolchain."""

        unknown = set(arguments).difference(YOUTUBE_DOWNLOAD_ARGUMENTS)
        if unknown:
            raise WorkspaceError("Builtin_youtube_download 包含不允许的参数：" + ", ".join(sorted(unknown)))
        url = _as_string(arguments.get("url")).strip()
        query = " ".join(_as_string(arguments.get("query")).split()).strip()
        if bool(url) == bool(query):
            raise WorkspaceError("YouTube 下载必须提供 url 或 query（二选一）")
        if url:
            if len(url) > MAX_YOUTUBE_URL_CHARS:
                raise WorkspaceError("url 过长")
            if not _is_youtube_url(url):
                raise WorkspaceError(
                    "仅支持 YouTube 链接（youtube.com、youtu.be 或 youtube-nocookie.com）"
                )
            target = url
        else:
            if len(query) > MAX_YOUTUBE_QUERY_CHARS:
                raise WorkspaceError("YouTube 搜索词最多 500 个字符")
            target = "ytsearch1:" + query

        executable = _find_local_executable("yt-dlp")
        executable_args: List[str] = [executable] if executable else [sys.executable, "-m", "yt_dlp"]
        workspace = self.conversation_workspace(conversation_id)
        extension = _as_string(arguments.get("output_extension") or arguments.get("extension")).strip().lstrip(".")
        format_selector = (
            _as_string(arguments.get("format_selector") or arguments.get("format")).strip()
            or "bv*[height<=720]+ba/b[height<=720]/b"
        )
        requested_filename = _as_string(arguments.get("filename")).strip()
        if requested_filename:
            requested_path = self.workspace.resolve(conversation_id, requested_filename)
            output_template = str(requested_path)
            if not requested_path.suffix:
                output_template += ".%(ext)s"
        else:
            output_template = str(workspace / "%(title)s.%(ext)s")

        command = executable_args + _yt_dlp_js_runtime_args() + [
            "--no-playlist",
            "--newline",
            "--socket-timeout",
            "30",
            "--print",
            "after_move:filepath",
            "-f",
            format_selector,
        ]
        ffmpeg_path = _find_local_executable("ffmpeg")
        if ffmpeg_path:
            command.extend(["--ffmpeg-location", str(Path(ffmpeg_path).parent)])

        # YouTube cookies are opt-in and site-specific.  Never reuse the
        # Bilibili cookie jar or a browser profile for another service.
        cookie_file = self.data_dir / "youtube-cookies.txt"
        has_cookie_file = cookie_file.is_file()
        if has_cookie_file:
            command.extend(["--cookies", str(cookie_file)])
        proxy = _windows_configured_https_proxy()
        if proxy:
            command.extend(["--proxy", proxy])
        if extension:
            command.extend(["--merge-output-format", extension])
        if bool(arguments.get("audio_only", False)):
            command.extend(["-x"])
            if extension:
                command.extend(["--audio-format", extension])
        command.extend(["-o", output_template, target])

        await self._send_tool_activity_notice(0, conversation_id, "Builtin_youtube_download")
        started = time.time()
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=(
                {
                    **os.environ,
                    "PATH": str(Path(ffmpeg_path).parent) + os.pathsep + os.environ.get("PATH", ""),
                }
                if ffmpeg_path
                else None
            ),
        )
        timed_out = False
        try:
            raw_output, _ = await asyncio.wait_for(
                process.communicate(), timeout=MAX_YOUTUBE_DOWNLOAD_SECONDS
            )
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            raw_output, _ = await process.communicate()
        output = raw_output.decode("utf-8", errors="replace") if raw_output else ""
        candidates: List[Path] = []
        for line in output.splitlines():
            candidate = Path(line.strip().strip('"'))
            if candidate.is_file():
                candidates.append(candidate)
        if not candidates:
            candidates = [
                path
                for path in workspace.glob("*")
                if path.is_file() and path.stat().st_mtime >= started
            ]
        if timed_out:
            return {
                "ok": False,
                "returncode": process.returncode,
                "output": output[-MAX_COMMAND_OUTPUT_CHARS:],
                "yt_dlp_output": output[-20_000:],
                "cookies_from_file": has_cookie_file,
                "proxy_from_windows": bool(proxy),
                "error": "yt-dlp 下载超过 600 秒，已终止子进程；请检查 YouTube 链接/搜索词、网络或 cookie。",
            }
        if process.returncode != 0 or not candidates:
            lowered = output.lower()
            if "sign in" in lowered or "login" in lowered or "not a bot" in lowered:
                error = "YouTube 要求登录或当前访问被判定为自动化请求；请更新运行数据目录中的 youtube-cookies.txt 后重试。"
            elif "403" in lowered or "forbidden" in lowered:
                error = "YouTube 返回 HTTP 403；请更新运行数据目录中的 youtube-cookies.txt 或检查网络代理。"
            else:
                error = "yt-dlp 下载失败或未找到输出文件；已保留完整诊断输出。"
            return {
                "ok": False,
                "returncode": process.returncode,
                "output": output[-MAX_COMMAND_OUTPUT_CHARS:],
                "yt_dlp_output": output[-20_000:],
                "cookies_from_file": has_cookie_file,
                "proxy_from_windows": bool(proxy),
                "error": error,
            }

        path = max(candidates, key=lambda item: item.stat().st_mtime)
        source_path = path
        audio_only = bool(arguments.get("audio_only", False))
        is_video = not audio_only and self._is_video_file(path)
        if path.stat().st_size > (MAX_VIDEO_FILE_BYTES if is_video else MAX_WORKSPACE_FILE_BYTES):
            raise WorkspaceError(
                "下载视频超过 2 GiB，不能发送"
                if is_video
                else "下载文件超过 100 MiB，不能发送"
            )
        if is_video:
            path = await self._transcode_video_for_qq(path)
        if path.stat().st_size > (MAX_VIDEO_FILE_BYTES if self._is_video_file(path) else MAX_WORKSPACE_FILE_BYTES):
            raise WorkspaceError(
                "下载视频超过 2 GiB，不能发送"
                if self._is_video_file(path)
                else "下载文件超过 100 MiB，不能发送"
            )
        if self._is_video_file(path) and not audio_only:
            sent = await self._send_video_to_conversation(conversation_id, path, path.name)
        else:
            sent = await self._send_file_to_conversation(conversation_id, path, path.name)
            if sent.get("ok") is True:
                sent["delivery"] = "file"
        return {
            **sent,
            "url": url,
            "query": query,
            "source": target,
            "path": path.name,
            "source_path": source_path.name if source_path != path else "",
            "qq_compatible": bool(path != source_path and path.suffix.lower() == ".mp4"),
            "bytes": path.stat().st_size,
            "yt_dlp_output": output[-20_000:],
            "cookies_from_file": has_cookie_file,
            "proxy_from_windows": bool(proxy),
        }

    async def _music_download(
        self,
        conversation_id: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Download audio, transcode it, and send a native QQ record segment."""

        url = _as_string(arguments.get("url")).strip()
        query = " ".join(_as_string(arguments.get("query")).split()).strip()
        preferred_title = _as_string(arguments.get("title")).strip()
        executable = _find_local_executable("yt-dlp")
        yt_dlp_command: List[str] = [executable] if executable else [sys.executable, "-m", "yt_dlp"]
        ffmpeg_path = _find_local_executable("ffmpeg")
        if not ffmpeg_path:
            return {
                "ok": False,
                "retry_safe": True,
                "qq_side_effect": False,
                "error": "未找到 ffmpeg，无法把音乐转码为 QQ 语音；请把 ffmpeg 加入服务进程 PATH 或设置 FFMPEG_PATH。",
            }
        workspace = self.conversation_workspace(conversation_id)
        music_dir = workspace / "music"
        cookie_file: Optional[Path] = None
        if _is_bilibili_url(url) or query:
            candidate = self.data_dir / "bilibili-cookies.txt"
            if candidate.is_file():
                cookie_file = candidate
        proxy = _windows_configured_https_proxy()
        try:
            await self._send_tool_activity_notice(0, conversation_id, "Builtin_music_download")
            downloaded = await download_music_async(
                url=url,
                output_dir=music_dir,
                yt_dlp_command=yt_dlp_command,
                ffmpeg_path=ffmpeg_path,
                cookies_file=cookie_file,
                proxy=proxy,
                preferred_title=preferred_title,
                query=query,
                timeout_seconds=MAX_MUSIC_DOWNLOAD_SECONDS,
            )
        except MusicDownloadError as exc:
            return {
                "ok": False,
                "retry_safe": True,
                "qq_side_effect": False,
                "cookies_from_file": bool(cookie_file),
                "error": str(exc),
            }
        except Exception as exc:
            return {
                "ok": False,
                "retry_safe": True,
                "qq_side_effect": False,
                "error": "音乐下载或转码失败：" + redact_error_detail(exc, limit=2_000),
            }
        if not self.adapter or not self.adapter.connected:
            return {
                "ok": False,
                "retry_safe": True,
                "qq_side_effect": False,
                "file": downloaded.path.name,
                "error": "音乐已下载并转码，但 OneBot 未连接，无法发送 QQ 语音。",
            }
        try:
            segments = await self._split_audio_for_qq_voice(
                downloaded.path,
                preferred_prefix=downloaded.title,
            )
        except Exception as exc:
            return {
                "ok": False,
                "delivery": "record_failed",
                "qq_side_effect": False,
                "file": downloaded.path.name,
                "error": "音乐切分为 QQ 语音失败：" + redact_error_detail(exc, limit=2_000),
            }
        sent = await self._send_record_segments(
            conversation_id,
            segments,
            display_filename=downloaded.path.name,
        )
        return {
            **sent,
            "path": str(downloaded.path),
            "bytes": downloaded.path.stat().st_size,
            "source_url": downloaded.source_url,
        }

    def _rolling_summary_candidates(self, conversation_id: str, *, limit: int = 16) -> List[str]:
        """Return recent private rolling summaries for the outbound leak gate.

        These values never reach the model or QQ.  They are only compared
        locally with a proposed outgoing text/image body.  Keeping several
        snapshots catches a provider repeating the immediately previous
        summary after the live pointer has moved forward.
        """

        candidates: List[str] = []
        seen = set()

        def add(value: Any) -> None:
            text = _as_string(value).strip()
            key = _summary_comparison_text(text)
            if text and key and key not in seen:
                seen.add(key)
                candidates.append(text)

        add(self.db.get_summary(conversation_id))
        for snapshot in self.db.list_summary_snapshots(conversation_id, limit=max(1, min(int(limit), 64))):
            if isinstance(snapshot, dict):
                add(snapshot.get("content"))
        return candidates

    async def _execute_tool(
        self,
        turn_id: int,
        group_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        tool_call_id: str,
        operation_slot: int = 0,
        trusted_reply_message_ids: Optional[Sequence[str]] = None,
        operation_namespace: str = "",
        app_sent_metadata: Optional[Dict[str, Any]] = None,
        allow_user_facing_group_summary: bool = False,
        rolling_summary_candidates: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        existing = self.db.get_tool_audit(turn_id, tool_call_id)
        if existing:
            return json_loads(existing.get("result_json"), {"ok": False, "error": "tool audit malformed"})
        private_summary_candidates = list(rolling_summary_candidates or self._rolling_summary_candidates(group_id))

        def is_private_rolling_summary(value: str) -> bool:
            return _looks_like_internal_summary(
                value,
                rolling_summary_candidates=private_summary_candidates,
                allow_user_facing_group_summary=allow_user_facing_group_summary,
            )

        # These tools are read-only and explicitly scoped to public web
        # data or the *current* durable group transcript.  They do not need a
        # live OneBot connection and must not enter the state-changing QQ
        # operation reservation path below.
        if tool_name in {
            "Builtin_Websearch",
            "Builtin_querymessage",
            "Builtin_querymemory",
            "Builtin_patch",
            "Builtin_list_group_files",
        }:
            activity_notice = None
            should_notice = isinstance(arguments, dict) and (
                (
                    tool_name == "Builtin_Websearch"
                    and _as_string(arguments.get("query")).strip()
                )
                or (
                    tool_name == "Builtin_patch"
                    and _as_string(arguments.get("url")).strip()
                )
            )
            if should_notice:
                activity_notice = await self._send_tool_activity_notice(turn_id, group_id, tool_name)
            result = await self._execute_read_only_tool(group_id, tool_name, arguments)
            if activity_notice and activity_notice.get("ok") is not True:
                # A notice outage is diagnostic only; it must not turn a
                # successful read-only lookup into a failed model tool.
                result.setdefault("activity_notice_warning", activity_notice.get("error", "进度提示未发送"))
            status = "success" if result.get("ok") is True else "failed"
            self.db.add_tool_audit(turn_id, group_id, tool_call_id, tool_name, arguments, result, status)
            return result
        if tool_name in {"list_workspace_files", "read_workspace_file", "write_workspace_file", "execute_command"}:
            try:
                result = await self._execute_workspace_tool(turn_id, group_id, tool_name, arguments)
            except Exception as exc:
                result = {
                    "ok": False,
                    "retry_safe": True,
                    "error": redact_error_detail(exc, limit=2_000),
                }
            status = "success" if result.get("ok") is True else "failed"
            self.db.add_tool_audit(turn_id, group_id, tool_call_id, tool_name, arguments, result, status)
            return result
        if not self.adapter or not self.adapter.connected:
            result = {"ok": False, "error": "OneBot 未连接"}
            self.db.add_tool_audit(turn_id, group_id, tool_call_id, tool_name, arguments, result, "failed")
            return result

        # Keep the model-provided arguments in the audit trail, but execute
        # only this normalized copy.  ``reply_to_message_id`` is optional:
        # a current-turn model may use an ID exposed by service-generated
        # metadata.  Invented/stale IDs are downgraded to an ordinary message;
        # a known ID in another group remains a hard boundary violation.
        effective_arguments = dict(arguments)
        reply_fallback_warning = ""
        allowed_reply_ids = (
            {_as_string(value) for value in trusted_reply_message_ids if _as_string(value)}
            if trusted_reply_message_ids is not None
            else None
        )

        # Reject malformed/non-permitted operations before reserving the event
        # batch.  They have not crossed a QQ state-change boundary, so they
        # must not prevent a later valid tool decision for the same pending
        # events from being executed.
        try:
            if tool_name == "send_group_message":
                text = _as_string(arguments.get("text")).strip()
                if not text:
                    raise ValueError("text 不能为空")
                if len(text) > MAX_QQ_TEXT_CHARS:
                    raise ValueError("文本超过 %s 字符限制" % MAX_QQ_TEXT_CHARS)
                if is_private_rolling_summary(text):
                    raise _InternalSummaryOutboundError()
                reply_to = _as_string(arguments.get("reply_to_message_id")).strip()
                recorded_groups = self.db.message_groups(reply_to) if reply_to else []
                if reply_to and str(group_id) not in recorded_groups:
                    if recorded_groups:
                        raise ValueError("引用消息属于其他已记录群，禁止跨群引用")
                    effective_arguments.pop("reply_to_message_id", None)
                    reply_fallback_warning = (
                        "reply_to_message_id 未在本机记录的任何群消息中找到；"
                        "已忽略该可选引用并按普通消息发送。"
                    )
                elif reply_to and allowed_reply_ids is not None and reply_to not in allowed_reply_ids:
                    effective_arguments.pop("reply_to_message_id", None)
                    reply_fallback_warning = (
                        "reply_to_message_id 不在本轮服务生成的可信当前群消息元数据中；"
                        "已忽略该可选引用并按普通消息发送。"
                    )
            elif tool_name == "recall_own_message":
                message_id = _as_string(arguments.get("message_id")).strip()
                owned = self.db.get_sent_message(message_id, group_id)
                if not owned:
                    raise ValueError("只能撤回本应用在当前群发送并记录的消息")
                if owned.get("recalled"):
                    raise ValueError("该消息已被撤回")
            elif tool_name == "Builtin_image_generation":
                prompt = _as_string(arguments.get("prompt")).strip()
                if not prompt:
                    raise ValueError("prompt 不能为空")
                if len(prompt) > MAX_IMAGE_GENERATION_PROMPT_CHARS:
                    raise ValueError("prompt 超过 %s 字符限制" % MAX_IMAGE_GENERATION_PROMPT_CHARS)
                size = _as_string(arguments.get("size") or "1024x1024").strip()
                if size not in {"1024x1024", "1792x1024", "1024x1792"}:
                    raise ValueError("size 只能是 1024x1024、1792x1024 或 1024x1792")
            elif tool_name == "Builtin_render_markdown_image":
                unknown = set(arguments).difference({"markdown"})
                if unknown:
                    raise ValueError(
                        "Builtin_render_markdown_image 包含不允许的参数：" + ", ".join(sorted(unknown))
                    )
                markdown = _as_string(arguments.get("markdown"))
                if not markdown.strip():
                    raise ValueError("markdown 不能为空")
                if len(markdown) > MAX_MARKDOWN_RENDER_CHARS:
                    raise ValueError("markdown 超过 %s 字符，请拆分后渲染" % MAX_MARKDOWN_RENDER_CHARS)
                # Rendering is an outbound QQ image operation, not a safe
                # private preview.  Apply the exact same rolling-summary
                # boundary here so the plain-text guard cannot be bypassed by
                # the direct-text renderer fallback or a model tool call.
                if is_private_rolling_summary(markdown):
                    raise _InternalSummaryOutboundError()
            elif tool_name == "send_group_file":
                unknown = set(arguments).difference({"path", "name"})
                if unknown:
                    raise ValueError("send_group_file 包含不允许的参数：" + ", ".join(sorted(unknown)))
                relative_path = _as_string(arguments.get("path")).strip()
                if not relative_path:
                    raise ValueError("path 不能为空")
                self._validate_send_file_path(
                    group_id,
                    relative_path,
                    _as_string(arguments.get("name")).strip(),
                )
            elif tool_name == "Builtin_download_group_file":
                unknown = set(arguments).difference({"file_id", "filename", "busid", "url"})
                if unknown:
                    raise ValueError("Builtin_download_group_file 包含不允许的参数：" + ", ".join(sorted(unknown)))
                if not _as_string(arguments.get("file_id")).strip():
                    raise ValueError("file_id 不能为空")
            elif tool_name == "Builtin_bilibili_download":
                unknown = set(arguments).difference(BILIBILI_DOWNLOAD_ARGUMENTS)
                if unknown:
                    raise ValueError("Builtin_bilibili_download 包含不允许的参数：" + ", ".join(sorted(unknown)))
                url = _as_string(arguments.get("url")).strip()
                if not url:
                    raise ValueError("url 不能为空")
                if len(url) > MAX_BILIBILI_URL_CHARS:
                    raise ValueError("url 过长")
                if not _is_bilibili_url(url):
                    raise ValueError("仅支持 Bilibili 视频链接（bilibili.com、b23.tv 或 bili2233.cn）")
            elif tool_name == "Builtin_youtube_download":
                unknown = set(arguments).difference(YOUTUBE_DOWNLOAD_ARGUMENTS)
                if unknown:
                    raise ValueError("Builtin_youtube_download 包含不允许的参数：" + ", ".join(sorted(unknown)))
                url = _as_string(arguments.get("url")).strip()
                query = " ".join(_as_string(arguments.get("query")).split()).strip()
                if bool(url) == bool(query):
                    raise ValueError("YouTube 下载必须提供 url 或 query（二选一）")
                if url:
                    if len(url) > MAX_YOUTUBE_URL_CHARS:
                        raise ValueError("url 过长")
                    if not _is_youtube_url(url):
                        raise ValueError(
                            "仅支持 YouTube 链接（youtube.com、youtu.be 或 youtube-nocookie.com）"
                        )
                elif len(query) > MAX_YOUTUBE_QUERY_CHARS:
                    raise ValueError("YouTube 搜索词最多 500 个字符")
            elif tool_name == "Builtin_music_download":
                unknown = set(arguments).difference(MUSIC_DOWNLOAD_ARGUMENTS)
                if unknown:
                    raise ValueError("Builtin_music_download 包含不允许的参数：" + ", ".join(sorted(unknown)))
                url = _as_string(arguments.get("url")).strip()
                query = " ".join(_as_string(arguments.get("query")).split()).strip()
                if bool(url) == bool(query):
                    raise ValueError("音乐下载必须提供 url 或 query（二选一）")
                if url:
                    if len(url) > MAX_MUSIC_URL_CHARS:
                        raise ValueError("音乐 URL 过长")
                    try:
                        parsed_url = urlsplit(url)
                    except ValueError as exc:
                        raise ValueError("音乐 URL 格式无效") from exc
                    if parsed_url.scheme.lower() not in {"http", "https"} or not parsed_url.netloc:
                        raise ValueError("音乐 URL 必须是 http(s) 链接")
                elif len(query) > 500:
                    raise ValueError("音乐搜索词最多 500 个字符")
                title = _as_string(arguments.get("title")).strip()
                if len(title) > 100:
                    raise ValueError("title 最多 100 个字符")
            elif tool_name == "Builtin_video_understanding":
                if not _as_string(arguments.get("path") or arguments.get("file_id")).strip():
                    raise ValueError("path 或 file_id 至少需要一个")
            else:
                raise ValueError("不允许的工具：" + tool_name)
        except Exception as exc:
            # This branch is deliberately before durable operation reservation
            # and before any adapter call.  The model may therefore receive a
            # single correction opportunity without risking a duplicate QQ
            # side effect.  Do not use this flag for adapter/network failures
            # or reservation/deduplication results: those can be ambiguous.
            result = {
                "ok": False,
                "error": str(exc),
                "retry_safe": True,
                "retry_safe_reason": "本地参数校验或工具权限检查已拒绝该请求；未向 QQ 发起操作。",
            }
            if isinstance(exc, _InternalSummaryOutboundError):
                # The provider's prose fallback must not feed the exact same
                # internal summary into the renderer.  llm.py recognizes
                # these service-owned fields and quietly keeps the content as
                # the turn summary instead of emitting another QQ action.
                result["internal_summary_outbound_blocked"] = True
                result["suppress_direct_text_fallback"] = True
                result["suppress_direct_reply_repair"] = True
            self.db.add_tool_audit(turn_id, group_id, tool_call_id, tool_name, arguments, result, "failed")
            return result

        # Markdown rendering is entirely local.  Do it before reserving a QQ
        # operation and before sending the progress notice, so a transient
        # Edge/CDP failure cannot consume an action slot or look ambiguous to
        # the model.  A later image delivery still remains behind the normal
        # durable QQ reservation.
        pre_rendered_images: Optional[List[Any]] = None
        if tool_name == "Builtin_render_markdown_image":
            try:
                pre_rendered_images = await self._render_markdown_images(
                    group_id,
                    _as_string(effective_arguments.get("markdown")),
                )
            except MarkdownRenderError as exc:
                result = _markdown_render_failure_result(exc)
                self.db.add_tool_audit(turn_id, group_id, tool_call_id, tool_name, arguments, result, "failed")
                return result
            except Exception as exc:
                error = MarkdownRenderError(
                    "Markdown 本地图片准备失败：" + redact_error_detail(exc, limit=1_000),
                    code="markdown_render_local_prepare_failed",
                    stage="local_prepare",
                )
                result = _markdown_render_failure_result(error)
                self.db.add_tool_audit(turn_id, group_id, tool_call_id, tool_name, arguments, result, "failed")
                return result

        operation_key, event_ids = self._tool_operation_key(
            turn_id,
            group_id,
            operation_slot,
            operation_namespace=operation_namespace,
        )
        previous_operation = self.db.reserve_tool_operation(
            operation_key, group_id, event_ids, tool_name, effective_arguments
        )
        if previous_operation is not None:
            result = json_loads(
                previous_operation.get("result_json"),
                {"ok": False, "error": "此前工具调用记录损坏；为避免重复执行，未再次调用 QQ。"},
            )
            if not isinstance(result, dict):
                result = {"ok": False, "error": "此前工具调用记录无效；为避免重复执行，未再次调用 QQ。"}
            else:
                result = dict(result)
            # Tell the Agent that this was a journal deduplication, not a new
            # safe validation rejection.  The failed operation must not be
            # selected again with the same arguments/slot; the exact original
            # diagnostic remains available in the result.
            if result.get("ok") is not True:
                result["deduplicated"] = True
            message_id = _as_string(result.get("message_id"))
            self.db.add_tool_audit(
                turn_id, group_id, tool_call_id, tool_name, arguments, result, "deduplicated", message_id
            )
            return result

        status = "failed"
        message_id = ""
        try:
            if tool_name == "send_group_message":
                text = _as_string(effective_arguments.get("text")).strip()
                if not text:
                    raise ValueError("text 不能为空")
                if len(text) > MAX_QQ_TEXT_CHARS:
                    raise ValueError("文本超过 %s 字符限制" % MAX_QQ_TEXT_CHARS)
                if is_private_rolling_summary(text):
                    raise _InternalSummaryOutboundError()
                segments: List[Dict[str, Any]] = []
                reply_to = _as_string(effective_arguments.get("reply_to_message_id")).strip()
                if reply_to:
                    if not self.db.has_group_message(group_id, reply_to):
                        raise ValueError("只能引用当前群中已记录的消息")
                    segments.append({"type": "reply", "data": {"id": reply_to}})
                segments.append({"type": "text", "data": {"text": text}})
                action, params = self._qq_send_action(group_id, segments)
                response = await self.adapter.call(action, params)
                data = response.get("data") or {}
                message_id = _as_string(data.get("message_id") if isinstance(data, dict) else "")
                result = {"ok": True, "message_id": message_id}
                if reply_fallback_warning:
                    result["warning"] = reply_fallback_warning
                    result["ignored_reply_to_message_id"] = _as_string(
                        arguments.get("reply_to_message_id")
                    ).strip()
                if message_id:
                    self.db.add_sent_message(message_id, group_id, turn_id, text)
                    # NapCat commonly does not report self messages.  Persist
                    # the successful send as evidence so the bot can remember
                    # its own explicit promises and decisions.
                    sent_content: Dict[str, Any] = {"app_sent": True}
                    metadata = app_sent_metadata if isinstance(app_sent_metadata, dict) else {}
                    # This optional metadata is service-owned only; it is not
                    # derived from any model tool arguments or QQ text.
                    for key, value in metadata.items():
                        if key == "memory_processed":
                            continue
                        if isinstance(key, str) and key and len(key) <= 80:
                            sent_content[key] = value
                    self.db.insert_event(
                        {
                            "dedupe_key": "%s:message:%s" % (group_id, message_id),
                            "group_id": group_id,
                            "event_type": "message.app_sent",
                            "sub_type": "",
                            "message_id": message_id,
                            "occurred_at": int(time.time()),
                            "sender_id": "",
                            "sender_name": "机器人",
                            "self_id": "",
                            "normalized_text": text,
                            "content": sent_content,
                            "raw": {},
                            "is_self": True,
                            "pending": False,
                            "archived": False,
                            "memory_processed": bool(metadata.get("memory_processed", False)),
                        }
                    )
                    self.db.mark_app_sent_event_ignored(group_id, message_id)
                status = "success"

            elif tool_name == "recall_own_message":
                message_id = _as_string(arguments.get("message_id")).strip()
                owned = self.db.get_sent_message(message_id, group_id)
                if not owned:
                    raise ValueError("只能撤回本应用在当前群发送并记录的消息")
                if owned.get("recalled"):
                    raise ValueError("该消息已被撤回")
                await self.adapter.call("delete_msg", {"message_id": message_id})
                self.db.mark_sent_message_recalled(message_id)
                result = {"ok": True, "message_id": message_id}
                status = "success"

            elif tool_name == "Builtin_image_generation":
                prompt = _as_string(effective_arguments.get("prompt")).strip()
                size = _as_string(effective_arguments.get("size") or "1024x1024").strip()
                api_key = self.secret_store.get_llm_api_key()
                if not _as_string(getattr(self.settings.llm, "image_model", "")).strip():
                    raise ValueError("请先在管理页面设置图片生成模型")
                if not api_key:
                    raise ValueError("请先在管理页面保存 LLM API key")
                client = ChatCompletionsClient(self.settings.llm, api_key)
                activity_notice = await self._send_tool_activity_notice(
                    turn_id, group_id, "Builtin_image_generation"
                )
                generated = await client.generate_image(prompt, size=size)
                stored = await self._store_generated_image(generated, prompt=prompt)
                # NapCat and OneBot accept a local file path for an image
                # segment.  The file is in the shared local media directory,
                # so this avoids embedding a huge base64 string in the WS.
                action, params = self._qq_send_action(
                    group_id, [{"type": "image", "data": {"file": str(stored.path)}}]
                )
                response = await self.adapter.call(action, params)
                data = response.get("data") or {}
                message_id = _as_string(data.get("message_id") if isinstance(data, dict) else "")
                result = {
                    "ok": True,
                    "message_id": message_id,
                    "image": {"media_id": stored.media_id, "mime_type": stored.mime_type, "byte_size": stored.byte_size},
                }
                if activity_notice.get("ok") is not True:
                    result["activity_notice_warning"] = activity_notice.get("error", "生成图片进度提示未发送")
                if message_id:
                    self.db.add_sent_message(
                        message_id,
                        group_id,
                        turn_id,
                        "[图片] " + prompt[:MAX_QQ_TEXT_CHARS - 5],
                    )
                    self.db.insert_event(
                        {
                            "dedupe_key": "%s:message:%s" % (group_id, message_id),
                            "group_id": group_id,
                            "event_type": "message.app_sent",
                            "sub_type": "",
                            "message_id": message_id,
                            "occurred_at": int(time.time()),
                            "sender_id": "",
                            "sender_name": "机器人",
                            "self_id": "",
                            "normalized_text": "[图片]",
                            "content": {
                                "app_sent": True,
                                "generated_image": True,
                                "prompt": prompt[:MAX_IMAGE_GENERATION_PROMPT_CHARS],
                                "media_id": stored.media_id,
                            },
                            "raw": {},
                            "is_self": True,
                            "pending": False,
                            "archived": False,
                            "memory_processed": True,
                        }
                    )
                    self.db.mark_app_sent_event_ignored(group_id, message_id)
                status = "success"

            elif tool_name == "Builtin_render_markdown_image":
                # The local render above must succeed before this operation is
                # reserved.  Keeping the fallback defensive makes direct test
                # calls robust without moving any renderer failure after QQ
                # progress/delivery has started.
                if pre_rendered_images is None:
                    raise MarkdownRenderError(
                        "Markdown 本地图片准备结果缺失",
                        code="markdown_render_missing_prepared_images",
                        stage="local_prepare",
                    )
                activity_notice = await self._send_tool_activity_notice(
                    turn_id, group_id, "Builtin_render_markdown_image"
                )
                stored_images = pre_rendered_images
                segments = [
                    {"type": "image", "data": {"file": str(item.path)}}
                    for item in stored_images
                ]
                action, params = self._qq_send_action(group_id, segments)
                response = await self.adapter.call(action, params)
                data = response.get("data") or {}
                message_id = _as_string(data.get("message_id") if isinstance(data, dict) else "")
                result = {
                    "ok": True,
                    "message_id": message_id,
                    "images": [
                        {
                            "media_id": item.media_id,
                            "mime_type": item.mime_type,
                            "byte_size": item.byte_size,
                        }
                        for item in stored_images
                    ],
                    "renderer": "MarkFlow",
                }
                if activity_notice.get("ok") is not True:
                    result["activity_notice_warning"] = activity_notice.get(
                        "error", "Markdown 图片渲染进度提示未发送"
                    )
                if message_id:
                    self._record_app_sent_markdown_images(group_id, turn_id, message_id, stored_images)
                status = "success"

            elif tool_name == "send_group_file":
                relative_path = _as_string(effective_arguments.get("path")).strip()
                requested_name = _as_string(effective_arguments.get("name")).strip()
                path = self._validate_send_file_path(group_id, relative_path, requested_name)
                requested_name = requested_name or path.name
                if self._is_audio_file(path, requested_name):
                    try:
                        segments = await self._split_audio_for_qq_voice(
                            path,
                            preferred_prefix=Path(requested_name).stem or path.stem,
                        )
                    except Exception as exc:
                        result = {
                            "ok": False,
                            "retry_safe": True,
                            "qq_side_effect": False,
                            "error": "音频切分为 QQ 语音失败：" + redact_error_detail(exc, limit=2_000),
                        }
                        status = "failed"
                    else:
                        result = await self._send_record_segments(
                            group_id,
                            segments,
                            display_filename=requested_name,
                        )
                        result.update({"path": str(path), "bytes": path.stat().st_size})
                elif self._is_video_file(path, requested_name):
                    qq_video = await self._transcode_video_for_qq(path)
                    result = await self._send_video_to_conversation(
                        group_id, qq_video, Path(requested_name).stem + ".mp4"
                    )
                else:
                    result = await self._send_file_to_conversation(group_id, path, requested_name)
                if result.get("ok") is not True:
                    if status != "failed":
                        raise WorkspaceError(_as_string(result.get("error") or "文件发送失败"))
                else:
                    message_id = _as_string(result.get("message_id"))
                    if result.get("delivery") == "video_card":
                        self._record_app_sent_video(
                            group_id, turn_id, message_id, _as_string(result.get("file")) or path.name
                        )
                    elif result.get("delivery") == "record":
                        for sent_id in result.get("message_ids") or ([message_id] if message_id else []):
                            self._record_app_sent_voice(
                                group_id,
                                turn_id,
                                _as_string(sent_id),
                                _as_string(result.get("file")) or requested_name,
                            )
                    else:
                        self._record_app_sent_file(
                            group_id, turn_id, message_id, _as_string(result.get("file")) or path.name
                        )
                    status = "success"

            elif tool_name == "Builtin_download_group_file":
                result = await self._download_group_file_to_workspace(
                    group_id,
                    _as_string(effective_arguments.get("file_id")),
                    _as_string(effective_arguments.get("filename")),
                    effective_arguments.get("busid", ""),
                    _as_string(effective_arguments.get("url")),
                )
                status = "success" if result.get("ok") is True else "failed"

            elif tool_name == "Builtin_bilibili_download":
                result = await self._bilibili_download(group_id, effective_arguments)
                if result.get("ok") is not True:
                    # Preserve yt-dlp's actual provider diagnostic for the
                    # model and dashboard.  Raising here would overwrite a
                    # useful Bilibili/provider diagnostic with one generic
                    # sentence.
                    status = "failed"
                else:
                    message_id = _as_string(result.get("message_id"))
                    filename = _as_string(result.get("file"))
                    if result.get("delivery") == "video_card":
                        self._record_app_sent_video(group_id, turn_id, message_id, filename)
                    else:
                        self._record_app_sent_file(group_id, turn_id, message_id, filename)
                    status = "success"

            elif tool_name == "Builtin_youtube_download":
                result = await self._youtube_download(group_id, effective_arguments)
                if result.get("ok") is not True:
                    status = "failed"
                else:
                    message_id = _as_string(result.get("message_id"))
                    filename = _as_string(result.get("file"))
                    if result.get("delivery") == "video_card":
                        self._record_app_sent_video(group_id, turn_id, message_id, filename)
                    else:
                        self._record_app_sent_file(group_id, turn_id, message_id, filename)
                    status = "success"

            elif tool_name == "Builtin_music_download":
                result = await self._music_download(group_id, effective_arguments)
                if result.get("ok") is not True:
                    status = "failed"
                else:
                    message_id = _as_string(result.get("message_id"))
                    for sent_id in result.get("message_ids") or ([message_id] if message_id else []):
                        self._record_app_sent_voice(
                            group_id,
                            turn_id,
                            _as_string(sent_id),
                            _as_string(result.get("file") or "音乐.mp3"),
                        )
                    status = "success"

            elif tool_name == "Builtin_video_understanding":
                result = await self._understand_video(
                    group_id,
                    effective_arguments,
                    self._resolve_reasoning_effort(self.db.get_group(group_id) or {}),
                )
                if result.get("ok") is not True:
                    # No requested QQ payload is sent by video understanding;
                    # this result is a local/file/vision diagnostic.  The
                    # orchestration can therefore safely tell an explicitly
                    # requesting member the real error if the model later
                    # returns no final text.  This does not authorize a retry
                    # of the failed tool itself.
                    result["safe_to_notify_user"] = True
                status = "success" if result.get("ok") is True else "failed"

            else:
                raise ValueError("不允许的工具：" + tool_name)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
            if tool_name in _SAFE_FINALIZATION_NOTICE_TOOLS:
                result["safe_to_notify_user"] = True
        self.db.finish_tool_operation(operation_key, status, result)
        self.db.add_tool_audit(turn_id, group_id, tool_call_id, tool_name, arguments, result, status, message_id)
        return result

    async def _execute_read_only_tool(
        self,
        group_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Run a bounded non-QQ tool and return factual JSON to the model.

        The LLM receives this result through a function-result envelope, but
        all returned search/page/chat text remains untrusted data under its
        immutable service boundary.  Every local validation failure is marked
        retry-safe because no QQ or external state-changing request occurred.
        """

        if not isinstance(arguments, dict):
            return {
                "ok": False,
                "retry_safe": True,
                "error": "工具参数必须是 JSON 对象；未执行。",
            }
        try:
            if tool_name == "Builtin_Websearch":
                unknown = set(arguments).difference({"query", "max_results"})
                if unknown:
                    raise ValueError("Builtin_Websearch 包含不允许的参数：" + ", ".join(sorted(unknown)))
                result = await google_search(arguments.get("query"), max_results=arguments.get("max_results", 5))
                result["untrusted_data"] = True
                return result
            if tool_name == "Builtin_querymessage":
                unknown = set(arguments).difference({"query", "limit"})
                if unknown:
                    raise ValueError("Builtin_querymessage 包含不允许的参数：" + ", ".join(sorted(unknown)))
                query = _as_string(arguments.get("query")).strip()
                if not query:
                    raise ValueError("query 不能为空")
                if len(query) > 500:
                    raise ValueError("query 最多允许 500 个字符")
                try:
                    limit = max(1, min(int(arguments.get("limit", 12)), 20))
                except (TypeError, ValueError) as exc:
                    raise ValueError("limit 必须是整数") from exc
                rows = self.db.search_group_message_context(group_id, query, limit)
                matches: List[Dict[str, Any]] = []
                remaining = 12_000
                for row in rows:
                    text = _as_string(row.get("normalized_text"))
                    if not text:
                        continue
                    text = text[: min(2_000, max(0, remaining))]
                    if not text:
                        break
                    remaining -= len(text)
                    matches.append(
                        {
                            "message_id": _as_string(row.get("message_id")),
                            "occurred_at": row.get("occurred_at") or 0,
                            "sender": _as_string(row.get("sender_name") or row.get("sender_id") or "成员"),
                            "is_bot_message": bool(row.get("is_self")),
                            "text": text,
                        }
                    )
                return {
                    "ok": True,
                    "query": query,
                    "current_group_only": True,
                    "matches": matches,
                    "untrusted_data": True,
                }
            if tool_name == "Builtin_querymemory":
                unknown = set(arguments).difference({"query", "limit"})
                if unknown:
                    raise ValueError(
                        "Builtin_querymemory 包含不允许的参数："
                        + ", ".join(sorted(unknown))
                    )
                query = _as_string(arguments.get("query")).strip()
                if not query:
                    raise ValueError("query 不能为空")
                if len(query) > 500:
                    raise ValueError("query 最多允许 500 个字符")
                try:
                    limit = max(1, min(int(arguments.get("limit", 12)), 20))
                except (TypeError, ValueError) as exc:
                    raise ValueError("limit 必须是整数") from exc
                rows: List[Dict[str, Any]] = []
                seen = set()
                for term in self._memory_query_terms([{"normalized_text": query}]) or [query]:
                    for item in self.db.search_group_memories(
                        group_id,
                        term,
                        active_only=True,
                        limit=limit,
                        include_evidence=True,
                    ):
                        memory_id = int(item.get("id") or 0)
                        if memory_id and memory_id not in seen:
                            seen.add(memory_id)
                            rows.append(item)
                            if len(rows) >= limit:
                                break
                    if len(rows) >= limit:
                        break
                return {
                    "ok": True,
                    "query": query,
                    "current_group_only": True,
                    "memories": [self._compact_memory_for_model(item) for item in rows],
                    "untrusted_data": True,
                }
            if tool_name == "Builtin_patch":
                unknown = set(arguments).difference({"url", "max_chars"})
                if unknown:
                    raise ValueError("Builtin_patch 包含不允许的参数：" + ", ".join(sorted(unknown)))
                result = await fetch_link(arguments.get("url"), max_chars=arguments.get("max_chars", 12_000))
                result["untrusted_data"] = True
                return result
            if tool_name == "Builtin_list_group_files":
                unknown = set(arguments).difference(set())
                if unknown:
                    raise ValueError("Builtin_list_group_files 不接受参数：" + ", ".join(sorted(unknown)))
                if _is_private_conversation(group_id):
                    return {"ok": True, "current_conversation_only": True, "files": [], "note": "私聊没有群文件列表"}
                if not self.adapter or not self.adapter.connected:
                    raise ValueError("OneBot 未连接")
                response = await self.adapter.call(
                    "get_group_root_files",
                    {"group_id": _safe_int(group_id) or group_id},
                )
                data = response.get("data") or {}
                if isinstance(data, dict):
                    files = data.get("files") or data.get("file_list") or []
                else:
                    files = data if isinstance(data, list) else []
                return {
                    "ok": True,
                    "current_group_only": True,
                    "files": [item for item in files if isinstance(item, dict)][:500],
                    "untrusted_data": True,
                }
            raise ValueError("不允许的只读工具：" + tool_name)
        except (ValueError, WebToolError) as exc:
            return {
                "ok": False,
                "retry_safe": True,
                "retry_safe_reason": "只读工具未产生 QQ 状态变化；可根据错误修正一次。",
                "error": redact_error_detail(exc, limit=1_200),
            }
        except Exception as exc:
            # A read-only network failure has no QQ-side ambiguity.  Giving
            # the model its bounded detail supports an agent-style alternate
            # query/URL decision without broadening any authority.
            return {
                "ok": False,
                "retry_safe": True,
                "retry_safe_reason": "只读工具失败前未执行 QQ 操作；可安全决定是否换一个查询或链接。",
                "error": redact_error_detail(exc, limit=1_200),
            }

    def dashboard_state(self) -> Dict[str, Any]:
        usage = self.media.storage_usage_bytes()
        journal_warning = _as_string(getattr(self.db, "journal_mode_warning", ""))
        runtime_warning = self.runtime_warning
        if journal_warning and journal_warning not in runtime_warning:
            runtime_warning = "; ".join(part for part in (runtime_warning, journal_warning) if part)
        return {
            "onebot_connected": self.onebot_connected,
            "enabled_group_count": self.db.enabled_group_count(),
            "pending_event_count": self.db.pending_event_count(),
            "media_usage_bytes": usage,
            "media_usage_label": self._format_bytes(usage),
            "runtime_warning": runtime_warning,
        }

    @staticmethod
    def _format_bytes(value: int) -> str:
        units = ["B", "KiB", "MiB", "GiB", "TiB"]
        number = float(value)
        for unit in units:
            if number < 1024 or unit == units[-1]:
                return ("%.1f %s" % (number, unit)) if unit != "B" else ("%d B" % int(number))
            number /= 1024
        return "%d B" % int(value)
