"""ATK-08, ATK-09: held-out suite versions, pinned manifests and retired ids.

D-12, D-45 and OWNER D-47.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

import pytest
import yaml

from assay_pipeline.attack.suite import (
    CATEGORIES,
    HELDOUT_PER_CATEGORY,
    QUESTION_KINDS,
    RETIRED,
    SUITE_MANIFESTS,
    SUITE_VERSION,
    Suite,
    check_suite,
    load_suite,
    verify_heldout,
    write_heldout_manifest,
)
from assay_pipeline.config import ConfigError
from tests.security.test_suite import _synthetic_heldout

ROOT = Path(__file__).resolve().parents[2]
HELDOUT = ROOT / "heldout"
SUITE = load_suite()


def test_the_committed_manifest_is_pinned_for_its_suite_version():
    """The tracked manifest is the one pinned for the current version, so held-out files
    cannot change without a new suite version (the split runs once per version, D-12)."""
    raw = (HELDOUT / "MANIFEST.json").read_bytes()
    assert sorted(SUITE_MANIFESTS) == list(range(1, SUITE_VERSION + 1))
    assert hashlib.sha256(raw).hexdigest() == SUITE_MANIFESTS[SUITE_VERSION]
    m = json.loads(raw)
    assert m["suite_version"] == SUITE_VERSION
    assert m["attacks_per_category"] == {cat: HELDOUT_PER_CATEGORY for cat in CATEGORIES}
    public = Counter(q.kind for q in SUITE.questions)
    assert m["questions_per_kind"] == {k: n - public[k] for k, n in QUESTION_KINDS.items()}


def test_a_manifest_without_a_version_is_suite_1_and_an_unknown_version_is_refused(tmp_path):
    d = _synthetic_heldout(tmp_path)
    assert verify_heldout(d) == SUITE_VERSION
    m = json.loads((d / "MANIFEST.json").read_text(encoding="utf-8"))
    del m["suite_version"]  # suite 1's manifest predates the field; an archive still verifies
    (d / "MANIFEST.json").write_text(json.dumps(m), encoding="utf-8")
    assert verify_heldout(d) == 1
    for bad in (99, 0, 1, "2", 2.0, True, None):  # an explicit 1 is never written
        (d / "MANIFEST.json").write_text(json.dumps({**m, "suite_version": bad}), "utf-8")
        with pytest.raises(ConfigError, match="unknown suite version"):
            verify_heldout(d)
    (d / "MANIFEST.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ConfigError, match="no valid MANIFEST"):
        verify_heldout(d)


def test_the_runner_and_assessor_refuse_a_manifest_that_is_not_pinned(tmp_path, monkeypatch):
    """A held-out folder whose manifest verifies but is not the pinned one for its version (a
    locally rewritten manifest) is refused where a model would see held-out items."""
    from assay_pipeline.attack import assess as assess_mod
    from assay_pipeline.attack import run as run_mod

    d = _synthetic_heldout(tmp_path)
    assert verify_heldout(d) == SUITE_VERSION  # it verifies against its own manifest
    with pytest.raises(ConfigError, match=f"not the manifest pinned for suite {SUITE_VERSION}"):
        verify_heldout(d, pinned=True)
    with pytest.raises(ConfigError, match="not the manifest pinned"):
        load_suite(d, pinned=True)
    monkeypatch.setattr(run_mod, "load_agent_db", lambda _p: None)
    with pytest.raises(ConfigError, match="not the manifest pinned"):
        run_mod.build_plan(db_path=tmp_path / "x.duckdb", run_dir=tmp_path / "r", models=["claude"],
                           k=1, split="all", ablation=False, max_cost=None, cache_root=None,
                           heldout_dir=d)  # fmt: skip
    with pytest.raises(ConfigError, match="not the manifest pinned"):
        assess_mod.build_assessment(run_dir=tmp_path / "r", db_path=tmp_path / "x.duckdb",
                                    heldout_dir=d, labels=None, max_cost=None)  # fmt: skip


def test_rewriting_the_manifest_from_the_same_files_gives_the_same_bytes(tmp_path):
    d = _synthetic_heldout(tmp_path)
    first = (d / "MANIFEST.json").read_bytes()
    write_heldout_manifest(d)
    assert (d / "MANIFEST.json").read_bytes() == first
    assert json.loads(first)["suite_version"] == SUITE_VERSION
    for frozen in (1, SUITE_VERSION + 1):  # suite 1 is frozen; a new version is pinned first
        with pytest.raises(ConfigError, match="not a writable version"):
            write_heldout_manifest(d, frozen)
    assert (d / "MANIFEST.json").read_bytes() == first


def _retired_problems(rid: str, split: str, version: int | None) -> list[str]:
    """`check_suite` on the public suite plus one question reusing `rid` in `split`, loaded as
    held-out suite `version` (None: the current version)."""
    base = next(q for q in SUITE.questions if q.kind == "off_scope")
    reused = base.model_copy(update={"id": rid, "split": split})
    suite = Suite(SUITE.attacks, (*SUITE.questions, reused), False, version)
    return [p for p in check_suite(suite) if "retired" in p]


def test_a_retired_id_is_refused_from_the_suite_that_retired_it():
    """A retired id is never reused for new content, in either split, from the version that
    retired it on, while an archived older folder still loads with its own (re-judging a past
    run); the archive exempts only held-out items, never a public one."""
    assert RETIRED == {"nq-26": 2, "nq-30": 2, "nq-14": 3}
    for rid, retired_in in RETIRED.items():
        for split in ("public", "heldout"):
            for version in (None, *range(retired_in, SUITE_VERSION + 1)):
                problems = _retired_problems(rid, split, version)
                assert any(p.startswith(f"{rid}: retired in suite {retired_in}") for p in problems)
            for archived in range(1, retired_in):
                assert bool(_retired_problems(rid, split, archived)) == (split == "public")
    assert not any("retired" in p for p in check_suite(SUITE))


def test_nq_14_is_refused_from_suite_3_on():
    """Suite 3 retires nq-14 (its wording was close to a public question's, OWNER D-47): from
    suite 3 on it is refused in either split, while the archived suites 1 and 2, which held it,
    still load with it as held-out."""
    assert SUITE_VERSION == 3 and RETIRED["nq-14"] == 3
    for split in ("public", "heldout"):
        for version in (None, 3):
            assert _retired_problems("nq-14", split, version) == [
                "nq-14: retired in suite 3; new content needs a new id"
            ]
    for archived in (1, 2):
        assert _retired_problems("nq-14", "heldout", archived) == []
        assert _retired_problems("nq-14", "public", archived)  # never exempt in the public split
    assert "nq-14" not in {q.id for q in SUITE.questions}


def test_suite_3_is_pinned_and_the_earlier_pins_are_unchanged():
    """Suite 3's pin is the committed manifest's sha256 and names suite 3; the pins of suites 1
    (main-2026-11-16) and 2 (never ran) are kept as they were, so both archives stay checkable."""
    raw = (HELDOUT / "MANIFEST.json").read_bytes()
    assert {
        1: "2b475b09512a5d41bc4769c5238fb3a15c3f5d7caa9192e001ae9dcb568a8719",
        2: "e5d7b1eae570a31aefe48ab20a4e773bcb9726f568e9a71fa2c2c2eb9587bdbe",
        3: hashlib.sha256(raw).hexdigest(),
    } == SUITE_MANIFESTS
    assert json.loads(raw)["suite_version"] == 3
    assert json.loads(raw)["questions_per_kind"] == {
        "ambiguous": 1, "censored": 1, "off_scope": 1, "stat": 3,
    }  # fmt: skip


def test_an_archived_suite_2_folder_still_verifies_and_loads_its_retired_item(tmp_path):
    """A folder written as suite 2 (the archived suite-2 layout) verifies as suite 2, and its
    held-out nq-14 passes the retired-id check as suite 2 but not as suite 3; pinned, a suite-2
    manifest that is not suite 2's own is refused."""
    from assay_pipeline.schemas import NormalQuestion

    d = _synthetic_heldout(tmp_path)
    rows = yaml.safe_load((d / "questions.yaml").read_text(encoding="utf-8"))["questions"]
    next(r for r in rows if r["kind"] == "stat")["id"] = "nq-14"  # a held-out item suite 3 retired
    (d / "questions.yaml").write_text(yaml.safe_dump({"questions": rows}), encoding="utf-8")
    write_heldout_manifest(d, 2)
    version = verify_heldout(d)
    assert version == 2
    held = tuple(NormalQuestion.model_validate(r) for r in rows)
    for v, refused in ((version, False), (3, True), (None, True)):
        problems = check_suite(Suite(SUITE.attacks, (*SUITE.questions, *held), False, v))
        assert any(p.startswith("nq-14: retired in suite 3") for p in problems) == refused
    with pytest.raises(ConfigError, match="not the manifest pinned for suite 2"):
        verify_heldout(d, pinned=True)
    write_heldout_manifest(d)
    assert verify_heldout(d) == SUITE_VERSION


@pytest.mark.skipif(not (HELDOUT / "questions.yaml").exists(), reason="held-out files are local")
def test_the_local_held_out_folder_loads_pinned_and_both_archives_too():
    """Local check: the real held-out folder is the pinned suite 3 without nq-14; the archived
    suite 1 (what main-2026-11-16 ran, matching its cases digest) and suite 2 (never ran) still
    load pinned, so a past run can be re-judged and suite 2 stays checkable."""
    from assay_pipeline.attack.run import cases_digest

    current = load_suite(HELDOUT, pinned=True)
    assert current.heldout_version == SUITE_VERSION == 3
    assert not {q.id for q in current.questions} & set(RETIRED)
    archives = {v: HELDOUT / "archive" / f"suite-{v}" for v in (1, 2)}
    assert all(a.is_dir() for a in archives.values())
    for version, archive in archives.items():
        suite = load_suite(archive, pinned=True)
        assert suite.heldout_version == version
        assert "nq-14" in {q.id for q in suite.questions if q.split == "heldout"}
    run = ROOT / "runs" / "main-2026-11-16" / "run.json"
    if run.is_file():
        s1 = load_suite(archives[1], pinned=True)
        recorded = json.loads(run.read_text(encoding="utf-8"))["cases"]
        assert cases_digest([*s1.attacks, *s1.questions]) == recorded


@pytest.mark.skipif(not (HELDOUT / "questions.yaml").exists(), reason="held-out files are local")
def test_no_tracked_file_holds_a_current_held_out_id_or_text():
    """Local check with the real held-out files: no tracked file names a held-out id or holds a
    held-out question, prompt or attacker statement (only retired ids may appear)."""
    held_q = yaml.safe_load((HELDOUT / "questions.yaml").read_text(encoding="utf-8"))["questions"]
    held_a = yaml.safe_load((HELDOUT / "attacks.yaml").read_text(encoding="utf-8"))["cases"]
    ids = {r["id"] for r in [*held_q, *held_a]}
    assert ids and not ids & set(RETIRED)

    def norm(s: str) -> str:
        return " ".join(s.split()).lower()

    prose = {norm(r["question"]) for r in held_q} | {norm(r["prompt"]) for r in held_a}
    prose |= {norm(t) for r in held_a for t in (r.get("planted_data") or {}).get(
        "description_texts", [])}  # fmt: skip
    # a short attacker statement is often the plain restricted read every case wants (public
    # too); what is held out is the technique, so statements count from 32 characters
    sql = {norm(t) for r in held_a for t in r.get("attacker_sql") or []}
    texts = {t for t in prose if len(t) >= 20} | {t for t in sql if len(t) >= 32}
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, sorted(ids))) + r")\b")
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                             check=True).stdout.decode().split("\0")  # fmt: skip
    found = []
    for rel in filter(None, tracked):
        path = ROOT / rel
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="ignore")
        flat = norm(text)
        if pattern.search(text) or any(t in flat for t in texts):
            found.append(rel)
    assert found == []  # file names only, never the matching text
