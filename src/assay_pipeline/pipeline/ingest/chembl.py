"""Ingest ChEMBL activities for the configured target into the raw cache (Tech Spec §2.2).

Resumable: a cursor records the offset reached; a rerun re-fetches nothing already cached.
The ChEMBL release is read first, and a resume never mixes two releases.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from assay_pipeline.config import BuildConfig

from .cache import CacheError, RawCache, request_key
from .http import Fetcher

SOURCE = "chembl"
# Fields later stages read from every activity record (CHM-02, Tech Spec §5.1).
REQUIRED_FIELDS = (
    "activity_id",
    "molecule_chembl_id",
    "canonical_smiles",
    "standard_value",
    "standard_units",
    "standard_relation",
    "standard_type",
    "pchembl_value",
    "assay_chembl_id",
    "assay_type",
    "assay_description",
    "assay_variant_mutation",
    "src_id",
    "data_validity_comment",
    "document_chembl_id",
    "target_chembl_id",
    "target_organism",
)


class IngestError(RuntimeError):
    """The ingest cannot continue safely; the message says why and what to do."""


@dataclass(frozen=True)
class IngestReport:
    source: str
    release: str
    pages: int
    records: int
    requests_made: int
    cached_hits: int
    complete: bool


def _check_page(
    cfg: BuildConfig,
    page: Any,
    offset: int,
    after_id: int | None,
    expected_total: int | None,
    release_dir: Any,
) -> list[dict]:
    """Validate a page before it is cached, so a bad page never poisons the cache."""
    try:
        records = page["activities"]
        total = int(page["page_meta"]["total_count"])
        if not isinstance(records, list):
            raise TypeError("activities is not a list")
    except (KeyError, TypeError, ValueError) as exc:
        raise IngestError(f"offset {offset}: malformed activity page ({exc!r})") from exc
    if expected_total is not None and total != expected_total:
        raise IngestError(
            f"offset {offset}: total_count changed from {expected_total} to {total} during the "
            f"ingest, so ChEMBL data moved under it; rerun later (nothing bad was cached), or "
            f"delete {release_dir} to start this release over"
        )
    t = cfg.target
    last = after_id
    for r in records:
        if not isinstance(r, dict):
            raise IngestError(f"offset {offset}: activity record is not an object")
        missing = [f for f in REQUIRED_FIELDS if f not in r]
        if missing:
            raise IngestError(f"offset {offset}: activity record lacks fields {missing}")
        if r["target_chembl_id"] != t.chembl_target_id or r["standard_type"] != t.activity_type:
            raise IngestError(
                f"offset {offset}: record {r['activity_id']} is {r['target_chembl_id']} "
                f"{r['standard_type']}, expected {t.chembl_target_id} {t.activity_type}"
            )
        aid = r["activity_id"]
        if not isinstance(aid, int) or isinstance(aid, bool):
            raise IngestError(f"offset {offset}: activity_id {aid!r} is not an integer")
        if last is not None and aid <= last:
            raise IngestError(
                f"offset {offset}: activity_id {aid} does not follow {last}; pages overlap or "
                "the ordering moved"
            )
        last = aid
    return records


def _page(
    cfg: BuildConfig,
    cache: RawCache,
    fetcher: Fetcher,
    release: str,
    url: str,
    params: dict[str, Any],
    after_id: int | None,
    expected_total: int | None,
) -> tuple[Any, list[dict], bool]:
    """Return (page, records, came_from_cache). A fetched page is cached only once valid."""
    key = request_key("GET", url, params)
    offset = int(params["offset"])
    where = cache.release_dir(SOURCE, release)
    if cache.has(SOURCE, release, key):
        try:
            page = json.loads(cache.get(SOURCE, release, key))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise IngestError(f"offset {offset}: cached page is not JSON ({exc})") from exc
        return page, _check_page(cfg, page, offset, after_id, expected_total, where), True
    got = fetcher.get_json(url, params)
    records = _check_page(cfg, got.data, offset, after_id, expected_total, where)
    cache.put(SOURCE, release, key, got.url, got.status, got.body, got.date)
    return got.data, records, False


def _incomplete(cache: RawCache, release: str) -> bool:
    cur = cache.read_cursor(SOURCE, release)
    if cur is None:
        return True  # a folder with no cursor never finished its first page
    return not cur.get("complete") and not cur.get("abandoned")


def ingest_chembl(
    cfg: BuildConfig, cache: RawCache, fetcher: Fetcher, *, new_release: bool = False
) -> IngestReport:
    lock = cache.lock(SOURCE)
    try:
        return _ingest(cfg, cache, fetcher, new_release)
    finally:
        lock.unlink(missing_ok=True)


def _ingest(cfg: BuildConfig, cache: RawCache, fetcher: Fetcher, new_release: bool) -> IngestReport:
    requests_before = fetcher.requests_made
    base = cfg.chembl.base_url
    status_url = f"{base}/status.json"
    got = fetcher.get_json(status_url, {})
    release = got.data.get("chembl_db_version") if isinstance(got.data, dict) else None
    if not isinstance(release, str) or not release.strip():
        raise IngestError(f"status.json has no chembl_db_version: {got.data!r}")
    try:
        cache.release_dir(SOURCE, release)
    except CacheError as exc:
        raise IngestError(f"ChEMBL reports an unusable release name: {exc}") from exc

    to_abandon = [
        other for other in cache.releases(SOURCE) if other != release and _incomplete(cache, other)
    ]
    if to_abandon and not new_release:
        raise IngestError(
            f"the cache holds an incomplete {to_abandon[0]} ingest, but ChEMBL now serves "
            f"{release}; rerun with --new-release to abandon it and start a cache for the new "
            "release"
        )

    fresh = {
        "release": release,
        "target": cfg.target.chembl_target_id,
        "activity_type": cfg.target.activity_type,
        "page_size": cfg.chembl.page_size,
        "offset": 0,
        "total_count": None,
        "complete": False,
    }
    cursor = cache.read_cursor(SOURCE, release) or fresh
    if cursor.get("target") is None:  # an abandoned folder that never cached a page
        if len(cache.index(SOURCE, release)) > 1:  # more than the status response
            raise IngestError(
                f"{cache.release_dir(SOURCE, release)} holds cached pages but its cursor names "
                "no config; delete the folder to start this release over"
            )
        cursor = fresh
    cursor.pop("abandoned", None)  # the live release is never abandoned
    cursor.pop("superseded_by", None)
    if (cursor.get("target"), cursor.get("activity_type"), cursor.get("page_size")) != (
        cfg.target.chembl_target_id,
        cfg.target.activity_type,
        cfg.chembl.page_size,
    ):
        raise IngestError(
            f"the {release} cache was built for {cursor.get('target')} "
            f"{cursor.get('activity_type')} page size {cursor.get('page_size')}; use another "
            "--cache folder for this config"
        )

    for other in to_abandon:  # only after this release's config is known to fit
        old = cache.read_cursor(SOURCE, other) or {"release": other, "complete": False}
        cache.write_cursor(SOURCE, other, {**old, "abandoned": True, "superseded_by": release})

    status_key = request_key("GET", status_url, {})
    if not cache.has(SOURCE, release, status_key):  # first status seen for this release
        cache.put(SOURCE, release, status_key, got.url, got.status, got.body, got.date)

    pages = records = hits = 0
    url = f"{base}/activity.json"
    offset = 0  # walk from the start: cached pages cost nothing, and counts stay exact
    last_id: int | None = None
    while not cursor["complete"]:
        params = {
            "target_chembl_id": cfg.target.chembl_target_id,
            "standard_type": cfg.target.activity_type,
            "order_by": "activity_id",
            "limit": cfg.chembl.page_size,
            "offset": offset,
        }
        try:
            page, found, cached = _page(
                cfg, cache, fetcher, release, url, params, last_id, cursor["total_count"]
            )
        except CacheError as exc:
            raise IngestError(f"offset {offset}: {exc}") from exc
        total = int(page["page_meta"]["total_count"])
        if cursor["total_count"] is None:
            cursor["total_count"] = total
        if not found and offset < total:
            raise IngestError(f"offset {offset}: empty page before total_count {total}")
        if found:
            last_id = int(found[-1]["activity_id"])
        pages += 1
        records += len(found)
        hits += cached
        offset += len(found)
        cursor["offset"] = max(cursor["offset"], offset)
        cursor["complete"] = offset >= total
        cache.write_cursor(SOURCE, release, cursor)
    if pages == 0:  # already complete: count what is cached
        records = int(cursor["total_count"] or 0)
    return IngestReport(
        source=SOURCE,
        release=release,
        pages=pages,
        records=records,
        requests_made=fetcher.requests_made - requests_before,
        cached_hits=hits,
        complete=True,
    )
