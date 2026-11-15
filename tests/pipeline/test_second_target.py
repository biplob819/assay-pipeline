"""OSS-06: a second target (ABL1) builds end to end from a config file, on recorded responses.

Ingest (ChEMBL and PubChem) -> snapshot -> agent database, driven only by
`tests/fixtures/targets/abl1/build.yaml`, served by `httpx.MockTransport` from real responses
recorded on 15 Nov 2026 (see the fixture's README.md). No network: sockets are disabled here.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from assay_pipeline.config import load_build_config, load_views_config
from assay_pipeline.pipeline.agentdb import build_agent_db
from assay_pipeline.pipeline.ingest.cache import RawCache
from assay_pipeline.pipeline.ingest.chembl import ingest_chembl
from assay_pipeline.pipeline.ingest.http import Fetcher
from assay_pipeline.pipeline.ingest.pubchem import ingest_pubchem
from assay_pipeline.pipeline.snapshot import MANIFEST, build_snapshot, read_snapshot
from tests.fixtures.targets.replay import Replay
from tests.pipeline.test_ingest_pubchem import GOLDEN_TERMS
from tests.pipeline.test_snapshot import golden_view

ABL1 = Path(__file__).resolve().parents[1] / "fixtures" / "targets" / "abl1"
CONFIG = ABL1 / "build.yaml"
CODE = "a" * 40
VIEWS = ("v_activities", "v_compounds", "v_consensus", "v_quality")


def cfg():
    return load_build_config(CONFIG)


def ingest(root: Path) -> tuple[RawCache, Replay]:
    """Both sources from the recording; PubChem with the pinned golden ledger, so a later edit
    of the shipped ledger cannot change which AIDs this fixture asks for."""
    replay = Replay(ABL1)
    fetcher = Fetcher(transport=httpx.MockTransport(replay), sleep=lambda _s: None)
    cache = RawCache(root)
    ingest_chembl(cfg(), cache, fetcher)
    ingest_pubchem(cfg(), cache, fetcher, terms=GOLDEN_TERMS)
    return cache, replay


def build(root: Path):
    cache, _ = ingest(root / "cache")
    return build_snapshot(cfg(), cache, root / "snapshots", terms=GOLDEN_TERMS, code_sha=CODE)


@pytest.fixture
def built(tmp_path):  # function scope, so it runs after the autouse no-network fixture
    result = build(tmp_path)
    db = build_agent_db(result.directory, tmp_path / "agent.duckdb")
    return result, db


def test_the_abl1_config_differs_from_the_default_only_in_target_and_page_size():
    default = load_build_config().model_dump()
    abl1 = cfg().model_dump()
    assert abl1["target"] == {
        "name": "ABL1",
        "chembl_target_id": "CHEMBL1862",
        "uniprot": "P00519",
        "gene_symbol": "ABL1",
        "activity_type": "IC50",
    }
    assert abl1["target"]["name"] != default["target"]["name"]
    assert abl1["chembl"]["page_size"] == 5
    for block in (abl1, default):
        block.pop("target")
        block["chembl"].pop("page_size")
    assert abl1 == default


@pytest.mark.golden
def test_ingest_uses_every_recorded_response_and_nothing_else(tmp_path):
    cache, replay = ingest(tmp_path)
    assert replay.unused() == []
    assert len(replay.served) == len(replay.entries)  # no request repeated
    assert cache.releases("chembl") == ["ChEMBL_37"]
    assert cache.releases("pubchem") == ["2026-11-15"]


def test_the_replay_refuses_a_request_it_has_no_recording_for(tmp_path):
    egfr = load_build_config()  # the default target asks for other URLs
    fetcher = Fetcher(transport=httpx.MockTransport(Replay(ABL1)), sleep=lambda _s: None)
    with pytest.raises(AssertionError, match="no recorded response"):
        ingest_chembl(egfr, RawCache(tmp_path), fetcher)


@pytest.mark.golden
def test_the_manifest_and_data_card_name_abl1(built):
    result, _ = built
    m = result.manifest
    assert (m.target, m.activity_type, m.planted) == ("ABL1", "IC50", False)
    assert m.source_releases == {"chembl": "ChEMBL_37", "pubchem": "2026-11-15"}
    assert m.created_at.isoformat().startswith("2026-11-15T21:14")  # ChEMBL Date header
    card = (result.directory / "DATA_CARD.md").read_text(encoding="utf-8")
    for needle in ("ABL1 IC50 snapshot", "CHEMBL1862", "gene ABL1", "UniProt P00519"):
        assert needle in card, needle
    for egfr in ("EGFR", "CHEMBL203", "P00533"):
        assert egfr not in card, egfr


@pytest.mark.golden
def test_both_sources_contribute_and_every_row_is_abl1(built):
    result, _ = built
    m = result.manifest
    assert m.rows_by_source == {"chembl": 10, "pubchem": 20}
    for reason in (
        "chembl_deposited_aid",
        "chembl_imported_aid",
        "depositor_not_cleared",
        "target_not_configured",
        "not_activity_type",
        "other_target",
    ):
        assert m.drops_by_reason.get(reason, 0) > 0, reason
    snap = read_snapshot(result.directory)
    assert {a.source for a in snap.activities} == {"chembl", "pubchem"}
    assert {a.target for a in snap.activities} == {"ABL1"}
    assert {c.target for c in snap.consensus} == {"ABL1"}
    pub = {a.depositor for a in snap.activities if a.source == "pubchem"}
    assert pub == {"BindingDB"}
    aids = {
        (a.source_record_id or "").split(":")[0] for a in snap.activities if a.source == "pubchem"
    }
    assert aids == {"1799577", "1806059"}  # 1796923's ABL1 rows are Ki, so all dropped


@pytest.mark.golden
def test_two_builds_give_identical_manifests(tmp_path):
    a = build(tmp_path / "one").manifest
    b = build(tmp_path / "two").manifest
    assert golden_view(a) == golden_view(b)
    assert a.raw_cache_sha256 == b.raw_cache_sha256


@pytest.mark.golden
def test_the_agent_database_opens_read_only_with_its_views_populated(built):
    from assay_pipeline.agent.connection import PermissionException, open_agent_db

    result, db = built
    assert db.snapshot == result.manifest.snapshot
    con = open_agent_db(db.path, load_views_config().limits)
    try:
        counts = {v: con.execute(f"SELECT count(*) FROM {v}").fetchone()[0] for v in VIEWS}
        assert all(n > 0 for n in counts.values()), counts
        sources = con.execute("SELECT DISTINCT source FROM v_activities ORDER BY 1").fetchall()
        assert sources == [("chembl",), ("pubchem",)]
        targets = con.execute("SELECT DISTINCT target FROM v_consensus").fetchall()
        assert targets == [("ABL1",)]
        quality = dict(
            con.execute(
                "SELECT key, count FROM v_quality WHERE metric = 'rows_by_source'"
            ).fetchall()
        )
        assert quality == {"chembl": 10, "pubchem": 20}
        with pytest.raises(PermissionException):
            con.execute("CREATE TABLE x (a INTEGER)")
    finally:
        con.close()
    assert (result.directory / MANIFEST).is_file()
