"""Data-side records (Tech Spec §3): raw responses, activities, drops, consensus, manifest.

Row records (`Activity`, `Drop`, `Consensus`) live in Parquet files, whose format version is
the manifest's `schema_version`. File records (`RawResponse` index lines, `Manifest`) carry
their own `schema_version`.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, StringConstraints, model_validator

from ._base import (
    Code,
    FrozenMap,
    InChIKey,
    Model,
    Name,
    NonBlank,
    NonNegInt,
    PosInt,
    Sha256,
    SnapshotId,
    Timestamp,
    Versioned,
)

Flag = Literal["QC1", "QC2", "QC3", "QC4", "QC5"]
FLAGS: tuple[Flag, ...] = ("QC1", "QC2", "QC3", "QC4", "QC5")
Source = Literal["chembl", "pubchem"]
Relation = Literal["=", "<", ">", "<=", ">=", "unknown"]
AssayType = Literal["B", "F", "A", "T", "P", "U"]  # ChEMBL types; U = unknown (PubChem)

PIC50_DECIMALS = 4
PIC50_TOLERANCE = 1e-9  # pic50 must be exactly pic50_of(value_nm), up to float noise


def pic50_of(value_nm: float) -> float:
    """pIC50 from a concentration in nM: 9 − log10(nM), rounded to 4 decimals."""
    return round(9.0 - math.log10(value_nm), PIC50_DECIMALS)


class RawResponse(Versioned):
    """One cached API response; its body is stored separately under its own hash."""

    source: Source
    request_key: Sha256
    url: NonBlank
    release: NonBlank
    fetched_at: Timestamp
    status: Annotated[int, Field(ge=100, le=599, strict=True)]
    body_sha256: Sha256


class Activity(Model):
    """One row of the unified table. Never merged with another row (D-6)."""

    activity_uid: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
    inchikey: InChIKey
    source_inchikey: InChIKey | None
    smiles: NonBlank
    target: Name
    variant: Name
    assay_type: AssayType
    assay_id: NonBlank
    assay_description: str
    value_original: Annotated[float, Field(strict=True, allow_inf_nan=False)] | None
    units_original: str | None
    value_nm: Annotated[float, Field(strict=True, ge=0.0, allow_inf_nan=False)] | None
    relation: Relation
    pic50: Annotated[float, Field(strict=True, allow_inf_nan=False)] | None
    source: Source
    source_record_id: NonBlank | None
    depositor: Name | None
    chembl_src_id: Annotated[int, Field(ge=0, strict=True)] | None
    data_validity_comment: str | None
    document_id: NonBlank | None
    flags: tuple[Flag, ...]
    flag_reasons: FrozenMap[NonBlank]
    snapshot: SnapshotId

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.value_nm is not None and self.value_nm > 0:
            if self.pic50 is None:
                raise ValueError("pic50 is required when value_nm is positive")
            if abs(self.pic50 - pic50_of(self.value_nm)) > PIC50_TOLERANCE:
                raise ValueError(
                    f"pic50 {self.pic50} does not equal 9 − log10({self.value_nm}) rounded to "
                    f"{PIC50_DECIMALS} decimals ({pic50_of(self.value_nm)})"
                )
        elif self.pic50 is not None:
            raise ValueError("pic50 must be null when value_nm is null or zero")
        pubchem = self.source == "pubchem"
        if (self.assay_type == "U") != pubchem:
            raise ValueError("assay_type 'U' is used for, and only for, PubChem rows")
        if (self.depositor is not None) != pubchem:
            raise ValueError("depositor is set for, and only for, PubChem rows")
        if pubchem and (self.chembl_src_id is not None or self.data_validity_comment is not None):
            raise ValueError("chembl_src_id and data_validity_comment are ChEMBL-only fields")
        if list(self.flags) != sorted(set(self.flags)):
            raise ValueError("flags must be sorted and unique")
        if set(self.flag_reasons) != set(self.flags):
            raise ValueError("flag_reasons must have exactly one entry per flag")
        return self


class Drop(Model):
    """A source row a stage dropped, with the reason (the drop ledger, §2.1)."""

    source: Source
    source_record_id: NonBlank | None
    stage: Code
    reason: Code


class Consensus(Model):
    """One row per QC2 key: median over relation "=" rows, with every member's uid (D-6)."""

    inchikey: InChIKey
    target: Name
    variant: Name
    assay_type: AssayType
    n_records: PosInt
    n_sources: Annotated[int, Field(ge=1, le=2, strict=True)]
    median_pic50: Annotated[float, Field(strict=True, allow_inf_nan=False)] | None
    spread_pic50: Annotated[float, Field(strict=True, ge=0.0, allow_inf_nan=False)] | None
    conflict: StrictBool
    member_uids: tuple[Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")], ...]
    snapshot: SnapshotId

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.median_pic50 is None) != (self.spread_pic50 is None):
            raise ValueError("median_pic50 and spread_pic50 are both null or both set")
        expected = self.spread_pic50 is not None and self.spread_pic50 > 1.0
        if self.conflict != expected:
            raise ValueError("conflict must be true exactly when spread_pic50 > 1.0")
        if len(set(self.member_uids)) != len(self.member_uids):
            raise ValueError("member_uids must be unique")
        if self.n_records != len(self.member_uids):
            raise ValueError("n_records must equal the number of member_uids")
        if self.n_sources > self.n_records:
            raise ValueError("n_sources cannot exceed n_records")
        if self.spread_pic50 is not None and self.spread_pic50 > 0 and self.n_records < 2:
            raise ValueError("a non-zero spread needs at least two records")
        return self


class Manifest(Versioned):
    """What a snapshot is made of, enough to rebuild and compare it (§5.5)."""

    snapshot: SnapshotId
    target: Name
    activity_type: Name
    created_at: Timestamp
    source_releases: FrozenMap[NonBlank]
    raw_cache_sha256: Sha256
    rows_by_source: FrozenMap[NonNegInt]
    drops_by_reason: FrozenMap[NonNegInt]
    flags_by_rule: FrozenMap[NonNegInt]
    files: FrozenMap[Sha256]
    content_sha256: FrozenMap[Sha256]
    tool_versions: FrozenMap[NonBlank]
    code_sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,40}$")]
    planted: StrictBool  # no default: every writer must say whether planted rows are present

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if set(self.flags_by_rule) != set(FLAGS):
            raise ValueError(f"flags_by_rule must have exactly the keys {', '.join(FLAGS)}")
        if set(self.files) != set(self.content_sha256):
            raise ValueError("files and content_sha256 must name the same paths")
        if not self.files:
            raise ValueError("a manifest must list at least one file")
        return self
