"""Validation helpers for evidence-backed, non-vector group memories.

The database deliberately stores ordinary text and source references rather
than embeddings.  Keeping normalization here makes the persistence boundary
strict without coupling it to a particular LLM or service implementation.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping


MEMORY_CONFIDENCE_STATUSES = frozenset({"confirmed", "uncertain", "retracted"})
MEMORY_CONFLICT_STATUSES = frozenset({"open", "resolved"})

MAX_MEMORY_KEY_CHARS = 300
MAX_MEMORY_KIND_CHARS = 64
MAX_MEMORY_STATEMENT_CHARS = 8_000
MAX_MEMORY_FIELD_CHARS = 2_000
MAX_MEMORY_EVIDENCE_CHARS = 8_000
MAX_MEMORY_REASON_CHARS = 4_000
MAX_MEMORY_METADATA_JSON_CHARS = 20_000
MAX_MEMORY_EVIDENCE_PER_WRITE = 100


def bounded_text(value: Any, *, name: str, maximum: int, required: bool = False) -> str:
    """Return a normalized text field, rejecting silent truncation."""

    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        text = str(value).strip()
    if required and not text:
        raise ValueError(f"{name} 不能为空")
    if len(text) > maximum:
        raise ValueError(f"{name} 最多允许 {maximum} 个字符")
    return text


def normalize_confidence_status(value: Any, *, allow_retracted: bool = False) -> str:
    status = bounded_text(
        value or "uncertain",
        name="记忆置信状态",
        maximum=32,
        required=True,
    ).lower()
    allowed = MEMORY_CONFIDENCE_STATUSES if allow_retracted else MEMORY_CONFIDENCE_STATUSES - {"retracted"}
    if status not in allowed:
        raise ValueError("记忆置信状态必须是 confirmed、uncertain%s" % (" 或 retracted" if allow_retracted else ""))
    return status


def normalize_memory_evidence(evidence: Iterable[Mapping[str, Any]] | Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Normalize evidence and require both a quote and a local source ID.

    Aliases mirror the concise shape an LLM normally emits: ``event_id``,
    ``message_id`` and ``quote``.  The canonical names are returned so callers
    never have to guess which shape reached SQLite.
    """

    if isinstance(evidence, Mapping):
        raw_items = [evidence]
    else:
        raw_items = list(evidence or [])
    if not raw_items:
        raise ValueError("每条记忆至少需要一条来源证据")
    if len(raw_items) > MAX_MEMORY_EVIDENCE_PER_WRITE:
        raise ValueError(f"单次最多写入 {MAX_MEMORY_EVIDENCE_PER_WRITE} 条记忆证据")

    normalized: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, Mapping):
            raise TypeError(f"第 {index} 条记忆证据必须是对象")
        raw_event_id = item.get("source_event_id", item.get("event_id"))
        source_event_id = None
        if raw_event_id not in (None, ""):
            try:
                source_event_id = int(raw_event_id)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"第 {index} 条证据的 event_id 必须是整数") from exc
            if source_event_id <= 0:
                raise ValueError(f"第 {index} 条证据的 event_id 必须为正数")
        source_message_id = bounded_text(
            item.get("source_message_id", item.get("message_id", "")),
            name=f"第 {index} 条证据的 message_id",
            maximum=256,
        )
        if source_event_id is None and not source_message_id:
            raise ValueError(f"第 {index} 条证据必须包含 event_id 或 message_id")
        evidence_text = bounded_text(
            item.get("evidence_text", item.get("quote", "")),
            name=f"第 {index} 条证据文本",
            maximum=MAX_MEMORY_EVIDENCE_CHARS,
            required=True,
        )
        observed_at = bounded_text(
            item.get("observed_at", ""),
            name=f"第 {index} 条证据时间",
            maximum=128,
        )
        normalized.append(
            {
                "source_event_id": source_event_id,
                "source_message_id": source_message_id,
                "evidence_text": evidence_text,
                "observed_at": observed_at,
            }
        )
    return normalized


def make_fts_query(query: str) -> str:
    """Build a literal-token FTS5 expression from untrusted search text."""

    tokens = [token for token in str(query or "").split() if token]
    if not tokens:
        return ""
    # FTS5 escapes a quote inside a quoted phrase by doubling it.  Treating
    # every token as a phrase prevents model/user input from becoming FTS
    # operators such as NEAR, column filters, or unbalanced parentheses.
    return " OR ".join('"%s"' % token.replace('"', '""') for token in tokens[:20])
