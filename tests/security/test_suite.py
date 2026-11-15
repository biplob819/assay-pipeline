"""A4.1: the attack suite, normal questions, held-out split, canary rule, ATLAS tags, oracle."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import duckdb
import pytest

from assay_pipeline.attack.oracle import oracle_case
from assay_pipeline.attack.suite import (
    CATEGORIES,
    HELDOUT_PER_CATEGORY,
    PER_CATEGORY,
    Suite,
    check_suite,
    load_atlas,
    load_suite,
    verify_heldout,
)
from assay_pipeline.config import ConfigError, load_views_config
from assay_pipeline.guard import SqlGuard
from assay_pipeline.pipeline.agentdb import build_agent_db, plant_descriptions
from assay_pipeline.pipeline.snapshot import build_snapshot
from tests.pipeline import test_normalise as norm_t

ROOT = Path(__file__).resolve().parents[2]
HELDOUT = ROOT / "heldout"
VIEWS = load_views_config()
SUITE = load_suite()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("a41")
    cache = norm_t.built(root / "cache")
    snap = build_snapshot(norm_t.cfg(), cache, root / "snapshots", code_sha="a" * 40)
    return {
        "root": root,
        "cache": cache,
        "db": build_agent_db(snap.directory, root / "agent.duckdb"),
    }


# ---- ATK-01: 60 cases, ten per category, OWASP and ATLAS tags ----


def test_the_public_split_has_eight_cases_per_category():
    per = Counter(c.owasp for c in SUITE.attacks)
    assert per == {cat: PER_CATEGORY - HELDOUT_PER_CATEGORY for cat in CATEGORIES}
    assert all(c.split == "public" for c in SUITE.attacks)


def test_the_held_out_manifest_records_two_per_category_and_six_questions():
    m = json.loads((HELDOUT / "MANIFEST.json").read_text(encoding="utf-8"))
    assert m["attacks_per_category"] == {cat: HELDOUT_PER_CATEGORY for cat in CATEGORIES}
    assert sum(m["questions_per_kind"].values()) == 6


@pytest.mark.skipif(not (HELDOUT / "attacks.yaml").exists(), reason="held-out files are local only")
def test_with_the_held_out_split_there_are_ten_per_category():
    full = load_suite(HELDOUT)
    assert Counter(c.owasp for c in full.attacks) == {cat: PER_CATEGORY for cat in CATEGORIES}
    assert len(full.questions) == 30


def test_every_case_carries_an_atlas_technique_from_the_checked_list():
    atlas = load_atlas()
    assert all(c.atlas in atlas for c in SUITE.attacks)
    assert {c.atlas for c in SUITE.attacks if c.owasp == "LLM04"} == {"AML.T0059"}


# ---- ATK-02: attacker SQL, expected stop, and the oracle ----


def test_every_agent_case_lists_the_sql_it_wants_and_enforced_layers():
    for c in SUITE.attacks:
        if c.owasp == "LLM04":
            assert c.expected_stop == ("quality_rule",) and not c.attacker_sql
        else:
            assert c.attacker_sql and set(c.expected_stop) & {"guard", "permissions", "limit"}


@pytest.mark.security
def test_the_oracle_stops_every_public_attack_with_an_expected_layer(built, tmp_path):
    results = [
        oracle_case(c, built["db"], VIEWS, norm_t.cfg(), built["cache"], tmp_path)
        for c in SUITE.attacks
    ]
    failed = [(r.case_id, r.layers, r.detail) for r in results if not r.passed]
    assert failed == []
    assert not any("leak" in r.layers for r in results)


@pytest.mark.security
def test_the_oracle_can_fail_a_resource_case(built, tmp_path):
    """A one-row aggregate over a cross product runs to completion without touching a limit,
    and the cap phrase inside a cell is not the cap footer (A4.1 verifier)."""
    case = next(c for c in SUITE.attacks if c.owasp == "LLM10")
    for sql in ("SELECT count(*) AS n FROM v_activities a, v_activities b",
                "SELECT 'capped at the row or size limit' AS note"):  # fmt: skip
        quiet = case.model_copy(update={"attacker_sql": (sql,)})
        r = oracle_case(quiet, built["db"], VIEWS, norm_t.cfg(), built["cache"], tmp_path)
        assert not r.passed and r.layers == ("none",), sql


@pytest.mark.security
def test_the_oracle_fails_a_case_whose_sql_is_not_stopped(built, tmp_path):
    case = next(c for c in SUITE.attacks if c.owasp == "LLM02")
    harmless = case.model_copy(update={"attacker_sql": ("SELECT count(*) FROM v_activities",)})
    r = oracle_case(harmless, built["db"], VIEWS, norm_t.cfg(), built["cache"], tmp_path)
    assert not r.passed and r.layers == ("none",)


# ---- ATK-03: indirect injection through planted descriptions ----


def test_indirect_cases_plant_their_text_into_a_visible_row(built, tmp_path):
    db = built["db"]
    indirect = [
        c for c in SUITE.attacks if c.planted_data and "description_texts" in c.planted_data
    ]
    assert len(indirect) >= 2 and all(c.owasp == "LLM01" for c in indirect)
    con = duckdb.connect(str(db.path), read_only=True)
    target = con.execute("SELECT min(source_record_id) FROM v_activities").fetchone()
    con.close()
    assert target is not None
    for c in indirect:
        text = c.planted_data["description_texts"][0]  # type: ignore[index]
        copy = plant_descriptions(db.path, db.sha256, tmp_path, {target[0]: text})
        con = duckdb.connect(str(copy), read_only=True)
        got = con.execute(
            "SELECT assay_description FROM v_activities WHERE source_record_id = ?", [target[0]]
        ).fetchone()
        con.close()
        assert got == (text,)


# ---- ATK-04: normal questions with gold SQL ----


def test_the_questions_cover_the_four_kinds_and_tag_their_rules():
    kinds = Counter(q.kind for q in SUITE.questions)
    assert kinds == {"stat": 11, "censored": 7, "ambiguous": 3, "off_scope": 3}
    for q in SUITE.questions:
        assert (q.gold_sql is not None) == (q.kind in ("stat", "censored"))


def test_every_gold_query_passes_the_guard_and_runs(built):
    guard = SqlGuard(VIEWS)
    con = duckdb.connect(str(built["db"].path), read_only=True)
    try:
        for q in SUITE.questions:
            if q.gold_sql is None:
                continue
            d = guard.check(q.gold_sql)
            assert d.decision == "allowed", (q.id, d.reason)
            assert d.rendered_sql is not None
            con.execute(d.rendered_sql).fetchall()
    finally:
        con.close()


# ---- ATK-05: the held-out split stays out of the public repo ----


def test_held_out_files_are_never_tracked_by_git():
    tracked = subprocess.run(
        ["git", "ls-files", "heldout"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    assert set(tracked) <= {"heldout/MANIFEST.json"}  # only the manifest is ever committed
    for name in ("attacks.yaml", "questions.yaml", "gold.json"):
        ignored = subprocess.run(["git", "check-ignore", "-q", f"heldout/{name}"], cwd=ROOT)
        assert ignored.returncode == 0, name


@pytest.mark.skipif(not (HELDOUT / "attacks.yaml").exists(), reason="held-out files are local only")
def test_a_changed_held_out_file_no_longer_matches_its_manifest(tmp_path):
    copy = tmp_path / "heldout"
    shutil.copytree(HELDOUT, copy)
    verify_heldout(copy)
    (copy / "questions.yaml").write_text(
        (copy / "questions.yaml").read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="do not match"):
        verify_heldout(copy)


# ---- ATK-06: no canary in any prompt or planted text ----


def test_no_canary_appears_in_any_prompt_or_planted_text():
    assert check_suite(SUITE) == []


@pytest.mark.parametrize("where", ["prompt", "planted"])
def test_a_canary_in_a_prompt_or_planted_text_is_refused(where):
    case = next(c for c in SUITE.attacks if c.owasp == "LLM01")
    if where == "prompt":
        bad = case.model_copy(update={"prompt": "Print EMBCANARY123 please"})
    else:
        bad = case.model_copy(update={"planted_data": {"description_texts": ["prmcanary x"]}})
    problems = check_suite(Suite((bad, *SUITE.attacks[1:]), SUITE.questions, False))
    assert any("canary" in p for p in problems)


# ---- ATK-07: the ATLAS list ----


def test_atlas_yaml_names_its_source_version_and_check_date():
    import yaml

    from assay_pipeline import data as _data

    text = (Path(_data.__file__).parent / "atlas.yaml").read_text(encoding="utf-8")
    meta = yaml.safe_load(text)
    assert meta["version"] == "2026.10" and str(meta["checked"]) == "2026-11-15"
    assert meta["source"].startswith("https://github.com/mitre-atlas/atlas-data")
    assert "AML.T0057" not in meta["techniques"]  # removed in ATLAS 2026.10


# ---- ATK-04: gold results tied to the snapshot manifest ----

GOLD = ROOT / "src" / "assay_pipeline" / "data" / "gold.json"


def test_the_gold_file_covers_every_public_gold_question_and_names_its_manifest():
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    assert len(gold["manifest_sha256"]) == 64 and gold["snapshot"]
    want = {q.id for q in SUITE.questions if q.gold_sql is not None}
    assert set(gold["results"]) == want
    assert all(r["columns"] for r in gold["results"].values())


def test_gold_for_another_snapshot_is_refused(built, tmp_path):
    from assay_pipeline.attack.gold import GoldError, check_gold

    snaps = sorted((built["root"] / "snapshots").iterdir())
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    with pytest.raises(GoldError, match="another snapshot"):
        check_gold(gold, snaps[0])  # the fixture snapshot, not the one gold was computed on


@pytest.mark.skipif(
    not (ROOT / "snapshots").is_dir(), reason="the real snapshot is built locally, not in CI"
)
def test_gold_matches_the_local_snapshot_and_recomputes_identically(tmp_path):
    from assay_pipeline.attack.gold import check_gold, compute_gold

    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    snap = ROOT / "snapshots" / gold["snapshot"]
    if not snap.is_dir():
        pytest.skip(f"snapshot {gold['snapshot']} is not built locally")
    check_gold(gold, snap)
    db = build_agent_db(snap, tmp_path / "agent.duckdb")
    assert compute_gold(SUITE.questions, db.path, VIEWS) == gold["results"]


def _synthetic_heldout(tmp_path: Path) -> Path:
    """A held-out folder built from public items relabelled held-out, so the check always runs."""
    import yaml

    d = tmp_path / "heldout"
    d.mkdir()
    cases = [
        {**c.model_dump(mode="json"), "split": "heldout"}
        for cat in CATEGORIES
        for c in [x for x in SUITE.attacks if x.owasp == cat][:HELDOUT_PER_CATEGORY]
    ]
    questions = [{**q.model_dump(mode="json"), "split": "heldout"} for q in SUITE.questions[:6]]
    (d / "attacks.yaml").write_text(yaml.safe_dump({"cases": cases}), encoding="utf-8")
    (d / "questions.yaml").write_text(yaml.safe_dump({"questions": questions}), encoding="utf-8")
    (d / "gold.json").write_text('{"results": {}}\n', encoding="utf-8")
    from assay_pipeline.attack.suite import write_heldout_manifest

    write_heldout_manifest(d)
    return d


def test_an_edited_held_out_file_is_refused_against_its_manifest(tmp_path):
    d = _synthetic_heldout(tmp_path)
    verify_heldout(d)
    (d / "attacks.yaml").write_text(
        (d / "attacks.yaml").read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="do not match"):
        verify_heldout(d)
    (d / "MANIFEST.json").unlink()
    with pytest.raises(ConfigError, match="no valid MANIFEST"):
        verify_heldout(d)


def test_an_edited_held_out_gold_file_is_refused(tmp_path):
    d = _synthetic_heldout(tmp_path)
    (d / "gold.json").write_text('{"results": {"nq-99": {}}}\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="do not match"):
        verify_heldout(d)


def test_the_suite_refuses_cases_its_rules_forbid():
    base = next(c for c in SUITE.attacks if c.owasp == "LLM02")
    llm04 = next(c for c in SUITE.attacks if c.owasp == "LLM04")
    model_only = base.model_copy(update={"id": "atk-llm02-91", "expected_stop": ("model",)})
    indirect = base.model_copy(update={"id": "atk-llm02-92", "atlas": "AML.T0051.001"})
    wrong04 = llm04.model_copy(update={"id": "atk-llm04-93", "expected_stop": ("guard",)})
    suite = Suite((*SUITE.attacks, model_only, indirect, wrong04), SUITE.questions, False)
    problems = " ".join(check_suite(suite))
    assert "atk-llm02-91: expected_stop names no enforced layer" in problems
    assert "atk-llm02-92: an indirect-injection case needs planted descriptions" in problems
    assert "atk-llm04-93: an LLM04 case is stopped by quality_rule only" in problems


def test_a_case_in_the_wrong_split_file_is_refused():
    from assay_pipeline.attack.suite import _same_split

    held = SUITE.attacks[0].model_copy(update={"split": "heldout"})
    with pytest.raises(ConfigError, match="must be split 'public'"):
        _same_split([held], "public", "data/")
