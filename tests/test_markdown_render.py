"""MarkFlow-style Markdown image tool coverage."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.llm import TOOLS, responses_tools
from app.markdown_render import (
    MarkdownRenderResult,
    MarkdownRenderError,
    MarkFlowMarkdownRenderer,
    RenderedMarkdownImage,
    _detect_markflow_root,
    _find_browser,
    _is_transient_cdp_context_error,
    render_markdown_images,
)
from app.service import AgentService, normalise_onebot_event


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cfc0000004010100b5d4a3b10000000049454e44ae426082"
)


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
        raise AssertionError(action)


class _FakeBrowserProcess:
    """Small process stand-in for renderer retry tests."""

    pid = 12345

    def terminate(self) -> None:
        return None

    def wait(self, timeout: Any = None) -> int:
        return 0

    def kill(self) -> None:
        return None


def _event() -> Dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": "100",
        "message_id": "input",
        "time": 1,
        "user_id": "member",
        "raw_message": "请把公式渲染成图片",
    }


def test_markdown_image_tool_is_exposed_for_completions_and_responses() -> None:
    completion = next(item for item in TOOLS if item["function"]["name"] == "Builtin_render_markdown_image")
    assert completion["function"]["parameters"]["required"] == ["markdown"]
    assert "KaTeX" in completion["function"]["description"]
    response = next(item for item in responses_tools() if item["name"] == "Builtin_render_markdown_image")
    assert response["parameters"]["required"] == ["markdown"]


@pytest.mark.asyncio
async def test_render_markdown_image_sends_markflow_image_and_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AgentService(tmp_path / "data")
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]
    event_id = service.db.insert_event(normalise_onebot_event(_event()))
    assert event_id
    turn_id = service.db.create_turn("100", [event_id])
    job_dir = tmp_path / "render-job"
    job_dir.mkdir()
    image = job_dir / "markdown-01.png"
    image.write_bytes(PNG)

    def fake_render(*_: Any, **__: Any) -> MarkdownRenderResult:
        return MarkdownRenderResult(
            job_dir=job_dir,
            images=(RenderedMarkdownImage(image, 1100, 900, 1, 1),),
        )

    monkeypatch.setattr("app.service.render_markdown_images", fake_render)
    try:
        result = await service._execute_tool(
            turn_id,
            "100",
            "Builtin_render_markdown_image",
            {"markdown": "# 标题\n\n$E=mc^2$"},
            "markdown-call",
        )
        assert result["ok"] is True
        assert result["message_id"] == "bot-2"
        assert result["renderer"] == "MarkFlow"
        assert adapter.calls[0][1]["message"][0]["data"]["text"] == "正在渲染 Markdown 图片，请稍等。"
        sent_segments = adapter.calls[1][1]["message"]
        assert len(sent_segments) == 1
        assert sent_segments[0]["type"] == "image"
        assert Path(sent_segments[0]["data"]["file"]).is_file()
        assert service.db.get_sent_message("bot-2", "100")
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_markdown_renderer_failure_is_retry_safe_before_any_qq_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AgentService(tmp_path / "data")
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]
    event_id = service.db.insert_event(normalise_onebot_event(_event()))
    assert event_id
    turn_id = service.db.create_turn("100", [event_id])

    def fail_render(*_: Any, **__: Any) -> MarkdownRenderResult:
        raise MarkdownRenderError(
            "Edge 渲染协议错误：Execution context was destroyed.",
            code="edge_cdp_context_destroyed",
            stage="cdp",
            transient=True,
            attempts=2,
            retry_attempted=True,
        )

    monkeypatch.setattr("app.service.render_markdown_images", fail_render)
    try:
        failed = await service._execute_tool(
            turn_id,
            "100",
            "Builtin_render_markdown_image",
            {"markdown": "# 标题"},
            "markdown-local-failure",
        )
        assert failed["ok"] is False
        assert failed["retry_safe"] is True
        assert failed["qq_side_effect"] is False
        assert failed["render_diagnostic"] == {
            "code": "edge_cdp_context_destroyed",
            "stage": "cdp",
            "transient": True,
            "attempts": 2,
            "local_retry_attempted": True,
        }
        assert adapter.calls == []
        audit = service.db.get_tool_audit(turn_id, "markdown-local-failure")
        assert audit is not None

        # The failure happened before the QQ action reservation, so a fresh
        # tool call for the same turn can actually render/send instead of
        # being treated as an ambiguous duplicate.
        job_dir = tmp_path / "retry-render"
        job_dir.mkdir()
        image = job_dir / "markdown-01.png"
        image.write_bytes(PNG)

        def success_render(*_: Any, **__: Any) -> MarkdownRenderResult:
            return MarkdownRenderResult(
                job_dir=job_dir,
                images=(RenderedMarkdownImage(image, 1100, 900, 1, 1),),
            )

        monkeypatch.setattr("app.service.render_markdown_images", success_render)
        retried = await service._execute_tool(
            turn_id,
            "100",
            "Builtin_render_markdown_image",
            {"markdown": "# 标题"},
            "markdown-local-retry",
        )
        assert retried["ok"] is True
        assert [action for action, _ in adapter.calls] == ["send_group_msg", "send_group_msg"]
    finally:
        await service.stop()


def test_transient_cdp_context_destroyed_retries_once_with_fresh_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: List[Path] = []

    async def fake_capture(_port: int, _width: int, output_dir: Path) -> List[tuple[Path, int, int]]:
        attempts.append(output_dir)
        if len(attempts) == 1:
            raise MarkdownRenderError(
                "Edge 渲染协议错误：Execution context was destroyed.",
                code="edge_cdp_context_destroyed",
                stage="cdp",
                transient=True,
            )
        image = output_dir / "markdown-01.png"
        image.write_bytes(PNG)
        return [(image, 1100, 900)]

    monkeypatch.setattr("app.markdown_render._detect_markflow_root", lambda *_: tmp_path)
    monkeypatch.setattr("app.markdown_render._find_browser", lambda *_: tmp_path / "edge.exe")
    monkeypatch.setattr("app.markdown_render._standalone_html", lambda *_: "<html></html>")
    monkeypatch.setattr("app.markdown_render._cdp_capture", fake_capture)
    monkeypatch.setattr("app.markdown_render.subprocess.Popen", lambda *_args, **_kwargs: _FakeBrowserProcess())

    result = MarkFlowMarkdownRenderer(tmp_path / "renders").render("# retry")
    try:
        assert len(attempts) == 2
        assert not attempts[0].exists()
        assert result.job_dir == attempts[1]
        assert result.images[0].path.read_bytes() == PNG
    finally:
        shutil.rmtree(result.job_dir, ignore_errors=True)


def test_transient_cdp_context_destroyed_stops_after_one_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: List[Path] = []

    async def fake_capture(_port: int, _width: int, output_dir: Path) -> List[tuple[Path, int, int]]:
        attempts.append(output_dir)
        raise MarkdownRenderError(
            "Edge 渲染协议错误：Execution context was destroyed.",
            code="edge_cdp_context_destroyed",
            stage="cdp",
            transient=True,
        )

    monkeypatch.setattr("app.markdown_render._detect_markflow_root", lambda *_: tmp_path)
    monkeypatch.setattr("app.markdown_render._find_browser", lambda *_: tmp_path / "edge.exe")
    monkeypatch.setattr("app.markdown_render._standalone_html", lambda *_: "<html></html>")
    monkeypatch.setattr("app.markdown_render._cdp_capture", fake_capture)
    monkeypatch.setattr("app.markdown_render.subprocess.Popen", lambda *_args, **_kwargs: _FakeBrowserProcess())

    with pytest.raises(MarkdownRenderError) as exc_info:
        MarkFlowMarkdownRenderer(tmp_path / "renders").render("# retry")
    assert len(attempts) == 2
    assert exc_info.value.diagnostic() == {
        "code": "edge_cdp_context_destroyed",
        "stage": "cdp",
        "transient": True,
        "attempts": 2,
        "local_retry_attempted": True,
    }
    assert not any(path.exists() for path in attempts)
    assert _is_transient_cdp_context_error("Execution context was destroyed.") is True
    assert _is_transient_cdp_context_error("MarkFlow preview JavaScript syntax error") is False


@pytest.mark.asyncio
async def test_markdown_image_tool_rejects_unknown_arguments_before_qq_send(tmp_path: Path) -> None:
    service = AgentService(tmp_path / "data")
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]
    event_id = service.db.insert_event(normalise_onebot_event(_event()))
    assert event_id
    turn_id = service.db.create_turn("100", [event_id])
    try:
        result = await service._execute_tool(
            turn_id,
            "100",
            "Builtin_render_markdown_image",
            {"markdown": "# 标题", "path": "not-allowed"},
            "bad-markdown-call",
        )
        assert result["ok"] is False
        assert result["retry_safe"] is True
        assert "不允许" in result["error"]
        assert adapter.calls == []
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_plain_text_allows_a_short_simple_reply(tmp_path: Path) -> None:
    service = AgentService(tmp_path / "data")
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]
    event_id = service.db.insert_event(normalise_onebot_event(_event()))
    assert event_id
    turn_id = service.db.create_turn("100", [event_id])
    try:
        result = await service._execute_tool(
            turn_id,
            "100",
            "send_group_message",
            {"text": "收到，我先看看。"},
            "short-plain-text",
        )
        assert result == {"ok": True, "message_id": "bot-1"}
        assert adapter.calls[0][1]["message"] == [{"type": "text", "data": {"text": "收到，我先看看。"}}]
    finally:
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("这是一段" * 130, "长度超过"),
        ("# 题解\n\n先说明思路。", "Markdown 标题"),
        ("```cpp\nint main() { return 0; }\n```", "代码围栏或 LaTeX"),
        ("设 $f(x)=x^2$，则 $f'(x)=2x$。", "LaTeX/公式"),
        ("| 名称 | 值 |\n| --- | --- |\n| a | 1 |", "表格"),
        ("第一步，读入数据。第二步，建立图。第三步，跑 DFS。", "多步骤说明"),
        (
            "题解：使用 Tarjan 求点双连通分量。先维护 dfn 和 low，再在 low[v] >= dfn[u] 时弹栈生成点双。"
            "根节点需要单独按 DFS 子树数处理，最后再统计每个分量。",
            "题解、完整解法",
        ),
    ],
)
async def test_plain_text_allows_structured_or_long_reply_without_forcing_renderer(
    tmp_path: Path,
    text: str,
    reason: str,
) -> None:
    service = AgentService(tmp_path / "data")
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]
    event_id = service.db.insert_event(normalise_onebot_event(_event()))
    assert event_id
    turn_id = service.db.create_turn("100", [event_id])
    try:
        result = await service._execute_tool(
            turn_id,
            "100",
            "send_group_message",
            {"text": text},
            "structured-%s" % abs(hash(text)),
        )
        assert result["ok"] is True, reason
        assert result["message_id"] == "bot-1"
        assert adapter.calls == [
            ("send_group_msg", {"group_id": 100, "message": [{"type": "text", "data": {"text": text}}]})
        ]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_markdown_source_can_use_plain_text_without_a_user_exception(tmp_path: Path) -> None:
    service = AgentService(tmp_path / "data")
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]
    event_id = service.db.insert_event(normalise_onebot_event(_event()))
    assert event_id
    turn_id = service.db.create_turn("100", [event_id])
    raw_markdown = "# 标题\n\n```cpp\nint main() { return 0; }\n```"
    try:
        result = await service._execute_tool(
            turn_id,
            "100",
            "send_group_message",
            {"text": raw_markdown},
            "raw-markdown-source",
        )
        assert result == {"ok": True, "message_id": "bot-1"}
        assert adapter.calls[0][1]["message"][-1]["data"]["text"] == raw_markdown
    finally:
        await service.stop()


def test_actual_markflow_render_smoke_when_local_assets_are_available(tmp_path: Path) -> None:
    """Exercise real Edge + MarkFlow assets without relying on network access."""

    try:
        _detect_markflow_root()
        _find_browser()
    except Exception as exc:
        pytest.skip("本机没有 MarkFlow/Edge 渲染环境：%s" % exc)
    result = render_markdown_images(
        "# MarkFlow 预览\n\n这是 **粗体**、`inline code` 与 $E=mc^2$。\n\n"
        "$$\\int_0^1 x^2 dx = \\frac{1}{3}$$\n\n"
        "```python\ndef fib(n):\n    return n if n < 2 else fib(n - 1) + fib(n - 2)\n```\n\n"
        "> 公式和代码块应与 MarkFlow 预览一致。",
        tmp_path,
    )
    try:
        assert len(result.images) == 1
        image = result.images[0]
        assert image.width >= 720
        assert image.height > 200
        assert image.path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        shutil.rmtree(result.job_dir, ignore_errors=True)
