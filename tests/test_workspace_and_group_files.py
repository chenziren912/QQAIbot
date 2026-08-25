from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.llm import TOOLS, responses_tools
from app.service import AgentService, normalise_onebot_event
from app.workspace import WorkspaceManager


class FakeAdapter:
    connected = True

    def __init__(self, source: Path) -> None:
        self.source = source
        self.calls = []

    async def call(self, action, params=None, timeout=None):
        self.calls.append((action, dict(params or {})))
        if action in {"send_group_msg", "send_private_msg", "upload_group_file", "upload_private_file"}:
            return {"status": "ok", "data": {"message_id": str(1000 + len(self.calls))}}
        if action == "get_group_root_files":
            return {"status": "ok", "data": {"files": [{"file_id": "f-1", "file_name": "a.txt", "file_size": 3}]}}
        if action == "get_file":
            return {"status": "ok", "data": {"file": str(self.source)}}
        raise AssertionError(action)


class GroupFileUrlAdapter(FakeAdapter):
    async def call(self, action, params=None, timeout=None):
        self.calls.append((action, dict(params or {})))
        if action == "get_group_file_url":
            return {"status": "ok", "data": {"file": str(self.source)}}
        return await super().call(action, params, timeout)


def test_group_upload_event_contains_file_metadata() -> None:
    event = normalise_onebot_event(
        {
            "post_type": "notice",
            "notice_type": "group_upload",
            "group_id": 123,
            "user_id": 9,
            "file": {"id": "f-1", "name": "题解.cpp", "size": 42, "busid": 7},
        }
    )
    assert event["normalized_text"] == "[群文件上传] 题解.cpp (42 bytes)"
    assert event["content"]["file"]["id"] == "f-1"
    assert "题解.cpp" in AgentService._format_events([event])


def test_workspace_isolated_and_atomic_text_write(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "Workspace")
    manager.write_text("100", "src/main.cpp", "int main() {}")
    manager.write_text("private:200", "note.txt", "hello")
    assert manager.read_text("100", "src/main.cpp") == "int main() {}"
    assert manager.read_text("private:200", "note.txt") == "hello"
    with pytest.raises(Exception):
        manager.read_text("100", "../private-200/note.txt")


def test_workspace_reads_common_windows_encodings_and_ooxml_documents(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "Workspace")
    root = manager.conversation_path("100")
    (root / "gbk.cpp").write_bytes("#include <iostream>\n// 中文注释".encode("gb18030"))
    (root / "utf16.txt").write_text("第一行\n第二行", encoding="utf-16")
    assert "中文注释" in manager.read_text("100", "gbk.cpp")
    assert "第二行" in manager.read_text("100", "utf16.txt")

    with zipfile.ZipFile(root / "note.docx", "w") as archive:
        archive.writestr(
            "word/document.xml",
            "<document xmlns='urn:w'><body><p><t>标题</t></p><p><t>正文内容</t></p></body></document>",
        )
    assert "标题" in manager.read_text("100", "note.docx")
    assert "正文内容" in manager.read_text("100", "note.docx")

    with zipfile.ZipFile(root / "table.xlsx", "w") as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            "<sst xmlns='urn:x'><si><t>姓名</t></si></sst>",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            "<worksheet xmlns='urn:x'><sheetData><row><c t='s'><v>0</v></c><c><v>42</v></c></row></sheetData></worksheet>",
        )
    table = manager.read_text("100", "table.xlsx")
    assert "姓名" in table and "42" in table


def test_workspace_pdf_text_and_scanned_page_rendering(tmp_path: Path) -> None:
    """PDFs are read by the service, not by model-authored probe scripts."""

    fitz = pytest.importorskip("fitz")
    manager = WorkspaceManager(tmp_path / "Workspace")
    root = manager.conversation_path("pdf-test")

    text_pdf = root / "text.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "PDF text layer works")
    document.save(str(text_pdf))
    document.close()
    assert "PDF text layer works" in manager.read_text("pdf-test", "text.pdf")

    scanned_pdf = root / "scanned.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "视觉页面内容")
    # Rasterize the page into an image-only PDF so pypdf has no text layer.
    pixmap = page.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
    image_only = fitz.open()
    image_page = image_only.new_page(width=page.rect.width, height=page.rect.height)
    image_page.insert_image(image_page.rect, stream=pixmap.tobytes("png"))
    image_only.save(str(scanned_pdf))
    image_only.close()
    document.close()
    with pytest.raises(Exception, match="没有可提取"):
        manager.read_text("pdf-test", "scanned.pdf")
    rendered = manager.render_pdf_pages("pdf-test", "scanned.pdf", root / "rendered")
    assert len(rendered) == 1
    assert rendered[0].suffix == ".jpg" and rendered[0].stat().st_size > 0


@pytest.mark.asyncio
async def test_file_tools_command_and_group_file_download(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / "Workspace"
    monkeypatch.setenv("QQ_AI_WORKSPACE_ROOT", str(workspace_root))
    service = AgentService(tmp_path / "data")
    service.db.upsert_group("123", "测试群")
    source = tmp_path / "remote.txt"
    source.write_text("remote", encoding="utf-8")
    adapter = FakeAdapter(source)
    service.adapter = adapter
    turn_id = service.db.create_turn("123", [])

    wrote = await service._execute_tool(
        turn_id, "123", "write_workspace_file", {"path": "edited.txt", "content": "done"}, "write-1"
    )
    assert wrote["ok"] is True
    command = await service._execute_tool(
        turn_id, "123", "execute_command", {"command": "python -c \"print('command-ok')\""}, "cmd-1"
    )
    assert command["ok"] is True
    assert "command-ok" in command["output"]
    assert any(
        call[0] == "send_group_msg"
        and "正在执行指令：python -c" in str(call[1].get("message"))
        for call in adapter.calls
    )

    downloaded = await service._execute_tool(
        turn_id, "123", "Builtin_download_group_file", {"file_id": "f-1", "filename": "copy.txt"}, "download-1", operation_slot=1
    )
    assert downloaded["ok"] is True
    assert (workspace_root / "123" / "copy.txt").read_text(encoding="utf-8") == "remote"

    listed = await service._execute_tool(turn_id, "123", "Builtin_list_group_files", {}, "list-1")
    assert listed["ok"] is True
    assert listed["files"][0]["file_id"] == "f-1"

    sent = await service._execute_tool(
        turn_id, "123", "send_group_file", {"path": "edited.txt", "name": "result.txt"}, "send-1", operation_slot=2
    )
    assert sent["ok"] is True
    assert any(call[0] == "upload_group_file" for call in adapter.calls)


@pytest.mark.asyncio
async def test_group_files_use_napcat_group_file_url_before_cached_get_file(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("QQ_AI_WORKSPACE_ROOT", str(tmp_path / "Workspace"))
    service = AgentService(tmp_path / "data")
    service.db.upsert_group("123", "测试群")
    source = tmp_path / "remote-gbk.txt"
    source.write_bytes("中文文件".encode("gb18030"))
    adapter = GroupFileUrlAdapter(source)
    service.adapter = adapter
    turn_id = service.db.create_turn("123", [])

    downloaded = await service._execute_tool(
        turn_id,
        "123",
        "Builtin_download_group_file",
        {"file_id": "/file-uuid", "filename": "remote.txt", "busid": "7"},
        "download-url",
    )
    assert downloaded["ok"] is True
    assert adapter.calls[0] == (
        "get_group_file_url",
        {"group_id": 123, "file_id": "/file-uuid", "busid": 7},
    )
    read = await service._execute_tool(
        turn_id,
        "123",
        "read_workspace_file",
        {"path": "remote.txt"},
        "read-url",
    )
    assert read["ok"] is True
    assert read["content"] == "中文文件"


def test_new_tools_are_available_in_chat_and_responses() -> None:
    names = {tool["function"]["name"] for tool in TOOLS}
    expected = {
        "list_workspace_files",
        "read_workspace_file",
        "write_workspace_file",
        "execute_command",
        "send_group_file",
        "Builtin_list_group_files",
        "Builtin_download_group_file",
        "Builtin_bilibili_download",
        "Builtin_youtube_download",
    }
    assert expected <= names
    assert expected <= {tool["name"] for tool in responses_tools()}
