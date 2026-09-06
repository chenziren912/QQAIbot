"""本机终端控制台的命令解析和执行。

终端控制台通过 FastAPI 的 loopback 接口调用这个模块。命令只操作本机
服务已经存在的管理能力，不把任意终端输入转交给 QQ 群，也不把密钥等
敏感配置回显到终端。
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Tuple, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - only for static type checkers
    from .service import AgentService


MAX_TERMINAL_OUTPUT_CHARS = 40_000
MAX_TERMINAL_LIST_LIMIT = 100


@dataclass(frozen=True)
class ParsedCommand:
    """A normalized command line."""

    name: str
    args: Tuple[str, ...]
    raw_args: str


@dataclass(frozen=True)
class TerminalResult:
    """The JSON-safe result returned by the terminal API."""

    output: str
    should_exit: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": True, "output": self.output, "exit": self.should_exit}


# Keep this table in one place so /help, README, and the command parser have a
# stable, inspectable contract. Descriptions are deliberately Chinese because
# the terminal is intended for the operator's PowerShell window.
COMMAND_HELP: Mapping[str, str] = {
    "/help [命令]": "显示全部命令；带命令名时显示该命令的用法。",
    "/status": "显示服务、OneBot、积压事件、Flow 任务和媒体占用状态。",
    "/config": "显示当前非敏感配置（不会显示 API Key、Token 或 Cookie）。",
    "/groups": "列出已发现的群聊和私聊，以及启用状态和积压数量。",
    "/group <会话ID>": "查看一个群聊或私聊的详细状态、摘要和工作目录。",
    "/enable <会话ID>": "启用一个会话；群聊会尝试读取历史，私聊立即开始监听。",
    "/disable <会话ID>": "停止处理一个会话的新消息；已保存的消息不会删除。",
    "/retry <会话ID>": "重试该会话的积压消息或初始化历史。",
    "/reset-memory <会话ID> reset": "清空该会话的摘要、长期记忆和会话规则，并重新计算原始消息；必须输入 reset 确认。",
    "/summary <会话ID> [字符数]": "查看该会话当前的滚动摘要，可选限制输出字符数。",
    "/memory <会话ID> [数量]": "查看该会话的长期记忆及证据数量。",
    "/events <会话ID> [数量]": "查看该会话最近的原始消息上下文。",
    "/turns <会话ID> [数量]": "查看该会话最近的 Agent 处理轮次和错误。",
    "/audits [数量]": "查看最近的工具调用审计记录。",
    "/flow": "查询本机 Google Flow API 的健康状态和待处理任务数量。",
    "/flow-jobs <会话ID> [状态] [数量]": "查看一个会话的视频生成任务；状态可填 queued、in_progress、completed 或 failed。",
    "/sync": "立即从已连接的 OneBot 同步群列表。",
    "/workspace <会话ID> [路径]": "列出会话工作目录中的文件；路径默认是工作区根目录。",
    "/rules": "显示全局 rules.md 内容；空文件会明确提示。",
    "/admin <内容>": "从终端向后台管理员 AI 发一条消息；AI 仍只能通过固定的 rules.md 工具写规则。",
    "/clear": "清空当前 PowerShell 终端画面（不影响服务和数据）。",
    "/quit": "退出终端控制台；不会停止已经运行的 QQ 服务。",
}


_ALIASES = {
    "help": "help",
    "h": "help",
    "帮助": "help",
    "status": "status",
    "state": "status",
    "状态": "status",
    "config": "config",
    "settings": "config",
    "配置": "config",
    "groups": "groups",
    "group": "group",
    "list-groups": "groups",
    "群": "groups",
    "群聊": "groups",
    "enable": "enable",
    "启用": "enable",
    "disable": "disable",
    "禁用": "disable",
    "retry": "retry",
    "重试": "retry",
    "reset-memory": "reset-memory",
    "reset_memory": "reset-memory",
    "reset": "reset-memory",
    "重置记忆": "reset-memory",
    "summary": "summary",
    "摘要": "summary",
    "memory": "memory",
    "memories": "memory",
    "记忆": "memory",
    "events": "events",
    "消息": "events",
    "turns": "turns",
    "轮次": "turns",
    "audits": "audits",
    "audit": "audits",
    "审计": "audits",
    "flow": "flow",
    "flow-health": "flow",
    "flow-jobs": "flow-jobs",
    "视频任务": "flow-jobs",
    "sync": "sync",
    "同步": "sync",
    "workspace": "workspace",
    "工作区": "workspace",
    "rules": "rules",
    "规则": "rules",
    "admin": "admin",
    "管理员": "admin",
    "clear": "clear",
    "清屏": "clear",
    "quit": "quit",
    "exit": "quit",
    "q": "quit",
    "退出": "quit",
}


def parse_command(line: str) -> ParsedCommand:
    """Parse one slash command without interpreting its free-form tail."""

    text = str(line or "").strip()
    if not text:
        raise ValueError("命令不能为空；输入 /help 查看可用命令。")
    match = re.match(r"^(\S+)(?:\s+(.*))?$", text, flags=re.DOTALL)
    if not match:
        raise ValueError("命令不能为空；输入 /help 查看可用命令。")
    token = match.group(1).strip()
    raw_args = match.group(2) or ""
    if token.startswith("/"):
        token = token[1:]
    name = _ALIASES.get(token.lower(), _ALIASES.get(token, token.lower()))
    args: Tuple[str, ...]
    if raw_args.strip():
        try:
            # ``posix=False`` preserves Windows backslashes in workspace paths.
            # Strip only the quote characters themselves so both quoted and
            # unquoted arguments remain convenient in PowerShell.
            lexer = shlex.shlex(raw_args, posix=False)
            lexer.whitespace_split = True
            lexer.commenters = ""
            values = list(lexer)
            args = tuple(
                value[1:-1]
                if len(value) >= 2 and value[:1] == value[-1:] and value[:1] in {"'", '"'}
                else value
                for value in values
            )
        except ValueError as exc:
            raise ValueError("命令引号不完整：" + str(exc)) from exc
    else:
        args = ()
        raw_args = ""
    return ParsedCommand(name=name, args=args, raw_args=raw_args.strip())


def help_text(command: str = "") -> str:
    """Return Chinese help text for all commands or one command."""

    requested = str(command or "").strip()
    if requested.startswith("/"):
        requested = requested[1:]
    canonical = _ALIASES.get(requested.lower(), _ALIASES.get(requested, requested.lower()))
    if requested:
        for syntax, description in COMMAND_HELP.items():
            syntax_name = syntax.split(" ", 1)[0].lstrip("/")
            if _ALIASES.get(syntax_name, syntax_name) == canonical:
                return f"{syntax}\n{description}"
        return f"未知命令：/{requested}\n输入 /help 查看全部中文说明。"
    lines = [
        "QQ AI 本机终端控制台",
        "输入带 / 的命令并按 Enter 执行；命令只在本机 loopback 服务上运行。",
        "",
        "可用命令：",
    ]
    lines.extend(f"  {syntax}：{description}" for syntax, description in COMMAND_HELP.items())
    lines.append("")
    lines.append("中文别名也可使用，例如 /状态、/群、/重置记忆。")
    return "\n".join(lines)


def _as_int(value: Any, default: int, *, minimum: int = 1, maximum: int = MAX_TERMINAL_LIST_LIMIT) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _clip(text: Any, limit: int = MAX_TERMINAL_OUTPUT_CHARS) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 80)] + f"\n…（输出过长，已截断；原长度 {len(value):,} 字符）"


def _json(value: Any, *, limit: int = MAX_TERMINAL_OUTPUT_CHARS) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        rendered = str(value)
    return _clip(rendered, limit)


def _format_bytes(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "未知"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if number < 1024 or unit == "TiB":
            return f"{number:.1f} {unit}" if unit != "B" else f"{int(number)} B"
        number /= 1024
    return "未知"


def _group_label(group: Mapping[str, Any]) -> str:
    group_id = str(group.get("group_id") or "")
    name = str(group.get("group_name") or "").strip()
    kind = "私聊" if str(group.get("conversation_type") or "group") == "private" else "群聊"
    return f"{group_id}（{name or '未命名'}，{kind}）"


def _require_group(service: "AgentService", group_id: str) -> Dict[str, Any]:
    target = str(group_id or "").strip()
    if not target:
        raise ValueError("请提供会话 ID。")
    group = service.db.get_group(target)
    if not group:
        raise ValueError("未知会话：" + target)
    return group


def _format_group_rows(groups: Iterable[Mapping[str, Any]]) -> str:
    rows = list(groups)
    if not rows:
        return "还没有发现群聊或私聊。请先连接 OneBot，或等待新的消息进入。"
    lines = [f"共 {len(rows)} 个会话："]
    for item in rows:
        enabled = "已启用" if item.get("enabled") else "未启用"
        pending = int(item.get("pending_count") or 0)
        error = str(item.get("last_error") or "").replace("\n", " ").strip()
        line = f"- {_group_label(item)}：{enabled}，积压 {pending}"
        if error:
            line += f"；告警：{_clip(error, 240)}"
        lines.append(line)
    return "\n".join(lines)


def _format_event(event: Mapping[str, Any]) -> str:
    sender = str(event.get("sender_name") or event.get("sender_id") or "未知成员")
    text = str(event.get("normalized_text") or "").replace("\r", "").strip()
    if not text:
        text = "（非文本消息或内容为空）"
    timestamp = str(event.get("occurred_at") or event.get("created_at") or "")
    marker = "机器人" if event.get("is_self") else sender
    return f"[{timestamp}] {marker}: {text}"


def _format_turn(turn: Mapping[str, Any]) -> str:
    status = str(turn.get("status") or "未知")
    started = str(turn.get("started_at") or "")
    finished = str(turn.get("finished_at") or "")
    error = str(turn.get("error") or "").replace("\n", " ").strip()
    line = f"#{turn.get('id', '?')} {status}，开始 {started or '未知'}"
    if finished:
        line += f"，结束 {finished}"
    if error:
        line += f"；错误：{_clip(error, 600)}"
    return line


async def execute_command(service: "AgentService", line: str) -> TerminalResult:
    """Execute one parsed command against a live :class:`AgentService`."""

    parsed = parse_command(line)
    name = parsed.name
    args = list(parsed.args)

    if name == "help":
        return TerminalResult(help_text(args[0] if args else ""))
    if name == "quit":
        return TerminalResult("已退出终端控制台；QQ 服务仍继续运行。", should_exit=True)
    if name == "clear":
        # The PowerShell client clears its own screen. Keeping this response
        # explicit makes direct HTTP callers deterministic.
        return TerminalResult("请由终端客户端清空当前画面。")
    if name == "status":
        state = service.dashboard_state()
        lines = [
            "服务状态：正常（终端接口可用）",
            f"OneBot：{'已连接' if state.get('onebot_connected') else '未连接'}",
            f"已启用会话：{state.get('enabled_group_count', 0)}",
            f"待处理事件：{state.get('pending_event_count', 0)}",
            f"待交付 Flow 视频任务：{state.get('flow_video_pending_count', 0)}",
            f"媒体占用：{state.get('media_usage_label') or _format_bytes(state.get('media_usage_bytes'))}",
        ]
        warning = str(state.get("runtime_warning") or "").strip()
        if warning:
            lines.append("运行告警：" + _clip(warning, 2_000))
        return TerminalResult("\n".join(lines))
    if name == "config":
        return TerminalResult(_json(service.public_settings(), limit=12_000))
    if name == "groups":
        return TerminalResult(_format_group_rows(service.db.list_groups()))
    if name == "group":
        if not args:
            raise ValueError("用法：/group <会话ID>")
        group = _require_group(service, args[0])
        summary_record = service.db.get_summary_record(args[0])
        memories = service.list_group_memories(args[0], limit=1_000, include_inactive=True)
        workspace = service.conversation_workspace(args[0])
        lines = [
            f"会话：{_group_label(group)}",
            f"状态：{'已启用' if group.get('enabled') else '未启用'}；初始化：{'完成' if group.get('initialized') else '未完成'}",
            f"积压事件：{group.get('pending_count', 0)}；长期记忆：{len(memories)} 条",
            f"摘要更新时间：{summary_record.get('updated_at') or '暂无'}；摘要最后事件 ID：{summary_record.get('last_event_id', 0)}",
            f"工作目录：{workspace}",
        ]
        error = str(group.get("last_error") or "").strip()
        if error:
            lines.append("会话告警：" + _clip(error, 4_000))
        return TerminalResult("\n".join(lines))
    if name in {"enable", "disable", "retry"}:
        if not args:
            raise ValueError(f"用法：/{name} <会话ID>")
        group_id = args[0]
        group = _require_group(service, group_id)
        if name == "enable":
            await service.enable_group(group_id, str(group.get("prompt_override") or ""), str(group.get("reasoning_effort") or "inherit"))
            return TerminalResult(f"已启用 {_group_label(service.db.get_group(group_id) or group)}。")
        if name == "disable":
            await service.disable_group(group_id, str(group.get("prompt_override") or ""), str(group.get("reasoning_effort") or "inherit"))
            return TerminalResult(f"已停用 {_group_label(service.db.get_group(group_id) or group)}；原始消息仍保留。")
        await service.retry_group(group_id)
        return TerminalResult(f"已请求重试 {_group_label(service.db.get_group(group_id) or group)}。")
    if name == "reset-memory":
        if len(args) < 2 or args[1].lower() != "reset":
            raise ValueError("这是破坏性操作。用法：/reset-memory <会话ID> reset")
        group = _require_group(service, args[0])
        result = await service.reset_group_memory(args[0])
        return TerminalResult(
            f"已重置 {_group_label(group)}：删除记忆 {result.get('memories', 0)} 条、摘要快照 {result.get('summary_snapshots', 0)} 条，"
            f"保留并重新计算 {result.get('events', 0)} 条原始消息。"
            + (" 已加入重算队列。" if result.get("scheduled") else " 当前会话未启用，启用后才会重算。")
        )
    if name == "summary":
        if not args:
            raise ValueError("用法：/summary <会话ID> [字符数]")
        _require_group(service, args[0])
        content = service.db.get_summary(args[0])
        if not content:
            return TerminalResult("该会话目前没有滚动摘要。")
        limit = _as_int(args[1], 20_000, minimum=200, maximum=MAX_TERMINAL_OUTPUT_CHARS) if len(args) > 1 else 20_000
        return TerminalResult("当前摘要：\n" + _clip(content, limit))
    if name == "memory":
        if not args:
            raise ValueError("用法：/memory <会话ID> [数量]")
        _require_group(service, args[0])
        limit = _as_int(args[1], 30, maximum=MAX_TERMINAL_LIST_LIMIT) if len(args) > 1 else 30
        memories = service.list_group_memories(args[0], limit=limit, include_inactive=True)
        if not memories:
            return TerminalResult("该会话没有长期记忆。")
        lines = [f"显示最近 {len(memories)} 条长期记忆："]
        for item in memories:
            statement = str(item.get("statement") or item.get("object_value") or "（无内容）").replace("\n", " ")
            status = str(item.get("status") or ("有效" if item.get("active") else "已撤回"))
            evidence = len(item.get("evidence") or []) if isinstance(item.get("evidence"), list) else 0
            lines.append(f"- #{item.get('id', '?')} [{status}] {statement}（证据 {evidence} 条）")
        return TerminalResult("\n".join(lines))
    if name == "events":
        if not args:
            raise ValueError("用法：/events <会话ID> [数量]")
        _require_group(service, args[0])
        limit = _as_int(args[1], 20, maximum=MAX_TERMINAL_LIST_LIMIT) if len(args) > 1 else 20
        events = service.db.recent_group_message_context_events(args[0], limit=limit)
        if not events:
            return TerminalResult("该会话没有可显示的消息记录。")
        return TerminalResult("\n".join(_format_event(item) for item in events))
    if name == "turns":
        if not args:
            raise ValueError("用法：/turns <会话ID> [数量]")
        _require_group(service, args[0])
        limit = _as_int(args[1], 20, maximum=MAX_TERMINAL_LIST_LIMIT) if len(args) > 1 else 20
        turns = service.db.list_group_turns(args[0], limit=limit)
        if not turns:
            return TerminalResult("该会话没有处理轮次。")
        return TerminalResult("\n".join(_format_turn(item) for item in turns))
    if name == "audits":
        limit = _as_int(args[0], 20, maximum=MAX_TERMINAL_LIST_LIMIT) if args else 20
        audits = service.db.list_recent_audits(limit=limit)
        if not audits:
            return TerminalResult("还没有工具审计记录。")
        lines = ["最近工具调用："]
        for item in audits:
            result = str(item.get("result_json") or "").replace("\n", " ")
            lines.append(
                f"- #{item.get('id', '?')} {item.get('tool_name', '未知工具')} [{item.get('status', '未知')}] "
                f"会话 {item.get('group_id', '?')}；结果：{_clip(result, 600)}"
            )
        return TerminalResult("\n".join(lines))
    if name == "flow":
        client = service._flow_video_client()
        try:
            health = await client.health()
            health_text = _json(health, limit=8_000)
        except Exception as exc:
            health_text = "Flow API 健康检查失败：" + str(exc)
        pending = service.db.pending_flow_video_jobs(limit=100)
        return TerminalResult(
            f"Flow API 地址：{client.base_url}\n待交付任务：{len(pending)}\n健康检查：\n{health_text}"
        )
    if name == "flow-jobs":
        if not args:
            raise ValueError("用法：/flow-jobs <会话ID> [状态] [数量]")
        _require_group(service, args[0])
        status = args[1] if len(args) > 1 else ""
        limit = _as_int(args[2], 20, maximum=MAX_TERMINAL_LIST_LIMIT) if len(args) > 2 else 20
        jobs = service.db.list_flow_video_jobs(args[0], status=status, limit=limit)
        if not jobs:
            return TerminalResult("该会话没有符合条件的视频任务。")
        lines = [f"显示 {len(jobs)} 个视频任务："]
        for job in jobs:
            lines.append(
                f"- {job.get('job_id', '?')} [{job.get('status', '未知')}] {job.get('progress', 0)}% "
                f"交付状态 {job.get('delivery_status', '未知')}；{str(job.get('prompt') or '').replace(chr(10), ' ')[:180]}"
            )
        return TerminalResult("\n".join(lines))
    if name == "sync":
        await service.sync_groups()
        return TerminalResult("已请求同步 OneBot 群列表。当前会话数：" + str(len(service.db.list_groups())))
    if name == "workspace":
        if not args:
            raise ValueError("用法：/workspace <会话ID> [路径]")
        _require_group(service, args[0])
        relative = args[1] if len(args) > 1 else "."
        files = service.workspace.list_files(args[0], relative_path=relative, recursive=False)
        if not files:
            return TerminalResult("该工作区目录为空。")
        lines = [f"工作区 {service.conversation_workspace(args[0])}："]
        for item in files[:MAX_TERMINAL_LIST_LIMIT]:
            kind = "目录" if item.is_dir else _format_bytes(item.size)
            lines.append(f"- {item.relative_path}（{kind}）")
        if len(files) > MAX_TERMINAL_LIST_LIMIT:
            lines.append(f"…还有 {len(files) - MAX_TERMINAL_LIST_LIMIT} 项未显示。")
        return TerminalResult("\n".join(lines))
    if name == "rules":
        content = service.rules_text()
        return TerminalResult("rules.md 当前为空。" if not content.strip() else "rules.md：\n" + _clip(content))
    if name == "admin":
        if not parsed.raw_args:
            raise ValueError("用法：/admin <要交给后台 AI 的内容>")
        result = await service.admin_chat(parsed.raw_args)
        output = str(result.get("assistant_text") or result.get("error") or "管理员 AI 没有返回内容。")
        tool_results = result.get("tool_results")
        if tool_results:
            output += "\n\n工具结果：\n" + _json(tool_results, limit=8_000)
        warning = str(result.get("warning") or "").strip()
        if warning:
            output += "\n\n告警：" + warning
        return TerminalResult(_clip(output))
    raise ValueError(f"未知命令：/{parsed.name}\n输入 /help 查看全部中文说明。")


__all__ = [
    "COMMAND_HELP",
    "MAX_TERMINAL_OUTPUT_CHARS",
    "ParsedCommand",
    "TerminalResult",
    "execute_command",
    "help_text",
    "parse_command",
]
