"""Build and agent configuration, loaded from YAML with errors a user can act on.

The defaults ship inside the package (`assay_pipeline/defaults/`), so a pip-installed `assay`
works with no repo checkout. Pass another file to build a snapshot for a different target.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Self, TypeVar

import yaml
from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from assay_pipeline.schemas._base import Model, Name, NonBlank, PosInt

M = TypeVar("M", bound=BaseModel)
DEFAULT_BUILD = "build.yaml"
DEFAULT_VIEWS = "views.yaml"


class ConfigError(ValueError):
    """A config file is missing, unreadable or invalid. The message names the file and field."""


class TargetConfig(Model):
    name: Name
    chembl_target_id: Annotated[str, StringConstraints(pattern=r"^CHEMBL[0-9]+$")]
    uniprot: Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{6,10}$")]
    gene_symbol: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9-]+$")]
    activity_type: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_]+$")]


class ChemblConfig(Model):
    base_url: Annotated[str, StringConstraints(pattern=r"^https://\S+[^/]$")]
    page_size: Annotated[int, Field(ge=1, le=1000, strict=True)]
    max_requests_per_second: Annotated[float, Field(strict=True, gt=0, le=10, allow_inf_nan=False)]


class PubchemConfig(Model):
    base_url: Annotated[str, StringConstraints(pattern=r"^https://\S+[^/]$")]
    # PubChem asks for fewer than 5 requests per second
    max_requests_per_second: Annotated[float, Field(strict=True, gt=0, lt=5, allow_inf_nan=False)]
    exclude_depositors: tuple[Name, ...]


class QualityConfig(Model):
    pic50_min: Annotated[float, Field(strict=True, allow_inf_nan=False)]
    pic50_max: Annotated[float, Field(strict=True, allow_inf_nan=False)]

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.pic50_min >= self.pic50_max:
            raise ValueError("pic50_min must be below pic50_max")
        return self


class RoundingConfig(Model):
    value_nm_significant_figures: Annotated[int, Field(ge=3, le=12, strict=True)]


class BuildConfig(Model):
    target: TargetConfig
    chembl: ChemblConfig
    pubchem: PubchemConfig
    quality: QualityConfig
    rounding: RoundingConfig


ViewName = Annotated[str, StringConstraints(pattern=r"^v_[a-z][a-z0-9_]*$")]
Ident = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]


class ViewSpec(Model):
    name: ViewName
    columns: tuple[Ident, ...]

    @model_validator(mode="after")
    def _columns(self) -> Self:
        if not self.columns or len(set(self.columns)) != len(self.columns):
            raise ValueError(f"{self.name}: columns must be non-empty and unique")
        return self


class Limits(Model):
    max_rows_executed: PosInt
    max_rows_shown: PosInt
    max_chars_shown: PosInt
    timeout_seconds: Annotated[float, Field(strict=True, gt=0, le=120, allow_inf_nan=False)]
    step_budget: PosInt
    memory_limit: Annotated[str, StringConstraints(pattern=r"^[0-9]+(MB|GB)$")]
    threads: PosInt

    @model_validator(mode="after")
    def _shown_within_executed(self) -> Self:
        if self.max_rows_shown > self.max_rows_executed:
            raise ValueError("max_rows_shown cannot exceed max_rows_executed")
        return self


# Functions the guard knows how to map (assay_pipeline.guard.sql.FUNCTION_CLASSES must match).
# Table functions (read_*, query*, glob, duckdb_*) are never on it (Tech Spec §6.1).
KNOWN_FUNCTIONS = frozenset(
    {"count", "sum", "avg", "median", "min", "max", "quantile_cont", "stddev_samp", "abs",
     "round", "log10", "ln", "exp", "power", "sqrt", "lower", "upper", "length", "trim",
     "contains", "starts_with", "coalesce", "nullif", "cast"}
)  # fmt: skip


class ViewsConfig(Model):
    views: tuple[ViewSpec, ...]
    functions: tuple[Ident, ...]
    limits: Limits

    @model_validator(mode="after")
    def _unique(self) -> Self:
        names = [v.name for v in self.views]
        if not names or len(set(names)) != len(names):
            raise ValueError("views must be non-empty with unique names")
        if len(set(self.functions)) != len(self.functions):
            raise ValueError("functions must be unique")
        return self

    @field_validator("functions")
    @classmethod
    def _known_functions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(value) - KNOWN_FUNCTIONS)
        if unknown:
            raise ValueError(f"functions not allowed in queries: {unknown}")
        return value

    @property
    def view_names(self) -> tuple[str, ...]:
        return tuple(v.name for v in self.views)


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of keeping the last one."""


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)  # type: ignore[arg-type]
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,  # type: ignore[arg-type]
)


def _read(path: Path | None, default: str) -> tuple[str, str]:
    if path is None:
        ref = resources.files("assay_pipeline") / "defaults" / default
        return f"<default {default}>", ref.read_text(encoding="utf-8")
    p = Path(path)
    if p.is_dir():
        raise ConfigError(f"{p}: is a directory, expected a YAML file")
    if not p.exists():
        raise ConfigError(f"{p}: file not found")
    try:
        return str(p), p.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{p}: not UTF-8 text ({exc.reason} at byte {exc.start})") from exc
    except OSError as exc:
        raise ConfigError(f"{p}: cannot read file: {exc.strerror or exc}") from exc


def _load(path: Path | None, default: str, model: type[M]) -> M:
    name, text = _read(path, default)
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{name}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{name}: expected a mapping at the top level")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "(top level)"
        raise ConfigError(f"{name}: {where}: {first['msg']}") from exc


def load_build_config(path: Path | str | None = None) -> BuildConfig:
    return _load(Path(path) if path is not None else None, DEFAULT_BUILD, BuildConfig)


def load_views_config(path: Path | str | None = None) -> ViewsConfig:
    return _load(Path(path) if path is not None else None, DEFAULT_VIEWS, ViewsConfig)


__all__ = [
    "BuildConfig",
    "ConfigError",
    "Limits",
    "NonBlank",
    "TargetConfig",
    "ViewSpec",
    "ViewsConfig",
    "load_build_config",
    "load_views_config",
]
