"""Statistics for the report (Tech Spec §7.6, RUN-07).

- Pass rate per (category, model) with a two-level bootstrap: resample cases, then trials
  within each resampled case (EvalKit D-41, L-36). 95% interval, fixed seed.
- pass^k: the share of cases blocked in every one of their k trials.
- Zero successes are a bound, not "0%": with n trials and no success the exact (Clopper-Pearson)
  95% upper bound on the true success rate is 1 - 0.05 ** (1 / n).
- Held-out results sit beside public ones and are flagged when outside the public interval.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

N_RESAMPLES = 10_000
ALPHA = 0.05


@dataclass(frozen=True)
class Interval:
    point: float
    lo: float
    hi: float


def two_level_interval(
    by_case: Mapping[str, Sequence[float]], *, n_resamples: int = N_RESAMPLES, seed: int = 0
) -> Interval:
    cases = sorted(by_case)
    if not cases or any(not by_case[c] for c in cases):
        raise ValueError("every case needs at least one trial")
    point = sum(sum(by_case[c]) / len(by_case[c]) for c in cases) / len(cases)
    rng = random.Random(seed)
    boots = []
    for _ in range(n_resamples):
        total = 0.0
        for _ in cases:
            trials = by_case[rng.choice(cases)]
            total += sum(rng.choice(trials) for _ in trials) / len(trials)
        boots.append(total / len(cases))
    boots.sort()
    lo = boots[int(ALPHA / 2 * n_resamples)]
    hi = boots[min(n_resamples - 1, int((1 - ALPHA / 2) * n_resamples))]
    return Interval(round(point, 6), round(lo, 6), round(hi, 6))


def pass_k(by_case: Mapping[str, Sequence[float]]) -> float:
    """The share of cases whose every trial scored 1 (blocked)."""
    if not by_case:
        raise ValueError("no cases")
    return round(sum(all(v == 1.0 for v in t) for t in by_case.values()) / len(by_case), 6)


def zero_success_bound(n_trials: int, alpha: float = ALPHA) -> float:
    """Exact upper (1 - alpha) bound on a rate with 0 successes in n trials."""
    if n_trials <= 0:
        raise ValueError("n_trials must be positive")
    return round(1 - alpha ** (1 / n_trials), 6)


def success_summary(successes: int, n_trials: int) -> str:
    if successes == 0:
        bound = zero_success_bound(n_trials)
        return f"0 of {n_trials} trials; the true rate is below about {bound:.0%} at 95%"
    return f"{successes} of {n_trials} trials ({successes / n_trials:.1%})"


def outside(interval: Interval, value: float) -> bool:
    return value < interval.lo or value > interval.hi
