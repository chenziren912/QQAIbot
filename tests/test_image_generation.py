"""Image-generation tool, activity notice, and own-message recall coverage."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.llm import LLMResult
from app.service import AgentService, normalise_onebot_event


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cfc0000004010100b5d4a3b10000000049454e44ae426082"
)


class Secrets:
    def get_llm_api_key(self) -> str:
        return "test-key"

    def set_llm_api_key(self, value: str) -> None:
        pass

    def get_onebot_token(self) -> str:
        return ""


class Adapter:
    connected = True

    def __init__(self) -> None:
        self.calls: List[tuple[str, Dict[str, Any]]] = []
        self.counter = 0

    async def call(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append((action, params))
        if action == "send_group_msg":
            self.counter += 1
            return {"data": {"message_id": "bot-%s" % self.counter}}
        if action == "delete_msg":
            return {"data": {}}
        raise AssertionError(action)


def _message(message_id: str = "input") -> Dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": "100",
        "message_id": message_id,
        "time": 1,
        "user_id": "member",
        "raw_message": "请生成一张图",
    }


@pytest.mark.asyncio
async def test_image_generation_sends_notice_image_and_can_recall_own_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AgentService(tmp_path, secret_store=Secrets())
    service.settings.llm.model = "test-image-model"
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]
    event_id = service.db.insert_event(normalise_onebot_event(_message()))
    assert event_id
    turn_id = service.db.create_turn("100", [event_id])

    async def fake_generate(self: Any, prompt: str, *, size: str = "1024x1024") -> Dict[str, Any]:
        assert prompt == "一只橙色猫"
        assert size == "1024x1024"
        return {"data": [{"b64_json": base64.b64encode(PNG).decode("ascii")}]}

    monkeypatch.setattr("app.service.ChatCompletionsClient.generate_image", fake_generate)
    result = await service._execute_tool(
        turn_id,
        "100",
        "Builtin_image_generation",
        {"prompt": "一只橙色猫"},
        "image-call",
    )
    assert result["ok"] is True
    assert result["message_id"] == "bot-2"
    assert adapter.calls[0][1]["message"][0]["data"]["text"] == "正在生成图片，请等待至少5s"
    assert adapter.calls[1][1]["message"][0]["type"] == "image"
    assert service.db.get_sent_message("bot-2", "100")

    recalled = await service._execute_tool(
        turn_id,
        "100",
        "recall_own_message",
        {"message_id": "bot-2"},
        "recall-image",
        operation_slot=1,
    )
    assert recalled == {"ok": True, "message_id": "bot-2"}
    assert adapter.calls[-1] == ("delete_msg", {"message_id": "bot-2"})
    await service.stop()


@pytest.mark.asyncio
async def test_search_and_patch_notice_but_history_lookup_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AgentService(tmp_path, secret_store=Secrets())
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]
    event_id = service.db.insert_event(normalise_onebot_event(_message()))
    assert event_id
    turn_id = service.db.create_turn("100", [event_id])

    async def search(*_: Any, **__: Any) -> Dict[str, Any]:
        return {"ok": True, "results": []}

    async def fetch(*_: Any, **__: Any) -> Dict[str, Any]:
        return {"ok": True, "text": "正文"}

    monkeypatch.setattr("app.service.google_search", search)
    monkeypatch.setattr("app.service.fetch_link", fetch)
    await service._execute_tool(turn_id, "100", "Builtin_querymessage", {"query": "猫"}, "q")
    await service._execute_tool(turn_id, "100", "Builtin_Websearch", {"query": "猫"}, "s")
    await service._execute_tool(turn_id, "100", "Builtin_patch", {"url": "https://example.com"}, "p")
    notices = [
        call[1]["message"][0]["data"]["text"]
        for call in adapter.calls
        if call[0] == "send_group_msg" and call[1]["message"][0]["type"] == "text"
    ]
    assert notices == ["正在搜索网络资料，请稍等。", "正在访问网页并读取内容，请稍等。"]
    await service.stop()
