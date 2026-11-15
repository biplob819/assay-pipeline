"""Quality rules QC1–QC5 and the consensus table (Tech Spec §5.3, D-6, FR4).

Rules flag and never delete (D-6): every normalised row is returned, with `flags` and one reason
per flag. Definitions, exactly as §5.3:

- QC1: `value_nm` cannot be computed: missing value, missing units, or units outside the molar
  set (normalise already left `value_nm` null for these; the reason says which).
- QC2: the row's key `(inchikey, target, variant, assay_type)` has at least two relation "="
  rows whose pIC50 spread (max − min) is above 1.0. Every row of that key is flagged, and its
  consensus row has `conflict = true`. Exactly 1.0 is not a conflict.
- QC3: `pic50` is present and outside [quality.pic50_min, quality.pic50_max] (default [3, 12]),
  censored rows included; the bounds themselves pass. A value of exactly 0 nM has no finite
  pIC50 (it would be +inf), so it is out of range too (D-29).
- QC4: `source_record_id` is missing or blank.
- QC5: `relation` is anything other than "=" (censored or unknown).

Spreads are compared after rounding to the pIC50 precision, so 7.3 − 6.3 is exactly 1.0, not
1.0000000000000009. Consensus: one row per key; the median and spread are over relation "="
rows with a pIC50 (QC5 rows are excluded); every row of the key is a member.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from assay_pipeline.config import BuildConfig
from assay_pipeline.schemas import Activity, Consensus
from assay_pipeline.schemas.data import FLAGS, PIC50_DECIMALS, Flag

CONFLICT_SPREAD = 1.0  # fixed by the schemas (Consensus.conflict), not configurable
Key = tuple[str, str, str, str]


def key_of(a: Activity) -> Key:
    return (a.inchikey, a.target, a.variant, a.assay_type)


def spread(values: Iterable[float]) -> float:
    vals = list(values)
    return round(max(vals) - min(vals), PIC50_DECIMALS)


def _qc1(a: Activity) -> str | None:
    if a.value_nm is not None:
        return None
    if a.value_original is None:
        return "no numeric value"
    if not a.units_original:
        return "no units"
    return f"value {a.value_original:g} {a.units_original!r} cannot be converted to nM"


def _qc3(a: Activity, low: float, high: float) -> str | None:
    if a.value_nm == 0:
        return f"value 0 nM: pIC50 is infinite, outside [{low:g}, {high:g}]"
    if a.pic50 is None or low <= a.pic50 <= high:
        return None
    return f"pIC50 {a.pic50:g} is outside [{low:g}, {high:g}]"


def _qc4(a: Activity) -> str | None:
    if a.source_record_id is None or not a.source_record_id.strip():
        return "no source record id"
    return None


def _qc5(a: Activity) -> str | None:
    if a.relation == "=":
        return None
    if a.relation == "unknown":
        return "relation unknown (no qualifier)"
    return f"censored value (relation {a.relation!r})"


@dataclass(frozen=True)
class QualityResult:
    activities: tuple[Activity, ...]  # every input row, flagged, in input order
    consensus: tuple[Consensus, ...]  # one row per key, sorted by key

    def flags_by_rule(self) -> dict[str, int]:
        counts = Counter(f for a in self.activities for f in a.flags)
        return {rule: counts.get(rule, 0) for rule in FLAGS}

    @property
    def conflicts(self) -> int:
        return sum(c.conflict for c in self.consensus)


def apply_quality(activities: Iterable[Activity], cfg: BuildConfig) -> QualityResult:
    rows = list(activities)
    snapshots = {a.snapshot for a in rows}
    if len(snapshots) > 1:
        raise ValueError(f"the table mixes snapshots {sorted(snapshots)}")
    low, high = cfg.quality.pic50_min, cfg.quality.pic50_max
    by_key: dict[Key, list[Activity]] = defaultdict(list)
    for a in rows:
        by_key[key_of(a)].append(a)

    conflicted: dict[Key, float] = {}
    consensus: list[Consensus] = []
    for key in sorted(by_key):
        members = by_key[key]
        exact = [a.pic50 for a in members if a.relation == "=" and a.pic50 is not None]
        med = round(statistics.median(exact), PIC50_DECIMALS) if exact else None
        spr = spread(exact) if exact else None
        conflict = len(exact) >= 2 and spr is not None and spr > CONFLICT_SPREAD
        if conflict and spr is not None:
            conflicted[key] = spr
        consensus.append(
            Consensus(
                inchikey=key[0],
                target=key[1],
                variant=key[2],
                assay_type=members[0].assay_type,
                n_records=len(members),
                n_sources=len({a.source for a in members}),
                median_pic50=med,
                spread_pic50=spr,
                conflict=conflict,
                member_uids=tuple(sorted(a.activity_uid for a in members)),
                snapshot=members[0].snapshot,
            )
        )

    flagged: list[Activity] = []
    for a in rows:
        reasons: dict[Flag, str] = {}
        checks: tuple[tuple[Flag, str | None], ...] = (
            ("QC1", _qc1(a)),
            (
                "QC2",
                f"key spread {conflicted[key_of(a)]:g} > {CONFLICT_SPREAD:g} across '=' rows"
                if key_of(a) in conflicted
                else None,
            ),
            ("QC3", _qc3(a, low, high)),
            ("QC4", _qc4(a)),
            ("QC5", _qc5(a)),
        )
        for rule, reason in checks:
            if reason is not None:
                reasons[rule] = reason
        data = a.model_dump()
        data["flags"] = tuple(sorted(reasons))
        data["flag_reasons"] = reasons
        flagged.append(Activity.model_validate(data))  # re-checked, never model_copy
    return QualityResult(activities=tuple(flagged), consensus=tuple(consensus))
