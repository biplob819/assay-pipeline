"""The content-addressed raw cache (D-2): every API response, frozen, per source release.

Layout: <root>/<source>/<release>/<key[:2]>/<key>.json.gz plus index.jsonl (one `RawResponse`
per stored body) and cursor.json (resume state). Bodies are gzip with mtime 0, so the same
response always gives the same file bytes.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from assay_pipeline.schemas import SCHEMA_VERSION, RawResponse
from assay_pipeline.schemas.data import Source

INDEX = "index.jsonl"
CURSOR = "cursor.json"
LOCK = ".lock"
RELEASE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
WINDOWS_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


class CacheError(RuntimeError):
    """The cache is inconsistent: a body does not match its hash, or the index is broken."""


def _safe_release(name: str) -> bool:
    """Allow-listed, no trailing dot, no Windows device name (with or without an extension)."""
    return (
        RELEASE_RE.fullmatch(name) is not None
        and not name.endswith(".")
        and name.split(".")[0].upper() not in WINDOWS_RESERVED
    )


def request_key(method: str, url: str, params: dict[str, Any]) -> str:
    canonical = json.dumps(
        [method.upper(), url, sorted((str(k), str(v)) for k, v in params.items())],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class RawCache:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def release_dir(self, source: Source, release: str) -> Path:
        if not _safe_release(release):
            raise CacheError(f"unsafe release name {release!r}")
        return self.root / source / release

    def releases(self, source: Source) -> list[str]:
        base = self.root / source
        if not base.is_dir():
            return []
        return sorted(p.name for p in base.iterdir() if p.is_dir() and _safe_release(p.name))

    def lock(self, source: Source) -> Path:
        """Take the single-writer lock for a source; raises if another ingest holds it."""
        path = self.root / source / LOCK
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="ascii") as fh:  # exclusive create: fails if held
                fh.write(str(os.getpid()))
        except FileExistsError as exc:
            raise CacheError(
                f"{path} exists: another ingest is writing this cache "
                "(delete the file if no ingest is running)"
            ) from exc
        return path

    def _body_path(self, source: Source, release: str, key: str) -> Path:
        return self.release_dir(source, release) / key[:2] / f"{key}.json.gz"

    def index(self, source: Source, release: str) -> dict[str, RawResponse]:
        path = self.release_dir(source, release) / INDEX
        out: dict[str, RawResponse] = {}
        if not path.is_file():
            return out
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            try:
                row = RawResponse.model_validate_json(line)
            except ValueError as exc:
                raise CacheError(f"{path}:{n}: invalid index line: {exc}") from exc
            out[row.request_key] = row
        return out

    def has(self, source: Source, release: str, key: str) -> bool:
        return key in self.index(source, release)

    def put(
        self,
        source: Source,
        release: str,
        key: str,
        url: str,
        status: int,
        body: bytes,
        fetched_at: str | None,
    ) -> RawResponse:
        if self.has(source, release, key):
            raise CacheError(f"{key} is already cached for {source} {release}")
        if not fetched_at:
            raise CacheError(f"{url}: the server sent no Date header; fetched_at must come from it")
        when = fetched_at
        row = RawResponse(
            schema_version=SCHEMA_VERSION,
            source=source,
            request_key=key,
            url=url,
            release=release,
            fetched_at=datetime.fromisoformat(when),
            status=status,
            body_sha256=hashlib.sha256(body).hexdigest(),
        )
        _atomic_write(self._body_path(source, release, key), gzip.compress(body, mtime=0))
        index = self.release_dir(source, release) / INDEX
        existing = index.read_bytes() if index.is_file() else b""
        _atomic_write(index, existing + row.model_dump_json().encode("utf-8") + b"\n")
        return row

    def get(self, source: Source, release: str, key: str) -> bytes:
        row = self.index(source, release).get(key)
        if row is None:
            raise CacheError(f"{key} is not cached for {source} {release}")
        try:
            body = gzip.decompress(self._body_path(source, release, key).read_bytes())
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            raise CacheError(f"{source} {release} {key}: body unreadable ({exc})") from exc
        if hashlib.sha256(body).hexdigest() != row.body_sha256:
            raise CacheError(f"{source} {release} {key}: body does not match its sha256")
        return body

    def read_cursor(self, source: Source, release: str) -> dict[str, Any] | None:
        path = self.release_dir(source, release) / CURSOR
        if not path.is_file():
            return None
        try:
            cursor = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CacheError(f"{path}: corrupt cursor ({exc})") from exc
        if not isinstance(cursor, dict):
            raise CacheError(f"{path}: corrupt cursor (not an object)")
        return cursor

    def write_cursor(self, source: Source, release: str, cursor: dict[str, Any]) -> None:
        data = json.dumps(cursor, indent=2, sort_keys=True) + "\n"
        _atomic_write(self.release_dir(source, release) / CURSOR, data.encode("utf-8"))
