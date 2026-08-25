from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.llm import LLMResult
from app.service import (
    AgentService,
    _events_explicitly_request_user_facing_group_summary,
    _looks_like_internal_summary,
    normalise_onebot_event,
)


class Secrets:
    def get_llm_api_key(self) -> str:
        return "test-key"


class Adapter:
    connected = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, params))
        if action == "send_group_msg":
            return {"data": {"message_id": "sent-1"}}
        return {"data": {}}


def _raw(text: str, message_id: str = "1", *, image: bool = False) -> dict[str, Any]:
    message: Any = text
    if image:
        message = [{"type": "image", "data": {"url": "https://example.test/image.jpg"}}]
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": "100",
        "message_id": message_id,
        "time": 1,
        "self_id": "bot",
        "user_id": "member",
        "sender": {"nickname": "成员"},
        "message": message,
        "raw_message": text,
    }


@pytest.mark.asyncio
async def test_media_only_event_still_allows_personal_agent_autonomy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    class Client:
        def __init__(self, *_: Any) -> None:
            pass

        async def run_turn(self, **kwargs: Any) -> LLMResult:
            captured.append(kwargs)
            return LLMResult("只保存内部摘要", [])

    monkeypatch.setattr("app.service.ChatCompletionsClient", Client)
    service = AgentService(tmp_path, secret_store=Secrets())
    service.db.upsert_group("100", "测试群")
    service.db.set_group_config("100", True)
    service.db.set_group_initialized("100", True)
    service.adapter = Adapter()  # type: ignore[assignment]
    event_id = service.db.insert_event(normalise_onebot_event(_raw("[图片]", image=True)))
    assert event_id is not None

    await service._run_group_worker("100")

    assert captured
    assert captured[0]["allow_group_actions"] is True
    assert "[图片]" in captured[0]["current_event_text"]
    assert not service.adapter.calls  # type: ignore[attr-defined]
    await service.stop()


@pytest.mark.asyncio
async def test_internal_summary_send_is_rejected_before_onebot(tmp_path: Path) -> None:
    service = AgentService(tmp_path, secret_store=Secrets())
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]
    service.db.upsert_group("100", "测试群")
    event_id = service.db.insert_event(normalise_onebot_event(_raw("当前消息")))
    assert event_id is not None
    turn_id = service.db.create_turn("100", [event_id])

    result = await service._execute_tool(
        turn_id,
        "100",
        "send_group_message",
        {"text": "群内主要围绕 Submerge 开发展开，这是本轮摘要。"},
        "summary-send",
    )

    assert result["ok"] is False
    assert result["retry_safe"] is True
    assert "内部摘要" in result["error"]
    assert not adapter.calls
    await service.stop()


_LEAKED_ROLLING_SUMMARY = (
    "群内此前进行海龟汤推理，商峻熙提醒不要出烂大街旧题，陈梓仁精准破解了“午夜敲门声”与“雪夜木屋的第四副碗筷”等硬核汤底；"
    "随后群友围绕免费云服务器与 Docker 容器部署、新智能体测试与死循环故障排查、Python 基础语法与代码义诊、"
    "微积分概念与优质高数自学资源推荐、洛谷题解排版渲染等话题展开深入探讨。"
    "针对白梓辰关于“被@互动规则”的误解，机器人进行了澄清；面对群友关于签到的讨论与要求，机器人亦在群内配合发送了签到指令。"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("send_group_message", {"text": _LEAKED_ROLLING_SUMMARY}),
        ("Builtin_render_markdown_image", {"markdown": _LEAKED_ROLLING_SUMMARY}),
    ],
)
async def test_unlabelled_rolling_summary_cannot_escape_as_text_or_markdown_image(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, str],
) -> None:
    """Regression for a provider prose fallback being auto-rendered to QQ."""

    service = AgentService(tmp_path, secret_store=Secrets())
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]
    service.db.upsert_group("100", "测试群")
    service.db.save_summary("100", _LEAKED_ROLLING_SUMMARY, 0)
    event_id = service.db.insert_event(normalise_onebot_event(_raw("@机器人 在吗", "leak-input")))
    assert event_id is not None
    turn_id = service.db.create_turn("100", [event_id])

    result = await service._execute_tool(
        turn_id,
        "100",
        tool_name,
        arguments,
        "rolling-summary-%s" % tool_name,
    )

    assert result["ok"] is False
    assert result["retry_safe"] is True
    assert result["internal_summary_outbound_blocked"] is True
    assert result["suppress_direct_text_fallback"] is True
    assert "内部摘要" in result["error"]
    # Most importantly, renderer validation happens before its progress
    # notice, screenshot work, or the final QQ image message.
    assert adapter.calls == []
    await service.stop()


def test_fresh_member_requested_group_recap_is_not_mistaken_for_private_summary() -> None:
    fresh_recap = "群内此前讨论了 Docker 部署和 Python 入门，随后大家又聊到如何排查脚本报错。"
    summary_request = normalise_onebot_event(_raw("帮我总结一下刚才群聊聊了什么", "summary-request"))

    assert _events_explicitly_request_user_facing_group_summary([summary_request]) is True
    assert _looks_like_internal_summary(fresh_recap) is False
    assert _looks_like_internal_summary(
        fresh_recap,
        allow_user_facing_group_summary=True,
    ) is False
    # A recap request can allow a newly authored answer, not an exact export
    # of a service-owned rolling-memory record.
    assert _looks_like_internal_summary(
        _LEAKED_ROLLING_SUMMARY,
        rolling_summary_candidates=[_LEAKED_ROLLING_SUMMARY],
        allow_user_facing_group_summary=True,
    ) is True
