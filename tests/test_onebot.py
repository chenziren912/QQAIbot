"""Stdlib integration tests for the OneBot reverse-WebSocket adapter."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from app.onebot import (
    OneBotActionError,
    OneBotAdapter,
    OneBotAuthenticationError,
    OneBotDisconnectedError,
)


class FakeWebSocket:
    """Small FastAPI-compatible socket double for adapter tests."""

    def __init__(self, token: str = "shared-token") -> None:
        self.headers = {"authorization": f"Bearer {token}"}
        self.query_params: dict[str, str] = {}
        self.inbound: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed: list[dict[str, Any]] = []
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self) -> dict[str, Any]:
        item = await self.inbound.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    async def close(self, **kwargs: Any) -> None:
        self.closed.append(kwargs)
        self.inbound.put_nowait(ConnectionError("closed"))


class OneBotAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def _wait_until(self, predicate: Any) -> None:
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(0.001)
        self.fail("condition did not become true")

    async def test_rejects_invalid_token_before_accepting(self) -> None:
        adapter = OneBotAdapter("shared-token")
        websocket = FakeWebSocket(token="wrong")

        with self.assertRaises(OneBotAuthenticationError):
            await adapter.attach(websocket)

        self.assertFalse(websocket.accepted)
        self.assertEqual(websocket.closed[-1]["code"], 1008)
        self.assertFalse(adapter.connected)

    async def test_dispatches_events_and_correlates_action_response(self) -> None:
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        adapter = OneBotAdapter("shared-token", on_event, default_timeout=1)
        websocket = FakeWebSocket()
        receiver = asyncio.create_task(adapter.attach(websocket))
        await self._wait_until(lambda: websocket.accepted)

        await websocket.inbound.put({"post_type": "message", "message": "hello"})
        await self._wait_until(lambda: events)
        self.assertEqual(events, [{"post_type": "message", "message": "hello"}])

        action = asyncio.create_task(adapter.call("send_group_msg", {"group_id": 123}))
        await self._wait_until(lambda: websocket.sent)
        echo = websocket.sent[-1]["echo"]
        await websocket.inbound.put(
            {"status": "ok", "retcode": 0, "data": {"message_id": 7}, "echo": echo}
        )
        self.assertEqual((await action)["data"], {"message_id": 7})

        await adapter.disconnect()
        await receiver

    async def test_replacing_connection_fails_old_pending_action(self) -> None:
        adapter = OneBotAdapter("shared-token", default_timeout=1)
        old_socket = FakeWebSocket()
        old_receiver = asyncio.create_task(adapter.attach(old_socket))
        await self._wait_until(lambda: old_socket.accepted)

        call = asyncio.create_task(adapter.call("get_group_info", {"group_id": 1}))
        await self._wait_until(lambda: old_socket.sent)

        new_socket = FakeWebSocket()
        new_receiver = asyncio.create_task(adapter.attach(new_socket))
        await self._wait_until(lambda: new_socket.accepted and adapter.connected)

        with self.assertRaises(OneBotDisconnectedError):
            await call
        await old_receiver
        self.assertEqual(old_socket.closed[-1]["code"], 1012)

        await adapter.disconnect()
        await new_receiver

    async def test_failed_onebot_action_raises_with_response(self) -> None:
        adapter = OneBotAdapter("shared-token", default_timeout=1)
        websocket = FakeWebSocket()
        receiver = asyncio.create_task(adapter.attach(websocket))
        await self._wait_until(lambda: websocket.accepted)

        action = asyncio.create_task(adapter.call("send_group_msg"))
        await self._wait_until(lambda: websocket.sent)
        await websocket.inbound.put(
            {"status": "failed", "retcode": 100, "message": "denied", "echo": websocket.sent[-1]["echo"]}
        )
        with self.assertRaises(OneBotActionError) as context:
            await action
        self.assertEqual(context.exception.response["retcode"], 100)

        await adapter.disconnect()
        await receiver


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
