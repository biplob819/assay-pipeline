"""The hash-chained, anchored audit log (Tech Spec §6.3, D-7).

Line 0 is a genesis line whose `prev_hash` is 64 zeros. Every line's `hash` is the SHA-256 of
its canonical JSON (sorted keys, UTF-8, no whitespace) without the `hash` field, and that JSON
includes `prev_hash`, so changing, deleting, inserting or reordering any line breaks the chain.
Anchors (seq and hash) are written to a separate file, so a truncated tail or a rewritten log is
caught too. Stated limit: whoever can rewrite both the log and the anchor file can forge history;
the production step is WORM storage or an external timestamping service.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from pydantic import ValidationError

from assay_pipeline.schemas import AUDIT_SCHEMA_VERSION, ZERO_HASH, Anchor, AuditLine

RESERVED_FIELDS = frozenset({"seq", "prev_hash", "hash", "at", "run_id", "schema_version"})
MAX_LOG_BYTES = 1 << 30  # verify refuses larger files rather than exhausting memory
_WIN_LOCK_OFFSET = 1 << 40  # Windows byte-range lock far past EOF, so readers are never blocked


class AuditError(RuntimeError):
    """The log cannot be written safely (held by another writer, closed, or broken)."""


def canonical_json(fields: dict[str, Any]) -> bytes:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def line_hash(line_fields: dict[str, Any]) -> str:
    body = {k: v for k, v in line_fields.items() if k != "hash"}
    return hashlib.sha256(canonical_json(body)).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _lines(raw: bytes) -> tuple[list[bytes], bool]:
    """Split on b"\\n" only (never U+2028 or other boundaries); returns (lines, complete)."""
    if not raw:
        return [], True
    parts = raw.split(b"\n")
    complete = parts[-1] == b""
    return (parts[:-1] if complete else parts), complete


def _unlock(handle: IO[bytes]) -> None:
    """Release explicitly: Windows may drop a lock only some time after the handle closes."""
    if sys.platform == "win32" and not handle.closed:
        import msvcrt

        try:
            handle.seek(_WIN_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass  # not held (the lock attempt failed); closing is enough


def _lock_shared_file(handle: IO[bytes]) -> None:
    """Block until this process alone may write the anchors file (shared between writers)."""
    if sys.platform == "win32":
        import msvcrt

        handle.seek(_WIN_LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # retries for about 10 s
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _open_log(path: Path) -> IO[bytes]:
    """Open for append without blocking: a FIFO swapped in at the path cannot hang the open."""
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags | nonblock, 0o644)
    except OSError as exc:
        raise AuditError(f"cannot open {path} for writing: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AuditError(f"{path} is not a regular file")
        if nonblock and sys.platform != "win32":
            import fcntl

            fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~nonblock)
        return os.fdopen(fd, "ab")
    except BaseException:
        os.close(fd)
        raise


def _lock(handle: IO[bytes]) -> None:
    """An OS lock on the open log file itself: per physical file (any path, symlink or
    hardlink), independent of TMPDIR or user, and gone with the process that held it."""
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(_WIN_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise AuditError("another writer holds this log") from exc


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    lines: int
    anchors: int
    first_broken_seq: int | None
    reason: str | None
    unanchored_tail: int  # lines after the last anchor (truncation there is undetectable)


def _fail(n: int, anchors: int, seq: int | None, reason: str) -> VerifyReport:
    return VerifyReport(False, n, anchors, seq, reason, 0)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _read_regular(path: Path) -> bytes:
    """Read a regular file only (a FIFO or device would hang or never end), within the cap."""
    st = path.stat()
    if not stat.S_ISREG(st.st_mode):
        raise OSError(f"{path} is not a regular file")
    if st.st_size > MAX_LOG_BYTES:
        raise OSError(f"{path} is larger than {MAX_LOG_BYTES} bytes")
    return path.read_bytes()


def _anchor_lines(apath: Path) -> list[bytes]:
    lines, complete = _lines(_read_regular(apath))
    return lines if complete else lines[:-1]  # a crash mid-anchor: the partial line anchors nothing


def _names_log(apath: Path, name: str) -> bool:
    """True when any anchor names this log, or the anchors file cannot be read (fail closed)."""
    if not apath.exists():
        return False
    try:
        lines, complete = _lines(_read_regular(apath))
        if not complete and lines[-1].strip():
            return True  # a partial last anchor may name it: fail closed
        for raw in lines if complete else lines[:-1]:
            fields = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
            if Anchor.model_validate(fields).log == name:
                return True
    except (OSError, UnicodeDecodeError, ValueError, ValidationError, RecursionError):
        return True
    return False


def verify(
    log: Path | str, anchors: Path | str | None = None, *, require_anchor: bool = True
) -> VerifyReport:
    """Re-hash every line, check the chain and seq continuity, and check every anchor.

    Never raises: any unreadable, undecodable or malformed input is a failing report. When an
    anchors file is given, at least one anchor must name this log (unless require_anchor=False).
    """
    log = Path(log)
    try:
        raw = _read_regular(log)
    except (OSError, ValueError, MemoryError) as exc:
        return _fail(0, 0, None, f"cannot read {log}: {exc}")
    text_lines, complete = _lines(raw)
    if not complete:
        return _fail(len(text_lines), 0, len(text_lines) - 1, "the last line is incomplete")
    if not text_lines:
        return _fail(0, 0, 0, "the log is empty (no genesis line)")
    prev = ZERO_HASH
    prev_at = None
    hashes: list[str] = []
    for n, raw_line in enumerate(text_lines):
        try:
            text = raw_line.decode("utf-8")
            fields = json.loads(text, object_pairs_hook=_no_duplicate_keys)
            line = AuditLine.model_validate(fields)
        except (UnicodeDecodeError, ValueError, ValidationError, RecursionError, TypeError) as exc:
            return _fail(n, 0, n, f"line {n} is not a valid audit line: {type(exc).__name__}")
        if canonical_json(fields).decode("utf-8") != text:
            return _fail(n, 0, n, f"line {n} is not in canonical form")
        if line.seq != n:
            return _fail(n, 0, n, f"line {n} has seq {line.seq}")
        if (n == 0) != (line.event == "genesis"):
            return _fail(n, 0, n, "only line 0 may be, and must be, the genesis line")
        if line.prev_hash != prev:
            return _fail(n, 0, n, f"line {n} does not link to line {n - 1}")
        if line_hash(fields) != line.hash:
            return _fail(n, 0, n, f"line {n} was changed after it was written")
        if prev_at is not None and line.at < prev_at:
            return _fail(n, 0, n, f"line {n} is timed before line {n - 1}")
        prev, prev_at = line.hash, line.at
        hashes.append(line.hash)
    total = len(hashes)
    if anchors is None:
        return VerifyReport(True, total, 0, None, None, total)
    apath = Path(anchors)
    try:
        anchor_lines = _anchor_lines(apath)
    except (OSError, ValueError, MemoryError) as exc:
        return _fail(total, 0, None, f"cannot read anchors {apath}: {exc}")
    n_anchors = 0
    matched = 0
    last_anchored = -1
    for k, raw_anchor in enumerate(anchor_lines):
        try:
            fields = json.loads(raw_anchor.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
            a = Anchor.model_validate(fields)
        except (UnicodeDecodeError, ValueError, ValidationError, RecursionError, TypeError) as exc:
            return _fail(
                total, n_anchors, None, f"anchor line {k} is invalid: {type(exc).__name__}"
            )
        n_anchors += 1
        if a.log != log.name:
            continue
        matched += 1
        if a.seq >= total:
            return _fail(
                total,
                n_anchors,
                total,
                f"anchor at seq {a.seq} but the log ends at seq {total - 1}: truncated",
            )
        if hashes[a.seq] != a.hash:
            return _fail(
                total, n_anchors, a.seq, f"line {a.seq} does not match its anchor: rewritten"
            )
        last_anchored = max(last_anchored, a.seq)
    if require_anchor and matched == 0:
        return _fail(total, n_anchors, None, f"no anchor in {apath.name} names {log.name}")
    return VerifyReport(True, total, n_anchors, None, None, total - 1 - last_anchored)


class AuditLog:
    """Appends hash-chained lines for one run. One writer per log file, enforced by an OS lock."""

    def __init__(
        self,
        path: Path | str,
        run_id: str,
        anchors: Path | str | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.path = Path(path)
        self.anchors_path = Path(anchors) if anchors else self.path.with_suffix(".anchors.jsonl")
        self.run_id = run_id
        self.clock = clock
        self._mutex = threading.Lock()
        self._closed = False
        self._broken: str | None = None
        self.resumed_unanchored = 0  # lines after the last anchor when resumed (S3.3 reports it)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle: IO[bytes] = _open_log(self.path)
        try:
            _lock(self._handle)
            st = os.stat(self.path)  # path-based on both sides of the identity check
            self._identity = (st.st_dev, st.st_ino)
            self._seq, self._last_hash, self._last_at = self._resume()
            self._size = os.fstat(self._handle.fileno()).st_size
            if self._seq < 0:
                self._write({"event": "genesis", "seq": 0, "prev_hash": ZERO_HASH})
        except BaseException:
            _unlock(self._handle)
            self._handle.close()
            raise

    def _resume(self) -> tuple[int, str, datetime | None]:
        raw = self.path.read_bytes()
        if not raw:
            if _names_log(self.anchors_path, self.path.name):
                raise AuditError(
                    f"{self.path} is empty but {self.anchors_path.name} anchors it: the log was "
                    "truncated; refusing to start it over"
                )
            return -1, ZERO_HASH, None
        if not self.anchors_path.exists():
            raise AuditError(
                f"{self.path} has lines but {self.anchors_path.name} is missing: refusing to "
                "resume (and re-anchor) a log that cannot be checked"
            )
        report = verify(self.path, self.anchors_path)
        if not report.ok:
            raise AuditError(
                f"{self.path} is broken at seq {report.first_broken_seq}: {report.reason}"
            )
        lines, _ = _lines(raw)
        first = AuditLine.model_validate_json(lines[0].decode("utf-8"))
        if first.run_id != self.run_id:
            raise AuditError(f"{self.path} belongs to run {first.run_id!r}, not {self.run_id!r}")
        self._repair_anchor_tail()  # only once the log is known good and ours
        self.resumed_unanchored = report.unanchored_tail
        last = AuditLine.model_validate_json(lines[-1].decode("utf-8"))
        return last.seq, last.hash, last.at

    def _repair_anchor_tail(self) -> None:
        """Drop a partial final anchor line left by a crash. Truncating in place never removes
        a complete anchor, even if this process dies part-way."""
        raw = self.anchors_path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            with self.anchors_path.open("r+b") as fh:
                fh.truncate(raw.rfind(b"\n") + 1)
                fh.flush()
                os.fsync(fh.fileno())

    @property
    def last_seq(self) -> int:
        """The seq of the last line written (a trial records its own line range with it)."""
        return self._seq

    def close(self) -> Anchor:
        """Anchor the final line and release the writer lock; the object is unusable after."""
        with self._mutex:
            if self._closed:
                raise AuditError("the log is already closed")
            try:
                return self._anchor_locked()
            finally:
                self._closed = True
                _unlock(self._handle)
                self._handle.close()

    def __enter__(self) -> AuditLog:
        return self

    def __exit__(self, *exc: object) -> None:
        if not self._closed:
            self.close()

    def _check_open(self) -> None:
        if self._closed:
            raise AuditError("the log is closed; open a new AuditLog to continue it")
        if self._broken:
            raise AuditError(f"the log is broken ({self._broken}); nothing more is written")
        try:
            st = os.stat(self.path)
        except OSError as exc:
            self._broken = f"{self.path} disappeared"
            raise AuditError(f"the log is broken ({self._broken})") from exc
        if (st.st_dev, st.st_ino) != self._identity:
            self._broken = f"{self.path} was replaced while open"
            raise AuditError(f"the log is broken ({self._broken}); nothing more is written")
        size = os.fstat(self._handle.fileno()).st_size
        if size != self._size:
            self._broken = f"{self.path} changed size under the writer ({self._size} -> {size})"
            raise AuditError(f"the log is broken ({self._broken}); nothing more is written")

    def _now(self) -> datetime:
        """The clock, checked (timezone-aware) and never earlier than the last line."""
        at = self.clock()
        if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
            raise AuditError(f"the clock returned {at!r}, not a timezone-aware datetime")
        if self._last_at is not None and at < self._last_at:
            at = self._last_at  # a clock stepping back never makes time run backwards
        return at

    def _write(self, fields: dict[str, Any]) -> AuditLine:
        empty = {k: None for k in AuditLine.model_fields if k != "schema_version"}
        at = self._now()
        draft = {
            **empty,
            **fields,
            "schema_version": AUDIT_SCHEMA_VERSION,
            "run_id": self.run_id,
            "at": at.isoformat(),
        }
        draft["prev_hash"] = fields.get("prev_hash", self._last_hash)
        draft["seq"] = fields.get("seq", self._seq + 1)
        draft["hash"] = "0" * 64
        line = AuditLine.model_validate(draft)  # field rules per event (N1.1)
        dumped = line.model_dump(mode="json")
        dumped["hash"] = line_hash(dumped)
        line = AuditLine.model_validate(dumped)
        data = canonical_json(dumped) + b"\n"
        try:
            self._handle.write(data)
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except BaseException as exc:
            # bytes may or may not be on disk: the chain head is unknown, so stop for good
            self._broken = f"a write failed: {type(exc).__name__}"
            raise AuditError(
                f"the log is broken ({self._broken}); nothing more is written"
            ) from exc
        self._seq, self._last_hash, self._last_at = line.seq, line.hash, line.at
        self._size += len(data)
        return line

    def append(self, **fields: Any) -> AuditLine:
        """Append one question, query or answer line; the log sets seq, hashes, time and run."""
        if fields.get("event") == "genesis" or RESERVED_FIELDS & set(fields):
            raise AuditError(
                "seq, prev_hash, hash, at, run_id, schema_version and genesis are set by the "
                "log, not callers"
            )
        with self._mutex:
            self._check_open()
            return self._write(fields)

    def anchor(self) -> Anchor:
        with self._mutex:
            return self._anchor_locked()

    def _anchor_locked(self) -> Anchor:
        self._check_open()
        a = Anchor(
            schema_version=AUDIT_SCHEMA_VERSION,
            log=self.path.name,
            seq=self._seq,
            hash=self._last_hash,
            at=self._now(),
        )
        data = a.model_dump_json().encode("utf-8") + b"\n"
        try:
            with self.anchors_path.open("ab") as fh:
                _lock_shared_file(fh)  # held to the close: no other writer can interleave
                start = fh.seek(0, os.SEEK_END)
                try:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                except BaseException:
                    fh.truncate(start)  # never leave a half anchor for later anchors to follow
                    raise
        except BaseException as exc:
            self._broken = f"an anchor write failed: {type(exc).__name__}"
            raise AuditError(
                f"the log is broken ({self._broken}); nothing more is written"
            ) from exc
        return a
