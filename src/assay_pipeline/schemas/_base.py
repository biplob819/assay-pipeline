"""Shared base model, strict field types and version helpers for every schema.

Models are deeply immutable: list fields are tuples, dict fields become `FrozenDict`,
and free-form JSON values (`JsonValue`) are frozen recursively. That keeps an object's
JSON identical from the moment it is written to the moment it is read back.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime
from typing import Annotated, Any, NoReturn, TypeVar

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

SCHEMA_VERSION = "1.0"
# Audit lines and anchors: 1.1 adds the `too_complex` guard reason (hand-read follow-up, D-40).
AUDIT_SCHEMA_VERSION = "1.1"
VERSION_PATTERN = r"^[0-9]+\.[0-9]+$"


class FrozenDict(dict):
    """A dict that refuses mutation and can be hashed."""

    def _blocked(self, *_: Any, **__: Any) -> NoReturn:
        raise TypeError("schema objects are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _blocked  # type: ignore[assignment]
    __ior__ = _blocked  # type: ignore[assignment]

    def __hash__(self) -> int:  # type: ignore[override]
        return hash(frozenset(self.items()))

    def __iter__(self) -> Iterator[Any]:
        return super().__iter__()

    # copy, deepcopy and pickle rebuild through the constructor, never through __setitem__
    def __reduce__(self) -> tuple[Any, ...]:
        return (FrozenDict, (dict(self),))

    def __copy__(self) -> FrozenDict:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> FrozenDict:
        import copy

        return FrozenDict({k: copy.deepcopy(v, memo) for k, v in self.items()})


def deep_freeze(value: Any) -> Any:
    """Recursively turn lists into tuples and dicts into FrozenDicts; reject sets."""
    if isinstance(value, Mapping):
        if not all(isinstance(k, str) for k in value):
            raise ValueError("JSON object keys must be strings")
        return FrozenDict({k: deep_freeze(value[k]) for k in sorted(value)})
    if isinstance(value, list | tuple):
        return tuple(deep_freeze(v) for v in value)
    if isinstance(value, set | frozenset):
        raise ValueError("sets are not JSON values; use a list")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("NaN and infinity are not allowed")
    if value is not None and not isinstance(value, str | int | float | bool):
        raise ValueError(f"{type(value).__name__} is not a JSON value")
    return value


def _freeze_mapping(value: Mapping[Any, Any]) -> FrozenDict:
    # sorted keys: equal records always serialise to identical JSON (§5.5)
    return FrozenDict({k: value[k] for k in sorted(value)})


# Strict scalar types: no bool-as-int, no "5"-as-5, no NaN or infinity.
NonNegInt = Annotated[int, Field(ge=0, strict=True)]
PosInt = Annotated[int, Field(ge=1, strict=True)]
StrictInt = Annotated[int, Field(strict=True)]
NonNegFloat = Annotated[float, Field(strict=True, ge=0.0, allow_inf_nan=False)]
Fraction = Annotated[float, Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)]
NonBlank = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
Name = Annotated[str, StringConstraints(min_length=1, pattern=r"^\S(.*\S)?$")]  # no edge spaces


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


# Naive timestamps are ambiguous across machines; aware ones are normalised to UTC so equal
# instants always serialise identically.
Timestamp = Annotated[AwareDatetime, AfterValidator(_to_utc)]

# Free-form JSON (tool arguments, gold rows): frozen recursively, sets rejected.
JsonValue = Annotated[Any, AfterValidator(deep_freeze)]


V = TypeVar("V")

# `dict[str, V]` that validates into a FrozenDict: FrozenMap[Interval], FrozenMap[bool], ...
FrozenMap = Annotated[dict[str, V], AfterValidator(_freeze_mapping)]


class Model(BaseModel):
    """Frozen, strict base: unknown fields are errors, objects are immutable, floats finite."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Versioned(Model):
    """A top-level file record that carries the results-format version. It is required."""

    schema_version: Annotated[str, StringConstraints(pattern=VERSION_PATTERN)]

    @field_validator("schema_version")
    @classmethod
    def _same_major(cls, v: str) -> str:
        if v.split(".", 1)[0] != SCHEMA_VERSION.split(".", 1)[0]:
            raise ValueError(
                f"results format {v} is not compatible with this version of assay-pipeline "
                f"(expects {SCHEMA_VERSION.split('.')[0]}.x)"
            )
        return v


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ZERO_HASH = "0" * 64
INCHIKEY_PATTERN = r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$"
InChIKey = Annotated[str, StringConstraints(pattern=INCHIKEY_PATTERN)]
SNAPSHOT_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\.[1-9][0-9]*$"


def _real_date(value: str) -> str:
    date.fromisoformat(value.split(".")[0])  # raises on 2026-13-45
    return value


SnapshotId = Annotated[str, StringConstraints(pattern=SNAPSHOT_PATTERN), AfterValidator(_real_date)]
Code = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]  # reason codes
