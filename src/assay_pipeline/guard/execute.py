"""Run a guard-approved query (Tech Spec §6.1–6.2): interrupt, sanitise errors, truncate.

Only `GuardDecision.rendered_sql` is ever executed. The model sees at most `max_rows_shown` rows
and `max_chars_shown` characters plus the total row count (D-16); the full result (at most
`max_rows_executed` rows) is hashed for the audit line. Database errors reach the model as one
of four generic messages, so catalog hints such as "Did you mean ..." never leak (H12).
"""

from __future__ import annotations

import hashlib
import json
import threading
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from assay_pipeline.config import Limits
from assay_pipeline.schemas import GuardDecision

ErrorClass = Literal["timeout", "memory", "permission", "other"]
GENERIC_MESSAGES: dict[str, str] = {
    "blocked": "That query is not permitted. Only single SELECT queries on the approved views "
    "are allowed.",
    "syntax": "The query could not be run (syntax error).",
    "timeout": "The query timed out. Narrow it with filters or aggregates.",
    "memory": "The query was too large to run. Narrow it with filters or aggregates.",
    "permission": "That query is not permitted. Only single SELECT queries on the approved "
    "views are allowed.",
    "other": "The query could not be run.",
}


class Connection(Protocol):  # the subset of duckdb.DuckDBPyConnection used here
    def execute(self, query: str) -> Any: ...
    def interrupt(self) -> None: ...


@dataclass(frozen=True)
class QueryResult:
    ok: bool
    columns: tuple[str, ...]
    rows_returned: int | None
    rows_shown: int | None
    result_sha256: str | None
    db_error_class: ErrorClass | None
    text: str  # exactly what the model sees


def classify(exc: BaseException) -> ErrorClass:
    name = type(exc).__name__
    if name == "InterruptException":
        return "timeout"
    if name == "OutOfMemoryException":
        return "memory"
    if name in ("PermissionException", "IOException", "InvalidInputException"):
        return "permission"
    return "other"


def _message(cls: ErrorClass, exc: BaseException) -> str:
    if cls == "other" and type(exc).__name__ in ("ParserException", "BinderException"):
        return GENERIC_MESSAGES["syntax"]
    return GENERIC_MESSAGES[cls]


def result_hash(columns: tuple[str, ...], rows: list[tuple[Any, ...]]) -> str:
    canonical = json.dumps(
        {"columns": list(columns), "rows": [list(r) for r in rows]},
        default=str,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


MAX_CELL_CHARS = 200  # one cell's share of what the model sees
MAX_HEADER_CHARS = 1_000
MAX_RESULT_BYTES = 8_000_000  # fetching stops here, whatever the row count (D-28)


FOOTER_MARK = "[result]"
# Every character str.splitlines() treats as a line boundary, plus the pipe and the marker
# bracket, is escaped in cells: data can never start a new line, split a column or look like
# the footer.
_ESCAPES = {
    ord("\\"): "\\\\", ord("|"): "\\|", ord("["): "\\[", ord("\n"): "\\n",
    ord("\r"): "\\r", 0x0B: "\\v", 0x0C: "\\f", 0x1C: "\\x1c", 0x1D: "\\x1d",
    0x1E: "\\x1e", 0x85: "\\x85", 0x2028: "\\u2028", 0x2029: "\\u2029",
}  # fmt: skip


def _cell(value: Any) -> str:
    """One cell as the model sees it: capped and escaped (see _ESCAPES)."""
    text = "NULL" if value is None else str(value)[: MAX_CELL_CHARS + 1]
    text = unicodedata.normalize("NFKC", text).translate(_ESCAPES)  # ［ → [ → \[
    return text if len(text) <= MAX_CELL_CHARS else text[: MAX_CELL_CHARS - 1] + "…"


def render_for_model(
    columns: tuple[str, ...],
    rows: list[tuple[Any, ...]],
    limits: Limits,
    *,
    truncated: bool = False,
) -> tuple[str, int]:
    """A plain-text table capped by rows and characters; returns (text, rows shown).

    The character cap always holds: header and footer count against it.
    """
    header = " | ".join(_cell(c) for c in columns)[:MAX_HEADER_CHARS]
    capped = truncated or len(rows) >= limits.max_rows_executed
    footer_tail = ", capped at the row or size limit" if capped else ""

    def footer(n: int) -> str:
        return f"{FOOTER_MARK} {n} of {len(rows)} rows shown{footer_tail}"

    lines = [header]
    shown = 0
    budget = limits.max_chars_shown - len(header) - 2 - len(footer(len(rows)))
    for row in rows[: limits.max_rows_shown]:
        line = " | ".join(_cell(v) for v in row)
        if budget - len(line) - 1 < 0:
            break
        lines.append(line)
        budget -= len(line) + 1
        shown += 1
    lines.append(footer(shown))
    text = "\n".join(lines)
    if len(text) > limits.max_chars_shown:  # a header alone over budget: hard cut, never exceed
        text = text[: limits.max_chars_shown - 1] + "…"
    return text, shown


def _fetch_bounded(cursor: Any, max_rows: int) -> tuple[list[tuple[Any, ...]], bool]:
    """Fetch row by row, stopping at max_rows or once about MAX_RESULT_BYTES have crossed into
    Python; every fetched row is kept, so the hash covers exactly what was fetched."""
    rows: list[tuple[Any, ...]] = []
    size = 0
    while len(rows) < max_rows:
        row = cursor.fetchone()
        if row is None:
            return rows, False
        rows.append(row)
        size += sum(len(v) if isinstance(v, str | bytes) else 16 for v in row)
        if size > MAX_RESULT_BYTES:
            return rows, len(rows) < max_rows
    return rows, False


def execute_guarded(con: Connection, decision: GuardDecision, limits: Limits) -> QueryResult:
    if decision.decision != "allowed" or decision.rendered_sql is None:
        return QueryResult(False, (), None, None, None, None, GENERIC_MESSAGES["blocked"])
    timer = threading.Timer(limits.timeout_seconds, con.interrupt)
    timer.daemon = True
    timer.start()
    try:
        cursor = con.execute(decision.rendered_sql)
        columns = tuple(d[0] for d in (cursor.description or []))
        rows, cut = _fetch_bounded(cursor, limits.max_rows_executed)
    except Exception as exc:  # every database error becomes a generic message
        cls = classify(exc)
        return QueryResult(False, (), None, None, None, cls, _message(cls, exc))
    finally:
        timer.cancel()
    text, shown = render_for_model(columns, rows, limits, truncated=cut)
    return QueryResult(True, columns, len(rows), shown, result_hash(columns, rows), None, text)
