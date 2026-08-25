"""Per-conversation working directories for the local QQ agent.

The agent gets one directory per group/private conversation.  The directory
name is derived from the durable conversation id and is never selected by the
model.  File tools resolve paths relative to that directory, while command
tools run with it as their current working directory.
"""

from __future__ import annotations

import os
import re
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


class WorkspaceError(RuntimeError):
    """Base class for workspace/file operation failures."""


class WorkspacePathError(WorkspaceError):
    """A model supplied path cannot be resolved inside its session workspace."""


class WorkspaceFileTooLargeError(WorkspaceError):
    """A file/text payload is too large to pass through the agent API."""


class WorkspaceFileFormatError(WorkspaceError):
    """A binary/document format needs a reader that is not available locally."""


DEFAULT_WORKSPACE_ROOT = Path("D:/Workspace")
MAX_WORKSPACE_TEXT_CHARS = 200_000
MAX_WORKSPACE_FILE_BYTES = 100 * 1024 * 1024
MAX_WORKSPACE_PDF_PAGES = 2_000
MAX_WORKSPACE_ZIP_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_WORKSPACE_PDF_VISION_PAGES = 64


def _component(value: Any) -> str:
    text = str(value or "").strip()
    text = text.replace("\\", "-").replace("/", "-")
    text = re.sub(r"[^0-9A-Za-z._-]+", "-", text).strip(" .-")
    return text[:120] or "conversation"


@dataclass(frozen=True)
class WorkspaceFile:
    path: Path
    relative_path: str
    name: str
    size: int
    is_dir: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.relative_path,
            "name": self.name,
            "size": self.size,
            "is_dir": self.is_dir,
        }


class WorkspaceManager:
    def __init__(self, root: Path | str = DEFAULT_WORKSPACE_ROOT) -> None:
        self.root = Path(root)

    def conversation_path(self, conversation_id: str) -> Path:
        path = self.root / _component(conversation_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve(self, conversation_id: str, relative_path: str = "") -> Path:
        base = self.conversation_path(conversation_id).resolve()
        supplied = str(relative_path or ".").strip()
        candidate = Path(supplied)
        # Absolute paths are interpreted as paths in the session workspace,
        # rather than allowing a model to select another conversation's root.
        if candidate.is_absolute():
            candidate = Path(str(candidate).lstrip("\\/"))
        resolved = (base / candidate).resolve()
        try:
            resolved.relative_to(base)
        except ValueError as exc:
            raise WorkspacePathError("文件路径必须位于当前会话工作目录内") from exc
        return resolved

    def describe(self, conversation_id: str) -> Dict[str, Any]:
        path = self.conversation_path(conversation_id)
        return {"root": str(path), "conversation_id": str(conversation_id)}

    def list_files(self, conversation_id: str, relative_path: str = ".", recursive: bool = False) -> List[WorkspaceFile]:
        directory = self.resolve(conversation_id, relative_path)
        if not directory.exists():
            return []
        if not directory.is_dir():
            raise WorkspaceError("工作区路径不是目录：" + str(relative_path))
        iterator: Iterable[Path] = directory.rglob("*") if recursive else directory.iterdir()
        result: List[WorkspaceFile] = []
        base = self.conversation_path(conversation_id).resolve()
        for item in sorted(iterator, key=lambda value: value.as_posix().lower()):
            try:
                stat = item.stat()
            except OSError:
                continue
            result.append(
                WorkspaceFile(
                    path=item,
                    relative_path=item.resolve().relative_to(base).as_posix(),
                    name=item.name,
                    size=0 if item.is_dir() else int(stat.st_size),
                    is_dir=item.is_dir(),
                )
            )
        return result

    def read_text(self, conversation_id: str, relative_path: str, max_chars: int = MAX_WORKSPACE_TEXT_CHARS) -> str:
        path = self.resolve(conversation_id, relative_path)
        if not path.is_file():
            raise WorkspaceError("文件不存在：" + str(relative_path))
        max_chars = max(1, min(int(max_chars), MAX_WORKSPACE_TEXT_CHARS))
        size = path.stat().st_size
        if size > MAX_WORKSPACE_FILE_BYTES:
            raise WorkspaceFileTooLargeError("文件超过 100 MiB，不能直接读取")
        text = self._extract_text(path)
        if len(text) > max_chars:
            return text[:max_chars] + "\n[内容已按 max_chars 截断]"
        return text

    @staticmethod
    def _local_name(tag: str) -> str:
        return str(tag).rsplit("}", 1)[-1]

    @classmethod
    def _xml_text(cls, payload: bytes) -> str:
        """Extract visible text from OOXML/ODF XML while preserving lines."""

        try:
            root = ET.fromstring(payload)
        except (ET.ParseError, ValueError) as exc:
            raise WorkspaceFileFormatError("文档 XML 损坏，无法读取") from exc
        chunks: List[str] = []
        for element in root.iter():
            name = cls._local_name(element.tag)
            if name in {"tab"}:
                chunks.append("\t")
            elif name in {"br", "cr"}:
                chunks.append("\n")
            elif name in {"t", "text", "p"} and element.text:
                chunks.append(element.text)
                if name in {"p"}:
                    chunks.append("\n")
        return "".join(chunks).replace("\r\n", "\n").strip()

    @classmethod
    def _read_zip_document(cls, path: Path) -> str:
        suffix = path.suffix.lower()
        try:
            with zipfile.ZipFile(path) as archive:
                # A small OOXML/ODF file can expand into an enormous payload.
                # Refuse pathological archives before reading XML into memory.
                uncompressed = sum(max(0, int(info.file_size)) for info in archive.infolist())
                if uncompressed > MAX_WORKSPACE_ZIP_UNCOMPRESSED_BYTES:
                    raise WorkspaceFileTooLargeError("文档解压后超过 200 MiB，拒绝读取")
                names = set(archive.namelist())
                if suffix == ".docx":
                    target = "word/document.xml"
                    if target not in names:
                        raise WorkspaceFileFormatError("DOCX 缺少 word/document.xml")
                    return cls._xml_text(archive.read(target))
                if suffix == ".pptx":
                    slide_names = sorted(
                        name for name in names
                        if name.startswith("ppt/slides/slide") and name.endswith(".xml")
                    )
                    if not slide_names:
                        raise WorkspaceFileFormatError("PPTX 没有可读取的幻灯片文本")
                    return "\n\n".join(cls._xml_text(archive.read(name)) for name in slide_names)
                if suffix == ".xlsx":
                    shared: List[str] = []
                    if "xl/sharedStrings.xml" in names:
                        shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                        for item in shared_root.iter():
                            if cls._local_name(item.tag) == "si":
                                shared.append("".join(part.text or "" for part in item.iter() if cls._local_name(part.tag) == "t"))
                    sheets = sorted(
                        name for name in names
                        if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
                    )
                    if not sheets:
                        raise WorkspaceFileFormatError("XLSX 没有可读取的工作表")
                    rendered: List[str] = []
                    for sheet in sheets:
                        root = ET.fromstring(archive.read(sheet))
                        for row in (item for item in root.iter() if cls._local_name(item.tag) == "row"):
                            values: List[str] = []
                            for cell in (item for item in row if cls._local_name(item.tag) == "c"):
                                cell_type = cell.attrib.get("t", "")
                                value = ""
                                for child in cell:
                                    if cls._local_name(child.tag) == "v":
                                        value = child.text or ""
                                        break
                                    if cls._local_name(child.tag) == "is":
                                        value = "".join(part.text or "" for part in child.iter() if cls._local_name(part.tag) == "t")
                                if cell_type == "s" and value.isdigit() and int(value) < len(shared):
                                    value = shared[int(value)]
                                values.append(value)
                            if values:
                                rendered.append("\t".join(values))
                    return "\n".join(rendered).strip()
                if suffix == ".odt" and "content.xml" in names:
                    return cls._xml_text(archive.read("content.xml"))
        except zipfile.BadZipFile as exc:
            raise WorkspaceFileFormatError("文档压缩包损坏，无法读取") from exc
        raise WorkspaceFileFormatError("不支持的压缩文档格式")

    @classmethod
    def _read_pdf_document(cls, path: Path) -> str:
        """Extract text from a PDF without asking the model to run shell probes.

        ``pypdf`` is a regular project dependency.  Importing it lazily keeps
        the module importable in a partially installed environment while
        returning a precise tool error instead of an opaque ``import`` failure.
        Page separators are retained so the model can cite page-local context.
        Image-only/scanned PDFs are reported explicitly; OCR is not silently
        faked as an empty document.
        """

        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - packaging regression
            raise WorkspaceFileFormatError(
                "PDF 解析器未安装；请在项目环境运行 python -m pip install -e .（依赖 pypdf），然后重启服务。"
            ) from exc
        try:
            reader = PdfReader(str(path), strict=False)
            if getattr(reader, "is_encrypted", False):
                try:
                    unlocked = reader.decrypt("")
                except Exception as exc:  # pragma: no cover - pypdf version detail
                    raise WorkspaceFileFormatError("PDF 已加密，无法在没有密码的情况下读取") from exc
                if not unlocked:
                    raise WorkspaceFileFormatError("PDF 已加密，需要密码才能读取")
            page_count = len(reader.pages)
            if page_count > MAX_WORKSPACE_PDF_PAGES:
                raise WorkspaceFileTooLargeError(
                    "PDF 页数超过 %s，无法一次读取；请先拆分文件" % MAX_WORKSPACE_PDF_PAGES
                )
            pages: List[str] = []
            for index, page in enumerate(reader.pages, 1):
                try:
                    page_text = page.extract_text() or ""
                except Exception as exc:  # pragma: no cover - malformed page/provider detail
                    raise WorkspaceFileFormatError("PDF 第 %s 页文本提取失败：%s" % (index, exc)) from exc
                page_text = page_text.replace("\x00", "").strip()
                if page_text:
                    pages.append("=== 第 %s 页 ===\n%s" % (index, page_text))
            if not pages:
                raise WorkspaceFileFormatError(
                    "PDF 没有可提取的文字（可能是扫描图片）；当前服务未启用 OCR，请先提供可搜索 PDF 或文本版。"
                )
            return "\n\n".join(pages)
        except WorkspaceError:
            raise
        except Exception as exc:
            raise WorkspaceFileFormatError("PDF 解析失败：%s" % exc) from exc

    @staticmethod
    def _decode_text_bytes(payload: bytes, path: Path) -> str:
        # BOMs are authoritative.  GB18030 covers GBK/常见 Windows 中文代码文件.
        encodings = ["utf-8-sig"]
        # Trying UTF-16LE on every byte string can silently turn GBK into
        # plausible-looking garbage.  Only try BOM/embedded-NUL candidates.
        has_utf16_bom = payload.startswith((b"\xff\xfe", b"\xfe\xff"))
        has_nul = b"\x00" in payload[:4_096]
        if has_utf16_bom:
            encodings.extend(["utf-16", "utf-16-le", "utf-16-be"])
        elif has_nul:
            encodings.extend(["utf-16-le", "utf-16-be"])
        encodings.extend(["gb18030", "big5"])
        last_error: Optional[Exception] = None
        for encoding in encodings:
            try:
                text = payload.decode(encoding)
                # A wrong UTF-16 guess usually leaves NULs throughout a
                # source file; keep trying a more plausible code page.
                if encoding.startswith("utf-16") and text.count("\x00") > max(2, len(text) // 20):
                    continue
                return text.replace("\x00", "")
            except UnicodeDecodeError as exc:
                last_error = exc
        raise WorkspaceFileFormatError(
            "无法识别文本编码（支持 UTF-8、UTF-16、GB18030、Big5）：" + str(path.name)
        ) from last_error

    @classmethod
    def _extract_text(cls, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            return cls._read_pdf_document(path)
        if suffix in {".docx", ".pptx", ".xlsx", ".odt"}:
            return cls._read_zip_document(path)
        if suffix in {".doc", ".xls", ".ppt"}:
            raise WorkspaceFileFormatError(
                "%s 是旧式二进制文档；当前服务支持 PDF、DOCX/XLSX/PPTX/ODT。请先转换为现代格式或纯文本。"
                % suffix.upper().lstrip(".")
            )
        payload = path.read_bytes()
        return cls._decode_text_bytes(payload, path)

    def render_pdf_pages(
        self,
        conversation_id: str,
        relative_path: str,
        output_dir: Path | str,
        *,
        max_pages: int = MAX_WORKSPACE_PDF_VISION_PAGES,
        dpi: int = 120,
    ) -> List[Path]:
        """Render a scanned PDF into bounded JPEG pages for the vision model.

        This is intentionally a workspace operation: the model supplies only
        a relative path, while the service owns the output directory and
        removes it after the vision request.  It prevents the model from
        choosing arbitrary local files as image inputs.
        """

        path = self.resolve(conversation_id, relative_path)
        if path.suffix.lower() != ".pdf":
            raise WorkspaceFileFormatError("只有 PDF 文件支持页面视觉读取")
        if not path.is_file():
            raise WorkspaceError("文件不存在：" + str(relative_path))
        if path.stat().st_size > MAX_WORKSPACE_FILE_BYTES:
            raise WorkspaceFileTooLargeError("PDF 超过 100 MiB，不能直接视觉读取")
        try:
            import fitz  # PyMuPDF
        except ImportError as exc:  # pragma: no cover - packaging regression
            raise WorkspaceFileFormatError(
                "PDF 视觉解析器未安装；请运行 python -m pip install -e . 后重启服务。"
            ) from exc
        try:
            document = fitz.open(str(path))
            if getattr(document, "needs_pass", False):
                document.close()
                raise WorkspaceFileFormatError("PDF 已加密，需要密码才能读取")
            page_count = len(document)
            limit = max(1, min(int(max_pages), MAX_WORKSPACE_PDF_VISION_PAGES))
            if page_count > limit:
                document.close()
                raise WorkspaceFileTooLargeError(
                    "PDF 共 %s 页，视觉读取最多 %s 页；请先拆分文件或指定较小文档。" % (page_count, limit)
                )
            target_dir = Path(output_dir)
            target_dir.mkdir(parents=True, exist_ok=True)
            zoom = max(72, min(int(dpi), 180)) / 72.0
            matrix = fitz.Matrix(zoom, zoom)
            rendered: List[Path] = []
            try:
                for index, page in enumerate(document, 1):
                    pixmap = page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB, alpha=False)
                    target = target_dir / ("page-%04d.jpg" % index)
                    target.write_bytes(pixmap.tobytes("jpg", jpg_quality=70))
                    if target.stat().st_size > 5 * 1024 * 1024:
                        target.unlink(missing_ok=True)
                        raise WorkspaceFileTooLargeError("PDF 第 %s 页渲染图片超过 5 MiB" % index)
                    rendered.append(target)
            finally:
                document.close()
            if not rendered:
                raise WorkspaceFileFormatError("PDF 没有可读取的页面")
            return rendered
        except WorkspaceError:
            raise
        except Exception as exc:
            raise WorkspaceFileFormatError("PDF 页面渲染失败：%s" % exc) from exc

    def write_text(self, conversation_id: str, relative_path: str, content: str) -> Dict[str, Any]:
        if not isinstance(content, str):
            raise WorkspaceError("content 必须是字符串")
        if len(content) > MAX_WORKSPACE_TEXT_CHARS:
            raise WorkspaceFileTooLargeError("文本超过 %s 字符" % MAX_WORKSPACE_TEXT_CHARS)
        path = self.resolve(conversation_id, relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".agent-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                descriptor = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
            temporary_name = ""
        finally:
            if temporary_name:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
        return {"ok": True, "path": path.relative_to(self.conversation_path(conversation_id).resolve()).as_posix(), "bytes": len(content.encode("utf-8"))}

    def validate_file(self, conversation_id: str, relative_path: str) -> Path:
        path = self.resolve(conversation_id, relative_path)
        if not path.is_file():
            raise WorkspaceError("文件不存在：" + str(relative_path))
        if path.stat().st_size > MAX_WORKSPACE_FILE_BYTES:
            raise WorkspaceFileTooLargeError("文件超过 100 MiB，不能通过 QQ 发送")
        return path


__all__ = [
    "DEFAULT_WORKSPACE_ROOT",
    "MAX_WORKSPACE_FILE_BYTES",
    "MAX_WORKSPACE_PDF_PAGES",
    "MAX_WORKSPACE_PDF_VISION_PAGES",
    "MAX_WORKSPACE_TEXT_CHARS",
    "MAX_WORKSPACE_ZIP_UNCOMPRESSED_BYTES",
    "WorkspaceError",
    "WorkspaceFile",
    "WorkspaceFileTooLargeError",
    "WorkspaceFileFormatError",
    "WorkspaceManager",
    "WorkspacePathError",
]
