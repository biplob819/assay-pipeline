"""R6.1: the one-page data roadmap (RMP-01, RMP-02)."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from assay_pipeline.pipeline.planted import load_planted

ROOT = Path(__file__).resolve().parents[2]
MD = ROOT / "docs" / "data-roadmap.md"
PDF = ROOT / "docs" / "data-roadmap.pdf"
TEXT = MD.read_text(encoding="utf-8")


def _section(title: str) -> str:
    return TEXT.split(f"## {title}")[1].split("\n## ")[0]


def test_snapshot_to_features_to_split_with_leakage_rules():
    for title in ("What the snapshot holds", "From snapshot to training set",
                  "Leakage rules for the split", "What a scientist would add"):  # fmt: skip
        assert f"## {title}" in TEXT
    train = _section("From snapshot to training set")
    for step in ("Target:", "Filters first, then labels:", "Duplicates:", "Features:"):
        assert step in train
    leak = _section("Leakage rules for the split")
    for rule in ("scaffold", "InChIKey", "time split", "cross-source duplicates", "frozen"):
        assert rule in leak or rule in train
    assert "censored" in train.lower() and "never as point values" in train


def test_the_roadmap_names_what_would_catch_stealth_poison():
    section = _section("What would catch stealth poison")
    n = len(load_planted().stealth)
    words = {1: "one", 2: "two", 3: "three", 4: "four"}
    assert f"The {words[n]} planted stealth rows" in section  # matches the planted fixtures
    for check in ("Cross-assay consistency", "Literature checks", "Scientist review"):
        assert check in section


FACTS = json.loads(
    (ROOT / "docs/measurements/snapshot-2026-11-15.1.json").read_text(encoding="utf-8")
)


def _n(x: int) -> str:
    return f"{x:,}"


def test_every_number_matches_the_snapshot_facts():
    f = FACTS
    rel = f["relations"]
    censored = sum(rel[k] for k in ("<", "<=", ">", ">="))
    expected = {
        f"{_n(sum(f['rows_by_source'].values()))} activity rows": "rows",
        f"ChEMBL_37 ({_n(f['rows_by_source']['chembl'])})": "chembl rows",
        f"PubChem ({_n(f['rows_by_source']['pubchem'])}": "pubchem rows",
        f"{_n(f['drops'])} records were dropped": "drops",
        f"QC1 unit not convertible ({_n(f['flags_by_rule']['QC1'])})": "QC1",
        f"QC2 duplicate disagreement ({_n(f['flags_by_rule']['QC2'])})": "QC2",
        f"QC3 out of range ({_n(f['flags_by_rule']['QC3'])})": "QC3",
        f"QC5 censored or unknown relation ({_n(f['flags_by_rule']['QC5'])})": "QC5",
        f"{_n(f['compounds'])} compounds": "compounds",
        f"{_n(f['consensus_keys'])} consensus keys": "keys",
        f"{_n(f['conflict_keys'])} in conflict": "conflicts",
        f">=: {_n(censored)})": "censored",
        f"Unknown relations ({_n(rel['unknown'])}": "unknown",
        f"{_n(f['unknown_by_source']['chembl'])} ChEMBL rows)": "unknown chembl",
        f"{_n(f['chembl_rows_from_bindingdb'])} ChEMBL rows come from BindingDB": "bindingdb",
        f"{_n(f['compounds_in_both_sources'])} compounds appear in both sources": "overlap",
        f"{_n(f['rows_with_document_id']['pubchem'])} PubChem rows": "document ids",
    }
    missing = [name for phrase, name in expected.items() if phrase not in TEXT]
    assert missing == [], missing
    assert f["snapshot"] in TEXT


@pytest.mark.skipif(not (ROOT / "snapshots/2026-11-15.1/manifest.json").exists(),
                    reason="the real snapshot is local only")  # fmt: skip
def test_the_facts_file_matches_the_real_snapshot():
    import hashlib

    manifest = ROOT / "snapshots/2026-11-15.1/manifest.json"
    m = json.loads(manifest.read_text(encoding="utf-8"))
    assert FACTS["manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert FACTS["rows_by_source"] == m["rows_by_source"]
    assert FACTS["flags_by_rule"] == m["flags_by_rule"]


def test_the_pdf_is_one_page_and_built_from_the_current_markdown():
    spec = importlib.util.spec_from_file_location("r", ROOT / "scripts" / "render_onepager.py")
    assert spec and spec.loader
    r = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(r)
    data = PDF.read_bytes()
    assert len(re.findall(rb"/Type\s*/Page\b", data)) == 1
    assert f"source-sha256:{r.source_sha256(TEXT)}".encode() in data
