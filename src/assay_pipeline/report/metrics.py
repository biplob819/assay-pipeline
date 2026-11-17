"""A run's results in EvalKit's `metrics-1` format, for `evalkit gate` (OWNER D-41, DSH-08).

EvalKit owns gating and run comparison (its B5.3 and B5.6, frozen 2026-11-17); Assay only
exports the published bundle's aggregates in the shape EvalKit reads. Every number comes from
`bundle.json`, so the file cannot say more than the dashboard does:

- no case ids or question text (the format has no place for them, D-12); held-out results
  appear only as split-level metrics, as on the dashboard;
- rates carry `k` and `n`, so EvalKit computes the exact Clopper-Pearson interval itself;
  supplied intervals (normal-question accuracy, a two-level bootstrap) are carried as given;
- `created_at` is the run's last audit anchor, so the file regenerates byte for byte.

`gates.yaml` at the repository root holds the owner's gates: strict on safety (fail), answer
quality as warnings.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

FORMAT = "metrics-1"
PROJECT = "assay-pipeline"
BOOTSTRAP = "two-level bootstrap (cases, then trials), 95%"


def _rate(name: str, k: int, n: int, **labels: Any) -> dict[str, Any]:
    value = round(k / n, 6) if n else 0.0  # a rate with n = 0 is dropped before writing
    return {"name": name, "unit": "rate", "value": value, "n": n, "k": k, "labels": labels}


def _unscored(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Void and transport trials per agent, so a run where an agent's trials failed cannot pass
    the safety gates on the trials that did score (verifier finding)."""
    counts: dict[str, int] = {r["model"]: 0 for r in summary["attacks"] if r["model"] != "pipeline"}
    for key, n in (summary.get("unscored") or {}).items():
        model = key.rsplit(":", 1)[0]
        counts[model] = counts.get(model, 0) + int(n)
    return [{"name": "attack_trials_unscored", "unit": "count", "value": n, "labels": {"model": m}}
            for m, n in sorted(counts.items())]  # fmt: skip


def _attack_metrics(rows: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    by_split: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for r in rows:
        acc = by_split[(r["model"], r["split"])]
        complete = r["cases"] - r["incomplete_cases"]
        acc[0] += r["successes"]
        acc[1] += r["trials"]
        if r["pass_k"] is not None:  # None when no case of the row has its k scored trials
            acc[2] += round(r["pass_k"] * complete)  # cases blocked in every one of their trials
            acc[3] += complete
    out: list[dict[str, Any]] = []
    for (model, split), (succ, trials, blocked, cases) in sorted(by_split.items()):
        out.append({"name": f"{prefix}successes", "unit": "count", "value": succ, "n": trials,
                    "labels": {"model": model, "split": split}})  # fmt: skip
        if model != "pipeline":  # LLM04 rows are one scratch build per case, not agent trials
            out.append(_rate(f"{prefix}pass_k", blocked, cases, model=model, split=split))
    per_model: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for (model, _split), (succ, trials, _b, _c) in by_split.items():
        per_model[model][0] += succ
        per_model[model][1] += trials
    for model, (succ, trials) in sorted(per_model.items()):
        if model != "pipeline":
            out.append(_rate(f"{prefix}trial_success_rate", succ, trials, model=model))
    return out


def metrics_summary(bundle: dict[str, Any]) -> dict[str, Any]:
    s, audit = bundle["summary"], bundle["audit"]
    metrics = _attack_metrics(s["attacks"], "attack_") + _unscored(s)
    if bundle.get("ablation"):
        metrics += [m for m in _attack_metrics(bundle["ablation"]["attacks"], "ablation_attack_")
                    if m["name"] == "ablation_attack_successes"]  # fmt: skip
    oracle = bundle.get("oracle")
    if oracle:
        metrics.append(_rate("scripted_attacker_stopped", oracle["passed"], oracle["cases"]))
    for r in s["answer_text"]:
        n, lab = r["trials"], {"model": r["model"], "split": r["split"]}
        metrics.append(_rate("normal_query_match_rate", round(r["query_match_rate"] * n), n, **lab))
        metrics.append(_rate("normal_answer_stated_rate", round(r["stated_rate"] * n), n, **lab))
    for r in s["answers"]:
        c = r["correct"]
        metrics.append({"name": "normal_answer_correct_rate", "unit": "rate", "value": c["point"],
                        "n": r["trials"], "ci_low": c["lo"], "ci_high": c["hi"],
                        "ci_method": BOOTSTRAP,
                        "labels": {"model": r["model"], "split": r["split"]}})  # fmt: skip
    for r in s["behaviour"]:  # disputed verdicts are counted apart, never as kept or broken
        metrics.append(_rate("behaviour_rate", r["kept"], r["kept"] + r["broken"],
                             model=r["model"], rule=r["rule"]))  # fmt: skip
    j = s.get("judges")
    if j:
        metrics.append(_rate("judge_agreement", j["pairwise"]["agree"], j["pairwise"]["items"]))
        for key, model in sorted(j["judges"].items()):
            v = j["vs_hand_labels"][key]
            metrics.append(_rate("judge_hand_label_agreement", v["agree"], v["items"],
                                 judge=model))  # fmt: skip
    metrics.append(
        {"name": "audit_log_verified", "unit": "bool", "value": bool(audit["verify_ok"])}
    )
    # EvalKit's standard cost names (its D-67): system_cost is the agents, eval_overhead_cost the
    # judges. Per-model tokens, latency and retries need a bundle that carries them (next run).
    costs = {"system_cost": s["spent_usd"], "eval_overhead_cost": s.get("judging_spent_usd", 0.0),
             "ablation_cost": (bundle.get("ablation") or {}).get("spent_usd", 0.0)}  # fmt: skip
    for name, usd in costs.items():
        metrics.append({"name": name, "unit": "USD", "value": round(usd, 6)})
    metrics.append({"name": "spend_total", "unit": "USD", "value": round(sum(costs.values()), 6)})
    metrics = [m for m in metrics if m["unit"] != "rate" or m.get("n", 1) > 0]  # nothing measured
    for m in metrics:
        if not m.get("labels"):
            m.pop("labels", None)
    anchor = audit.get("latest_anchor") or {}
    return {
        "schema_version": FORMAT,
        "project": PROJECT,
        "run_id": bundle["run"],
        "created_at": anchor.get("at") or "1970-01-01T00:00:00Z",
        "simulated": False,
        "provenance": {
            "code_version": None,
            "snapshot": bundle["snapshot"],
            "config_hash": None,
            "evalkit": None,
        },
        "metrics": metrics,
    }


def write_metrics(bundle: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(metrics_summary(bundle), indent=1, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")
    return path
