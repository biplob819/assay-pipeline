"""Normalise ChEMBL and PubChem rows into `Activity` rows (Tech Spec §2.2, §5.1, §5.2, D-3, D-6).

Reads the raw cache only (D-2): no network, no clock, no randomness.

- Structures: the RDKit-standardised parent (cleanup, largest fragment, uncharge; no tautomer
  canonicalisation), its canonical SMILES and InChIKey (D-3). The RDKit version is reported.
- Values: converted to nM over the molar set (pM, nM, uM/µM/μM, mM, M); anything else (mass
  units, missing or non-numeric values, negatives) keeps the row with `value_nm = None`, for
  QC1 to flag (D-6: flag, never delete). `value_nm` is rounded to the configured significant
  figures here, the only place, and `pic50 = pic50_of(value_nm)` is computed from the rounded
  value (§5.5).
- Relations: ChEMBL's `=`, `<`, `>`, `<=`, `>=` are kept; anything else (`~`, missing) and every
  PubChem row (the concise table has no qualifier) is `unknown` (H4).
- Variants: ChEMBL's assay variant mutation, or `wild-type/unspecified`; PubChem rows are always
  `wild-type/unspecified` (no mutation column: H2, a stated limit).
- Rows are dropped only when no `Activity` can be built: a structure RDKit cannot read, or one
  with whitespace inside (RDKit would read only its first part) (`unparseable_structure`), a
  blank structure (`no_structure`), a ChEMBL assay type outside B/F/A/T/P
  (`unknown_assay_type`), or a record whose fields have the wrong type or shape
  (`malformed_record`). Every drop is in the ledger with its reason; no record crashes the run.
- Stated D-3 limits: RDKit `Cleanup` disconnects metals (cisplatin's parent is ammonia), and the
  largest fragment can be a large counterion (pamoate salts).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, cast

from pydantic import TypeAdapter, ValidationError

from assay_pipeline.config import BuildConfig
from assay_pipeline.schemas import Activity, Drop
from assay_pipeline.schemas._base import Name, SnapshotId
from assay_pipeline.schemas.data import AssayType, Source, pic50_of

from .ingest.cache import RawCache
from .ingest.chembl import read_chembl
from .ingest.pubchem import DepositorTerms, PubchemRow, read_pubchem

STAGE = "normalise"
WILD_TYPE = "wild-type/unspecified"
TO_NM = {"pM": 1e-3, "nM": 1.0, "uM": 1e3, "µM": 1e3, "μM": 1e3, "mM": 1e6, "M": 1e9}
RELATIONS = frozenset({"=", "<", ">", "<=", ">="})
CHEMBL_ASSAY_TYPES = frozenset({"B", "F", "A", "T", "P"})
# Activity fields whose values come straight from a source record: a schema refusal on one of
# these is bad source data (a drop); a refusal anywhere else is a bug and is raised.
SOURCE_FIELDS = frozenset(
    {
        "variant",
        "source_inchikey",
        "depositor",
        "assay_id",
        "document_id",
        "assay_description",
        "units_original",
        "data_validity_comment",
    }
)
NUMBER = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")  # ASCII only


class StructureError(ValueError):
    """RDKit could not build a parent structure."""


# ---- structures (D-3) ----


def rdkit_version() -> str:
    import rdkit

    return rdkit.__version__


def standardise(smiles: str) -> tuple[str, str]:
    """(canonical SMILES, InChIKey) of the RDKit parent: cleanup, largest fragment, uncharge."""
    from rdkit import Chem, rdBase
    from rdkit.Chem.MolStandardize import rdMolStandardize

    rdBase.DisableLog("rdApp.*")  # parse warnings become drops with a reason, not log noise
    if not smiles.strip() or any(ch.isspace() for ch in smiles.strip()):
        # RDKit reads up to the first whitespace and takes the rest as a name: a different
        # compound, silently. A SMILES with whitespace inside is not read at all.
        raise StructureError("the SMILES is blank or has whitespace inside")
    mol = Chem.MolFromSmiles(smiles.strip())
    if mol is None:
        raise StructureError("RDKit cannot parse the SMILES")
    try:
        mol = rdMolStandardize.Cleanup(mol)
        mol = rdMolStandardize.LargestFragmentChooser().choose(mol)
        mol = rdMolStandardize.Uncharger().uncharge(mol)
        Chem.SanitizeMol(mol)
    except Exception as exc:  # RDKit raises several C++-mapped types
        raise StructureError(f"RDKit cannot standardise it ({type(exc).__name__})") from exc
    parent = Chem.MolToSmiles(mol)
    key = Chem.MolToInchiKey(mol)
    if not parent or not key:
        raise StructureError("RDKit gave no SMILES or InChIKey for the parent")
    return parent, key


# ---- values ----


def round_sig(value: float, figures: int) -> float:
    """Round to significant figures once, exactly as a decimal string would show it."""
    if value == 0:
        return 0.0
    return float(f"{value:.{figures - 1}e}")


def parse_value(raw: Any) -> float | None:
    """A finite float from a source value (string or number), else None."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        if isinstance(raw, int | float):
            value = float(raw)
        elif isinstance(raw, str) and NUMBER.fullmatch(raw.strip()):
            text = raw.strip()
            value = float(text)  # ASCII digits only: no "1_000", no non-Latin digits
            mantissa = re.split(r"[eE]", text)[0]
            if value == 0 and any(ch in "123456789" for ch in mantissa):
                return None  # a non-zero value that underflows (e.g. "1e-400") is not zero
        else:
            return None
    except OverflowError:  # a JSON integer too large for a float
        return None
    return value if math.isfinite(value) else None


def to_nm(value: float | None, units: str | None, figures: int) -> float | None:
    """nM rounded to `figures`, or None when it cannot be computed (QC1 flags it later)."""
    if value is None or not isinstance(units, str) or value < 0:
        return None
    factor = TO_NM.get(units.strip())
    if factor is None:
        return None
    nm = round_sig(value * factor, figures)
    if not math.isfinite(nm) or (nm == 0 and value != 0):
        return None  # overflow, or a positive value that underflows to zero
    return nm + 0.0  # never -0.0


def pic50_for(value_nm: float | None) -> float | None:
    return pic50_of(value_nm) if value_nm is not None and value_nm > 0 else None


def activity_uid(source: Source, record_id: str) -> str:
    return hashlib.sha256(f"{source}\0{record_id}".encode()).hexdigest()[:16]


# ---- results ----


@dataclass(frozen=True)
class NormaliseResult:
    snapshot: str
    activities: tuple[Activity, ...]
    drops: tuple[Drop, ...]  # ingest drops (PubChem) and normalise drops, in one ledger
    releases: dict[str, str]  # source -> release read
    rows_in: dict[str, int]  # source -> rows offered to normalise
    source_key_mismatches: int  # rows whose source InChIKey differs from the RDKit parent's
    validity_comments: int  # ChEMBL rows kept with a data_validity_comment (§5.1)
    rdkit_version: str

    def drops_by_reason(self) -> dict[str, int]:
        return dict(sorted(Counter(d.reason for d in self.drops).items()))


# ---- mapping ----


class _Structures:
    """Standardise each distinct input SMILES once."""

    def __init__(self) -> None:
        self._seen: dict[str, tuple[str, str] | StructureError] = {}

    def get(self, smiles: str) -> tuple[str, str]:
        if smiles not in self._seen:
            try:
                self._seen[smiles] = standardise(smiles)
            except StructureError as exc:
                self._seen[smiles] = exc
        got = self._seen[smiles]
        if isinstance(got, StructureError):
            raise got
        return got


def _structure(
    structures: _Structures, smiles: Any, source: Source, rid: str, drops: list[Drop]
) -> tuple[str, str] | None:
    if not isinstance(smiles, str) or not smiles.strip():
        drops.append(Drop(source=source, source_record_id=rid, stage=STAGE, reason="no_structure"))
        return None
    try:
        return structures.get(smiles.strip())
    except StructureError:
        drops.append(
            Drop(source=source, source_record_id=rid, stage=STAGE, reason="unparseable_structure")
        )
        return None


def _chembl_activity(
    r: dict, cfg: BuildConfig, snapshot: str, structures: _Structures, drops: list[Drop]
) -> Activity | None:
    rid = str(r["activity_id"])
    assay_type = r.get("assay_type")
    if not isinstance(assay_type, str) or assay_type not in CHEMBL_ASSAY_TYPES:
        drops.append(
            Drop(source="chembl", source_record_id=rid, stage=STAGE, reason="unknown_assay_type")
        )
        return None
    parent = _structure(structures, r.get("canonical_smiles"), "chembl", rid, drops)
    if parent is None:
        return None
    smiles, inchikey = parent
    assay_id, variant_raw = r.get("assay_chembl_id"), r.get("assay_variant_mutation")
    units, relation = r.get("standard_units"), r.get("standard_relation")
    comment, doc, src = r.get("data_validity_comment"), r.get("document_chembl_id"), r.get("src_id")
    well_typed = (
        isinstance(assay_id, str)
        and assay_id.strip() != ""
        and not any(ch.isspace() for ch in assay_id.strip())
        and not (isinstance(doc, str) and any(ch.isspace() for ch in doc.strip()))
        and all(isinstance(v, str | None) for v in (variant_raw, units, relation, comment, doc))
        and isinstance(r.get("assay_description"), str | None)
        and (src is None or (isinstance(src, int) and not isinstance(src, bool) and src >= 0))
    )
    if not well_typed:
        drops.append(
            Drop(source="chembl", source_record_id=rid, stage=STAGE, reason="malformed_record")
        )
        return None
    assert isinstance(assay_id, str)
    value = parse_value(r.get("standard_value"))
    value_nm = to_nm(value, units, cfg.rounding.value_nm_significant_figures)
    variant = (variant_raw or "").strip() or WILD_TYPE
    return _build(
        drops,
        "chembl",
        rid,
        lambda: Activity(
            activity_uid=activity_uid("chembl", rid),
            inchikey=inchikey,
            source_inchikey=None,  # the activity endpoint carries no structure key
            smiles=smiles,
            target=cfg.target.name,
            variant=variant,
            assay_type=cast(AssayType, assay_type),  # checked against B/F/A/T/P above
            assay_id=assay_id.strip(),
            assay_description=str(r.get("assay_description") or ""),
            value_original=value,
            units_original=units if units and units.strip() else None,
            value_nm=value_nm,
            relation=relation if relation in RELATIONS else "unknown",
            pic50=pic50_for(value_nm),
            source="chembl",
            source_record_id=rid,
            depositor=None,
            chembl_src_id=src,
            data_validity_comment=comment if comment and comment.strip() else None,
            document_id=doc.strip() if doc and doc.strip() else None,
            flags=(),
            flag_reasons={},
            snapshot=snapshot,
        ),
    )


def _build(drops: list[Drop], source: Source, rid: str, make: Any) -> Activity | None:
    """Build the row. A schema refusal on source-derived fields only (a newline in a variant, a
    malformed source key) drops this record with a reason; any other refusal (pIC50, nM value,
    snapshot, target, the model-level checks) is a bug and is raised, never hidden as data."""
    try:
        return make()
    except ValidationError as exc:
        fields = {e["loc"][0] if e["loc"] else None for e in exc.errors()}
        if not fields <= SOURCE_FIELDS:
            raise
        drops.append(
            Drop(source=source, source_record_id=rid, stage=STAGE, reason="malformed_record")
        )
        return None


def _pubchem_activity(
    r: PubchemRow, cfg: BuildConfig, snapshot: str, structures: _Structures, drops: list[Drop]
) -> Activity | None:
    rid = r.source_record_id
    parent = _structure(structures, r.smiles, "pubchem", rid, drops)
    if parent is None:
        return None
    smiles, inchikey = parent
    value = parse_value(r.value_um)
    value_nm = to_nm(value, "uM", cfg.rounding.value_nm_significant_figures)
    pmid = r.pubmed_id.strip()
    return _build(
        drops,
        "pubchem",
        rid,
        lambda: Activity(
            activity_uid=activity_uid("pubchem", rid),
            inchikey=inchikey,
            source_inchikey=r.inchikey,
            smiles=smiles,
            target=cfg.target.name,
            variant=WILD_TYPE,
            assay_type="U",
            assay_id=str(r.aid),
            assay_description=r.assay_name,
            value_original=value,
            units_original="uM" if value is not None else None,
            value_nm=value_nm,
            relation="unknown",
            pic50=pic50_for(value_nm),
            source="pubchem",
            source_record_id=rid,
            depositor=r.depositor,
            chembl_src_id=None,
            data_validity_comment=None,
            document_id=f"PMID:{pmid}" if pmid.isascii() and pmid.isdigit() else None,
            flags=(),
            flag_reasons={},
            snapshot=snapshot,
        ),
    )


def normalise(
    cfg: BuildConfig,
    cache: RawCache,
    snapshot: str,
    *,
    chembl_release: str | None = None,
    pubchem_release: str | None = None,
    terms: DepositorTerms | None = None,
) -> NormaliseResult:
    """Map every cached ChEMBL and PubChem row of the configured target to `Activity` rows."""
    TypeAdapter(SnapshotId).validate_python(snapshot)  # a caller error, raised before any row
    TypeAdapter(Name).validate_python(cfg.target.name)
    chembl_rel, chembl_rows = read_chembl(cfg, cache, chembl_release)
    pubchem = read_pubchem(cfg, cache, pubchem_release, terms)
    structures = _Structures()
    drops: list[Drop] = list(pubchem.drops)
    out: list[Activity] = []
    for r in chembl_rows:
        a = _chembl_activity(r, cfg, snapshot, structures, drops)
        if a is not None:
            out.append(a)
    for p in pubchem.rows:
        a = _pubchem_activity(p, cfg, snapshot, structures, drops)
        if a is not None:
            out.append(a)
    offered = len(chembl_rows) + len(pubchem.rows)
    malformed = sum(1 for d in drops if d.stage == STAGE and d.reason == "malformed_record")
    if offered and malformed == offered:
        raise ValueError(
            f"all {offered} rows were refused as malformed_record: that is a mapping bug, not data"
        )
    uids = Counter(a.activity_uid for a in out)
    clash = [u for u, n in uids.items() if n > 1]
    if clash:
        raise ValueError(f"activity_uid collision for {clash[:3]}: two rows share an id")
    out.sort(key=lambda a: (a.inchikey, a.source, a.source_record_id or "", a.activity_uid))
    drops.sort(key=lambda d: (d.source, d.stage, d.source_record_id or "", d.reason))
    mismatches = sum(
        1 for a in out if a.source_inchikey is not None and a.source_inchikey != a.inchikey
    )
    return NormaliseResult(
        snapshot=snapshot,
        activities=tuple(out),
        drops=tuple(drops),
        releases={"chembl": chembl_rel, "pubchem": pubchem.release},
        rows_in={"chembl": len(chembl_rows), "pubchem": len(pubchem.rows)},
        source_key_mismatches=mismatches,
        validity_comments=sum(1 for a in out if a.data_validity_comment),
        rdkit_version=rdkit_version(),
    )
