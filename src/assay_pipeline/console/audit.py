"""An audit log held in memory (CON-04, H14): the same hash-chained lines as the file log
(S3.2), so the page can show a visitor exactly what the real log would record, but nothing is
written to disk and everything goes when the app restarts."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from assay_pipeline.audit import AuditError, AuditLog, line_hash
from assay_pipeline.schemas import SCHEMA_VERSION, ZERO_HASH, Anchor, AuditLine

MAX_LINES = 20_000  # a long-lived app restarts its log rather than grow without bound


class MemoryAuditLog(AuditLog):
    """`AuditLog`'s interface (append, last_seq), kept in a list. A subclass so the agent's tool
    and loop accept it unchanged; none of the file machinery is used."""

    def __init__(self, run_id: str, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.run_id = run_id
        self.clock = clock
        self._mutex = threading.Lock()
        self._closed = False
        self._broken: str | None = None
        self.resumed_unanchored = 0
        self.lines: list[dict[str, Any]] = []
        self._seq, self._last_hash, self._last_at = -1, ZERO_HASH, None
        self._write({"event": "genesis", "seq": 0, "prev_hash": ZERO_HASH})

    def _check_open(self) -> None:
        if self._closed:
            raise AuditError("the log is closed")
        if len(self.lines) >= MAX_LINES:
            raise AuditError("the in-memory log is full; restart the console")

    def _write(self, fields: dict[str, Any]) -> AuditLine:
        empty = {k: None for k in AuditLine.model_fields if k != "schema_version"}
        draft = {**empty, **fields, "schema_version": SCHEMA_VERSION, "run_id": self.run_id,
                 "at": self._now().isoformat()}  # fmt: skip
        draft["prev_hash"] = fields.get("prev_hash", self._last_hash)
        draft["seq"] = fields.get("seq", self._seq + 1)
        draft["hash"] = "0" * 64
        dumped = AuditLine.model_validate(draft).model_dump(mode="json")
        dumped["hash"] = line_hash(dumped)
        line = AuditLine.model_validate(dumped)
        self.lines.append(dumped)
        self._seq, self._last_hash, self._last_at = line.seq, line.hash, line.at
        return line

    def anchor(self) -> Anchor:
        raise AuditError("an in-memory log has no anchors: it ends when the app restarts")

    def close(self) -> Anchor:  # type: ignore[override]
        self._closed = True
        return Anchor(log="memory", seq=self._seq, hash=self._last_hash, at=self._last_at)  # type: ignore[arg-type]

    def since(self, seq: int) -> list[dict[str, Any]]:
        """Lines after `seq` (a case's own lines, for the page's audit view)."""
        return [x for x in self.lines if x["seq"] > seq]

    def verify_chain(self) -> bool:
        prev = ZERO_HASH
        for n, x in enumerate(self.lines):
            if x["seq"] != n or x["prev_hash"] != prev or line_hash(x) != x["hash"]:
                return False
            prev = x["hash"]
        return True
