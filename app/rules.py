"""Safe storage for the local administrator's durable bot rules.

The control plane intentionally exposes only one rules document.  Keeping the
target path fixed here means a model/tool call can supply *contents* but can
never select a filesystem path to write.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional, Union


MAX_RULES_CHARS = 60_000
# UTF-8 can use at most four bytes for one Python Unicode code point.  The
# byte cap makes a manually-provided string unable to turn a character limit
# into an unexpectedly large on-disk file.
MAX_RULES_UTF8_BYTES = MAX_RULES_CHARS * 4


class RulesStoreError(RuntimeError):
    """The local ``rules.md`` document could not be read or written safely."""


class RulesContentTooLargeError(RulesStoreError, ValueError):
    """A rules document exceeds the bounded local-memory policy."""


class RulesStore:
    """Read and atomically replace the one project-local ``data/rules.md`` file.

    ``data_dir`` is a directory owned by the application.  The public API has
    no path parameter for ``read`` or ``write``: every operation is pinned to
    ``<data_dir>/rules.md``.  This is important because the caller may be an
    LLM tool and its inputs must never become filesystem paths.
    """

    filename = "rules.md"
    max_chars = MAX_RULES_CHARS
    max_utf8_bytes = MAX_RULES_UTF8_BYTES

    def __init__(self, data_dir: Union[Path, str]) -> None:
        # Resolve once so relative data directories cannot change their target
        # after the current working directory changes.  The filename remains
        # a constant rather than accepting a path from a caller.
        self._data_dir = Path(data_dir).expanduser().resolve()
        self._path = self._data_dir / self.filename

    @property
    def path(self) -> Path:
        """The fixed local rules path, exposed for status/UI display only."""

        return self._path

    def read(self) -> str:
        """Return current rules, or ``""`` when the document does not exist.

        Corrupt/non-UTF-8 or oversized operator-created files are reported
        instead of being silently treated as no rules.  That avoids a model
        acting as though durable instructions disappeared.
        """

        try:
            size = self._path.stat().st_size
        except FileNotFoundError:
            return ""
        except OSError as exc:
            raise RulesStoreError(f"无法读取 rules.md：{exc}") from exc

        if size > self.max_utf8_bytes:
            raise RulesContentTooLargeError(
                f"rules.md 超过允许的 UTF-8 大小上限（{self.max_utf8_bytes} 字节）。"
            )
        try:
            content = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RulesStoreError(f"无法读取 UTF-8 rules.md：{exc}") from exc
        self._validate_content(content)
        return content

    def write(self, content: str) -> None:
        """Atomically replace ``rules.md`` with UTF-8 ``content``.

        A temporary sibling is fully flushed before :func:`os.replace`, so a
        power loss or failed write never leaves a partial final rules file.
        """

        payload = self._validate_content(content)
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RulesStoreError(f"无法创建 rules.md 所在目录：{exc}") from exc

        descriptor = -1
        temporary_path: Optional[Path] = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".rules.",
                suffix=".tmp",
                dir=str(self._data_dir),
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1  # ownership passed to the file object
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._path)
        except RulesStoreError:
            raise
        except OSError as exc:
            raise RulesStoreError(f"无法原子写入 rules.md：{exc}") from exc
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    # The final rules file has either already replaced the
                    # temporary one or an operator can remove a harmless temp
                    # file later.  Never mask the primary write error here.
                    pass

    def clear(self) -> None:
        """Keep a durable empty rules document without accepting a path."""

        self.write("")

    def _validate_content(self, content: str) -> bytes:
        if not isinstance(content, str):
            raise TypeError("rules.md 内容必须是字符串")
        if len(content) > self.max_chars:
            raise RulesContentTooLargeError(
                f"rules.md 最多允许 {self.max_chars} 个字符，当前为 {len(content)}。"
            )
        try:
            payload = content.encode("utf-8")
        except UnicodeError as exc:
            raise RulesStoreError(f"rules.md 内容不能编码为 UTF-8：{exc}") from exc
        if len(payload) > self.max_utf8_bytes:
            raise RulesContentTooLargeError(
                f"rules.md 超过允许的 UTF-8 大小上限（{self.max_utf8_bytes} 字节）。"
            )
        return payload
