"""ATK-08: held-out suite versions, pinned manifests and retired ids (D-12, D-45)."""

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
    with pytest.raises(ConfigError, match="not the manifest pinned for suite 2"):
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


def test_a_retired_id_is_refused_from_the_suite_that_retired_it():
    """nq-26 and nq-30 were retired in suite 2: new content never reuses their ids, in either
    split, while an archived suite-1 folder still loads with its own (main-2026-11-16 re-judging);
    the archive exempts only held-out items, never a public one."""
    assert RETIRED == {"nq-26": 2, "nq-30": 2}
    base = next(q for q in SUITE.questions if q.kind == "off_scope")
    for rid in RETIRED:
        for split in ("public", "heldout"):
            reused = base.model_copy(update={"id": rid, "split": split})
            qs = (*SUITE.questions, reused)
            for version in (None, 2):
                problems = check_suite(Suite(SUITE.attacks, qs, False, version))
                assert any(p.startswith(f"{rid}: retired in suite 2") for p in problems)
            old = check_suite(Suite(SUITE.attacks, qs, False, 1))  # an archived suite 1
            assert any("retired" in p for p in old) == (split == "public")
    assert not any("retired" in p for p in check_suite(SUITE))


@pytest.mark.skipif(not (HELDOUT / "questions.yaml").exists(), reason="held-out files are local")
def test_the_local_held_out_folder_loads_pinned_and_the_suite_1_archive_too():
    """Local check: the real held-out folder is the pinned suite 2, and the archived suite 1
    (what main-2026-11-16 ran) still loads pinned, so that run can be re-judged."""
    assert load_suite(HELDOUT, pinned=True).heldout_version == SUITE_VERSION
    archive = HELDOUT / "archive" / "suite-1"
    if archive.is_dir():
        assert load_suite(archive, pinned=True).heldout_version == 1


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
