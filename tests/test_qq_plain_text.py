from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.service import AgentService, normalise_onebot_event, normalise_qq_plain_text


def test_plain_qq_text_removes_accidental_bold_markers_but_keeps_code() -> None:
    value = "主要是 **网页搜索** 和 **图片生成**。`**literal**`\n```\n**code**\n```"
    assert normalise_qq_plain_text(value) == (
        "主要是 网页搜索 和 图片生成。`**literal**`\n```\n**code**\n```"
    )


class _Secrets:
    def get_llm_api_key(self) -> str:
        return "test-key"


class _Adapter:
    connected = True

    def __init__(self) -> None:
        self.calls: List[tuple[str, Dict[str, Any]]] = []

    async def call(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append((action, params))
        return {"data": {"message_id": "plain-1"}}


@pytest.mark.asyncio
async def test_send_group_message_cleans_plain_markdown_before_qq_send(tmp_path: Path) -> None:
    service = AgentService(tmp_path / "data", secret_store=_Secrets())
    adapter = _Adapter()
    service.adapter = adapter  # type: ignore[assignment]
    event = {
        "post_type": "message",
        "message_type": "group",
        "group_id": "100",
        "message_id": "input",
        "time": 1,
        "user_id": "member",
        "raw_message": "测试",
    }
    event_id = service.db.insert_event(normalise_onebot_event(event))
    assert event_id
    turn_id = service.db.create_turn("100", [event_id])
    try:
        result = await service._execute_tool(
            turn_id,
            "100",
            "send_group_message",
            {"text": "这是 **可读文字**，`**代码**`"},
            "plain-call",
        )
        assert result["ok"] is True
        assert adapter.calls[0][1]["message"][0]["data"]["text"] == "这是 可读文字，`**代码**`"
    finally:
        await service.stop()
