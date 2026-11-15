"""Record and replay real API responses for a target fixture (OSS-06).

`Replay` is an `httpx.MockTransport` handler that serves `<target>/responses/` by request
(method, host, path, sorted query) and fails loudly on any request it has no recording for.

`Recorder` wraps a real transport and trims responses as they pass, so the ingest that drives
the recording makes its later requests from the trimmed data: ChEMBL activity pages get a
smaller `page_meta.total_count` (and a null `next` on the last kept page), and the PubChem AID
list keeps only the chosen AIDs. Every other response is stored unchanged. Re-record with

    TMPDIR=... uv run python -m tests.fixtures.targets.replay abl1

which uses the network (only www.ebi.ac.uk and pubchem.ncbi.nlm.nih.gov, through the project's
own rate-limited `Fetcher`) and rewrites `<target>/responses/`.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
INDEX = "index.json"
ALLOWED_HOSTS = frozenset({"www.ebi.ac.uk", "pubchem.ncbi.nlm.nih.gov"})

# What each recorded target keeps (see its README.md for why these were chosen).
TRIMS: dict[str, dict[str, Any]] = {
    "abl1": {
        "chembl_total": 10,  # two pages of five activities
        "keep_aids": (1622, 32559, 493040, 1796923, 1799577, 1806059, 1963317),
    },
}


def request_id(request: httpx.Request) -> str:
    if request.content:
        raise AssertionError(f"{request.method} {request.url}: a request body is not recorded")
    query = sorted(request.url.params.multi_items())
    return json.dumps(
        [request.method, request.url.host, request.url.path, query], separators=(",", ":")
    )


def encode(data: Any) -> bytes:
    """How bodies are stored and served: the EGFR fixtures' layout (indent 1, UTF-8)."""
    return (json.dumps(data, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


def _response(status: int, date: str, body: bytes) -> httpx.Response:
    headers = {"Date": date, "Content-Type": "application/json"}
    return httpx.Response(status, headers=headers, content=body)


class Replay:
    """Serves a recorded target; `unused()` lists recordings no request asked for."""

    def __init__(self, target_dir: Path) -> None:
        self.dir = Path(target_dir) / "responses"
        entries = json.loads((self.dir / INDEX).read_text(encoding="utf-8"))["responses"]
        self.entries = {e["request"]: e for e in entries}
        self.served: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        rid = request_id(request)
        entry = self.entries.get(rid)
        if entry is None:
            raise AssertionError(f"no recorded response for {rid}; re-record the fixture")
        self.served.append(rid)
        body = (self.dir / entry["file"]).read_bytes()
        return _response(entry["status"], entry["date"], body)

    def unused(self) -> list[str]:
        return sorted(set(self.entries) - set(self.served))


def _file_name(request: httpx.Request, taken: Iterable[str]) -> str:
    path, params = request.url.path, request.url.params
    if path.endswith("/status.json"):
        stem = "chembl_status"
    elif path.endswith("/activity.json"):
        stem = f"chembl_activity_offset_{params['offset']}"
    elif path.endswith("/assay.json"):
        stem = f"chembl_src7_assays_offset_{params['offset']}"
    elif path.endswith("/aids/JSON"):
        stem = "pubchem_aids"
    elif path.endswith("/summary/JSON"):
        stem = "pubchem_summary"
    elif path.endswith("/concise/JSON"):
        stem = f"pubchem_concise_{path.split('/')[-3]}"
    elif "/property/" in path:
        stem = "pubchem_properties"
    else:
        stem = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_")[-60:]
    name, n = f"{stem}.json", 2
    while name in taken:
        name, n = f"{stem}_{n}.json", n + 1
    return name


class Recorder(httpx.BaseTransport):
    """A transport that forwards to the real one, trims, stores and returns what it stored.
    `close()` is the base no-op, so the inner pool outlives each `Fetcher` client."""

    def __init__(self, inner: httpx.BaseTransport, *, chembl_total: int, keep_aids: Iterable[int]):
        self.inner = inner
        self.chembl_total = chembl_total
        self.keep_aids = frozenset(keep_aids)
        self.entries: dict[str, dict[str, Any]] = {}
        self.bodies: dict[str, bytes] = {}

    def _trim(self, request: httpx.Request, data: Any) -> Any:
        path = request.url.path
        if path.endswith("/activity.json"):
            meta = data["page_meta"]
            meta["total_count"] = self.chembl_total
            if int(request.url.params["offset"]) + len(data["activities"]) >= self.chembl_total:
                meta["next"] = None
        elif path.endswith("/aids/JSON"):
            listed = data["IdentifierList"]["AID"]
            missing = self.keep_aids - set(listed)
            if missing:
                raise AssertionError(f"AIDs {sorted(missing)} are no longer listed")
            data["IdentifierList"]["AID"] = [a for a in listed if a in self.keep_aids]
        return data

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host not in ALLOWED_HOSTS:
            raise AssertionError(f"{request.url.host} is not an allowed recording host")
        resp = self.inner.handle_request(request)
        resp.read()
        resp.close()
        if resp.status_code not in (200, 404):  # busy or failed: let the Fetcher retry
            keep = {k: v for k, v in resp.headers.items() if k.lower() in ("date", "retry-after")}
            return httpx.Response(resp.status_code, headers=keep, content=resp.content)
        body = encode(self._trim(request, json.loads(resp.content)))
        rid = request_id(request)
        name = self.entries.get(rid, {}).get("file") or _file_name(
            request, (e["file"] for e in self.entries.values())
        )
        date = resp.headers["Date"]
        self.entries[rid] = {"request": rid, "file": name, "status": resp.status_code, "date": date}
        self.bodies[name] = body
        return _response(resp.status_code, date, body)

    def save(self, target_dir: Path) -> None:
        out = Path(target_dir) / "responses"
        out.mkdir(parents=True, exist_ok=True)
        for old in out.glob("*.json"):
            old.unlink()
        for name, body in self.bodies.items():
            (out / name).write_bytes(body)
        entries = sorted(self.entries.values(), key=lambda e: e["file"])
        (out / INDEX).write_bytes(encode({"responses": entries}))


def record(target: str, terms: Any) -> None:
    from assay_pipeline.config import load_build_config
    from assay_pipeline.pipeline.ingest.cache import RawCache
    from assay_pipeline.pipeline.ingest.chembl import ingest_chembl
    from assay_pipeline.pipeline.ingest.http import Fetcher
    from assay_pipeline.pipeline.ingest.pubchem import ingest_pubchem

    target_dir = HERE / target
    cfg = load_build_config(target_dir / "build.yaml")
    recorder = Recorder(httpx.HTTPTransport(retries=0), **TRIMS[target])
    with tempfile.TemporaryDirectory(prefix="record-") as tmp:  # honours TMPDIR
        cache = RawCache(tmp)
        rate = min(cfg.chembl.max_requests_per_second, cfg.pubchem.max_requests_per_second)
        fetcher = Fetcher(transport=recorder, max_requests_per_second=rate)
        print(ingest_chembl(cfg, cache, fetcher))
        print(ingest_pubchem(cfg, cache, fetcher, terms=terms))
    recorder.save(target_dir)
    print(f"recorded {len(recorder.entries)} responses into {target_dir / 'responses'}")


if __name__ == "__main__":
    from tests.pipeline.test_ingest_pubchem import GOLDEN_TERMS

    record(sys.argv[1], GOLDEN_TERMS)
