"""Gather a finished run into a public results bundle (B5.1, REL-03).

The bundle is what the dashboard is rendered from and what the repo publishes: plain JSON with
no held-out text. Held-out results appear only as aggregates (the summaries already group them);
the audit tail keeps public cases only, because a held-out prompt or query must never be
published (D-12). Raw trajectories, judge reasons and behaviour rows stay in `runs/`.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from assay_pipeline.audit import verify
from assay_pipeline.pipeline.snapshot import sha256_file

AUDIT_TAIL = 30  # recent public audit lines shown on the dashboard


class ReportError(RuntimeError):
    """The run or snapshot is incomplete for a report."""


def _lines(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _pct(values: list[int], p: float) -> int:
    """Nearest-rank percentile of a non-empty list."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def efficiency(run_dir: Path) -> list[dict[str, Any]]:
    """Per-agent cost and, when every scored trial recorded it, token use and latency (DSH-09,
    EvalKit D-67). Aggregates only: no case id leaves this function. A rerun's later line for
    the same trial replaces the earlier one, as the resume does."""
    trials: dict[tuple[str, str, int], dict[str, Any]] = {}
    spent: dict[str, float] = {}  # every attempt, reruns included, as the run's spend counts it
    for r in _lines(run_dir / "trials.jsonl"):
        trials[(r["case_id"], r["model"], r["trial"])] = r
        spent[r["model"]] = spent.get(r["model"], 0.0) + r["cost_usd"]
    usage = {(r["case_id"], r["model"], r["trial"]): r["usage"]
             for r in _lines(run_dir / "trials-trajectories.jsonl") if r.get("usage")}  # fmt: skip
    out = []
    for model in sorted({k[1] for k in trials} - {"pipeline"}):
        rows = {k: r for k, r in trials.items() if k[1] == model}
        scored = {k: r for k, r in rows.items() if r["status"] == "scored"}
        graded = [r for r in scored.values() if r.get("answer_correct") is not None]
        correct = sum(1 for r in graded if r["answer_correct"])
        graded_cost = sum(r["cost_usd"] for r in graded)
        row: dict[str, Any] = {
            "model": model, "trials": len(rows), "scored": len(scored),
            "errors": sum(1 for r in rows.values() if r["status"] in ("transport", "void")),
            "cost_usd": round(spent[model], 6),  # all attempts; per-case means use the last
            "cost_per_case": round(sum(r["cost_usd"] for r in scored.values()) / len(scored), 6)
            if scored else None,
            "graded": len(graded), "correct": correct,
            "cost_per_correct": round(graded_cost / correct, 6) if correct else None,
        }  # fmt: skip
        used = [usage[k] for k in scored if k in usage]
        if scored and len(used) == len(scored):  # partial coverage would bias the means
            n = len(used)
            for key in ("tokens_in", "tokens_out", "tokens_cached", "tokens_reasoning"):
                vals = [u.get(key) for u in used]
                row[f"{key}_per_case"] = (None if any(v is None for v in vals)
                                          else round(sum(vals) / n))  # type: ignore[arg-type]  # fmt: skip
            cached = [u.get("tokens_cached") for u in used]
            total_in = sum(u["tokens_in"] for u in used)
            row["cache_hit_share"] = (None if any(v is None for v in cached) or not total_in
                                      else round(sum(cached) / total_in, 6))  # type: ignore[arg-type]  # fmt: skip
            lat = [u["latency_ms"] for u in used]
            row.update(latency_p50=_pct(lat, 50), latency_p95=_pct(lat, 95),
                       latency_p99=_pct(lat, 99))  # fmt: skip
        out.append(row)
    return out


def _json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReportError(f"{path}: {exc}") from exc


def _public_ids() -> set[str]:
    from assay_pipeline.attack.suite import load_suite

    s = load_suite()  # the public split only
    return {c.id for c in s.attacks} | {q.id for q in s.questions}


def _public_oracle(raw: dict[str, Any], public: set[str]) -> dict[str, Any]:
    """Per-case oracle results for public cases; held-out cases only as counts per category
    and stopping layer, so no held-out id is published."""
    held: dict[str, dict[str, int]] = {}
    for c in raw.get("results", []):
        if c["case_id"] not in public:
            first = c["layers"][0] if c["layers"] else "none"
            held.setdefault(c["owasp"], {}).setdefault(first, 0)
            held[c["owasp"]][first] += 1
    return {k: raw[k] for k in ("snapshot", "manifest_sha256", "cases", "passed") if k in raw} | {
        "results": [c for c in raw.get("results", []) if c["case_id"] in public],
        "heldout_by_owasp": held,
    }


def collect(
    run_dir: Path, snapshots_root: Path, snapshot: str, oracle: Path | None = None
) -> dict[str, Any]:
    summary = _json(run_dir / "summary.json")
    if summary.get("trials_sha256") != sha256_file(run_dir / "trials.jsonl"):
        raise ReportError(f"{run_dir}: the run has not finished; rerun `assay attack` first")
    if "behaviour" not in summary:
        raise ReportError(f"{run_dir}: no behaviour section; run `assay behaviour` first")
    ablation = run_dir / "ablation-summary.json"
    audit = run_dir / "audit.jsonl"
    check = verify(audit, audit.with_suffix(".anchors.jsonl"))
    anchors = [json.loads(x) for x in audit.with_suffix(".anchors.jsonl").read_text(
        encoding="utf-8").splitlines() if x.strip()]  # fmt: skip
    public = _public_ids()
    lines = [json.loads(x) for x in audit.read_text(encoding="utf-8").splitlines() if x.strip()]
    keys = ("seq", "at", "case_id", "model", "sql", "guard_decision", "guard_reason",
            "rows_returned", "db_error_class")  # fmt: skip
    queries = [{k: x.get(k) for k in keys} for x in lines
               if x.get("event") == "query" and x.get("case_id") in public]  # fmt: skip
    decisions: dict[str, dict[str, int]] = {}
    for x in lines:  # counts cover every case, held-out included (no text)
        if x.get("event") == "query":
            key = x.get("guard_decision") or "?"
            if key == "blocked":
                key = f"blocked: {x.get('guard_reason')}"
            elif x.get("db_error_class"):
                key = f"allowed, database {x.get('db_error_class')}"
            kind = "attack" if str(x.get("case_id", "")).startswith("atk-") else "question"
            row = decisions.setdefault(key, {"attack": 0, "question": 0})
            row[kind] += 1
    tail = queries[-AUDIT_TAIL:]
    blocked = [q for q in queries if q["guard_decision"] == "blocked"][-AUDIT_TAIL // 2 :]
    history = []
    for m_path in sorted(snapshots_root.glob("*/manifest.json")):
        m = _json(m_path)
        history.append({"snapshot": m["snapshot"], "created_at": m.get("created_at"),
                        "rows_by_source": m.get("rows_by_source", {}),
                        "manifest_sha256": sha256_file(m_path)})  # fmt: skip
    manifest_path = snapshots_root / snapshot / "manifest.json"
    m = _json(manifest_path)
    return {
        "run": run_dir.name,
        "snapshot": snapshot,
        "manifest_sha256": sha256_file(manifest_path),
        "summary": {k: v for k, v in summary.items() if k != "trials_sha256"},
        "ablation": _json(ablation) if ablation.is_file() else None,
        "efficiency": efficiency(run_dir),
        "oracle": _public_oracle(_json(oracle), public) if oracle is not None else None,
        "audit": {
            "verify_ok": check.ok,
            "lines": len(lines),
            "anchors": len(anchors),
            "latest_anchor": anchors[-1] if anchors else None,
            "decisions": dict(sorted(decisions.items(), key=lambda kv: -sum(kv[1].values()))),
            "blocked": blocked,
            "tail": tail,
        },
        "quality": {
            "flags_by_rule": m.get("flags_by_rule", {}),
            "drops_by_reason": m.get("drops_by_reason", {}),
            "rows_by_source": m.get("rows_by_source", {}),
            "source_releases": m.get("source_releases", {}),
            "target": m.get("target"),
            "history": history,
        },
    }


def write_bundle(bundle: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "bundle.json"
    path.write_text(json.dumps(bundle, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path
