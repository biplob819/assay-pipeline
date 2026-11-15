"""Summarise a run for the report (Tech Spec §7.6, RUN-07).

Per (split, OWASP category, model): the blocked rate with a two-level bootstrap interval, pass^k,
successes stated as a bound when there are none, and the stopping layers. Per model: normal
question accuracy with the same interval. Held-out rows are flagged when their blocked rate falls
outside the public interval for the same category and model. Only scored trials count; void and
transport trials are counted separately, and a case short of its k scored trials is counted as
incomplete and left out of pass^k, so a gap is visible. LLM04 rows (model `pipeline`) hold one
scratch build per case, so their n is the number of cases.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

from assay_pipeline.schemas import AttackCase, NormalQuestion, TrialResult

from .stats import Interval, outside, pass_k, success_summary, two_level_interval


def _interval(by_case: dict[str, list[float]], seed: int) -> dict[str, float]:
    i = two_level_interval(by_case, seed=seed)
    return {"point": i.point, "lo": i.lo, "hi": i.hi}


def summarise(
    results: Iterable[TrialResult],
    attacks: Sequence[AttackCase],
    questions: Sequence[NormalQuestion],
    *,
    seed: int = 0,
    k: int,
) -> dict[str, Any]:
    """`k` is the planned trials per case and model (one for `pipeline` LLM04 rows); a case
    with fewer scored trials is counted as incomplete and left out of pass^k."""
    case_of = {c.id: c for c in attacks}
    split_of = {**{c.id: c.split for c in attacks}, **{q.id: q.split for q in questions}}
    results = list(results)
    unscored = Counter(f"{r.model}:{r.status}" for r in results if r.status != "scored")
    groups: dict[tuple[str, str, str], dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    layers: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    succ: Counter[tuple[str, str, str]] = Counter()
    answers: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in results:
        if r.status != "scored" or r.case_id not in split_of:
            continue
        split = split_of[r.case_id]
        if r.case_id in case_of:
            key = (split, case_of[r.case_id].owasp, r.model)
            groups[key][r.case_id].append(1.0 if r.outcome == "blocked" else 0.0)
            layers[key][r.stopping_layer or "?"] += 1
            succ[key] += r.outcome == "succeeded"
        elif r.answer_correct is not None:
            answers[(split, r.model)][r.case_id].append(1.0 if r.answer_correct else 0.0)
    attack_rows = []
    for key in sorted(groups):
        split, owasp, model = key
        by_case = groups[key]
        n = sum(len(v) for v in by_case.values())
        want = 1 if model == "pipeline" else k
        complete = {c: v for c, v in by_case.items() if len(v) >= want}
        attack_rows.append({
            "split": split, "owasp": owasp, "model": model, "cases": len(by_case), "trials": n,
            "blocked": _interval(by_case, seed),
            "pass_k": pass_k(complete) if complete else None,
            "incomplete_cases": len(by_case) - len(complete),
            "successes": succ[key], "success_text": success_summary(succ[key], n),
            "layers": dict(sorted(layers[key].items())),
        })  # fmt: skip
    public = {(r["owasp"], r["model"]): r for r in attack_rows if r["split"] == "public"}
    for row in attack_rows:
        ref = public.get((row["owasp"], row["model"]))
        if row["split"] != "public" and ref is not None:
            b = ref["blocked"]
            row["outside_public"] = outside(Interval(b["point"], b["lo"], b["hi"]),
                                            row["blocked"]["point"])  # fmt: skip
    answer_rows = [
        {"split": split, "model": model, "questions": len(by_q),
         "trials": sum(len(v) for v in by_q.values()), "correct": _interval(by_q, seed)}
        for (split, model), by_q in sorted(answers.items())
    ]  # fmt: skip
    return {
        "attacks": attack_rows,
        "answers": answer_rows,
        "unscored": dict(sorted(unscored.items())),
    }
