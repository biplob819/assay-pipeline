"""Build, read and publish a snapshot (Tech Spec §2.1, §4 versioning, §5.5, D-1, D-15, H6).

A snapshot folder holds:
- `activities.parquet`, `consensus.parquet`, `drops.parquet` — written deterministically:
  explicit Arrow schemas, rows pre-sorted, no pandas metadata, a fixed row-group size, zstd at a
  fixed level, dictionary encoding on;
- `DATA_CARD.md`, `DATA_LICENSE.md`;
- `manifest.json` (canonical JSON of `Manifest`);
- `_stages/<stage>.json` — the stage manifests (D-1).

Rebuilding from the same raw cache with the same code gives a byte-identical manifest (the
rebuild gate). Nothing reads a clock: `created_at` is the ChEMBL status fetch time in the cache.
`code_sha` fingerprints the package's own source files, so it is exact for a pip install too.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import metadata, resources
from pathlib import Path
from typing import Any

from assay_pipeline.config import BuildConfig
from assay_pipeline.schemas import SCHEMA_VERSION, Activity, Consensus, Drop, Manifest
from assay_pipeline.schemas.data import FLAGS

from .ingest.cache import RawCache
from .ingest.pubchem import DepositorTerms, load_depositor_terms
from .normalise import normalise
from .quality import apply_quality

ROW_GROUP_SIZE = 65536
ZSTD_LEVEL = 9
STAGES_DIR = "_stages"
MANIFEST = "manifest.json"
DATA_FILES = ("activities.parquet", "consensus.parquet", "drops.parquet")
DOC_FILES = ("DATA_CARD.md", "DATA_LICENSE.md")
CODE_VERSIONS = {"normalise": "1", "quality": "1", "snapshot": "1"}  # bump when logic changes


class SnapshotError(RuntimeError):
    """The snapshot cannot be built, read or published safely."""


# ---- hashing and canonical forms ----


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def content_hash(rows: Sequence[dict[str, Any]]) -> str:
    """sha256 over canonical rows: equal content gives an equal hash whatever the file bytes."""
    h = hashlib.sha256()
    for row in rows:
        h.update(canonical(row))
        h.update(b"\n")
    return h.hexdigest()


def code_fingerprint() -> str:
    """sha256 over the package's source files (path and bytes), truncated to 40 hex."""
    root = resources.files("assay_pipeline")
    h = hashlib.sha256()
    base = Path(str(root))
    for path in sorted(base.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".yaml"} and "__pycache__" not in path.parts:
            h.update(path.relative_to(base).as_posix().encode("utf-8") + b"\0")
            h.update(path.read_bytes())
    return h.hexdigest()[:40]


def tool_versions() -> dict[str, str]:
    """Libraries that can change file bytes or row content; Python is recorded as major.minor."""
    out = {"python": ".".join(platform.python_version_tuple()[:2])}
    for dist in ("assay-pipeline", "pyarrow", "rdkit", "pydantic"):
        out[dist] = metadata.version(dist)
    return out


def raw_cache_hash(cache: RawCache, releases: dict[str, str]) -> str:
    """sha256 over the index of every release read: request key and body hash, sorted."""
    h = hashlib.sha256()
    for source in sorted(releases):
        release = releases[source]
        for key, row in sorted(cache.index(source, release).items()):  # type: ignore[arg-type]
            h.update(f"{source}\0{release}\0{key}\0{row.body_sha256}\n".encode())
    return h.hexdigest()


# ---- rows to Arrow ----


def _arrow() -> Any:
    import pyarrow as pa

    return pa


def _schemas() -> dict[str, Any]:
    pa = _arrow()
    s, f, i, b = pa.string(), pa.float64(), pa.int64(), pa.bool_()
    return {
        "activities": pa.schema(
            [
                ("activity_uid", s),
                ("inchikey", s),
                ("source_inchikey", s),
                ("smiles", s),
                ("target", s),
                ("variant", s),
                ("assay_type", s),
                ("assay_id", s),
                ("assay_description", s),
                ("value_original", f),
                ("units_original", s),
                ("value_nm", f),
                ("relation", s),
                ("pic50", f),
                ("source", s),
                ("source_record_id", s),
                ("depositor", s),
                ("chembl_src_id", i),
                ("data_validity_comment", s),
                ("document_id", s),
                ("flags", pa.list_(s)),
                ("flag_reasons", s),
                ("snapshot", s),
            ]
        ),
        "consensus": pa.schema(
            [
                ("inchikey", s),
                ("target", s),
                ("variant", s),
                ("assay_type", s),
                ("n_records", i),
                ("n_sources", i),
                ("median_pic50", f),
                ("spread_pic50", f),
                ("conflict", b),
                ("member_uids", pa.list_(s)),
                ("snapshot", s),
            ]
        ),
        "drops": pa.schema([("source", s), ("source_record_id", s), ("stage", s), ("reason", s)]),
    }


def activity_row(a: Activity) -> dict[str, Any]:
    row = a.model_dump(mode="json")
    row["flags"] = list(a.flags)
    row["flag_reasons"] = canonical(dict(a.flag_reasons)).decode("utf-8")
    return row


def consensus_row(c: Consensus) -> dict[str, Any]:
    row = c.model_dump(mode="json")
    row["member_uids"] = list(c.member_uids)
    return row


def drop_row(d: Drop) -> dict[str, Any]:
    return d.model_dump(mode="json")


def write_parquet(path: Path, table_name: str, rows: Sequence[dict[str, Any]]) -> None:
    """Deterministic Parquet: fixed schema, order, row groups, compression and encoding."""
    pa = _arrow()
    import pyarrow.parquet as pq

    schema = _schemas()[table_name]
    columns = {name: [r[name] for r in rows] for name in schema.names}
    table = pa.Table.from_pydict(columns, schema=schema)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(
        table,
        tmp,
        row_group_size=ROW_GROUP_SIZE,
        compression="zstd",
        compression_level=ZSTD_LEVEL,
        use_dictionary=True,
        write_statistics=True,
        store_schema=False,  # no Arrow/pandas schema blob in the footer
    )
    os.replace(tmp, path)


def read_parquet_rows(path: Path) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    return pq.read_table(path).to_pylist()


# ---- snapshot id (§4 versioning) ----


def identity(inputs: dict[str, str], code: str) -> str:
    """What a snapshot is built from: same identity, same content (and the same id)."""
    return sha256_bytes(canonical({**inputs, "code": code}))


def _stage_identity(folder: Path) -> str | None:
    try:
        rec = json.loads((folder / STAGES_DIR / "snapshot.json").read_text(encoding="utf-8"))
        return identity(
            {k: v for k, v in rec["inputs"].items() if k != "code"}, rec["inputs"]["code"]
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def assign_snapshot_id(out_root: Path, day: str, ident: str) -> str:
    """`YYYY-MM-DD.n`: the date is the ChEMBL fetch date. A rebuild with the same identity (raw
    cache, fetch time, config, ledger, planted rows, library versions, code) reuses its id on any
    day; anything else takes the next n, so different content never reuses or replaces an id.
    Only folders named exactly after their manifest's id count; hidden temp folders never do."""
    taken: dict[int, str | None] = {}
    if out_root.is_dir():
        for folder in out_root.iterdir():
            if folder.name.startswith(".") or not (folder / MANIFEST).is_file():
                continue
            try:
                m = Manifest.model_validate_json((folder / MANIFEST).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            d, _, n = m.snapshot.partition(".")
            if d == day and folder.name == m.snapshot:
                taken[int(n)] = _stage_identity(folder)
    for n, ident_n in sorted(taken.items()):
        if ident_n == ident:
            return f"{day}.{n}"
    return f"{day}.{max(taken, default=0) + 1}"


def chembl_fetch_time(cache: RawCache, release: str) -> str:
    rows = [r for r in cache.index("chembl", release).values() if r.url.endswith("/status.json")]
    if len(rows) != 1:
        raise SnapshotError(f"chembl {release}: expected one cached status.json, found {len(rows)}")
    return rows[0].fetched_at.isoformat().replace("+00:00", "Z")


# ---- the build ----


@dataclass(frozen=True)
class BuildResult:
    manifest: Manifest
    directory: Path
    skipped: bool  # True when an up-to-date snapshot was found and nothing was rebuilt


def _stage_record(
    stage: str,
    inputs: dict[str, str],
    outputs: dict[str, str],
    rows_in: int,
    rows_out: int,
    drops: dict[str, int],
    tools: dict[str, str],
) -> dict[str, Any]:
    return {
        "stage": stage,
        "code_version": CODE_VERSIONS[stage],
        "inputs": inputs,
        "outputs": outputs,
        "rows_in": rows_in,
        "rows_out": rows_out,
        "drops_by_reason": drops,
        "tool_versions": tools,
    }


def _write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def build_snapshot(
    cfg: BuildConfig,
    cache: RawCache,
    out_root: Path | str,
    *,
    chembl_release: str | None = None,
    pubchem_release: str | None = None,
    terms: DepositorTerms | None = None,
    planted: Sequence[Activity] = (),
    code_sha: str | None = None,
) -> BuildResult:
    """Normalise, flag and write a snapshot under `out_root/<snapshot id>/`.

    `planted` rows (LLM01/LLM04 scratch builds) are added before the rules run and mark the
    manifest `planted=true`, which the publish path refuses (SNP-06).
    """
    out_root = Path(out_root)
    terms = terms or load_depositor_terms()
    code = code_sha or code_fingerprint()
    tools = tool_versions()
    # releases and the raw-cache hash first: they decide the id and the skip rule
    from .ingest.chembl import read_chembl
    from .ingest.pubchem import read_pubchem

    chembl_rel, _ = read_chembl(cfg, cache, chembl_release)
    pubchem_rel = read_pubchem(cfg, cache, pubchem_release, terms).release
    releases = {"chembl": chembl_rel, "pubchem": pubchem_rel}
    raw_sha = raw_cache_hash(cache, releases)
    created_at = chembl_fetch_time(cache, chembl_rel)
    day = created_at[:10]
    unmarked = [a for a in planted if not _is_planted(a)]
    if unmarked:
        raise SnapshotError(
            f"{len(unmarked)} planted rows lack the planted:/PLANTED- markers; build them with "
            "planted.planted_activity so they can never pass as real rows"
        )
    planted_sha = content_hash([activity_row(a) for a in planted]) if planted else None
    config_sha = sha256_bytes(canonical(content_config(cfg)))
    libraries = dict(tools)  # every tool version is part of what the manifest says
    inputs = {
        "raw_cache": raw_sha,
        "created_at": created_at,
        "config": config_sha,
        "depositor_terms": terms.digest(),
        "libraries": sha256_bytes(canonical(libraries)),
        "code_versions": sha256_bytes(canonical(CODE_VERSIONS)),
        **({"planted": planted_sha} if planted_sha else {}),
    }
    ident = identity(inputs, code)
    _restore_orphans(out_root)
    snap = assign_snapshot_id(out_root, day, ident)
    folder = out_root / snap

    # skip rule (D-1): an existing snapshot is skipped only when it reads back fully verified
    # and every stage manifest is present and agrees with it and with these inputs
    if (folder / MANIFEST).is_file() and _stage_identity(folder) == ident:
        try:
            existing = read_snapshot(folder)
            stages_ok = _stages_agree(folder, existing.manifest)
        except SnapshotError:
            existing, stages_ok = None, False
        if existing is not None and stages_ok:
            return BuildResult(existing.manifest, folder, skipped=True)

    normalised = normalise(
        cfg, cache, snap, chembl_release=chembl_rel, pubchem_release=pubchem_rel, terms=terms
    )
    rows = [*normalised.activities, *[a.model_copy(update={"snapshot": snap}) for a in planted]]
    quality = apply_quality(rows, cfg)
    activities = sorted(
        quality.activities,
        key=lambda a: (a.inchikey, a.source, a.source_record_id or "", a.activity_uid),
    )
    drops = list(normalised.drops)

    tmp_root = Path(tempfile.mkdtemp(prefix=f".{snap}-", dir=_ensure(out_root)))
    try:
        act_rows = [activity_row(a) for a in activities]
        con_rows = [consensus_row(c) for c in quality.consensus]
        drop_rows = [drop_row(d) for d in drops]
        write_parquet(tmp_root / "activities.parquet", "activities", act_rows)
        write_parquet(tmp_root / "consensus.parquet", "consensus", con_rows)
        write_parquet(tmp_root / "drops.parquet", "drops", drop_rows)
        content = {
            "activities.parquet": content_hash(act_rows),
            "consensus.parquet": content_hash(con_rows),
            "drops.parquet": content_hash(drop_rows),
        }
        rows_by_source = dict(sorted(Counter(a.source for a in activities).items()))
        drops_by_reason = dict(sorted(Counter(d.reason for d in drops).items()))
        flags_by_rule = quality.flags_by_rule()
        card = data_card(
            cfg,
            snap,
            releases,
            created_at,
            rows_by_source,
            drops_by_reason,
            flags_by_rule,
            normalised,
            quality,
            activities,
            bool(planted),
        ).encode("utf-8")
        licence = data_licence(terms).encode("utf-8")
        _write_bytes(tmp_root / "DATA_CARD.md", card)
        _write_bytes(tmp_root / "DATA_LICENSE.md", licence)
        content["DATA_CARD.md"] = sha256_bytes(card)
        content["DATA_LICENSE.md"] = sha256_bytes(licence)
        files = {name: sha256_file(tmp_root / name) for name in (*DATA_FILES, *DOC_FILES)}
        manifest = Manifest(
            schema_version=SCHEMA_VERSION,
            snapshot=snap,
            target=cfg.target.name,
            activity_type=cfg.target.activity_type,
            created_at=created_at,  # type: ignore[arg-type]
            source_releases=releases,
            raw_cache_sha256=raw_sha,
            rows_by_source=rows_by_source,
            drops_by_reason=drops_by_reason,
            flags_by_rule=flags_by_rule,
            files=dict(sorted(files.items())),
            content_sha256=dict(sorted(content.items())),
            tool_versions=tools,
            code_sha=code,
            planted=bool(planted),
        )
        (tmp_root / STAGES_DIR).mkdir()
        n_in = sum(normalised.rows_in.values())
        stages = {
            "normalise": _stage_record(
                "normalise",
                {"raw_cache": raw_sha, "config": config_sha, "depositor_terms": terms.digest()},
                {"rows": content_hash([activity_row(a) for a in normalised.activities])},
                n_in,
                len(normalised.activities),
                normalised.drops_by_reason(),
                {"rdkit": normalised.rdkit_version},
            ),
            "quality": _stage_record(
                "quality",
                {
                    "rows": content_hash([activity_row(a) for a in normalised.activities]),
                    "config": config_sha,
                    **({"planted": planted_sha} if planted_sha else {}),
                },
                {
                    "activities.parquet": content["activities.parquet"],
                    "consensus.parquet": content["consensus.parquet"],
                },
                len(rows),
                len(activities),
                {},
                libraries,
            ),
            "snapshot": _stage_record(
                "snapshot",
                {**inputs, "code": code},
                files,
                n_in,
                len(activities),
                drops_by_reason,
                tools,
            ),
        }
        for name, record in stages.items():
            _write_bytes(tmp_root / STAGES_DIR / f"{name}.json", canonical(record) + b"\n")
        _write_bytes(tmp_root / MANIFEST, canonical(manifest.model_dump(mode="json")) + b"\n")
        tmp_root.chmod(0o755)  # mkdtemp makes 0700
        _swap_in(tmp_root, folder)  # the snapshot appears whole, or not at all
    except BaseException:
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise
    return BuildResult(manifest, folder, skipped=False)


def content_config(cfg: BuildConfig) -> dict[str, Any]:
    """The config fields that can change snapshot content (not base URLs or rate limits)."""
    d = cfg.model_dump(mode="json")
    return {
        "target": d["target"],
        "quality": d["quality"],
        "rounding": d["rounding"],
        "exclude_depositors": sorted(d["pubchem"]["exclude_depositors"]),
    }


def _is_planted(a: Activity) -> bool:
    return (a.source_record_id or "").startswith("planted:") or a.assay_id.startswith("PLANTED-")


def _restore_orphans(out_root: Path) -> None:
    """A crash between the two renames of a swap leaves `.<id>-old-<pid>` and no `<id>`: put the
    old snapshot back, so it is neither lost nor rebuilt under another id."""
    if not out_root.is_dir():
        return
    for old in sorted(out_root.glob(".*-old-*")):
        name = old.name[1:].rsplit("-old-", 1)[0]
        target = out_root / name
        if old.is_dir() and not target.exists():
            os.replace(old, target)


def _swap_in(new: Path, folder: Path) -> None:
    """Replace `folder` with `new`; the old folder is moved aside first and removed only after
    the new one is in place, so a failure never leaves neither."""
    old = None
    if folder.exists():
        old = folder.with_name(f".{folder.name}-old-{os.getpid()}")
        if old.exists():
            shutil.rmtree(old)
        os.replace(folder, old)
    try:
        os.replace(new, folder)
    except BaseException:
        if old is not None:
            os.replace(old, folder)
        raise
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)


def _stages_agree(folder: Path, manifest: Manifest) -> bool:
    for stage in CODE_VERSIONS:
        try:
            rec = json.loads((folder / STAGES_DIR / f"{stage}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(rec, dict) or rec.get("stage") != stage:
            return False
        if rec.get("code_version") != CODE_VERSIONS[stage]:
            return False
        if stage == "snapshot" and rec.get("outputs") != dict(manifest.files):
            return False
    return True


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---- reading and verifying ----


@dataclass(frozen=True)
class Snapshot:
    manifest: Manifest
    activities: tuple[Activity, ...]
    consensus: tuple[Consensus, ...]
    drops: tuple[Drop, ...]


def verify_files(folder: Path, manifest: Manifest) -> None:
    for name, digest in manifest.files.items():
        path = folder / name
        if not path.is_file():
            raise SnapshotError(f"{folder}: {name} is missing")
        if sha256_file(path) != digest:
            raise SnapshotError(f"{folder}: {name} does not match the manifest")


def read_snapshot(folder: Path | str) -> Snapshot:
    """Read and verify a snapshot: file hashes, content hashes, and every row re-validated."""
    folder = Path(folder)
    try:
        manifest = Manifest.model_validate_json((folder / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotError(f"{folder}: no valid {MANIFEST} ({exc})") from exc
    expected = {*DATA_FILES, *DOC_FILES}
    if set(manifest.files) != expected or set(manifest.content_sha256) != expected:
        raise SnapshotError(f"{folder}: the manifest must list exactly {sorted(expected)}")
    if folder.name != manifest.snapshot:
        raise SnapshotError(f"{folder}: folder name differs from snapshot id {manifest.snapshot}")
    verify_files(folder, manifest)
    try:
        return _read_rows(folder, manifest)
    except SnapshotError:
        raise
    except Exception as exc:  # malformed Parquet, rows the schemas refuse
        raise SnapshotError(f"{folder}: unreadable snapshot ({type(exc).__name__}: {exc})") from exc


def _read_rows(folder: Path, manifest: Manifest) -> Snapshot:
    act_rows = read_parquet_rows(folder / "activities.parquet")
    con_rows = read_parquet_rows(folder / "consensus.parquet")
    drop_rows = read_parquet_rows(folder / "drops.parquet")
    for name, rows in (
        ("activities.parquet", act_rows),
        ("consensus.parquet", con_rows),
        ("drops.parquet", drop_rows),
    ):
        if content_hash(rows) != manifest.content_sha256[name]:
            raise SnapshotError(f"{folder}: {name} content does not match the manifest")
    activities = []
    for r in act_rows:
        r = dict(r)
        r["flag_reasons"] = json.loads(r["flag_reasons"])
        r["flags"] = tuple(r["flags"])
        activities.append(Activity.model_validate(r))
    consensus = [
        Consensus.model_validate({**r, "member_uids": tuple(r["member_uids"])}) for r in con_rows
    ]
    drops = [Drop.model_validate(r) for r in drop_rows]
    if any(a.snapshot != manifest.snapshot for a in activities):
        raise SnapshotError(f"{folder}: a row belongs to another snapshot")
    # the manifest's counts are recomputed from the rows, so it cannot certify itself
    if dict(Counter(a.source for a in activities)) != dict(manifest.rows_by_source):
        raise SnapshotError(f"{folder}: rows_by_source does not match the rows")
    if dict(Counter(d.reason for d in drops)) != dict(manifest.drops_by_reason):
        raise SnapshotError(f"{folder}: drops_by_reason does not match the rows")
    flags = Counter(f for a in activities for f in a.flags)
    if {r: flags.get(r, 0) for r in FLAGS} != dict(manifest.flags_by_rule):
        raise SnapshotError(f"{folder}: flags_by_rule does not match the rows")
    _check_integrity(folder, activities, consensus)
    planted_rows = [a for a in activities if _is_planted(a)]
    stage_planted = None
    stage_file = folder / STAGES_DIR / "snapshot.json"
    if stage_file.is_file():
        try:
            stage_planted = json.loads(stage_file.read_text(encoding="utf-8"))["inputs"].get(
                "planted"
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise SnapshotError(f"{folder}: the snapshot stage manifest is unreadable") from exc
    if not manifest.planted and (planted_rows or stage_planted):
        raise SnapshotError(f"{folder}: planted rows inside a manifest that says planted=false")
    card = (folder / "DATA_CARD.md").read_text(encoding="utf-8")
    says_planted = "- Planted rows: YES" in card
    if says_planted != manifest.planted:
        raise SnapshotError(f"{folder}: the data card and the manifest disagree on planted rows")
    return Snapshot(manifest, tuple(activities), tuple(consensus), tuple(drops))


def _check_integrity(
    folder: Path, activities: Sequence[Activity], consensus: Sequence[Consensus]
) -> None:
    """Cross-checks that need no config: unique uids, consensus members partitioning the rows,
    QC2 flags agreeing with conflicts, and medians and spreads recomputed from the rows."""
    import statistics

    from assay_pipeline.schemas.data import PIC50_DECIMALS

    from .quality import key_of, spread

    uids = [a.activity_uid for a in activities]
    if len(set(uids)) != len(uids):
        raise SnapshotError(f"{folder}: an activity_uid repeats")
    by_uid = {a.activity_uid: a for a in activities}
    members = [u for c in consensus for u in c.member_uids]
    if sorted(members) != sorted(uids):
        raise SnapshotError(f"{folder}: consensus members do not partition the activities")
    for c in consensus:
        rows = [by_uid[u] for u in c.member_uids]
        key = (c.inchikey, c.target, c.variant, c.assay_type)
        if any(key_of(a) != key for a in rows):
            raise SnapshotError(f"{folder}: a consensus row lists a member of another key")
        exact = [a.pic50 for a in rows if a.relation == "=" and a.pic50 is not None]
        median = round(statistics.median(exact), PIC50_DECIMALS) if exact else None
        if c.median_pic50 != median or c.spread_pic50 != (spread(exact) if exact else None):
            raise SnapshotError(f"{folder}: a consensus median or spread does not match its rows")
        if any(("QC2" in a.flags) != c.conflict for a in rows):
            raise SnapshotError(f"{folder}: QC2 flags disagree with the consensus conflicts")


def publish(folder: Path | str, dest_root: Path | str) -> Path:
    """Copy a verified snapshot to `dest_root/<id>/`. A planted snapshot is refused (SNP-06)."""
    snap = read_snapshot(folder)
    if snap.manifest.planted:
        raise SnapshotError(
            f"{folder}: the manifest says planted=true; planted rows never reach a published "
            "snapshot (SNP-06)"
        )
    dest = Path(dest_root) / snap.manifest.snapshot
    if dest.exists():
        existing = read_snapshot(dest)
        if existing.manifest != snap.manifest:
            raise SnapshotError(f"{dest} already holds a different snapshot with this id")
        return dest
    tmp = Path(tempfile.mkdtemp(prefix=".publish-", dir=_ensure(Path(dest_root))))
    try:
        for name in (*DATA_FILES, *DOC_FILES, MANIFEST):  # fixed names: nothing else is copied
            shutil.copy2(Path(folder) / name, tmp / name)
        tmp.chmod(0o755)
        os.replace(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return dest


# ---- documents (SNP-05) ----


STATED_LIMITS = (
    "PubChem rows carry no mutation: BindingDB wild-type and mutant rows look alike (H2).",
    "PubChem record ids are positional within a release, so PubChem uids are stable within a "
    "release, not across releases.",
    "Assays naming the target only by a RefSeq accession are dropped (target_not_configured).",
    "The PubChem list of AIDs that ChEMBL imported is read live, not tied to the ChEMBL release.",
    "No QC2 key spans ChEMBL and PubChem (PubChem rows are assay type U); cross-source overlap "
    "is reported below by InChIKey.",
    "RDKit Cleanup disconnects metals and the largest fragment can be a large counterion (D-3).",
    "Stealth poison (plausible values wrong by 10x with no record to disagree) is not caught "
    "by any rule (H7).",
)


def data_card(
    cfg: BuildConfig,
    snap: str,
    releases: dict[str, str],
    created_at: str,
    rows_by_source: dict[str, int],
    drops_by_reason: dict[str, int],
    flags_by_rule: dict[str, int],
    normalised: Any,
    quality: Any,
    activities: Sequence[Activity],
    planted: bool,
) -> str:
    by_source: dict[str, set[str]] = {"chembl": set(), "pubchem": set()}
    for a in activities:
        by_source[a.source].add(a.inchikey)
    overlap = len(by_source["chembl"] & by_source["pubchem"])
    rows_in = normalised.rows_in
    t = cfg.target
    lines = [
        f"# Data card: {t.name} {t.activity_type} snapshot {snap}",
        "",
        "Generated from the raw cache by `assay snapshot`; every number here is reproducible.",
        "",
        "## Sources and releases",
        "",
        f"- ChEMBL (target {t.chembl_target_id}): release {releases['chembl']}, "
        f"fetched {created_at}",
        f"- PubChem (gene {t.gene_symbol}, UniProt {t.uniprot}): release {releases['pubchem']}",
        f"- Target: {t.name} ({t.activity_type})",
        f"- Planted rows: {'YES — not for publication' if planted else 'none'}",
        "",
        "## Yields",
        "",
        "| Source | Rows offered to normalise | Rows in the snapshot |",
        "| --- | --- | --- |",
    ]
    for source in ("chembl", "pubchem"):
        lines.append(f"| {source} | {rows_in.get(source, 0)} | {rows_by_source.get(source, 0)} |")
    lines += [
        "",
        "Dropped rows by reason (ingest and normalise):",
        "",
        "| Reason | Rows |",
        "| --- | --- |",
    ]
    lines += [f"| {k} | {v} |" for k, v in drops_by_reason.items()] or ["| (none) | 0 |"]
    lines += [
        "",
        "## Quality rules",
        "",
        "Rules flag and never delete (D-6). Definitions: Tech Spec §5.3.",
        "",
        "| Rule | Meaning | Rows flagged |",
        "| --- | --- | --- |",
    ]
    meaning = {
        "QC1": "nM value cannot be computed",
        "QC2": "conflict: '=' spread above 1.0 pIC50 on the same key",
        "QC3": f"pIC50 outside [{cfg.quality.pic50_min:g}, {cfg.quality.pic50_max:g}] or 0 nM",
        "QC4": "no source record id",
        "QC5": "censored or unknown relation",
    }
    lines += [f"| {r} | {meaning[r]} | {flags_by_rule[r]} |" for r in FLAGS]
    lines += [
        "",
        f"- Consensus keys: {len(quality.consensus)}, in conflict: {quality.conflicts}",
        f"- ChEMBL rows kept with a data validity comment: {normalised.validity_comments}",
        f"- PubChem rows whose source InChIKey differs from the RDKit parent: "
        f"{normalised.source_key_mismatches}",
        f"- Compounds (InChIKey) present in both ChEMBL and PubChem, any depositor: {overlap} "
        "(D-24)",
        f"- RDKit {normalised.rdkit_version}",
        "",
        "## Stated limits",
        "",
        *[f"- {limit}" for limit in STATED_LIMITS],
        "",
        "## Licence",
        "",
        "Data: CC BY-SA 3.0, see DATA_LICENSE.md. Code: MIT.",
        "",
    ]
    return "\n".join(lines)


def data_licence(terms: DepositorTerms) -> str:
    lines = [
        "# Data licence",
        "",
        "This snapshot is derived from ChEMBL and PubChem data and is licensed under the "
        "Creative Commons Attribution-ShareAlike 3.0 Unported licence (CC BY-SA 3.0): "
        "https://creativecommons.org/licenses/by-sa/3.0/ (D-15).",
        "",
        "## Attribution",
        "",
        "- ChEMBL, EMBL-EBI (https://www.ebi.ac.uk/chembl/), licensed CC BY-SA 3.0. Zdrazil B. et "
        "al., The ChEMBL Database in 2023, Nucleic Acids Research 52 (2024) D1180–D1192.",
        "- PubChem, NCBI (https://pubchem.ncbi.nlm.nih.gov/). Rows are kept only from depositors "
        "cleared in the depositor ledger:",
    ]
    for d in sorted(terms.depositors, key=lambda x: x.name):
        if d.keep:
            licence = d.licence.strip().rstrip(".")
            lines.append(f"  - {d.name}: {licence}. Attribution: {d.attribution}.")
    lines += [
        "",
        "The code that builds this snapshot is MIT licensed and does not inherit this licence.",
        "",
    ]
    return "\n".join(lines)
