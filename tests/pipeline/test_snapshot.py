"""Q2.2: deterministic snapshot, manifest, id rule, stage manifests, data card, publish."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assay_pipeline.pipeline import snapshot as snapmod
from assay_pipeline.pipeline.planted import load_planted, planted_activity
from assay_pipeline.pipeline.snapshot import (
    MANIFEST,
    SnapshotError,
    assign_snapshot_id,
    build_snapshot,
    publish,
    read_snapshot,
)
from tests.pipeline import test_normalise as norm_t

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "snapshot"
CODE = "a" * 40


def cfg():
    return norm_t.cfg()


@pytest.fixture
def cache(tmp_path):
    return norm_t.built(tmp_path / "cache")


def build(cache, root, **kw):
    kw.setdefault("code_sha", CODE)
    return build_snapshot(cfg(), cache, root, **kw)


# ---- SNP-01: deterministic Parquet ----


def test_two_builds_write_identical_bytes(cache, tmp_path):
    a = build(cache, tmp_path / "a")
    b = build(cache, tmp_path / "b")
    for name in (*snapmod.DATA_FILES, *snapmod.DOC_FILES, MANIFEST):
        assert (a.directory / name).read_bytes() == (b.directory / name).read_bytes(), name


def test_parquet_carries_no_pandas_or_arrow_schema_metadata(cache, tmp_path):
    import pyarrow.parquet as pq

    r = build(cache, tmp_path / "s")
    for name in snapmod.DATA_FILES:
        meta = pq.ParquetFile(r.directory / name).metadata
        assert meta.metadata is None or not any(
            k in meta.metadata for k in (b"pandas", b"ARROW:schema")
        )
        assert meta.row_group(0).column(0).compression == "ZSTD"


def test_rows_are_written_in_the_section_5_5_order(cache, tmp_path):
    snap = read_snapshot(build(cache, tmp_path / "s").directory)
    keys = [
        (a.inchikey, a.source, a.source_record_id or "", a.activity_uid) for a in snap.activities
    ]
    assert keys == sorted(keys)


def test_a_snapshot_reads_back_to_the_same_rows(cache, tmp_path):
    r = build(cache, tmp_path / "s")
    snap = read_snapshot(r.directory)
    assert len(snap.activities) == 18 and len(snap.drops) == 19
    assert all(a.snapshot == r.manifest.snapshot for a in snap.activities)
    assert sum(len(c.member_uids) for c in snap.consensus) == 18


# ---- SNP-02: the manifest ----


def test_the_manifest_records_everything_needed_to_compare_builds(cache, tmp_path):
    m = build(cache, tmp_path / "s").manifest
    assert m.source_releases == {"chembl": "ChEMBL_37", "pubchem": "2026-11-15"}
    assert m.rows_by_source == {"chembl": 15, "pubchem": 3}
    assert set(m.flags_by_rule) == {"QC1", "QC2", "QC3", "QC4", "QC5"}
    assert m.drops_by_reason["chembl_deposited_aid"] == 1
    assert set(m.files) == set(m.content_sha256) == {*snapmod.DATA_FILES, *snapmod.DOC_FILES}
    assert {"pyarrow", "rdkit", "pydantic", "assay-pipeline", "python"} <= set(m.tool_versions)
    assert m.created_at.isoformat().startswith("2026-11-15T13:24:00")  # from the cache, no clock
    assert m.code_sha == CODE and m.planted is False


def test_the_default_code_sha_fingerprints_the_package_source():
    a, b = snapmod.code_fingerprint(), snapmod.code_fingerprint()
    assert a == b and len(a) == 40 and int(a, 16) >= 0


def test_a_tampered_file_or_row_is_caught_on_read(cache, tmp_path):
    r = build(cache, tmp_path / "s")
    card = r.directory / "DATA_CARD.md"
    card.write_text(card.read_text(encoding="utf-8") + "x", encoding="utf-8", newline="\n")
    with pytest.raises(SnapshotError, match=r"DATA_CARD\.md does not match"):
        read_snapshot(r.directory)


def test_content_hashes_ignore_file_bytes_but_not_rows():
    rows = [{"a": 1, "b": "x"}]
    assert snapmod.content_hash(rows) == snapmod.content_hash([{"b": "x", "a": 1}])
    assert snapmod.content_hash(rows) != snapmod.content_hash([{"a": 2, "b": "x"}])


# ---- SNP-03: the snapshot id ----


def test_the_id_is_the_chembl_fetch_date_and_a_rebuild_reuses_it(cache, tmp_path):
    root = tmp_path / "s"
    first = build(cache, root)
    assert first.manifest.snapshot == "2026-11-15.1"
    (first.directory / snapmod.STAGES_DIR / "quality.json").unlink()  # force a real rebuild
    again = build(cache, root)
    assert not again.skipped and again.manifest.snapshot == "2026-11-15.1"
    import shutil

    shutil.rmtree(first.directory / snapmod.STAGES_DIR)  # identity unknowable: never overwritten
    assert build(cache, root).manifest.snapshot == "2026-11-15.2"


def test_a_code_change_takes_the_next_id(cache, tmp_path):
    root = tmp_path / "s"
    build(cache, root)
    other = build(cache, root, code_sha="b" * 40)
    assert other.manifest.snapshot == "2026-11-15.2"
    assert build(cache, root).manifest.snapshot == "2026-11-15.1"  # the first code keeps .1


def test_assign_snapshot_id_ignores_folders_without_a_valid_manifest(tmp_path):
    (tmp_path / "junk").mkdir()
    (tmp_path / "junk" / MANIFEST).write_text("{}", encoding="utf-8")
    (tmp_path / ".2026-11-15.1-tmp").mkdir()  # a leftover temp folder never counts
    assert assign_snapshot_id(tmp_path, "2026-11-15", "i" * 64) == "2026-11-15.1"


# ---- SNP-04: the rebuild gate ----


@pytest.mark.rebuild
def test_rebuild_gate_two_clean_builds_give_identical_manifests(tmp_path):
    manifests = []
    for side in ("one", "two"):
        cache = norm_t.built(tmp_path / side / "cache")
        r = build_snapshot(cfg(), cache, tmp_path / side / "snapshots")  # real code fingerprint
        manifests.append((r.directory / MANIFEST).read_bytes())
    assert manifests[0] == manifests[1]


GOLDEN = FIX / "expected_manifest.json"
UNSTABLE = ("code_sha", "tool_versions")  # change with every commit or Python version


def golden_view(m) -> dict:
    """The manifest minus what legitimately varies: the code fingerprint, tool versions, and the
    Parquet file bytes (zstd builds may differ between platforms; row content may not)."""
    got = {k: v for k, v in m.model_dump(mode="json").items() if k not in UNSTABLE}
    got["files"] = {k: v for k, v in got["files"].items() if not k.endswith(".parquet")}
    return got


def golden_cache(root: Path):
    """Both recorded sources, PubChem ingested with the pinned golden ledger."""
    from assay_pipeline.pipeline.ingest.cache import RawCache
    from assay_pipeline.pipeline.ingest.chembl import ingest_chembl
    from assay_pipeline.pipeline.ingest.pubchem import ingest_pubchem

    pub = norm_t.pubchem_t
    cache = RawCache(root)
    ingest_chembl(cfg(), cache, norm_t.chembl_t.fetcher(norm_t.chembl_t.Server()))
    ingest_pubchem(cfg(), cache, pub.fetcher(pub.Server()), terms=pub.GOLDEN_TERMS)
    return cache


@pytest.mark.golden
def test_golden_manifest_from_the_recorded_responses(tmp_path):
    cache = golden_cache(tmp_path / "cache")
    m = build(cache, tmp_path / "s", terms=norm_t.pubchem_t.GOLDEN_TERMS).manifest
    assert golden_view(m) == json.loads(GOLDEN.read_text(encoding="utf-8"))


# ---- SNP-05: data card and licence ----


def test_the_data_card_reports_sources_yields_rules_and_limits(cache, tmp_path):
    r = build(cache, tmp_path / "s")
    card = (r.directory / "DATA_CARD.md").read_text(encoding="utf-8")
    for needle in (
        "ChEMBL_37",
        "2026-11-15",
        "| chembl | 15 | 15 |",
        "| pubchem | 3 | 3 |",
        "| chembl_deposited_aid | 1 |",
        "| QC5 | censored or unknown relation | 5 |",
        "present in both ChEMBL and PubChem, any depositor: 0",
        "Stated limits",
        "Stealth poison",
        "CC BY-SA 3.0",
    ):
        assert needle in card, needle


def test_the_licence_names_cc_by_sa_and_every_cleared_depositor(cache, tmp_path):
    r = build(cache, tmp_path / "s")
    text = (r.directory / "DATA_LICENSE.md").read_text(encoding="utf-8")
    assert "CC BY-SA 3.0" in text and "ChEMBL" in text and "BindingDB" in text
    assert "MIT" in text


# ---- SNP-06: publish refuses planted snapshots ----


def test_publish_copies_a_verified_snapshot(cache, tmp_path):
    r = build(cache, tmp_path / "s")
    dest = publish(r.directory, tmp_path / "published")
    assert read_snapshot(dest).manifest == r.manifest
    assert publish(r.directory, tmp_path / "published") == dest  # idempotent


def test_publish_refuses_a_planted_snapshot(cache, tmp_path):
    case = load_planted().cases[3]
    rows = [
        planted_activity(
            case.id, i, x.smiles, x.value, x.units, x.relation, True, cfg(), "2026-11-15.1"
        )
        for i, x in enumerate(case.rows)
    ]
    r = build(cache, tmp_path / "s", planted=rows)
    assert r.manifest.planted is True
    assert r.manifest.snapshot != build(cache, tmp_path / "s").manifest.snapshot
    with pytest.raises(SnapshotError, match="planted=true"):
        publish(r.directory, tmp_path / "published")
    assert not (tmp_path / "published" / r.manifest.snapshot).exists()


def test_publish_refuses_a_tampered_snapshot(cache, tmp_path):
    r = build(cache, tmp_path / "s")
    (r.directory / "drops.parquet").write_bytes(b"not parquet")
    with pytest.raises(SnapshotError):
        publish(r.directory, tmp_path / "published")


def test_publish_refuses_to_overwrite_a_different_snapshot_with_the_same_id(cache, tmp_path):
    a = build(cache, tmp_path / "a")
    b = build(cache, tmp_path / "b", code_sha="c" * 40)
    assert a.manifest.snapshot == b.manifest.snapshot  # same id in two separate roots
    publish(a.directory, tmp_path / "published")
    with pytest.raises(SnapshotError, match="different snapshot"):
        publish(b.directory, tmp_path / "published")


# ---- SNP-07: stage manifests and the skip rule ----


def test_stage_manifests_record_inputs_outputs_and_counts(cache, tmp_path):
    r = build(cache, tmp_path / "s")
    for stage in ("normalise", "quality", "snapshot"):
        rec = json.loads((r.directory / "_stages" / f"{stage}.json").read_text(encoding="utf-8"))
        assert rec["stage"] == stage and rec["code_version"] and rec["inputs"] and rec["outputs"]
        assert rec["rows_out"] >= 0 and "drops_by_reason" in rec and "tool_versions" in rec


def test_an_up_to_date_snapshot_is_skipped(cache, tmp_path, monkeypatch):
    first = build(cache, tmp_path / "s")
    monkeypatch.setattr(snapmod, "normalise", lambda *a, **k: pytest.fail("rebuilt"))
    again = build(cache, tmp_path / "s")
    assert again.skipped and again.manifest == first.manifest


@pytest.mark.parametrize("change", ["code_version", "file", "terms"])
def test_a_changed_input_or_damaged_output_is_rebuilt(cache, tmp_path, monkeypatch, change):
    first = build(cache, tmp_path / "s")
    kw = {}
    if change == "code_version":
        monkeypatch.setitem(snapmod.CODE_VERSIONS, "snapshot", "2")
    elif change == "file":
        (first.directory / "consensus.parquet").write_bytes(b"damaged")
    else:
        kw["terms"] = norm_t.pubchem_t.GOLDEN_TERMS  # another ledger digest
    if change == "terms":
        with pytest.raises(Exception, match="depositor ledger"):
            build(cache, tmp_path / "s", **kw)  # the release itself was built with the other
        return
    again = build(cache, tmp_path / "s", **kw)
    assert not again.skipped
    read_snapshot(again.directory)


def test_a_failed_build_leaves_no_partial_snapshot(cache, tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(snapmod, "data_licence", boom)
    with pytest.raises(RuntimeError):
        build(cache, tmp_path / "s")
    assert [p.name for p in (tmp_path / "s").iterdir()] == []


# ---- the CLI ----


def test_cli_snapshot_and_publish(cache, tmp_path, capsys):
    from assay_pipeline import cli

    root = tmp_path / "snaps"
    args = ["snapshot", "--cache", str(cache.root), "--out", str(root), "--json"]
    assert cli.main(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["manifest"]["rows_by_source"] == {"chembl": 15, "pubchem": 3}
    folder = out["folder"]
    assert cli.main(["publish", folder, "--dest", str(tmp_path / "pub"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert (
        cli.main(["snapshot", "--cache", str(tmp_path / "empty"), "--out", str(root), "--json"])
        == 2
    )
    assert json.loads(capsys.readouterr().out)["ok"] is False


# ---- verifier pass 1 findings ----


def _rewrite_manifest(folder: Path, **changes) -> None:
    path = folder / MANIFEST
    data = json.loads(path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        data[key] = value
    path.write_text(json.dumps(data), encoding="utf-8", newline="\n")


def test_flipping_planted_to_false_does_not_get_a_planted_snapshot_published(cache, tmp_path):
    case = load_planted().cases[3]
    rows = [
        planted_activity(
            case.id, i, x.smiles, x.value, x.units, x.relation, True, cfg(), "2026-11-15.1"
        )
        for i, x in enumerate(case.rows)
    ]
    r = build(cache, tmp_path / "s", planted=rows)
    _rewrite_manifest(r.directory, planted=False)
    with pytest.raises(SnapshotError, match="planted rows inside"):
        publish(r.directory, tmp_path / "published")
    assert not (tmp_path / "published").exists() or not any((tmp_path / "published").iterdir())


@pytest.mark.parametrize("extra", ["../evil.txt", "notes.txt"])
def test_a_manifest_listing_other_files_is_refused(cache, tmp_path, extra):
    r = build(cache, tmp_path / "s")
    m = json.loads((r.directory / MANIFEST).read_text(encoding="utf-8"))
    (tmp_path / "s" / "evil.txt").write_text("x", encoding="utf-8")
    digest = snapmod.sha256_bytes(b"x")
    _rewrite_manifest(r.directory, files={**m["files"], extra: digest},
                      content_sha256={**m["content_sha256"], extra: digest})  # fmt: skip
    with pytest.raises(SnapshotError, match="exactly"):
        publish(r.directory, tmp_path / "published")
    assert not (tmp_path / "evil.txt").exists()


def test_a_manifest_missing_a_file_is_a_clean_error(cache, tmp_path, capsys):
    from assay_pipeline import cli

    r = build(cache, tmp_path / "s")
    m = json.loads((r.directory / MANIFEST).read_text(encoding="utf-8"))
    files = {k: v for k, v in m["files"].items() if k != "activities.parquet"}
    content = {k: v for k, v in m["content_sha256"].items() if k != "activities.parquet"}
    _rewrite_manifest(r.directory, files=files, content_sha256=content)
    assert cli.main(["publish", str(r.directory), "--dest", str(tmp_path / "p"), "--json"]) == 2
    assert "exactly" in json.loads(capsys.readouterr().out)["error"]


def test_a_consistent_tamper_is_caught_by_recomputing_the_counts(cache, tmp_path):
    r = build(cache, tmp_path / "s")
    path = r.directory / "activities.parquet"
    rows = snapmod.read_parquet_rows(path)[1:]  # one row deleted
    snapmod.write_parquet(path, "activities", rows)
    m = json.loads((r.directory / MANIFEST).read_text(encoding="utf-8"))
    _rewrite_manifest(
        r.directory,
        files={**m["files"], "activities.parquet": snapmod.sha256_file(path)},
        content_sha256={**m["content_sha256"], "activities.parquet": snapmod.content_hash(rows)},
    )
    with pytest.raises(SnapshotError, match="does not match the rows"):
        read_snapshot(r.directory)
    again = build(cache, tmp_path / "s")  # and the skip rule rebuilds it
    assert not again.skipped and len(read_snapshot(again.directory).activities) == 18


@pytest.mark.parametrize("damage", ["delete-normalise", "garbage-quality", "edit-outputs"])
def test_damaged_stage_manifests_force_a_rebuild(cache, tmp_path, damage):
    r = build(cache, tmp_path / "s")
    stages = r.directory / snapmod.STAGES_DIR
    if damage == "delete-normalise":
        (stages / "normalise.json").unlink()
    elif damage == "garbage-quality":
        (stages / "quality.json").write_text("garbage", encoding="utf-8")
    else:
        rec = json.loads((stages / "snapshot.json").read_text(encoding="utf-8"))
        rec["outputs"]["activities.parquet"] = "0" * 64
        (stages / "snapshot.json").write_text(json.dumps(rec), encoding="utf-8")
    again = build(cache, tmp_path / "s")
    assert not again.skipped and (stages / "normalise.json").is_file()
    assert again.manifest.snapshot == r.manifest.snapshot


def test_a_same_day_reingest_with_another_fetch_time_is_a_new_snapshot(tmp_path):
    from assay_pipeline.pipeline.ingest.cache import RawCache
    from assay_pipeline.pipeline.ingest.chembl import ingest_chembl
    from assay_pipeline.pipeline.ingest.pubchem import ingest_pubchem

    first = build(norm_t.built(tmp_path / "c1"), tmp_path / "s")
    chembl = norm_t.chembl_t
    later = RawCache(tmp_path / "c2")

    class Later(chembl.Server):
        def __call__(self, request):
            resp = super().__call__(request)
            return type(resp)(resp.status_code, content=resp.content,
                              headers={"Date": "Sun, 15 Nov 2026 15:00:00 GMT"})  # fmt: skip

    ingest_chembl(cfg(), later, chembl.fetcher(Later()))
    pub = norm_t.pubchem_t
    ingest_pubchem(cfg(), later, pub.fetcher(pub.Server()))
    second = build(later, tmp_path / "s")
    assert not second.skipped and second.manifest.snapshot == "2026-11-15.2"
    assert second.manifest.created_at != first.manifest.created_at
    assert read_snapshot(first.directory).manifest == first.manifest  # the first is untouched


def test_a_config_change_takes_a_new_id_and_never_replaces_the_old_snapshot(cache, tmp_path):
    first = build(cache, tmp_path / "s")
    narrow = cfg().model_copy(
        update={"quality": cfg().quality.model_copy(update={"pic50_max": 6.0})}
    )
    second = build_snapshot(narrow, cache, tmp_path / "s", code_sha=CODE)
    assert second.manifest.snapshot == "2026-11-15.2"
    assert read_snapshot(first.directory).manifest == first.manifest


def test_a_renamed_snapshot_folder_is_refused_and_never_deleted(cache, tmp_path):
    first = build(cache, tmp_path / "s")
    moved = first.directory.with_name("2026-11-15.2")
    first.directory.rename(moved)
    with pytest.raises(SnapshotError, match="folder name"):
        read_snapshot(moved)
    other = build(cache, tmp_path / "s", code_sha="b" * 40)
    assert other.manifest.snapshot == "2026-11-15.1" and moved.is_dir()


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="POSIX permissions")
def test_snapshots_are_readable_by_others(cache, tmp_path):
    r = build(cache, tmp_path / "s")
    assert r.directory.stat().st_mode & 0o755 == 0o755
    dest = publish(r.directory, tmp_path / "p")
    assert dest.stat().st_mode & 0o755 == 0o755


def test_a_rebuild_over_an_old_folder_leaves_no_leftovers(cache, tmp_path):
    r = build(cache, tmp_path / "s")
    (r.directory / snapmod.STAGES_DIR / "quality.json").unlink()
    build(cache, tmp_path / "s")
    assert sorted(p.name for p in (tmp_path / "s").iterdir()) == [r.manifest.snapshot]


# ---- verifier pass 2 nice-to-haves ----


def _tamper(folder: Path, table: str, rows: list, **manifest_changes) -> None:
    """Rewrite one Parquet file and make the manifest's hashes (and any counts given) agree."""
    name = f"{table}.parquet"
    path = folder / name
    snapmod.write_parquet(path, table, rows)
    m = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    _rewrite_manifest(
        folder,
        files={**m["files"], name: snapmod.sha256_file(path)},
        content_sha256={**m["content_sha256"], name: snapmod.content_hash(rows)},
        **manifest_changes,
    )


def test_hiding_conflicts_with_consistent_counts_is_caught(cache, tmp_path):
    r = build(cache, tmp_path / "s")
    rows = snapmod.read_parquet_rows(r.directory / "activities.parquet")
    for row in rows:
        if "QC2" in row["flags"]:
            row["flags"] = [f for f in row["flags"] if f != "QC2"]
            reasons = json.loads(row["flag_reasons"])
            reasons.pop("QC2")
            row["flag_reasons"] = snapmod.canonical(reasons).decode()
    flags = dict(r.manifest.flags_by_rule)
    flags["QC2"] = 0
    _tamper(r.directory, "activities", rows, flags_by_rule=flags)
    with pytest.raises(SnapshotError, match="QC2 flags disagree"):
        read_snapshot(r.directory)


def test_a_moved_consensus_median_is_caught(cache, tmp_path):
    r = build(cache, tmp_path / "s")
    rows = snapmod.read_parquet_rows(r.directory / "consensus.parquet")
    target = next(row for row in rows if row["median_pic50"] is not None)
    target["median_pic50"] += 2
    _tamper(r.directory, "consensus", rows)
    with pytest.raises(SnapshotError, match="median or spread"):
        read_snapshot(r.directory)


def test_a_duplicated_row_is_caught(cache, tmp_path):
    r = build(cache, tmp_path / "s")
    rows = snapmod.read_parquet_rows(r.directory / "activities.parquet")
    rows[1] = dict(rows[0])
    _tamper(r.directory, "activities", rows)
    with pytest.raises(SnapshotError):
        read_snapshot(r.directory)


def test_planted_rows_without_markers_are_refused_at_build(cache, tmp_path):
    real = read_snapshot(build(cache, tmp_path / "s").directory).activities[0]
    copy = real.model_copy(update={"activity_uid": "f" * 16, "source_record_id": "999999"})
    with pytest.raises(SnapshotError, match="markers"):
        build(cache, tmp_path / "s2", planted=[copy])


def test_the_data_card_must_agree_with_the_manifest_on_planted_rows(cache, tmp_path):
    r = build(cache, tmp_path / "s")
    card = r.directory / "DATA_CARD.md"
    text = card.read_text(encoding="utf-8").replace("- Planted rows: none", "- Planted rows: YES")
    card.write_text(text, encoding="utf-8", newline="\n")
    m = json.loads((r.directory / MANIFEST).read_text(encoding="utf-8"))
    digest = snapmod.sha256_file(card)
    _rewrite_manifest(r.directory, files={**m["files"], "DATA_CARD.md": digest},
                      content_sha256={**m["content_sha256"], "DATA_CARD.md": digest})  # fmt: skip
    with pytest.raises(SnapshotError, match="disagree on planted"):
        read_snapshot(r.directory)


def test_a_rate_limit_or_url_change_reuses_the_id(cache, tmp_path):
    first = build(cache, tmp_path / "s")
    tweaked = cfg().model_copy(
        update={"pubchem": cfg().pubchem.model_copy(update={"max_requests_per_second": 2.0})}
    )
    again = build_snapshot(tweaked, cache, tmp_path / "s", code_sha=CODE)
    assert again.skipped and again.manifest.snapshot == first.manifest.snapshot


def test_an_orphaned_old_folder_from_a_crashed_swap_is_restored(cache, tmp_path):
    first = build(cache, tmp_path / "s")
    orphan = first.directory.with_name(f".{first.directory.name}-old-12345")
    first.directory.rename(orphan)  # a crash between the two renames
    again = build(cache, tmp_path / "s")
    assert again.skipped and again.manifest == first.manifest and not orphan.exists()


def test_a_garbage_snapshot_stage_manifest_gets_a_clear_message(cache, tmp_path):
    r = build(cache, tmp_path / "s")
    (r.directory / snapmod.STAGES_DIR / "snapshot.json").write_text("garbage", encoding="utf-8")
    with pytest.raises(SnapshotError, match="stage manifest is unreadable"):
        read_snapshot(r.directory)
