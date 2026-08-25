"""Async OneBot v11 reverse-WebSocket adapter.

``OneBotAdapter.attach`` is intended to be used directly as the body of a
FastAPI WebSocket endpoint::

    @app.websocket("/onebot/v11")
    async def onebot_endpoint(websocket: WebSocket) -> None:
        await adapter.attach(websocket)

The adapter accepts and owns the socket until it is disconnected.  A new,
authenticated connection replaces an older one, which is useful when NapCat
reconnects.  It deliberately has no FastAPI runtime dependency; a FastAPI (or
Starlette-compatible) WebSocket merely needs ``accept``, ``close``,
``receive_json``, and ``send_json`` methods.
"""

from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - type-only optional dependency
    try:
        from fastapi import WebSocket
    except ImportError:
        WebSocket = Any  # type: ignore[misc,assignment]
else:
    WebSocket = Any


logger = logging.getLogger(__name__)

OneBotEvent = dict[str, Any]
EventCallback = Callable[[OneBotEvent], Optional[Awaitable[None]]]


class OneBotError(RuntimeError):
    """Base class for adapter errors."""


class OneBotAuthenticationError(OneBotError):
    """Raised after an incoming WebSocket fails shared-token validation."""


class OneBotDisconnectedError(OneBotError):
    """Raised when an action cannot complete because the socket disconnected."""


class OneBotActionTimeoutError(OneBotError):
    """Raised when OneBot did not return a response before the requested timeout."""


class OneBotActionError(OneBotError):
    """Raised when OneBot responds to an action with a non-success status."""

    def __init__(self, action: str, response: Mapping[str, Any]) -> None:
        self.action = action
        self.response = dict(response)
        status = response.get("status")
        retcode = response.get("retcode")
        message = response.get("message") or response.get("wording")
        detail = f"OneBot action {action!r} failed"
        if status is not None or retcode is not None:
            detail += f" (status={status!r}, retcode={retcode!r})"
        if message:
            detail += f": {message}"
        super().__init__(detail)


class _SocketClosed(Exception):
    """Internal signal used by the generic ``receive`` fallback."""


@dataclass
class _PendingCall:
    future: asyncio.Future[dict[str, Any]]
    session: int
    action: str


class OneBotAdapter:
    """Own one authenticated OneBot v11 reverse-WebSocket connection.

    Args:
        token: Required shared access token.  An incoming client may send it as
            ``Authorization: Bearer <token>`` (the OneBot v11 convention), an
            ``access_token``/``token`` query parameter, or an ``X-OneBot-Token``
            header.  Supporting query/header alternatives makes local NapCat
            deployments easier while keeping a token mandatory.
        on_event: Called asynchronously for each non-response JSON object.  The
            receive loop does not await this callback inline, so a callback may
            safely call :meth:`call` without deadlocking action responses.
        default_timeout: Seconds to wait in :meth:`call` when no timeout is
            supplied.  ``None`` disables the call timeout.

    ``call`` returns the complete OneBot response object (rather than only its
    ``data`` member), and raises :class:`OneBotActionError` for a failed OneBot
    response.
    """

    def __init__(
        self,
        token: str | None = None,
        on_event: EventCallback | None = None,
        *,
        access_token: str | None = None,
        event_handler: EventCallback | None = None,
        default_timeout: float | None = 30.0,
    ) -> None:
        """Create an adapter.

        ``access_token`` and ``event_handler`` are accepted as descriptive
        aliases for integrations that use those names.  Supplying both an alias
        and its primary counterpart is rejected when their values differ.
        """

        if token is not None and access_token is not None and token != access_token:
            raise ValueError("token and access_token must match when both are supplied")
        chosen_token = token if token is not None else access_token
        if not isinstance(chosen_token, str) or not chosen_token:
            raise ValueError("a non-empty OneBot shared token is required")
        if default_timeout is not None and default_timeout <= 0:
            raise ValueError("default_timeout must be positive or None")
        if on_event is not None and event_handler is not None and on_event is not event_handler:
            raise ValueError("on_event and event_handler cannot both be supplied")

        self._token = chosen_token
        self._on_event = on_event if on_event is not None else event_handler
        self._default_timeout = default_timeout

        self._state_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._websocket: WebSocket | None = None
        self._session = 0
        self._pending: dict[str, _PendingCall] = {}
        self._event_tasks: set[asyncio.Task[None]] = set()

    @property
    def connected(self) -> bool:
        """Whether an authenticated reverse-WebSocket is currently attached."""

        return self._websocket is not None

    @property
    def connection_id(self) -> int:
        """Monotonically increasing identity of the current connection.

        Zero means no connection has been attached during this adapter's
        lifetime.  Consumers can use this to distinguish a new reconnect from
        an older socket that is still being replaced.
        """

        return self._session

    async def attach(self, websocket: WebSocket) -> None:
        """Authenticate, accept, and serve a reverse OneBot WebSocket.

        This coroutine normally returns only after the peer disconnects.  A
        subsequent authenticated call replaces the existing connection and
        fails actions that were waiting on the old connection.

        Raises:
            OneBotAuthenticationError: if the peer does not present the shared
                token.  The socket is closed with policy-violation code 1008
                before the exception is raised.
        """

        if not self._token_matches(websocket):
            await self._close_socket(websocket, code=1008, reason="invalid OneBot access token")
            raise OneBotAuthenticationError("invalid OneBot access token")

        try:
            await websocket.accept()
        except Exception as exc:
            # FastAPI should only call this method on an unaccepted WebSocket.
            # Raising a clear adapter error is more useful than leaving callers
            # with a framework-specific exception.
            raise OneBotDisconnectedError("could not accept OneBot WebSocket") from exc

        async with self._state_lock:
            previous_socket = self._websocket
            previous_session = self._session if previous_socket is not None else None
            self._session += 1
            session = self._session
            self._websocket = websocket

        if previous_socket is not None and previous_session is not None:
            self._fail_pending_for_session(
                previous_session,
                OneBotDisconnectedError("OneBot connection was replaced by a new connection"),
            )
            await self._close_socket(
                previous_socket,
                code=1012,
                reason="replaced by a newer OneBot connection",
            )

        try:
            while self._is_current(websocket, session):
                try:
                    payload = await self._receive_json(websocket)
                except asyncio.CancelledError:
                    raise
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                    # Malformed peer traffic does not justify dropping a healthy
                    # reverse connection; ignore it and continue serving.
                    logger.warning("ignoring malformed OneBot WebSocket payload: %s", exc)
                    continue
                except _SocketClosed:
                    break
                except Exception as exc:  # WebSocketDisconnect and transport errors
                    logger.info("OneBot WebSocket receive loop ended: %s", exc)
                    break

                if not isinstance(payload, dict):
                    logger.warning("ignoring non-object OneBot WebSocket payload")
                    continue
                await self._handle_payload(payload, websocket, session)
        finally:
            await self._detach_if_current(websocket, session, "OneBot connection closed")

    async def disconnect(self, *, code: int = 1000, reason: str = "adapter shutdown") -> None:
        """Close the active connection and fail all its waiting action calls."""

        async with self._state_lock:
            websocket = self._websocket
            session = self._session
            self._websocket = None

        if websocket is None:
            return

        self._fail_pending_for_session(session, OneBotDisconnectedError(reason))
        await self._close_socket(websocket, code=code, reason=reason)

    async def call(
        self,
        action: str,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run a OneBot action and return its complete successful response.

        The request's UUID ``echo`` is generated by the adapter and used only to
        correlate its response.  The target action/parameters are otherwise
        sent unchanged.
        """

        if not isinstance(action, str) or not action:
            raise ValueError("action must be a non-empty string")
        if params is not None and not isinstance(params, Mapping):
            raise TypeError("params must be a mapping or None")
        selected_timeout = self._default_timeout if timeout is None else timeout
        if selected_timeout is not None and selected_timeout <= 0:
            raise ValueError("timeout must be positive or None")

        echo = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()

        async with self._state_lock:
            websocket = self._websocket
            session = self._session
            if websocket is None:
                raise OneBotDisconnectedError("no authenticated OneBot connection is active")
            self._pending[echo] = _PendingCall(future=future, session=session, action=action)

        try:
            async with self._send_lock:
                if not self._is_current(websocket, session):
                    raise OneBotDisconnectedError("OneBot connection closed before action was sent")
                try:
                    await websocket.send_json(
                        {
                            "action": action,
                            "params": dict(params or {}),
                            "echo": echo,
                        }
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._detach_if_current(websocket, session, "OneBot send failed")
                    await self._close_socket(websocket, code=1011, reason="OneBot send failed")
                    # ``_detach_if_current`` fails every outstanding call,
                    # including this one.  This call raises a clearer chained
                    # exception below instead of awaiting its future, so mark
                    # that future's exception as retrieved to avoid an asyncio
                    # "Future exception was never retrieved" warning.
                    if future.done() and not future.cancelled():
                        future.exception()
                    raise OneBotDisconnectedError("could not send OneBot action") from exc

            try:
                if selected_timeout is None:
                    response = await future
                else:
                    response = await asyncio.wait_for(future, timeout=selected_timeout)
            except asyncio.TimeoutError as exc:
                raise OneBotActionTimeoutError(
                    f"OneBot action {action!r} timed out after {selected_timeout:g} seconds"
                ) from exc
        finally:
            # A late response after timeout/cancellation is harmless and is not
            # allowed to resolve a future belonging to a later action.
            pending = self._pending.get(echo)
            if pending is not None and pending.future is future:
                self._pending.pop(echo, None)

        if not self._response_is_success(response):
            raise OneBotActionError(action, response)
        return response

    async def _handle_payload(
        self,
        payload: dict[str, Any],
        websocket: WebSocket,
        session: int,
    ) -> None:
        if not self._is_current(websocket, session):
            return

        if self._is_action_response(payload):
            echo = payload.get("echo")
            key = str(echo)
            pending = self._pending.get(key)
            if pending is None:
                logger.debug("received unmatched OneBot action response echo=%r", echo)
                return
            if pending.session != session:
                logger.debug("ignoring stale OneBot action response echo=%r", echo)
                return
            self._pending.pop(key, None)
            if not pending.future.done():
                pending.future.set_result(payload)
            return

        if self._on_event is not None:
            task = asyncio.create_task(self._invoke_event_handler(payload))
            self._event_tasks.add(task)
            task.add_done_callback(self._event_tasks.discard)

    async def _invoke_event_handler(self, payload: OneBotEvent) -> None:
        try:
            result = self._on_event(payload) if self._on_event is not None else None
            if inspect.isawaitable(result):
                await result
            elif result is not None:
                logger.warning("OneBot event callback returned a non-awaitable value")
        except asyncio.CancelledError:
            raise
        except Exception:
            # An application callback must not bring down reception or prevent
            # action responses from resolving.
            logger.exception("OneBot event callback failed")

    async def _detach_if_current(
        self,
        websocket: WebSocket,
        session: int,
        reason: str,
    ) -> None:
        async with self._state_lock:
            if self._websocket is not websocket or self._session != session:
                return
            self._websocket = None

        self._fail_pending_for_session(session, OneBotDisconnectedError(reason))

    def _fail_pending_for_session(self, session: int, error: OneBotDisconnectedError) -> None:
        for echo, pending in list(self._pending.items()):
            if pending.session != session:
                continue
            self._pending.pop(echo, None)
            if not pending.future.done():
                pending.future.set_exception(error)

    @staticmethod
    def _is_action_response(payload: Mapping[str, Any]) -> bool:
        # An event can technically carry an ``echo`` field as arbitrary data.
        # Treat it as a response only when the standard response fields exist.
        return "echo" in payload and ("status" in payload or "retcode" in payload)

    @staticmethod
    def _response_is_success(response: Mapping[str, Any]) -> bool:
        status = response.get("status")
        retcode = response.get("retcode")
        if status is not None and str(status).lower() not in {"ok", "success"}:
            return False
        if retcode is not None and str(retcode) not in {"0", "0.0"}:
            return False
        return True

    def _is_current(self, websocket: WebSocket, session: int) -> bool:
        return self._websocket is websocket and self._session == session

    def _token_matches(self, websocket: WebSocket) -> bool:
        supplied = self._extract_token(websocket)
        return supplied is not None and hmac.compare_digest(supplied, self._token)

    @staticmethod
    def _extract_token(websocket: WebSocket) -> str | None:
        headers = getattr(websocket, "headers", None)

        def header(name: str) -> str | None:
            if headers is None:
                return None
            try:
                value = headers.get(name)
                if value is None:
                    # Simple dicts used by tests are usually case-sensitive.
                    value = headers.get(name.title())
                if value is None:
                    value = headers.get(name.upper())
            except (AttributeError, TypeError):
                return None
            return str(value) if value is not None else None

        authorization = header("authorization")
        if authorization:
            scheme, _, credentials = authorization.partition(" ")
            if scheme.lower() == "bearer" and credentials.strip():
                return credentials.strip()

        for header_name in ("x-onebot-token", "x-access-token"):
            value = header(header_name)
            if value:
                return value

        query_params = getattr(websocket, "query_params", None)
        if query_params is not None:
            for key in ("access_token", "token"):
                try:
                    value = query_params.get(key)
                except (AttributeError, TypeError):
                    value = None
                if value:
                    return str(value)
        return None

    @staticmethod
    async def _receive_json(websocket: WebSocket) -> Any:
        receive_json = getattr(websocket, "receive_json", None)
        if callable(receive_json):
            return await receive_json()

        receive = getattr(websocket, "receive", None)
        if not callable(receive):
            raise TypeError("websocket must provide receive_json() or receive()")
        packet = await receive()
        if not isinstance(packet, Mapping):
            raise ValueError("WebSocket receive() did not return a mapping")
        if packet.get("type") == "websocket.disconnect":
            raise _SocketClosed()
        raw = packet.get("text")
        if raw is None:
            raw = packet.get("bytes")
        if raw is None:
            raise ValueError("WebSocket packet had no text or bytes payload")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    @staticmethod
    async def _close_socket(websocket: WebSocket, *, code: int, reason: str) -> None:
        close = getattr(websocket, "close", None)
        if not callable(close):
            return
        try:
            await close(code=code, reason=reason)
        except TypeError:
            # Small test doubles and some compatible WebSocket implementations
            # only accept a close code.
            try:
                await close(code=code)
            except Exception:
                logger.debug("error while closing OneBot WebSocket", exc_info=True)
        except Exception:
            logger.debug("error while closing OneBot WebSocket", exc_info=True)


# A descriptive alias is convenient for callers that distinguish protocol
# versions in their application wiring.
OneBotV11Adapter = OneBotAdapter


__all__ = [
    "EventCallback",
    "OneBotActionError",
    "OneBotActionTimeoutError",
    "OneBotAdapter",
    "OneBotAuthenticationError",
    "OneBotDisconnectedError",
    "OneBotError",
    "OneBotEvent",
    "OneBotV11Adapter",
]
