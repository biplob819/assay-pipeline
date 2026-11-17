"""DSH-09: per-agent efficiency in the bundle and as EvalKit's D-67 metrics (cost per case,
cost per correct answer, error rate; tokens, cache share and latency once every scored trial
recorded them). Aggregates only. No model calls."""

from __future__ import annotations

import json
from pathlib import Path

from assay_pipeline.report.collect import efficiency
from assay_pipeline.report.metrics import metrics_summary
from tests.report.test_metrics import LATEST, METRICS, check_metrics_1


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _trial(case, model, trial, status="scored", cost=0.01, correct=None):
    return {"case_id": case, "model": model, "trial": trial, "status": status,
            "cost_usd": cost, "answer_correct": correct}  # fmt: skip


def _usage(case, model, trial, tin=1000, tout=100, cached=500, reasoning=None, ms=2000):
    return {"case_id": case, "model": model, "trial": trial,
            "usage": {"tokens_in": tin, "tokens_out": tout, "tokens_cached": cached,
                      "tokens_reasoning": reasoning, "turns": 2, "latency_ms": ms}}  # fmt: skip


def test_efficiency_counts_costs_errors_and_reruns_per_agent(tmp_path):
    _write(tmp_path / "trials.jsonl", [
        _trial("atk-a", "m", 1, status="transport", cost=0.002),
        _trial("atk-a", "m", 1, cost=0.01),  # the rerun replaces the transport line
        _trial("atk-b", "m", 1, status="void", cost=0.0),
        _trial("nq-1", "m", 1, cost=0.02, correct=True),
        _trial("nq-2", "m", 1, cost=0.04, correct=False),
        _trial("atk-c", "pipeline", 1, cost=0.0),
    ])  # fmt: skip
    (row,) = efficiency(tmp_path)
    assert row["model"] == "m" and (row["trials"], row["scored"], row["errors"]) == (4, 3, 1)
    assert row["cost_per_case"] == round(0.07 / 3, 6) and row["cost_per_correct"] == 0.06
    assert row["cost_usd"] == 0.072  # the retried transport attempt was paid for too
    assert "tokens_in_per_case" not in row  # no usage recorded (runs before DSH-09)
    assert "atk-" not in json.dumps(row) and "nq-" not in json.dumps(row)


def test_tokens_and_latency_appear_only_when_every_scored_trial_has_them(tmp_path):
    trials = [_trial(f"atk-{i}", "m", 1) for i in range(20)]
    _write(tmp_path / "trials.jsonl", trials)
    _write(
        tmp_path / "trials-trajectories.jsonl",
        [_usage(f"atk-{i}", "m", 1, ms=100 * (i + 1)) for i in range(19)],
    )  # one missing
    assert "latency_p50" not in efficiency(tmp_path)[0]
    _write(
        tmp_path / "trials-trajectories.jsonl",
        [_usage(f"atk-{i}", "m", 1, ms=100 * (i + 1)) for i in range(20)],
    )
    (row,) = efficiency(tmp_path)
    assert (row["tokens_in_per_case"], row["tokens_out_per_case"]) == (1000, 100)
    assert row["tokens_cached_per_case"] == 500 and row["cache_hit_share"] == 0.5
    assert row["tokens_reasoning_per_case"] is None  # a provider without a reasoning split
    assert (row["latency_p50"], row["latency_p95"], row["latency_p99"]) == (1000, 1900, 2000)
    doc = metrics_summary({**json.loads(LATEST.read_text(encoding="utf-8")),
                           "efficiency": [row]})  # fmt: skip
    check_metrics_1(doc)
    names = {m["name"] for m in doc["metrics"] if m.get("labels", {}).get("model") == "m"}
    assert {"tokens_in_per_case", "cache_hit_share", "latency_p95", "error_rate"} <= names
    assert "tokens_reasoning_per_case" not in names


def test_the_published_file_carries_cost_per_case_and_error_rate_per_agent():
    doc = json.loads(METRICS.read_text(encoding="utf-8"))
    by = {(m["name"], m.get("labels", {}).get("model")): m for m in doc["metrics"]}
    for model in ("claude-sonnet-5-5", "gpt-6.1-sol"):
        assert by[("cost_per_case", model)]["labels"] == {"model": model, "mode": "live"}
        assert by[("cost_per_case", model)]["n"] == 240
        assert (by[("error_rate", model)]["k"], by[("error_rate", model)]["n"]) == (0, 240)
        assert by[("cost_per_correct", model)]["value"] > by[("cost_per_case", model)]["value"]
    total = sum(
        by[("cost_per_case", m)]["value"] * 240 for m in ("claude-sonnet-5-5", "gpt-6.1-sol")
    )
    run_cost = next(m["value"] for m in doc["metrics"] if m["name"] == "system_cost")
    assert abs(total - run_cost) < 0.01  # the agents' cost is the run's cost
