"""DSH-10 (OWNER D-41): the release gate and run-vs-run compare through EvalKit 0.3's public
API (`evalkit.gates.run_gate`, `evalkit.compare`). EvalKit computes every verdict; these tests
check that Assay's published file passes its own gates, that a bad run fails them, and that the
comparison reaches the bundle and the dashboard without paths or case ids. No model calls."""

from __future__ import annotations

import json
import re
from pathlib import Path

from evalkit.gates import run_gate

from assay_pipeline.report.compare import compare_metrics
from assay_pipeline.report.html import render
from tests.report.test_metrics import LATEST, METRICS, PINNED, PINNED_METRICS

ROOT = Path(__file__).resolve().parents[2]
GATES = ROOT / "gates.yaml"


def _variant(tmp_path: Path, name: str, change) -> Path:
    doc = json.loads(PINNED_METRICS.read_text(encoding="utf-8"))
    doc["run_id"] = name
    for m in doc["metrics"]:
        change(m)
    out = tmp_path / f"{name}.json"
    out.write_text(json.dumps(doc), encoding="utf-8")
    return out


def test_the_published_run_passes_every_gate():
    report = run_gate(METRICS, GATES)
    assert report.exit_code == 0 and report.verdict == "pass" and not report.errors


def test_the_published_comparison_completed_and_names_an_earlier_run():
    bundle = json.loads(LATEST.read_text(encoding="utf-8"))
    c = bundle.get("compare")
    if len(list((ROOT / "docs/results").glob("*/metrics.json"))) == 1:
        assert c is None  # the first published run has nothing to compare with
        return
    assert c is not None, "a second published run must carry its comparison"
    assert not c["errors"] and c["exit_code"] in (0, 1)
    prev = c["previous_run"]
    assert prev != bundle["run"] and (ROOT / "docs/results" / prev / "metrics.json").exists()


def test_one_attack_success_fails_the_release_gate(tmp_path):
    def one_success(m):
        if m["name"] == "attack_trial_success_rate" and m["labels"] == {"model": "gpt-6.1-sol"}:
            m["k"], m["value"] = 1, round(1 / 150, 6)
        if m["name"] == "attack_successes" and m["labels"] == {"model": "gpt-6.1-sol",
                                                               "split": "public"}:  # fmt: skip
            m["value"] = 1

    report = run_gate(_variant(tmp_path, "bad", one_success), GATES)
    assert report.exit_code == 1


def test_a_quality_drop_only_warns(tmp_path):
    def worse_b1(m):
        if m["name"] == "behaviour_rate" and m["labels"] == {"model": "gpt-6.1-sol", "rule": "B1"}:
            m["k"], m["value"] = 30, round(30 / 56, 6)

    report = run_gate(_variant(tmp_path, "meh", worse_b1), GATES)
    warned = [r for r in report.rows if r.result == "warn"]
    assert report.exit_code == 0 and report.verdict == "pass"  # warnings never fail a release
    assert [(r.metric, r.labels) for r in warned] == [
        ("behaviour_rate", {"model": "gpt-6.1-sol", "rule": "B1"})
    ]


def test_the_comparison_keeps_changed_rows_only_without_paths_or_ids(tmp_path):
    def worse_b1(m):
        if m["name"] == "behaviour_rate" and m["labels"] == {"model": "gpt-6.1-sol", "rule": "B1"}:
            m["k"], m["value"] = 30, round(30 / 56, 6)

    new = _variant(tmp_path, "next-run", worse_b1)
    c = compare_metrics(PINNED_METRICS, new, GATES)
    assert c == compare_metrics(PINNED_METRICS, new, GATES)  # deterministic: seeded
    assert c["previous_run"] == "main-2026-11-16" and c["rows"] > 50
    (row,) = c["changed"]
    assert row["metric"] == "behaviour_rate" and row["diff"] < 0 and row["gate"] == "warn"
    assert row["pairing"] == "unpaired" and row["ci_high"] < 0
    # an interval that excludes 0 is not a flag by itself: Holm runs across every compared row
    assert 0 <= row["p_holm"] <= 1 and row["flagged"] == (row["p_holm"] < 0.05)
    text = json.dumps(c)
    assert str(tmp_path) not in text and not re.search(r"\b(atk|nq)-", text)


def test_the_dashboard_shows_the_comparison_only_when_there_is_one(tmp_path):
    bundle = json.loads(PINNED.read_text(encoding="utf-8"))
    assert "Compared with the last run" not in render(bundle)  # the first run's page is unchanged
    new = _variant(tmp_path, "next-run", lambda m: None)
    bundle["compare"] = compare_metrics(PINNED_METRICS, new, GATES)
    page = render(bundle)
    assert "Compared with the last run" in page and "No metric changed." in page
    assert "main-2026-11-16" in page
    assert "Holm p" in page and "is Holm-adjusted" not in page  # the shown interval is unadjusted


def test_an_older_baseline_is_compared_without_gates_and_says_why(tmp_path):
    """Verifier finding: a previous run from before a gated metric existed made the whole
    comparison an unexplained "error"; it now compares without gates and says so."""
    old = _variant(tmp_path, "older", lambda m: None)
    doc = json.loads(old.read_text(encoding="utf-8"))
    doc["metrics"] = [m for m in doc["metrics"] if m["name"] != "attack_trials_unscored"]
    old.write_text(json.dumps(doc), encoding="utf-8")
    c = compare_metrics(old, PINNED_METRICS, GATES)
    assert c["exit_code"] != 2 and c["gates"].startswith("not applied: ")
    assert "attack_trials_unscored" in c["gates"] and c["missing"] >= 1
    assert str(tmp_path) not in json.dumps(c)


def test_an_unreadable_baseline_is_reported_on_the_page_not_raised(tmp_path):
    bad = tmp_path / "prev-run" / "metrics.json"
    bad.parent.mkdir()
    bad.write_text("{not json", encoding="utf-8")
    c = compare_metrics(bad, METRICS, GATES)
    assert c["exit_code"] == 2 and c["errors"] and c["previous_run"] == "prev-run"
    assert str(tmp_path) not in json.dumps(c)
    bundle = json.loads(LATEST.read_text(encoding="utf-8"))
    page = render({**bundle, "compare": c})
    assert "could not be completed" in page and "not valid JSON" in page
    assert "This is not the release gate" in " ".join(page.split())


def test_each_bound_gate_matches_one_pooled_row_per_agent_in_every_published_run(tmp_path):
    """DSH-11 (D-44, EvalKit 0.4): EvalKit's security kit writes a per-split row and a pooled
    `split: all` row, so a bound gate on its output must name `split: all` or a small held-out
    split alone (0 of 30) fails it. Assay's own export has exactly one pooled row per agent with
    no `split` label, so its bound gates name the model only; naming `split: all` would match
    nothing (exit 2). If the export ever gains split labels, this test says so."""
    import yaml

    gates = yaml.safe_load(GATES.read_text(encoding="utf-8"))["gates"]
    bound = [g for g in gates if g.get("bound")]
    assert {g["labels"]["model"] for g in bound} == {"claude-sonnet-5-5", "gpt-6.1-sol"}
    for path in sorted((ROOT / "docs/results").glob("*/metrics.json")):
        report = run_gate(path, GATES)
        assert report.exit_code == 0, path.parent.name
        for i, g in enumerate(gates, 1):
            if not g.get("bound"):
                continue
            rows = [r for r in report.rows if r.gate == i]
            assert len(rows) == 1 and "split" not in rows[0].labels, (path.parent.name, i)
        doc = json.loads(path.read_text(encoding="utf-8"))
        pooled = [m for m in doc["metrics"] if m["name"] == "attack_trial_success_rate"]
        assert pooled and all("split" not in m["labels"] for m in pooled)
    with_split = [{**g, "labels": {**g["labels"], "split": "all"}} if g.get("bound") else g
                  for g in gates]  # fmt: skip
    alt = tmp_path / "gates-split-all.yaml"
    alt.write_text(yaml.safe_dump({"gates": with_split, "heldout_splits": ["heldout"]}),
                   encoding="utf-8")  # fmt: skip
    assert run_gate(METRICS, alt).exit_code == 2  # a gate that matches nothing is an input error
