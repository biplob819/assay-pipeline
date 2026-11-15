"""LLM04 planted rows: the fixtures and the scratch-build runner (Tech Spec §7.1, QCR-07, H7).

A case plants impossible rows next to real normalised rows, runs the quality rules on the
combined scratch table, and passes when every planted row carries every flag it should. The
published snapshot never contains planted rows (SNP-06); the manifest's `planted` field says so.
Stealth rows are run the same way and are expected to stay unflagged: known misses (H7).
The "absent from every approved view" half of a case is checked once the agent database exists
(Q2.3 extends `run_case` with the views).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field, field_validator, model_validator

from assay_pipeline.config import BuildConfig, ConfigError, _UniqueKeyLoader
from assay_pipeline.schemas import Activity
from assay_pipeline.schemas._base import Model, NonBlank
from assay_pipeline.schemas.data import Flag, Relation

from .normalise import activity_uid, pic50_for, standardise, to_nm
from .quality import apply_quality

Code = Annotated[str, Field(pattern=r"^llm04-[a-z0-9-]+$")]


def _real_number(value: object) -> object:
    """YAML numbers only: a bool or a quoted string is not a value."""
    if isinstance(value, bool | str):
        raise ValueError("value must be a YAML number")
    return value


def _structure(smiles: str) -> str:
    try:
        standardise(smiles)
    except ValueError as exc:
        raise ValueError(f"SMILES {smiles!r} cannot be standardised: {exc}") from exc
    return smiles


class PlantedRow(Model):
    smiles: NonBlank
    value: Annotated[float, Field(strict=False, allow_inf_nan=False)] | None
    units: str | None
    relation: Relation
    record_id: bool = True
    expect: tuple[Flag, ...] = Field(min_length=1)

    _number = field_validator("value", mode="before")(classmethod(lambda cls, v: _real_number(v)))
    _smiles = field_validator("smiles")(classmethod(lambda cls, v: _structure(v)))

    @field_validator("expect")
    @classmethod
    def _unique(cls, v: tuple[Flag, ...]) -> tuple[Flag, ...]:
        if len(set(v)) != len(v):
            raise ValueError("expect lists a flag twice")
        return v


class PlantedCase(Model):
    id: Code
    rule: Literal["QC1", "QC2", "QC3", "QC4"]
    rows: tuple[PlantedRow, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _tests_its_rule(self) -> PlantedCase:
        if any(self.rule not in r.expect for r in self.rows):
            raise ValueError(f"every row of a {self.rule} case must expect {self.rule}")
        return self


class StealthRow(Model):
    id: Code
    smiles: NonBlank
    value: Annotated[float, Field(strict=False, allow_inf_nan=False)]
    units: NonBlank
    relation: Relation

    _number = field_validator("value", mode="before")(classmethod(lambda cls, v: _real_number(v)))
    _smiles = field_validator("smiles")(classmethod(lambda cls, v: _structure(v)))


class PlantedSuite(Model):
    cases: tuple[PlantedCase, ...]
    stealth: tuple[StealthRow, ...]


def load_planted(path: Path | str | None = None) -> PlantedSuite:
    if path is None:
        res = resources.files("assay_pipeline").joinpath("defaults/planted_rows.yaml")
        raw, name = res.read_text(encoding="utf-8"), "defaults/planted_rows.yaml"
    else:
        try:
            raw, name = Path(path).read_text(encoding="utf-8"), str(path)
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"{path}: cannot read planted rows ({exc})") from exc
    try:
        suite = PlantedSuite.model_validate(yaml.load(raw, Loader=_UniqueKeyLoader))
    except Exception as exc:
        raise ConfigError(f"{name}: invalid planted rows: {exc}") from exc
    ids = [c.id for c in suite.cases] + [s.id for s in suite.stealth]
    if len(ids) != len(set(ids)):
        raise ConfigError(f"{name}: a case id repeats")
    return suite


def planted_activity(
    case_id: str,
    i: int,
    smiles: str,
    value: float | None,
    units: str | None,
    relation: Relation,
    record_id: bool,
    cfg: BuildConfig,
    snapshot: str,
) -> Activity:
    parent, key = standardise(smiles)
    nm = to_nm(value, units, cfg.rounding.value_nm_significant_figures)
    rid = f"planted:{case_id}:{i}"
    return Activity(
        activity_uid=activity_uid("chembl", rid),
        inchikey=key,
        source_inchikey=None,
        smiles=parent,
        target=cfg.target.name,
        variant="wild-type/unspecified",
        assay_type="B",
        assay_id=f"PLANTED-{case_id}",
        assay_description=f"planted row for {case_id}",
        value_original=value,
        units_original=units,
        value_nm=nm,
        relation=relation,
        pic50=pic50_for(nm),
        source="chembl",
        source_record_id=rid if record_id else None,
        depositor=None,
        chembl_src_id=None,
        data_validity_comment=None,
        document_id=None,
        flags=(),
        flag_reasons={},
        snapshot=snapshot,
    )


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    passed: bool
    planted_uids: tuple[str, ...]
    missing: dict[str, tuple[str, ...]]  # planted uid -> expected flags it did not get


def _snapshot_of(base: Sequence[Activity], snapshot: str | None) -> str:
    if snapshot is not None:
        return snapshot
    if not base:
        raise ValueError("an empty base needs an explicit snapshot id")
    return base[0].snapshot


def run_case(
    case: PlantedCase, base: Sequence[Activity], cfg: BuildConfig, snapshot: str | None = None
) -> CaseResult:
    """Plant the case's rows next to `base` in a scratch table and check every expected flag."""
    snap = _snapshot_of(base, snapshot)
    planted = [
        planted_activity(case.id, i, r.smiles, r.value, r.units, r.relation, r.record_id, cfg, snap)
        for i, r in enumerate(case.rows)
    ]
    planted_keys = {a.inchikey for a in planted}
    if planted_keys & {a.inchikey for a in base}:
        raise ValueError(f"{case.id}: a planted structure collides with a real compound")
    result = apply_quality([*base, *planted], cfg)
    by_uid = {a.activity_uid: a for a in result.activities}
    missing: dict[str, tuple[str, ...]] = {}
    for row, a in zip(case.rows, planted, strict=True):
        got = set(by_uid[a.activity_uid].flags)
        lacking = tuple(f for f in row.expect if f not in got)
        if lacking:
            missing[a.activity_uid] = lacking
    return CaseResult(case.id, not missing, tuple(a.activity_uid for a in planted), missing)


def run_stealth(
    row: StealthRow, base: Sequence[Activity], cfg: BuildConfig, snapshot: str | None = None
) -> tuple[str, ...]:
    """The flags a stealth row gets in a scratch table; expected empty (a known miss, H7)."""
    snap = _snapshot_of(base, snapshot)
    a = planted_activity(row.id, 0, row.smiles, row.value, row.units, row.relation, True, cfg, snap)
    if a.inchikey in {b.inchikey for b in base}:
        raise ValueError(f"{row.id}: a stealth structure collides with a real compound")
    result = apply_quality([*base, a], cfg)
    return next(x.flags for x in result.activities if x.activity_uid == a.activity_uid)


# ---- the full scratch build: views (Q2.3) ----


@dataclass(frozen=True)
class ViewCaseResult:
    case_id: str
    passed: bool
    flags_ok: bool
    leaks: tuple[str, ...]  # how a hidden planted row, or an unflagged visible one, showed


def run_case_in_views(
    case: PlantedCase, cache: object, cfg: BuildConfig, workdir: Path | str
) -> ViewCaseResult:
    """Plant the case into a scratch snapshot and agent database built from `cache`, then check
    the approved views. A planted row whose flags are only QC2/QC5 may show in `v_activities`
    with those flags (§5.4 shows QC2 and QC5 rows, flagged), and a QC2 key's consensus row must
    say conflict; every other planted row must be absent from every view (§7.1)."""
    from .agentdb import build_agent_db, planted_visibility
    from .ingest.cache import RawCache
    from .snapshot import build_snapshot

    if not isinstance(cache, RawCache):
        raise TypeError("cache must be a RawCache")
    workdir = Path(workdir)
    probe = build_snapshot(cfg, cache, workdir / "clean")
    rows = [
        planted_activity(
            case.id, i, r.smiles, r.value, r.units, r.relation, r.record_id, cfg,
            probe.manifest.snapshot,
        )
        for i, r in enumerate(case.rows)
    ]  # fmt: skip
    built = build_snapshot(cfg, cache, workdir / "planted", planted=rows)
    db = build_agent_db(built.directory, workdir / f"{case.id}.duckdb")
    seen = planted_visibility(db.path)
    leaks: list[str] = []
    flags_ok = True
    visible_ids = {r[0] or r[1]: tuple(r[2]) for r in seen["v_activities"]}
    in_table = {r[0] or r[1]: tuple(r[2]) for r in seen["activities"]}
    for row, planted_row in zip(case.rows, rows, strict=True):
        rid = planted_row.source_record_id or planted_row.assay_id
        expected = set(row.expect)
        stored = in_table.get(rid)
        if stored is None or not expected <= set(stored):
            flags_ok = False  # the plant itself must have landed, with its flags
            leaks.append(f"{rid}: not planted with its flags {sorted(expected)}: {stored}")
        if expected <= {"QC2", "QC5"}:
            got = visible_ids.get(rid)
            if got is None or not expected <= set(got):
                flags_ok = False
                leaks.append(f"{rid}: visible without its flags {sorted(expected)}: {got}")
        elif rid in visible_ids:
            leaks.append(f"{rid}: a {sorted(expected)} row is visible in v_activities")
    hidden_keys = {
        r.inchikey
        for r, row in zip(rows, case.rows, strict=True)
        if not set(row.expect) <= {"QC2", "QC5"}
    }
    for (key,) in seen["v_compounds"]:
        if key in hidden_keys:
            leaks.append(f"{key}: a hidden planted compound shows in v_compounds")
    for key, conflict in seen["v_consensus"]:
        if key in hidden_keys:
            leaks.append(f"{key}: a hidden planted key shows in v_consensus")
        elif case.rule == "QC2" and not conflict:
            leaks.append(f"{key}: a QC2 planted key shows without conflict=true")
    if case.rule == "QC2":
        qc2_keys = {r.inchikey for r in rows}
        shown = {key for key, conflict in seen["v_consensus"] if conflict}
        if not qc2_keys <= shown:
            leaks.append(f"{sorted(qc2_keys - shown)}: a QC2 key has no consensus row in conflict")
    return ViewCaseResult(case.id, flags_ok and not leaks, flags_ok, tuple(leaks))
