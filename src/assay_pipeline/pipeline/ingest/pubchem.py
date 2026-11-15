"""Ingest PubChem BioAssay rows for the configured target into the raw cache (Tech Spec §5.2).

Steps, every response cached before the next is chosen:
1. the AIDs listed for the configured gene symbol (the gene symbol is not species-specific);
2. their summaries in batches, giving each AID's depositor and target accessions;
3. ChEMBL's own list of PubChem assays it imported (ChEMBL source 7), so those are not counted
   twice (D-24);
4. the concise data table of every kept AID;
5. the structures (SMILES, InChIKey) of the CIDs in kept rows, in batches.

An AID is dropped, first reason wins: `no_summary`, `chembl_deposited_aid` (its depositor is
ChEMBL, always, or another excluded depositor, compared without case: ChEMBL deposits its own
data, H3), `chembl_imported_aid`, `target_not_configured` (no target accession equals the
configured UniProt accession; RefSeq-only assays are dropped here, a stated limit),
`depositor_not_cleared` (not cleared in `depositor_terms.yaml`, D-15). Rows of kept AIDs are
dropped as `other_target`, `not_activity_type`, `no_sid`, `no_cid` or `no_structure`.

A row's record id is `AID:SID:n`, where n numbers the rows that share an AID and SID in table
order (BindingDB repeats a SID for separate measurements), so every row has its own id.

The release is the UTC date of the AID listing's Date header (PubChem has no release number).
The cursor records the config and the depositor ledger's digest; a run or read with another
ledger is refused. A rerun resumes an incomplete release and re-fetches nothing already cached;
a complete release makes the rerun a no-op until `--new-release`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib import resources
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import Field, StringConstraints, ValidationError

from assay_pipeline.config import BuildConfig, ConfigError, _UniqueKeyLoader
from assay_pipeline.schemas import Drop
from assay_pipeline.schemas._base import Model, Name, NonBlank

from .cache import CacheError, RawCache, request_key
from .chembl import IngestError
from .http import Fetcher

SOURCE = "pubchem"
STAGE = "ingest"
SUMMARY_BATCH = 100
PROPERTY_BATCH = 100
CHEMBL_PAGE = 1000
CHEMBL_PUBCHEM_SRC_ID = 7  # ChEMBL's source id for PubChem BioAssay imports
CONCISE_COLUMNS = (
    "AID",
    "SID",
    "CID",
    "Activity Outcome",
    "Target Accession",
    "Activity Value [uM]",
    "Activity Name",
    "Assay Name",
    "PubMed ID",
)
SRC_ASSAY_RE = re.compile(r"^([0-9]{1,12})(?:_[0-9]+)?$")  # "493040_69" is a part of AID 493040
POSITIVE_ID = re.compile(r"^[1-9][0-9]{0,11}$")
ALWAYS_EXCLUDED = ("ChEMBL",)  # ChEMBL-deposited AIDs are always dropped (H3, D-24)
NOT_FOUND = frozenset({404})  # PubChem answers "nothing here" with a PUGREST.NotFound fault


# ---- the depositor ledger (D-15) ----


class DepositorTerm(Model):
    name: Name
    keep: Annotated[bool, Field(strict=True)]  # "yes", "on", 1 are refused
    licence: NonBlank
    attribution: NonBlank
    evidence: tuple[Annotated[str, StringConstraints(pattern=r"^https://\S+$")], ...] = Field(
        min_length=1
    )
    checked: date


class DepositorTerms(Model):
    depositors: tuple[DepositorTerm, ...]

    def cleared(self) -> frozenset[str]:
        return frozenset(d.name for d in self.depositors if d.keep)

    def digest(self) -> str:
        """Identifies the ledger a release was built with (recorded in the cursor)."""
        body = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


class _LedgerLoader(_UniqueKeyLoader):
    """Only true/false are booleans (YAML 1.2), so `keep: yes` or `keep: on` is refused."""


_LedgerLoader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first, resolvers in _UniqueKeyLoader.yaml_implicit_resolvers.items()
}
_LedgerLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def load_depositor_terms(path: Path | str | None = None) -> DepositorTerms:
    if path is None:
        text = resources.files("assay_pipeline").joinpath("defaults/depositor_terms.yaml")
        raw, name = text.read_text(encoding="utf-8"), "defaults/depositor_terms.yaml"
    else:
        try:
            raw, name = Path(path).read_text(encoding="utf-8"), str(path)
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"{path}: cannot read depositor terms ({exc})") from exc
    try:
        data = yaml.load(raw, Loader=_LedgerLoader)
        terms = DepositorTerms.model_validate(data)
    except (yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"{name}: invalid depositor terms: {exc}") from exc
    names = [d.name.casefold() for d in terms.depositors]
    if len(names) != len(set(names)):
        raise ConfigError(f"{name}: a depositor is listed twice (names compare without case)")
    return terms


# ---- results ----


@dataclass(frozen=True)
class AidDecision:
    aid: int
    depositor: str | None
    reason: str | None  # None = kept


@dataclass(frozen=True)
class PubchemRow:
    source_record_id: str  # AID:SID:n, n numbering rows that share an AID and SID
    aid: int
    sid: str
    cid: int
    depositor: str
    outcome: str
    value_um: str  # as PubChem sent it; N1.4 converts to nM
    activity_name: str
    assay_name: str
    pubmed_id: str
    smiles: str
    inchikey: str


@dataclass(frozen=True)
class PubchemRead:
    release: str
    decisions: tuple[AidDecision, ...]
    rows: tuple[PubchemRow, ...]
    drops: tuple[Drop, ...]


@dataclass(frozen=True)
class PubchemIngestReport:
    source: str
    release: str
    aids_listed: int
    aids_kept: int
    rows: int
    drops_by_reason: dict[str, int]
    requests_made: int
    cached_hits: int
    complete: bool


# ---- response checks: a response is cached only after it passes ----


def _bad(where: str, why: str) -> IngestError:
    return IngestError(f"{where}: unexpected PubChem response ({why}); nothing was cached")


def _check_aid_list(data: Any, status: int, gene: str) -> list[int]:
    if _not_found(data, status, "AID list"):
        raise IngestError(f"PubChem lists no assays for gene symbol {gene!r}; check the config")
    return _check_aids(data, "AID list")


def _check_aids(data: Any, where: str) -> list[int]:
    try:
        aids = data["IdentifierList"]["AID"]
    except (KeyError, TypeError) as exc:
        raise _bad(where, f"no IdentifierList.AID: {exc!r}") from exc
    if not isinstance(aids, list) or not all(
        isinstance(a, int) and not isinstance(a, bool) and a > 0 for a in aids
    ):
        raise _bad(where, "AIDs are not positive integers")
    if len(set(aids)) != len(aids):
        raise _bad(where, "an AID is listed twice")
    return sorted(aids)


def _check_summaries(data: Any, where: str, asked: list[int]) -> dict[int, dict]:
    try:
        rows = data["AssaySummaries"]["AssaySummary"]
    except (KeyError, TypeError) as exc:
        raise _bad(where, f"no AssaySummaries.AssaySummary: {exc!r}") from exc
    if not isinstance(rows, list):
        raise _bad(where, "summaries are not a list")
    out: dict[int, dict] = {}
    for r in rows:
        if not isinstance(r, dict) or not isinstance(r.get("AID"), int):
            raise _bad(where, "a summary has no integer AID")
        if not isinstance(r.get("SourceName"), str) or not r["SourceName"].strip():
            raise _bad(where, f"AID {r['AID']} has no SourceName")
        targets = r.get("Target", [])
        if not isinstance(targets, list) or not all(
            isinstance(t, dict) and isinstance(t.get("Accession", ""), str) for t in targets
        ):
            raise _bad(where, f"AID {r['AID']} has a malformed Target list")
        if r["AID"] not in asked or r["AID"] in out:
            raise _bad(where, f"AID {r['AID']} was not asked for, or came twice")
        out[r["AID"]] = r
    return out


def _check_chembl_assays(data: Any, where: str) -> tuple[list[int], int]:
    try:
        assays = data["assays"]
        total = int(data["page_meta"]["total_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise _bad(where, f"malformed ChEMBL assay page: {exc!r}") from exc
    if not isinstance(assays, list):
        raise _bad(where, "assays is not a list")
    aids = []
    for a in assays:
        src = a.get("src_assay_id") if isinstance(a, dict) else None
        m = SRC_ASSAY_RE.match(src) if isinstance(src, str) else None
        if m is None:
            raise _bad(where, f"src_assay_id {src!r} is not a PubChem AID")
        aids.append(int(m.group(1)))
    return aids, total


def _check_concise(data: Any, where: str, aid: int) -> list[dict[str, str]]:
    try:
        cols = data["Table"]["Columns"]["Column"]
        rows = data["Table"].get("Row", [])
    except (KeyError, TypeError, AttributeError) as exc:
        raise _bad(where, f"no Table.Columns.Column: {exc!r}") from exc
    if not isinstance(cols, list) or not all(isinstance(c, str) for c in cols):
        raise _bad(where, "columns are not strings")
    if len(set(cols)) != len(cols):
        raise _bad(where, "a column name repeats")
    missing = [c for c in CONCISE_COLUMNS if c not in cols]
    if missing:
        raise _bad(where, f"columns {missing} are missing")
    if not isinstance(rows, list):
        raise _bad(where, "rows are not a list")
    out = []
    for r in rows:
        cells = r.get("Cell") if isinstance(r, dict) else None
        if not isinstance(cells, list) or len(cells) != len(cols):
            raise _bad(where, "a row's cells do not match the columns")
        if not all(v is None or isinstance(v, str) for v in cells):
            raise _bad(where, "a cell is not a string (the concise table sends strings)")
        row = {c: ("" if v is None else v) for c, v in zip(cols, cells, strict=True)}
        if row["AID"] != str(aid):
            raise _bad(where, f"a row belongs to AID {row['AID']}, not {aid}")
        out.append(row)
    return out


def _not_found(data: Any, status: int, where: str) -> bool:
    """True only for a 404 carrying PubChem's NotFound fault; any other 404 is an error."""
    if status != 404:
        return False
    fault = data.get("Fault") if isinstance(data, dict) else None
    if isinstance(fault, dict) and fault.get("Code") == "PUGREST.NotFound":
        return True
    raise _bad(where, "HTTP 404 without a PUGREST.NotFound fault")


def _check_properties(
    data: Any, status: int, where: str, asked: list[int]
) -> dict[int, dict] | None:
    """CIDs with a SMILES and an InChIKey; a CID PubChem leaves out (or sends without either)
    is simply absent, and its rows are dropped as `no_structure`. None: PubChem said NotFound
    for the whole batch."""
    if _not_found(data, status, where):
        return None
    try:
        props = data["PropertyTable"]["Properties"]
    except (KeyError, TypeError) as exc:
        raise _bad(where, f"no PropertyTable.Properties: {exc!r}") from exc
    if not isinstance(props, list):
        raise _bad(where, "properties are not a list")
    out: dict[int, dict] = {}
    for p in props:
        cid = p.get("CID") if isinstance(p, dict) else None
        if not isinstance(cid, int) or cid not in asked or cid in out:
            raise _bad(where, f"CID {cid!r} was not asked for, or came twice")
        if all(isinstance(p.get(k), str) and p[k].strip() for k in ("SMILES", "InChIKey")):
            out[cid] = p
        else:
            out[cid] = {}  # seen, but without a usable structure
    return out


# ---- the request plan, shared by the ingest and the reader ----


class _Source:
    """Gets JSON either from the cache or (ingest only) by fetching, checking, then caching."""

    def __init__(self, cache: RawCache, release: str, fetcher: Fetcher | None) -> None:
        self.cache, self.release, self.fetcher = cache, release, fetcher
        self.hits = 0

    def get(
        self,
        url: str,
        params: dict[str, Any],
        check: Callable[[Any, int], Any],
        ok_statuses: frozenset[int] = frozenset(),
    ) -> Any:
        """`check(data, status)` validates the answer; only then is a fetched answer cached."""
        key = request_key("GET", url, params)
        where = f"{url} {dict(sorted(params.items()))}" if params else url
        if self.cache.has(SOURCE, self.release, key):
            try:
                data = json.loads(self.cache.get(SOURCE, self.release, key))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise IngestError(f"{where}: cached response is not JSON ({exc})") from exc
            self.hits += 1
            return check(data, self.cache.index(SOURCE, self.release)[key].status)
        if self.fetcher is None:
            raise IngestError(
                f"{where}: not in the complete {self.release} cache; run "
                "`assay ingest pubchem --new-release` to build a release for this config"
            )
        got = self.fetcher.get_json(url, params, ok_statuses)
        out = check(got.data, got.status)
        self.cache.put(SOURCE, self.release, key, got.url, got.status, got.body, got.date)
        return out


def _batches(items: list[int], n: int) -> list[list[int]]:
    return [items[i : i + n] for i in range(0, len(items), n)]


def _aid_list_url(cfg: BuildConfig) -> str:
    return f"{cfg.pubchem.base_url}/assay/target/genesymbol/{cfg.target.gene_symbol}/aids/JSON"


def _walk(
    cfg: BuildConfig, src: _Source, terms: DepositorTerms
) -> tuple[list[AidDecision], list[PubchemRow], list[Drop]]:
    base = cfg.pubchem.base_url
    t = cfg.target
    aids = src.get(
        _aid_list_url(cfg), {}, lambda d, s: _check_aid_list(d, s, t.gene_symbol), NOT_FOUND
    )

    summaries: dict[int, dict] = {}
    for batch in _batches(aids, SUMMARY_BATCH):
        url = f"{base}/assay/aid/{','.join(map(str, batch))}/summary/JSON"
        summaries.update(
            src.get(url, {}, lambda d, _s, b=batch: _check_summaries(d, "assay summaries", b))
        )

    imported: set[int] = set()
    chembl_url = f"{cfg.chembl.base_url}/assay.json"
    offset = 0
    while True:
        params = {
            "target_chembl_id": t.chembl_target_id,
            "src_id": CHEMBL_PUBCHEM_SRC_ID,
            "only": "assay_chembl_id,src_assay_id",
            "order_by": "assay_chembl_id",
            "limit": CHEMBL_PAGE,
            "offset": offset,
        }
        page, total = src.get(
            chembl_url, params, lambda d, _s: _check_chembl_assays(d, "ChEMBL imported assays")
        )
        imported.update(page)
        offset += len(page)
        if offset >= total:
            break
        if not page:
            raise IngestError(f"ChEMBL imported assays: empty page before total_count {total}")

    excluded = {x.strip().casefold() for x in (*ALWAYS_EXCLUDED, *cfg.pubchem.exclude_depositors)}
    cleared = terms.cleared()
    decisions: list[AidDecision] = []
    drops: list[Drop] = []
    for aid in aids:
        s = summaries.get(aid)
        depositor = s["SourceName"] if s else None
        accessions = {x.get("Accession", "") for x in s.get("Target", [])} if s else set()
        if s is None:
            reason = "no_summary"
        elif (depositor or "").strip().casefold() in excluded:
            reason = "chembl_deposited_aid"
        elif aid in imported:
            reason = "chembl_imported_aid"
        elif t.uniprot not in accessions:
            reason = "target_not_configured"
        elif depositor not in cleared:
            reason = "depositor_not_cleared"
        else:
            reason = None
        decisions.append(AidDecision(aid, depositor, reason))
        if reason:
            drops.append(Drop(source=SOURCE, source_record_id=str(aid), stage=STAGE, reason=reason))

    kept_rows: list[tuple[int, str, str, dict[str, str]]] = []  # aid, depositor, record id, row
    for d in decisions:
        if d.reason is not None:
            continue
        url = f"{base}/assay/aid/{d.aid}/concise/JSON"
        seen: dict[str, int] = {}
        table = src.get(url, {}, lambda data, _s, a=d.aid: _check_concise(data, "concise", a))
        for i, row in enumerate(table):
            sid = row["SID"]
            if POSITIVE_ID.match(sid):
                seen[sid] = seen.get(sid, 0) + 1
                rid = f"{d.aid}:{sid}:{seen[sid]}"  # numbered over the whole table, so stable
            else:
                rid = f"{d.aid}:?:{i + 1}"
            if row["Target Accession"] != t.uniprot:
                reason = "other_target"
            elif row["Activity Name"] != t.activity_type:
                reason = "not_activity_type"
            elif not POSITIVE_ID.match(sid):
                reason = "no_sid"
            elif not POSITIVE_ID.match(row["CID"]):
                reason = "no_cid"
            else:
                kept_rows.append((d.aid, d.depositor or "", rid, row))
                continue
            drops.append(Drop(source=SOURCE, source_record_id=rid, stage=STAGE, reason=reason))

    cids = sorted({int(r["CID"]) for _, _, _, r in kept_rows})
    structures: dict[int, dict] = {}

    def properties(batch: list[int]) -> dict[int, dict] | None:
        url = f"{base}/compound/cid/{','.join(map(str, batch))}/property/SMILES,InChIKey/JSON"
        return src.get(
            url, {}, lambda d, s: _check_properties(d, s, "properties", batch), NOT_FOUND
        )

    for batch in _batches(cids, PROPERTY_BATCH):
        got = properties(batch)
        if got is None and len(batch) > 1:
            # a NotFound for a whole batch is re-asked one CID at a time, so a transient fault
            # costs at most the CIDs it really hits, never the whole batch
            for cid in batch:
                structures.update(properties([cid]) or {})
        else:
            structures.update(got or {})

    rows: list[PubchemRow] = []
    for aid, depositor, rid, r in kept_rows:
        p = structures.get(int(r["CID"]))
        if not p:
            drops.append(
                Drop(source=SOURCE, source_record_id=rid, stage=STAGE, reason="no_structure")
            )
            continue
        rows.append(
            PubchemRow(
                source_record_id=rid,
                aid=aid,
                sid=r["SID"],
                cid=int(r["CID"]),
                depositor=depositor,
                outcome=r["Activity Outcome"],
                value_um=r["Activity Value [uM]"],
                activity_name=r["Activity Name"],
                assay_name=r["Assay Name"],
                pubmed_id=r["PubMed ID"],
                smiles=p["SMILES"],
                inchikey=p["InChIKey"],
            )
        )
    return decisions, rows, drops


# ---- releases and the cursor ----


def _cursor_config(cfg: BuildConfig, terms: DepositorTerms) -> dict[str, Any]:
    t = cfg.target
    return {
        "depositor_terms_sha256": terms.digest(),
        "gene_symbol": t.gene_symbol,
        "uniprot": t.uniprot,
        "chembl_target_id": t.chembl_target_id,
        "activity_type": t.activity_type,
        "exclude_depositors": sorted(cfg.pubchem.exclude_depositors),
    }


RELEASE_NAME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(?:\.[1-9][0-9]*)?$")


def _releases(cache: RawCache) -> list[str]:
    """Release folders only: a date name (with an optional `.N`) whose cursor names it; other
    folders in the cache (a `backup` copy, say) are never taken for a release."""
    out = []
    for name in cache.releases(SOURCE):
        if not RELEASE_NAME.match(name):
            continue
        cur = cache.read_cursor(SOURCE, name)
        if cur is not None and cur.get("release") == name:
            out.append(name)
    return sorted(out, key=_release_key)


def _state(cache: RawCache, release: str) -> str:
    cur = cache.read_cursor(SOURCE, release)
    if cur is None:
        return "incomplete"
    if cur.get("abandoned"):
        return "abandoned"
    return "complete" if cur.get("complete") else "incomplete"


def _release_key(name: str) -> tuple[str, int]:
    """Order releases by date, then by the numeric `.N` suffix (".10" after ".9")."""
    day, _, n = name.partition(".")
    return day, int(n) if n.isdigit() else 1


def _check_cursor(
    cfg: BuildConfig, cache: RawCache, release: str, terms: DepositorTerms
) -> dict[str, Any]:
    cur = cache.read_cursor(SOURCE, release)
    if cur is None:
        raise IngestError(
            f"{cache.release_dir(SOURCE, release)} has no cursor: delete it to start over"
        )
    if cur.get("config") != _cursor_config(cfg, terms):
        raise IngestError(
            f"the pubchem {release} cache was built for another config or depositor ledger "
            f"({cur.get('config')}); run `assay ingest pubchem --new-release`, or use another "
            "--cache folder for this config"
        )
    return cur


def _new_release_name(cache: RawCache, day: str) -> str:
    taken = set(cache.releases(SOURCE))
    if day not in taken:
        return day
    n = 2
    while f"{day}.{n}" in taken:
        n += 1
    return f"{day}.{n}"


def read_pubchem(
    cfg: BuildConfig,
    cache: RawCache,
    release: str | None = None,
    terms: DepositorTerms | None = None,
) -> PubchemRead:
    """Rebuild the decisions, rows and drops of a complete release from the cache alone."""
    if release is None:
        done = [r for r in _releases(cache) if _state(cache, r) == "complete"]
        if not done:
            raise IngestError("no complete pubchem release in the cache; run the ingest first")
        release = done[-1]
    if _state(cache, release) != "complete":
        raise IngestError(f"pubchem release {release} is not complete")
    terms = terms or load_depositor_terms()
    _check_cursor(cfg, cache, release, terms)
    try:
        decisions, rows, drops = _walk(cfg, _Source(cache, release, None), terms)
    except CacheError as exc:
        raise IngestError(f"pubchem {release}: {exc}") from exc
    return PubchemRead(release, tuple(decisions), tuple(rows), tuple(drops))


def ingest_pubchem(
    cfg: BuildConfig,
    cache: RawCache,
    fetcher: Fetcher,
    *,
    new_release: bool = False,
    terms: DepositorTerms | None = None,
) -> PubchemIngestReport:
    terms = terms or load_depositor_terms()
    lock = cache.lock(SOURCE)
    try:
        return _ingest(cfg, cache, fetcher, new_release, terms)
    except CacheError as exc:
        raise IngestError(f"pubchem: {exc}") from exc
    finally:
        lock.unlink(missing_ok=True)


def _ingest(
    cfg: BuildConfig, cache: RawCache, fetcher: Fetcher, new_release: bool, terms: DepositorTerms
) -> PubchemIngestReport:
    before = fetcher.requests_made
    states = {r: _state(cache, r) for r in _releases(cache)}
    incomplete = [r for r, s in states.items() if s == "incomplete"]
    complete = [r for r, s in states.items() if s == "complete"]
    if incomplete and new_release:
        for r in incomplete:
            cur = cache.read_cursor(SOURCE, r) or {}
            cache.write_cursor(SOURCE, r, {**cur, "abandoned": True})
        incomplete = []
    if len(incomplete) > 1:
        raise IngestError(f"several incomplete pubchem releases {incomplete}: delete all but one")

    written = 0  # responses this call cached before the walk (they are not cache hits)
    if incomplete:
        release = incomplete[0]
        cursor = _check_cursor(cfg, cache, release, terms)
    elif complete and not new_release:
        release = complete[-1]
        _check_cursor(cfg, cache, release, terms)
        read = _walk(cfg, _Source(cache, release, None), terms)
        return _report(release, read, fetcher.requests_made - before, -1)
    else:
        url = _aid_list_url(cfg)
        got = fetcher.get_json(url, {}, NOT_FOUND)
        _check_aid_list(got.data, got.status, cfg.target.gene_symbol)
        if not got.date:
            raise IngestError(f"{url}: no Date header, so the release date is unknown")
        day = datetime.fromisoformat(got.date).astimezone(UTC).date().isoformat()
        release = _new_release_name(cache, day)
        cursor = {"release": release, "config": _cursor_config(cfg, terms), "complete": False}
        cache.write_cursor(SOURCE, release, cursor)
        cache.put(
            SOURCE, release, request_key("GET", url, {}), got.url, got.status, got.body, got.date
        )
        written = 1

    src = _Source(cache, release, fetcher)
    walked = _walk(cfg, src, terms)
    cache.write_cursor(SOURCE, release, {**cursor, "complete": True})
    return _report(release, walked, fetcher.requests_made - before, src.hits - written)


def _report(
    release: str,
    walked: tuple[list[AidDecision], list[PubchemRow], list[Drop]],
    requests: int,
    hits: int,
) -> PubchemIngestReport:
    decisions, rows, drops = walked
    by_reason: dict[str, int] = {}
    for d in drops:
        by_reason[d.reason] = by_reason.get(d.reason, 0) + 1
    return PubchemIngestReport(
        source=SOURCE,
        release=release,
        aids_listed=len(decisions),
        aids_kept=sum(d.reason is None for d in decisions),
        rows=len(rows),
        drops_by_reason=dict(sorted(by_reason.items())),
        requests_made=requests,
        cached_hits=max(hits, 0),
        complete=True,
    )
