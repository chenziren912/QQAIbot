"""Music downloads must be sent as QQ record messages, not files."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.music import VoiceSegment
from app.service import AgentService


class Adapter:
    connected = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, action: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        self.calls.append((action, dict(params)))
        return {"status": "ok", "data": {"message_id": str(len(self.calls))}}


@pytest.mark.asyncio
async def test_music_download_sends_record_segment_for_group_and_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AgentService(tmp_path / "data")
    voice = tmp_path / "voice.mp3"
    voice.write_bytes(b"mp3")
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]

    async def fake_download(*args: object, **kwargs: object) -> Any:
        return SimpleNamespace(path=voice, title="demo", source_url="https://example.test/music")

    async def fake_split(path: Path, **kwargs: object) -> list[VoiceSegment]:
        return [VoiceSegment(path=path, index=1, start_seconds=0)]

    monkeypatch.setattr("app.service.download_music_async", fake_download)
    monkeypatch.setattr(service, "_split_audio_for_qq_voice", fake_split)
    try:
        group_turn = service.db.create_turn("123", [])
        private_turn = service.db.create_turn("private:456", [])
        group = await service._execute_tool(
            group_turn,
            "123",
            "Builtin_music_download",
            {"url": "https://example.test/music", "title": "demo"},
            "music-group",
        )
        private = await service._execute_tool(
            private_turn,
            "private:456",
            "Builtin_music_download",
            {"url": "https://example.test/music"},
            "music-private",
        )
    finally:
        await service.stop()

    assert group["ok"] is True
    assert group["delivery"] == "record"
    assert private["ok"] is True
    assert private["delivery"] == "record"
    voice_calls = [
        call for call in adapter.calls
        if call[1].get("message", [{}])[0].get("type") == "record"
    ]
    assert [call[0] for call in voice_calls] == [
        "send_group_msg",
        "send_private_msg",
    ]
    assert voice_calls[0][1]["group_id"] == 123
    assert voice_calls[1][1]["user_id"] == 456
    assert voice_calls[0][1]["message"] == [
        {"type": "record", "data": {"file": voice.resolve().as_uri()}}
    ]
    assert voice_calls[1][1]["message"] == [
        {"type": "record", "data": {"file": voice.resolve().as_uri()}}
    ]


@pytest.mark.asyncio
async def test_music_validation_rejects_non_http_url_without_qq_call(tmp_path: Path) -> None:
    service = AgentService(tmp_path / "data")
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]
    try:
        turn_id = service.db.create_turn("123", [])
        result = await service._execute_tool(
            turn_id,
            "123",
            "Builtin_music_download",
            {"url": "javascript:alert(1)"},
            "music-invalid",
        )
    finally:
        await service.stop()

    assert result["ok"] is False
    assert result["retry_safe"] is True
    assert "http(s)" in result["error"]
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_music_download_sends_each_50_second_segment_as_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AgentService(tmp_path / "data")
    source = service.conversation_workspace("123") / "long.mp3"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"mp3")
    parts = [
        VoiceSegment(source.with_name("long.part-000.mp3"), 1, 0),
        VoiceSegment(source.with_name("long.part-001.mp3"), 2, 50),
        VoiceSegment(source.with_name("long.part-002.mp3"), 3, 100),
    ]
    for part in parts:
        part.path.write_bytes(b"part")
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]

    async def fake_download(*args: object, **kwargs: object) -> Any:
        return SimpleNamespace(path=source, title="long", source_url="https://example.test/music")

    async def fake_split(*args: object, **kwargs: object) -> list[VoiceSegment]:
        return parts

    monkeypatch.setattr("app.service.download_music_async", fake_download)
    monkeypatch.setattr(service, "_split_audio_for_qq_voice", fake_split)
    try:
        result = await service._execute_tool(
            service.db.create_turn("123", []),
            "123",
            "Builtin_music_download",
            {"url": "https://example.test/music"},
            "long-music",
        )
    finally:
        await service.stop()

    assert result["ok"] is True
    assert result["total_segments"] == 3
    voice_calls = [call for call in adapter.calls if call[1].get("message", [{}])[0].get("type") == "record"]
    assert len(voice_calls) == 3
    assert all(call[1]["message"][0]["data"]["file"].startswith("file:///") for call in voice_calls)


@pytest.mark.asyncio
async def test_send_group_file_audio_is_voice_not_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = AgentService(tmp_path / "data")
    service.db.upsert_group("123", "音乐群")
    audio = service.conversation_workspace("123") / "song.mp3"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"mp3")
    part = audio.parent / ".qq-voice" / "song.part-000.mp3"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"part")
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]

    async def fake_split(*args: object, **kwargs: object) -> list[VoiceSegment]:
        return [VoiceSegment(part, 1, 0)]

    monkeypatch.setattr(service, "_split_audio_for_qq_voice", fake_split)
    try:
        result = await service._execute_tool(
            service.db.create_turn("123", []),
            "123",
            "send_group_file",
            {"path": "song.mp3"},
            "send-audio",
        )
    finally:
        await service.stop()

    assert result["ok"] is True
    assert result["delivery"] == "record"
    assert [action for action, _ in adapter.calls] == ["send_group_msg"]
    assert adapter.calls[0][1]["message"][0]["type"] == "record"


@pytest.mark.asyncio
async def test_music_query_routes_to_dedicated_downloader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = AgentService(tmp_path / "data")
    voice = tmp_path / "voice.mp3"
    voice.write_bytes(b"mp3")
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]
    captured: dict[str, Any] = {}

    async def fake_download(*args: object, **kwargs: object) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(path=voice, title="Alan Walker Alone", source_url="bilisearch1:Alan Walker Alone")

    async def fake_split(path: Path, **kwargs: object) -> list[VoiceSegment]:
        return [VoiceSegment(path=path, index=1, start_seconds=0)]

    monkeypatch.setattr("app.service.download_music_async", fake_download)
    monkeypatch.setattr(service, "_split_audio_for_qq_voice", fake_split)
    try:
        result = await service._execute_tool(
            service.db.create_turn("123", []),
            "123",
            "Builtin_music_download",
            {"query": "Alan Walker Alone"},
            "music-query",
        )
    finally:
        await service.stop()

    assert result["ok"] is True
    assert captured["query"] == "Alan Walker Alone"
    assert captured["url"] == ""


def test_music_tool_is_exposed_to_model() -> None:
    from app.llm import TOOLS

    names = {
        str((tool.get("function") or {}).get("name") or "")
        for tool in TOOLS
    }
    assert "Builtin_music_download" in names
