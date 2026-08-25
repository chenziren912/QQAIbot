"""Loopback-only FastAPI control plane for the QQ group agent."""

from __future__ import annotations

import hmac
import inspect
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .onebot import OneBotAuthenticationError
from .service import AgentService


logger = logging.getLogger(__name__)

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}
MAX_ADMIN_CHAT_MESSAGE_CHARS = 12_000
MAX_MEMORY_CORRECTION_CHARS = 8_000
MAX_MEMORY_NOTE_CHARS = 2_000

_MEMORY_STATUS_LABELS = {
    "active": "有效",
    "confirmed": "已人工确认",
    "pending": "待核验",
    "proposed": "待核验",
    "superseded": "已被更正",
    "retracted": "已撤回",
    "rejected": "已拒绝",
    "deleted": "已软删除",
    "hidden": "已软删除",
}
_MEMORY_TYPE_LABELS = {
    "alias": "称呼 / 别名",
    "preference": "偏好",
    "relationship": "关系",
    "commitment": "承诺 / 待办",
    "background": "背景事实",
}
_MEMORY_CONFIDENCE_LABELS = {
    "confirmed": "已确认",
    "verified": "已确认",
    "human_confirmed": "人工确认",
    "uncertain": "待更多证据",
    "pending": "待核验",
    "retracted": "已撤回",
}
_INACTIVE_MEMORY_STATUSES = {"superseded", "retracted", "rejected", "deleted", "hidden"}


def _host_name(value: str) -> str:
    """Extract a normalized hostname from a Host or Origin header."""

    try:
        candidate = value.strip()
        parsed = urlsplit(candidate if "://" in candidate else "//" + candidate)
        return (parsed.hostname or "").lower()
    except ValueError:
        return ""


def _is_local_host(value: str) -> bool:
    return _host_name(value) in _LOCAL_HOSTS


def _is_explicit_foreign_web_origin(value: str) -> bool:
    """Return whether *value* is an HTTP(S) Origin outside loopback.

    Browsers and extensions can send opaque/non-web origins (for example
    ``null``) for a local form submit.  Those requests still need the
    unguessable CSRF field below, so treating them as a third-party web site
    makes normal local Chromium usage fail without improving the boundary.
    A real HTTP(S) origin is retained as a useful defence-in-depth check.
    """

    try:
        scheme = urlsplit(value.strip()).scheme.lower()
    except ValueError:
        return False
    return scheme in {"http", "https"} and not _is_local_host(value)


def _checkbox(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "on", "yes"}


def _form_value(form: Any, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value if isinstance(value, str) else default


def _memory_id(record: Any) -> str:
    """Return the stable id exposed by a service/database memory record."""

    try:
        value = record.get("memory_id") or record.get("id")
    except AttributeError:
        return ""
    return str(value or "").strip()


def _jsonish(value: Any) -> Any:
    """Decode JSON-backed SQLite fields without making the UI depend on it."""

    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[:1] not in {"[", "{"}:
        return value
    try:
        return json.loads(stripped)
    except (TypeError, ValueError):
        return value


def _pretty_json(value: Any) -> str:
    """Render JSON-backed audit values in a readable, lossless form."""

    if value is None:
        return ""
    if isinstance(value, str):
        decoded = _jsonish(value)
        if decoded is value:
            return value
        value = decoded
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            pass
    return str(value)


def _audit_dashboard_record(record: Any) -> Dict[str, Any]:
    """Add explicit pretty request/response fields for the audit panel.

    The database keeps the original JSON strings for idempotency and audit
    fidelity. The dashboard used to put only a compact response in a
    paragraph, which made long web-search/image results look missing. Keep
    the raw fields and add display-only decoded text instead.
    """

    try:
        raw = dict(record)
    except (TypeError, ValueError):
        raw = {}
    raw["arguments_text"] = _pretty_json(raw.get("arguments_json", raw.get("arguments")))
    raw["result_text"] = _pretty_json(raw.get("result_json", raw.get("result")))
    return raw


def _memory_dashboard_record(record: Any) -> Dict[str, Any]:
    """Normalize evolving memory schemas into a stable template contract."""

    try:
        raw = dict(record)
    except (TypeError, ValueError):
        raw = {}

    subject = str(raw.get("subject_name") or raw.get("subject") or raw.get("subject_id") or "").strip()
    predicate = str(raw.get("predicate") or "").strip()
    value = str(raw.get("value") or raw.get("object_value") or "").strip()
    content = str(
        raw.get("content")
        or raw.get("text")
        or raw.get("statement")
        or raw.get("fact")
        or ""
    ).strip()
    if not content:
        content = " ".join(part for part in (subject, predicate, value) if part).strip()

    evidence_value = _jsonish(
        raw.get("evidence")
        or raw.get("evidence_json")
        or raw.get("sources")
        or raw.get("source_evidence")
    )
    if isinstance(evidence_value, dict):
        evidence_value = [evidence_value]
    evidence_items = []
    if isinstance(evidence_value, list):
        for item in evidence_value[:50]:
            if isinstance(item, str):
                evidence_items.append({"quote": item})
            elif isinstance(item, dict):
                evidence_items.append(
                    {
                        "message_id": item.get("message_id")
                        or item.get("source_message_id")
                        or item.get("event_id")
                        or item.get("source_event_id")
                        or item.get("id"),
                        "quote": item.get("quote")
                        or item.get("evidence_text")
                        or item.get("excerpt")
                        or item.get("text")
                        or item.get("content"),
                        "sender": item.get("sender_name") or item.get("sender") or item.get("author"),
                        "created_at": item.get("observed_at")
                        or item.get("created_at")
                        or item.get("time")
                        or item.get("timestamp"),
                    }
                )
    if not evidence_items:
        quote = raw.get("evidence_text") or raw.get("source_excerpt") or raw.get("quote")
        source_ids = _jsonish(raw.get("source_event_ids") or raw.get("source_message_ids"))
        if isinstance(source_ids, (str, int)):
            source_ids = [source_ids]
        if quote:
            evidence_items.append(
                {
                    "message_id": (source_ids or [None])[0],
                    "quote": quote,
                    "sender": raw.get("source_sender_name") or raw.get("source_sender"),
                    "created_at": raw.get("source_created_at") or raw.get("source_time"),
                }
            )
        elif isinstance(source_ids, list):
            evidence_items.extend({"message_id": source_id} for source_id in source_ids[:50])

    confidence = raw.get("confidence")
    confidence_label = "未知"
    try:
        confidence_number = float(confidence)
        if 0 <= confidence_number <= 1:
            confidence_number *= 100
        confidence_label = f"{confidence_number:.0f}%"
    except (TypeError, ValueError):
        confidence_status = raw.get("confidence_status")
        if confidence_status not in (None, ""):
            confidence_label = _MEMORY_CONFIDENCE_LABELS.get(
                str(confidence_status).lower(), str(confidence_status)
            )
        elif confidence not in (None, ""):
            confidence_label = str(confidence)

    status = str(raw.get("status") or ("active" if raw.get("active", True) else "retracted")).strip().lower()
    if raw.get("superseded_by_memory_id"):
        status = "superseded"
    memory_type = str(
        raw.get("memory_type") or raw.get("kind") or raw.get("category") or "background"
    ).strip().lower()
    normalized = dict(raw)
    normalized.update(
        {
            "memory_id": _memory_id(raw),
            "content": content or "（未提供可显示内容）",
            "status": status,
            "status_label": _MEMORY_STATUS_LABELS.get(status, status or "未知"),
            "memory_type": memory_type,
            "memory_type_label": _MEMORY_TYPE_LABELS.get(memory_type, memory_type or "事实"),
            "confidence_label": confidence_label,
            "evidence_items": evidence_items,
            "is_inactive": status in _INACTIVE_MEMORY_STATUSES,
            "is_confirmed": str(raw.get("confidence_status") or "").lower()
            in {"confirmed", "verified", "human_confirmed"}
            or status == "confirmed",
            "updated_at": raw.get("updated_at") or raw.get("confirmed_at") or raw.get("created_at") or "",
        }
    )
    return normalized


def create_app(data_dir: Path | str | None = None) -> FastAPI:
    """Build the local application.

    ``data_dir`` is injectable for tests.  Normal startup may set
    ``QQ_AI_DATA_DIR`` so mutable SQLite/media state can live on a healthy
    NTFS user-profile volume even when the source tree is on exFAT/removable
    storage.  Direct callers use the same user-profile default, so invoking
    Uvicorn without the PowerShell launcher can never silently fall back to a
    damaged project-local database.
    """

    package_dir = Path(__file__).resolve().parent
    configured_data_dir = os.environ.get("QQ_AI_DATA_DIR", "").strip()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    user_data_dir = (
        Path(local_app_data) / "QQAIGroupAgent" / "data"
        if local_app_data
        else Path.home() / ".qq-ai-group-agent" / "data"
    )
    selected_data_dir = (
        Path(data_dir)
        if data_dir is not None
        else (Path(configured_data_dir) if configured_data_dir else user_data_dir)
    )
    resolved_data_dir = selected_data_dir.expanduser().resolve()
    service = AgentService(resolved_data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(title="QQ 群 AI 代理", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.service = service
    app.state.csrf_token = secrets.token_urlsafe(32)
    templates = Jinja2Templates(directory=str(package_dir / "templates"))
    app.mount("/static", StaticFiles(directory=str(package_dir / "static")), name="static")

    @app.middleware("http")
    async def loopback_host_guard(request: Request, call_next: Any) -> Any:
        # Binding uvicorn to 127.0.0.1 is the primary boundary.  Checking Host
        # too closes common DNS-rebinding/Host-header paths if it is ever put
        # behind a local proxy by accident.
        if not _is_local_host(request.headers.get("host", "")):
            return JSONResponse({"detail": "This control plane is loopback-only."}, status_code=403)
        return await call_next(request)

    async def require_form_csrf(request: Request) -> Any:
        origin = request.headers.get("origin")
        if origin and _is_explicit_foreign_web_origin(origin):
            raise HTTPException(status_code=403, detail="Cross-origin form submissions are not allowed.")
        form = await request.form()
        received = _form_value(form, "csrf_token")
        expected = app.state.csrf_token
        if not received or not hmac.compare_digest(received, expected):
            raise HTTPException(status_code=403, detail="Invalid CSRF token.")
        return form

    async def list_group_memories(group_id: str) -> tuple[list[Any], bool, str]:
        """Load all memory states through the optional service contract.

        Keeping this adapter here lets an old local data directory/control
        plane still start while its service/database migration is being
        installed. Mutations remain disabled unless the full contract is
        available, so the compatibility path cannot bypass group scoping.
        """

        lister = getattr(service, "list_group_memories", None)
        if not callable(lister):
            return [], False, "长期记忆后端尚未就绪；重启更新后的服务即可启用。"
        try:
            result = lister(group_id, limit=100, include_inactive=True)
            if inspect.isawaitable(result):
                result = await result
            return list(result or []), True, ""
        except Exception as exc:
            logger.exception("could not load group memories for %s", group_id)
            return [], True, "读取群记忆失败：" + str(exc)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Any:
        state = service.dashboard_state()
        state["onebot_status"] = "反向 WebSocket 已认证" if state["onebot_connected"] else "等待反向 WebSocket 连接"
        state["last_warning"] = state.pop("runtime_warning", "")

        # Keep the dashboard usable while upgrading an existing local data
        # directory.  The service supplies these methods in normal operation;
        # the fallbacks avoid turning a missing optional history/rules feature
        # into a broken control plane page.
        list_admin_messages = getattr(service, "list_admin_messages", None)
        try:
            admin_messages = list_admin_messages(limit=40) if callable(list_admin_messages) else []
        except Exception:
            logger.exception("could not load administrator chat history")
            admin_messages = []

        get_rules_text = getattr(service, "rules_text", None)
        try:
            rules_text = get_rules_text() if callable(get_rules_text) else ""
        except Exception:
            logger.exception("could not load rules.md for dashboard")
            rules_text = ""

        groups = []
        for stored_group in service.db.list_groups():
            group = dict(stored_group)
            workspace_getter = getattr(service, "conversation_workspace", None)
            if callable(workspace_getter):
                try:
                    group["workspace_path"] = str(workspace_getter(str(group.get("group_id") or "")))
                except Exception:
                    group["workspace_path"] = ""
            memory_rows, memory_feature_available, memory_load_error = await list_group_memories(
                str(group.get("group_id") or "")
            )
            group["memories"] = [_memory_dashboard_record(row) for row in memory_rows]
            group["memory_feature_available"] = memory_feature_available
            group["memory_load_error"] = memory_load_error
            groups.append(group)

        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "state": state,
                "settings": service.public_settings(),
                "groups": groups,
                "recent_events": service.db.list_recent_events(),
                "recent_turns": service.db.list_recent_turns(),
                "audits": [_audit_dashboard_record(row) for row in service.db.list_recent_audits()],
                "admin_messages": admin_messages,
                "rules_text": rules_text,
                "rules_path_label": str(getattr(service, "rules_path_label", "rules.md") or "rules.md"),
                "admin_chat_max_chars": MAX_ADMIN_CHAT_MESSAGE_CHARS,
                "csrf_token": app.state.csrf_token,
            },
        )

    @app.get("/healthz")
    async def healthz() -> Dict[str, Any]:
        return {"ok": True, **service.dashboard_state()}

    @app.post("/settings")
    async def save_settings(request: Request) -> RedirectResponse:
        form = await require_form_csrf(request)
        values = {
            "llm_base_url": _form_value(form, "llm_base_url"),
            "llm_endpoint_mode": _form_value(form, "llm_endpoint_mode", "completions"),
            "llm_model": _form_value(form, "llm_model"),
            "image_model": _form_value(form, "image_model", "gemini-3.1-flash-image"),
            "llm_api_key": _form_value(form, "llm_api_key"),
            "send_reasoning_effort": _checkbox(form.get("send_reasoning_effort")),
            "global_reasoning_effort": _form_value(form, "global_reasoning_effort", "off"),
            "vision_enabled": _checkbox(form.get("vision_enabled")),
            "timeout_seconds": _form_value(form, "timeout_seconds"),
            "global_prompt": _form_value(form, "global_prompt"),
            "media_budget_gib": _form_value(form, "media_budget_gib"),
            "onebot_token": _form_value(form, "onebot_token"),
        }
        try:
            await service.update_settings(values)
        except Exception as exc:
            logger.exception("could not save settings")
            service.runtime_warning = "保存配置失败：" + str(exc)
        return RedirectResponse("/", status_code=303)

    @app.post("/admin/chat")
    async def admin_chat(request: Request) -> RedirectResponse:
        """Send one local administrator instruction to the configured model.

        The service owns message persistence and the sole rules-writing tool.
        This route deliberately only validates local form input and follows a
        POST/redirect/GET flow, so a refresh cannot repeat an LLM request.
        """

        form = await require_form_csrf(request)
        message = _form_value(form, "message").strip()
        if not message:
            service.runtime_warning = "管理员对话未发送：内容不能为空。"
            return RedirectResponse("/#admin-chat", status_code=303)
        if len(message) > MAX_ADMIN_CHAT_MESSAGE_CHARS:
            service.runtime_warning = (
                "管理员对话未发送：单次内容最多 "
                f"{MAX_ADMIN_CHAT_MESSAGE_CHARS:,} 个字符。"
            )
            return RedirectResponse("/#admin-chat", status_code=303)

        try:
            result = await service.admin_chat(message)
            if isinstance(result, dict) and result.get("warning"):
                service.runtime_warning = "管理员对话告警：" + str(result["warning"])
        except Exception as exc:
            logger.exception("administrator chat request failed")
            service.runtime_warning = "管理员对话失败：" + str(exc)
        return RedirectResponse("/#admin-chat", status_code=303)

    @app.post("/groups/{group_id}/toggle")
    async def toggle_group(group_id: str, request: Request) -> RedirectResponse:
        form = await require_form_csrf(request)
        if not service.db.get_group(group_id):
            raise HTTPException(status_code=404, detail="Unknown group")
        enabled = _checkbox(form.get("enabled"))
        prompt = _form_value(form, "prompt_override")
        effort = _form_value(form, "reasoning_effort", "inherit")
        if enabled:
            await service.enable_group(group_id, prompt, effort)
        else:
            await service.disable_group(group_id, prompt, effort)
        return RedirectResponse("/", status_code=303)

    @app.post("/groups/{group_id}/retry")
    async def retry_group(group_id: str, request: Request) -> RedirectResponse:
        await require_form_csrf(request)
        if not service.db.get_group(group_id):
            raise HTTPException(status_code=404, detail="Unknown group")
        await service.retry_group(group_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/groups/{group_id}/reset-memory")
    async def reset_group_memory(group_id: str, request: Request) -> RedirectResponse:
        """Destructively reset one group's derived memory and recompute history."""

        form = await require_form_csrf(request)
        if not service.db.get_group(group_id):
            raise HTTPException(status_code=404, detail="Unknown group")
        if _form_value(form, "confirm_reset") != "reset":
            raise HTTPException(status_code=400, detail="Reset confirmation is required")
        resetter = getattr(service, "reset_group_memory", None)
        if not callable(resetter):
            service.runtime_warning = "群记忆重置不可用：请重启更新后的服务。"
            return RedirectResponse(f"/#group-memory-{group_id}", status_code=303)
        try:
            result = resetter(group_id)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, dict):
                service.runtime_warning = (
                    "已清空该会话的长期记忆、摘要和群级规则；保留原始消息并开始重新计算。"
                    + (" 当前会话已加入重算队列。" if result.get("scheduled") else " 会话未启用，启用后开始重算。")
                )
        except Exception as exc:
            logger.exception("could not reset group memory for %s", group_id)
            service.runtime_warning = "群记忆重置失败：" + str(exc)
        return RedirectResponse(f"/#group-memory-{group_id}", status_code=303)

    @app.post("/groups/{group_id}/memories/{memory_id}/moderate")
    async def moderate_group_memory(group_id: str, memory_id: str, request: Request) -> RedirectResponse:
        """Apply a local, group-scoped human review action to one memory."""

        form = await require_form_csrf(request)
        if not service.db.get_group(group_id):
            raise HTTPException(status_code=404, detail="Unknown group")

        action = _form_value(form, "action").strip().lower()
        if action not in {"confirm", "correct", "retract", "delete"}:
            raise HTTPException(status_code=400, detail="Unknown memory moderation action")
        replacement_text = _form_value(form, "replacement_text").strip()
        note = _form_value(form, "note").strip()
        if action == "correct" and not replacement_text:
            service.runtime_warning = "记忆更正未保存：更正后的内容不能为空。"
            return RedirectResponse(f"/#group-memory-{group_id}", status_code=303)
        if len(replacement_text) > MAX_MEMORY_CORRECTION_CHARS:
            service.runtime_warning = (
                f"记忆更正未保存：内容最多 {MAX_MEMORY_CORRECTION_CHARS:,} 个字符。"
            )
            return RedirectResponse(f"/#group-memory-{group_id}", status_code=303)
        if len(note) > MAX_MEMORY_NOTE_CHARS:
            service.runtime_warning = f"记忆操作未保存：备注最多 {MAX_MEMORY_NOTE_CHARS:,} 个字符。"
            return RedirectResponse(f"/#group-memory-{group_id}", status_code=303)

        # Do not trust a memory id from the URL. Resolve it from this exact
        # group's complete (including inactive) list before reaching the
        # mutating service method.
        memory_rows, feature_available, load_error = await list_group_memories(group_id)
        if not feature_available:
            service.runtime_warning = "记忆操作不可用：长期记忆后端尚未就绪。"
            return RedirectResponse(f"/#group-memory-{group_id}", status_code=303)
        if load_error:
            service.runtime_warning = "记忆操作不可用：" + load_error
            return RedirectResponse(f"/#group-memory-{group_id}", status_code=303)
        if not any(hmac.compare_digest(_memory_id(row), str(memory_id)) for row in memory_rows):
            raise HTTPException(status_code=404, detail="Memory does not belong to this group")

        moderator = getattr(service, "moderate_group_memory", None)
        if not callable(moderator):
            service.runtime_warning = "记忆操作不可用：长期记忆管理接口尚未就绪。"
            return RedirectResponse(f"/#group-memory-{group_id}", status_code=303)
        try:
            result = moderator(
                group_id,
                memory_id,
                action,
                replacement_text=replacement_text,
                note=note,
            )
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, dict) and result.get("warning"):
                service.runtime_warning = "记忆操作告警：" + str(result["warning"])
        except Exception as exc:
            logger.exception("could not moderate memory %s in group %s", memory_id, group_id)
            service.runtime_warning = "记忆操作失败：" + str(exc)
        return RedirectResponse(f"/#group-memory-{group_id}", status_code=303)

    @app.websocket("/onebot/v11")
    async def onebot_reverse_ws(websocket: WebSocket) -> None:
        # Reverse WS clients normally have no Origin.  Both this local Host
        # check and the mandatory OneBot token are enforced before event input.
        if not _is_local_host(websocket.headers.get("host", "")):
            await websocket.close(code=1008, reason="loopback-only endpoint")
            return
        origin = websocket.headers.get("origin")
        if origin and not _is_local_host(origin):
            await websocket.close(code=1008, reason="cross-origin endpoint")
            return
        try:
            await service.attach_onebot(websocket)
        except OneBotAuthenticationError:
            logger.warning("Rejected OneBot connection with invalid token")
        except Exception:
            # The adapter has already closed a disconnected peer.  Do not leak
            # internal details through a WebSocket error response.
            logger.exception("OneBot reverse WebSocket ended unexpectedly")

    return app
