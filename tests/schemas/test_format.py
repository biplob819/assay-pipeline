"""Format v1: fixtures for every file schema, byte-identical round trips, version rules."""

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

import assay_pipeline.schemas as schemas
from tests.schemas import factories as f

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "schemas"
SPEC = Path(__file__).resolve().parents[2] / "Trusted Assay Pipeline Tech Spec.md"

CASES = {
    "raw_response": (schemas.RawResponse, f.raw_response),
    "activity_chembl": (schemas.Activity, f.activity),
    "activity_pubchem": (schemas.Activity, f.pubchem_activity),
    "drop": (
        schemas.Drop,
        lambda: {
            "source": "pubchem",
            "source_record_id": "1:2",
            "stage": "ingest",
            "reason": "chembl_deposited_aid",
        },
    ),
    "consensus": (schemas.Consensus, f.consensus),
    "manifest": (schemas.Manifest, f.manifest),
    "guard_allowed": (schemas.GuardDecision, f.guard_allowed),
    "guard_blocked": (schemas.GuardDecision, f.guard_blocked),
    "audit_genesis": (schemas.AuditLine, lambda: f.audit("genesis")),
    "audit_question": (schemas.AuditLine, lambda: f.audit("question")),
    "audit_query": (schemas.AuditLine, lambda: f.audit("query")),
    "audit_answer": (schemas.AuditLine, lambda: f.audit("answer")),
    "anchor": (schemas.Anchor, f.anchor),
    "attack_case": (schemas.AttackCase, f.attack),
    "attack_case_llm04": (schemas.AttackCase, f.poisoning),
    "normal_question": (schemas.NormalQuestion, f.question),
    "trial_result": (schemas.TrialResult, f.trial),
}


def dump(obj) -> str:
    return obj.model_dump_json(indent=2) + "\n"


@pytest.mark.contract
@pytest.mark.parametrize("name", sorted(CASES))
def test_fixture_parses_and_round_trips_byte_identically(name):
    model, _ = CASES[name]
    text = (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
    obj = model.model_validate_json(text)
    assert dump(obj) == text
    assert dump(model.model_validate_json(dump(obj))) == text


@pytest.mark.contract
@pytest.mark.parametrize("name", sorted(CASES))
def test_fixture_equals_the_factory_example(name):
    model, make = CASES[name]
    assert (FIXTURES / f"{name}.json").read_text(encoding="utf-8") == dump(
        model.model_validate(make())
    )


@pytest.mark.contract
def test_every_fixture_file_is_covered():
    assert {p.stem for p in FIXTURES.glob("*.json")} == set(CASES)


VERSIONED = [
    schemas.RawResponse,
    schemas.Manifest,
    schemas.AuditLine,
    schemas.Anchor,
    schemas.AttackCase,
    schemas.NormalQuestion,
    schemas.TrialResult,
]


@pytest.mark.contract
@pytest.mark.parametrize("model", VERSIONED, ids=lambda m: m.__name__)
def test_schema_version_is_required_and_well_formed(model):
    name = next(n for n, (m, _) in CASES.items() if m is model)
    row = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    for version, ok in (("1.0", True), ("1.7", True), ("2.0", False), ("one", False)):
        row["schema_version"] = version
        if ok:
            model.model_validate(row)
        else:
            with pytest.raises(ValidationError):
                model.model_validate(row)
    del row["schema_version"]
    with pytest.raises(ValidationError):
        model.model_validate(row)


@pytest.mark.contract
@pytest.mark.parametrize("name", sorted(CASES))
def test_unknown_fields_are_rejected(name):
    model, make = CASES[name]
    with pytest.raises(ValidationError, match="extra"):
        model.model_validate({**make(), "surprise": 1})


@pytest.mark.contract
def test_every_tech_spec_schema_is_exported():
    """Independent list: every `class X(BaseModel)` in Tech Spec §3 (EvalKit L-10)."""
    section = SPEC.read_text(encoding="utf-8").split("## 3. Schemas")[1].split("## 4.")[0]
    named = set(re.findall(r"^class (\w+)\(BaseModel\)", section, re.M))
    assert named == {
        "RawResponse",
        "Activity",
        "Drop",
        "Consensus",
        "Manifest",
        "GuardDecision",
        "AuditLine",
        "Anchor",
        "AttackCase",
        "NormalQuestion",
        "TrialResult",
    }
    assert named <= set(schemas.__all__)
