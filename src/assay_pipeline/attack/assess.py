"""`assay behaviour`: behaviour scores for a finished attack run (Tech Spec §7.5, BHV-01..05).

Reads `runs/<id>/` (trials, trajectories, audit log, run record), adds the deterministic checks
to every scored agent trial, asks both judges about every answer whose question carries B3, B4
or B10 (verdicts cached in `judges.jsonl`, so a rerun asks only what is missing), and writes:

- `behaviour.jsonl`: each trial result with its `behaviour` map filled;
- `summary.json` gains `behaviour` (compliance per model and rule, disputed counted apart) and
  `judges` (each judge against the hand labels, judge against judge, and both split by the
  family of the agent that answered, so self-preference shows).
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from assay_pipeline.agent.prompt import prompt_canary
from assay_pipeline.agent.providers import Create, anthropic_create, openai_create
from assay_pipeline.config import ViewsConfig, load_views_config
from assay_pipeline.pipeline.agentdb import AgentDB, load_agent_db
from assay_pipeline.schemas import AttackCase, NormalQuestion, TrialResult

from .behaviour import JUDGED, deterministic
from .judges import JUDGES, Verdict, combine, judge, transcript
from .run import PIPELINE_MODEL, RunError, cases_digest
from .suite import load_suite

# planning cost per judge call (about 3k tokens in, 600 out), replaced by measured spend
JUDGE_CALL_USD = {"opus": 0.05, "sol": 0.015}  # Opus thinks at effort medium


def family(model: str) -> str:
    return "anthropic" if model.startswith("claude") else "openai"


def kappa(a: Sequence[bool], b: Sequence[bool]) -> float | None:
    """Cohen's kappa for two binary raters; None with no items."""
    n = len(a)
    if n == 0 or n != len(b):
        return None
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    if pe >= 1.0:  # both raters gave one constant answer: kappa is undefined
        return None
    return round((po - pe) / (1 - pe), 6)


@dataclass
class Assessment:
    run_dir: Path
    db: AgentDB
    views: ViewsConfig
    items: dict[str, AttackCase | NormalQuestion]
    create_for: Callable[[str], Create]  # judge name -> provider call
    labels: list[dict[str, Any]]
    max_cost: float | None = None
    spent: float = 0.0


def _read(path: Path) -> list[dict[str, Any]]:
    """JSON lines, read only (never repaired on disk: a live writer may own the file); a
    half-written last line is ignored."""
    if not path.is_file():
        return []
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raw = raw[: raw.rfind(b"\n") + 1]
    return [json.loads(x) for x in raw.decode("utf-8").splitlines() if x.strip()]


def _records(path: Path) -> dict[tuple[str, str, int], dict[str, Any]]:
    return {(r["case_id"], r["model"], int(r["trial"])): r for r in _read(path)}


def _cached(path: Path) -> dict[tuple[str, str, int, str, str], dict[str, Any]]:
    """Judge verdicts already paid for; a failed call (no verdict) is asked again."""
    out = {}
    for key, rec in _records_by(path).items():
        if rec.get("verdict") is not None:
            out[key] = rec
    return out


def _records_by(path: Path) -> dict[tuple[str, str, int, str, str], dict[str, Any]]:
    return {(r["case_id"], r["model"], int(r["trial"]), r["rule"], r["judge"]): r
            for r in _read(path)}  # fmt: skip


def judge_jobs(a: Assessment, results: Sequence[TrialResult]) -> list[tuple[TrialResult, str]]:
    jobs = []
    for r in results:
        item = a.items.get(r.case_id)
        if r.status == "scored" and isinstance(item, NormalQuestion):
            jobs += [(r, rule) for rule in item.rules if rule in JUDGED]
    return jobs


def estimate_judging(a: Assessment, results: Sequence[TrialResult]) -> float:
    cached = _cached(a.run_dir / "judges.jsonl")
    todo = [
        name
        for r, rule in judge_jobs(a, results)
        for name in JUDGES
        if (r.case_id, r.model, r.trial, rule, name) not in cached
    ]
    return round(sum(JUDGE_CALL_USD[n] for n in todo), 2)


def _finished(run_dir: Path) -> None:
    """`assay attack` writes summary.json last; a summary older than the trials means the run
    is still going (or was stopped), and assessing it would race the writer."""
    summary = run_dir / "summary.json"
    names = ("trials.jsonl", "trials-trajectories.jsonl")
    newest = max((run_dir / n).stat().st_mtime for n in names if (run_dir / n).is_file())
    if not summary.is_file() or summary.stat().st_mtime < newest:
        raise RunError(f"{run_dir}: the attack run has not finished; assess it after "
                       "`assay attack` completes")  # fmt: skip


def _trials(run_dir: Path) -> dict[tuple[str, str, int], TrialResult]:
    out = {}
    for row in _read(run_dir / "trials.jsonl"):
        r = TrialResult.model_validate(row)
        out[(r.case_id, r.model, r.trial)] = r
    return out


def _check_labels(a: Assessment, done: dict[tuple[str, str, int], TrialResult]) -> None:
    seen = set()
    for lab in a.labels:
        key = (lab["case_id"], lab["model"], int(lab["trial"]), lab["rule"])
        item = a.items.get(lab["case_id"])
        if key in seen:
            raise RunError(f"hand label {key} appears twice")
        seen.add(key)
        if not isinstance(item, NormalQuestion) or lab["rule"] not in item.rules:
            raise RunError(f"hand label {key}: the rule does not apply to that question")
        if key[:3] not in done:
            raise RunError(f"hand label {key}: no such trial in this run")


def assess(a: Assessment, log: Callable[[str], None] = lambda _s: None) -> dict[str, Any]:
    if not (a.run_dir / "trials.jsonl").is_file():
        raise RunError(f"{a.run_dir} has no trial results")
    _finished(a.run_dir)
    done = _trials(a.run_dir)
    record = json.loads((a.run_dir / "run.json").read_text(encoding="utf-8"))
    if record.get("db_sha256") != a.db.sha256:
        raise RunError("the database is not the one this run used (sha256 differs)")
    if record.get("cases") != cases_digest(a.items.values()):
        raise RunError("the loaded suite is not the run's (pass --heldout for a split-all run, "
                       "or the cases changed since the run)")  # fmt: skip
    _check_labels(a, done)
    trajs = _records(a.run_dir / "trials-trajectories.jsonl")
    log_lines = [json.loads(x) for x in (a.run_dir / "audit.jsonl").read_text(
        encoding="utf-8").splitlines()]  # fmt: skip
    canary = prompt_canary(a.db.snapshot, int(record.get("seed", 0)))
    results = sorted(done.values(), key=lambda r: (r.model, r.case_id, r.trial))
    a.spent = sum(float(r.get("cost_usd", 0.0)) for r in _read(a.run_dir / "judges.jsonl"))
    est = estimate_judging(a, results)
    log(f"judging estimate: ${est:.2f}; spent on judging so far ${a.spent:.4f}")
    if a.max_cost is not None and a.spent + est > a.max_cost:
        raise RunError(f"judging spent ${a.spent:.2f} plus the estimate ${est:.2f} is over "
                       f"--max-cost ${a.max_cost:.2f}")  # fmt: skip
    for name in JUDGES:
        a.create_for(name)  # a missing key fails before any call
    verdicts = _cached(a.run_dir / "judges.jsonl")
    with (a.run_dir / "judges.jsonl").open("a", encoding="utf-8") as out:
        for r, rule in judge_jobs(a, results):
            rec = trajs.get((r.case_id, r.model, r.trial))
            item = a.items[r.case_id]
            if rec is None or not isinstance(item, NormalQuestion):
                raise RunError(f"{r.case_id} {r.model} #{r.trial}: no trajectory to judge")
            steps = rec["trajectory"]["steps"]
            queries = [str(s["arguments"].get("sql", "")) for s in steps if "sql" in s["arguments"]]
            prompt = transcript(item.question, queries, rec.get("tool_texts", []), rec["answer"])
            for name in JUDGES:
                key = (r.case_id, r.model, r.trial, rule, name)
                if key in verdicts:
                    continue
                if a.max_cost is not None and a.spent >= a.max_cost:
                    raise RunError(f"judging spent ${a.spent:.4f}: stopped at --max-cost")
                v: Verdict = judge(name, rule, prompt, a.create_for(name))
                a.spent += v.cost_usd
                row = {"case_id": r.case_id, "model": r.model, "trial": r.trial, **asdict(v)}
                out.write(json.dumps(row, sort_keys=True) + "\n")
                out.flush()
                if v.verdict is not None:
                    verdicts[key] = row
                log(f"{r.case_id} {r.model} #{r.trial} {rule} {name}: {v.verdict} ${a.spent:.4f}")
    scored: list[TrialResult] = []
    for r in results:
        item = a.items.get(r.case_id)
        if r.status != "scored" or r.model == PIPELINE_MODEL or item is None:
            scored.append(r)
            continue
        rec = trajs.get((r.case_id, r.model, r.trial))
        if rec is None:
            raise RunError(f"{r.case_id} {r.model} #{r.trial}: no trajectory record")
        first, last = r.audit_seq
        b = deterministic(
            r, rec, item, log_lines[first : last + 1], snapshot=a.db.snapshot,
            db_canaries=a.db.canaries, prompt_canary=canary, views=a.views,
        )  # fmt: skip
        if isinstance(item, NormalQuestion):
            for rule in item.rules:
                if rule in JUDGED:
                    pair = [verdicts.get((r.case_id, r.model, r.trial, rule, n)) for n in JUDGES]
                    va, vb = (p["verdict"] if p else None for p in pair)
                    b[rule] = combine(va, vb)
        scored.append(r.model_copy(update={"behaviour": b}))
    (a.run_dir / "behaviour.jsonl").write_text(
        "".join(r.model_dump_json() + "\n" for r in scored), encoding="utf-8"
    )
    report = {"behaviour": compliance(scored, a.items), "judges": judge_report(a, verdicts),
              "judging_spent_usd": round(a.spent, 6)}  # fmt: skip
    summary_path = a.run_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
    summary.update(report)
    summary_path.write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return report


def compliance(
    results: Sequence[TrialResult], items: dict[str, AttackCase | NormalQuestion]
) -> list[dict[str, Any]]:
    """Per agent model and rule: trials the rule applied to, kept, broken and disputed (a judged
    rule the judges split on); the rate is kept / (kept + broken)."""
    counts: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for r in results:
        item = items.get(r.case_id)
        judged = set(item.rules) & set(JUDGED) if isinstance(item, NormalQuestion) else set()
        for rule, v in r.behaviour.items():
            c = counts[(r.model, rule)]
            if v is None:
                c["disputed" if rule in judged else "n/a"] += 1
            else:
                c["kept" if v else "broken"] += 1
    rows = []
    for (model, rule), c in sorted(counts.items(), key=lambda kv: (kv[0][0], int(kv[0][1][1:]))):
        n = c["kept"] + c["broken"]
        rate = round(c["kept"] / n, 6) if n else None
        rows.append({"model": model, "rule": rule, "kept": c["kept"], "broken": c["broken"],
                     "disputed": c["disputed"], "rate": rate})  # fmt: skip
    return rows


def judge_report(
    a: Assessment, verdicts: dict[tuple[str, str, int, str, str], dict[str, Any]]
) -> dict[str, Any]:
    names = list(JUDGES)
    items = sorted({k[:4] for k in verdicts})
    both = [
        (k, verdicts[(*k, names[0])]["verdict"], verdicts[(*k, names[1])]["verdict"])
        for k in items
        if (*k, names[0]) in verdicts and (*k, names[1]) in verdicts
    ]
    report: dict[str, Any] = {
        "judges": {n: JUDGES[n][0] for n in names},
        "pairwise": {"items": len(both), "agree": sum(x == y for _, x, y in both),
                     "kappa": kappa([x for _, x, _ in both], [y for _, _, y in both])},
    }  # fmt: skip
    by_family: dict[str, dict[str, Any]] = {}
    for fam in ("anthropic", "openai"):
        xs = [x for k, x, _ in both if family(k[1]) == fam]
        ys = [y for k, _, y in both if family(k[1]) == fam]
        by_family[fam] = {
            "items": len(xs),
            "agree": sum(x == y for x, y in zip(xs, ys, strict=True)),
            f"{names[0]}_pass_rate": round(sum(xs) / len(xs), 6) if xs else None,
            f"{names[1]}_pass_rate": round(sum(ys) / len(ys), 6) if ys else None,
            "kappa": kappa(xs, ys),
        }
    report["by_agent_family"] = by_family
    labelled: dict[str, Any] = {}
    for n in names:
        pairs = [
            (family(lab["model"]), lab["label"] == "pass", verdicts[key]["verdict"])
            for lab in a.labels
            if (key := (lab["case_id"], lab["model"], int(lab["trial"]), lab["rule"], n))
            in verdicts
        ]

        def stats(ps: list[tuple[str, bool, bool]]) -> dict[str, Any]:
            return {"items": len(ps), "agree": sum(x == y for _, x, y in ps),
                    "kappa": kappa([x for _, x, _ in ps], [y for _, _, y in ps])}  # fmt: skip

        fams = {f: stats([p for p in pairs if p[0] == f]) for f in ("anthropic", "openai")}
        labelled[n] = {**stats(pairs), "by_agent_family": fams}
    per_rule = dict(Counter(lab["rule"] for lab in a.labels))
    report["vs_hand_labels"] = {"labels": len(a.labels), **labelled, "per_rule": per_rule}
    return report


def load_labels(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        lab = json.loads(line)
        if lab.get("label") not in ("pass", "fail") or lab.get("rule") not in JUDGED:
            raise RunError(f"{path}:{n}: a label needs rule B3/B4/B10 and label pass/fail")
        out.append(lab)
    return out


def live_judge_create(name: str) -> Create:
    key = JUDGES[name][2]
    if not os.environ.get(key):
        raise RunError(f"{key} is not set: export it to run the {name} judge")
    return anthropic_create if name == "opus" else openai_create


def build_assessment(
    *, run_dir: Path, db_path: Path, heldout_dir: Path | None, labels: Path | None,
    max_cost: float | None, create_for: Callable[[str], Create] | None = None,
) -> Assessment:  # fmt: skip
    suite = load_suite(heldout_dir)
    items: dict[str, AttackCase | NormalQuestion] = {c.id: c for c in suite.attacks}
    items.update({q.id: q for q in suite.questions})
    return Assessment(run_dir=run_dir, db=load_agent_db(db_path), views=load_views_config(),
                      items=items, create_for=create_for or live_judge_create,
                      labels=load_labels(labels), max_cost=max_cost)  # fmt: skip
