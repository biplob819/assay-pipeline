"""N1.1 verifier findings: every one has a test."""

import copy
import pickle

import pytest
from pydantic import ValidationError

from assay_pipeline.schemas import (
    Activity,
    AttackCase,
    AuditLine,
    Consensus,
    FrozenDict,
    Manifest,
    NormalQuestion,
    TrialResult,
    pic50_of,
)
from tests.schemas import factories as f


def bad(model, row, needle: str = "") -> None:
    with pytest.raises(ValidationError) as exc:
        model.model_validate(row)
    assert needle in str(exc.value), str(exc.value)


@pytest.mark.parametrize("pic50", [7.38724, 9 - 1.6127838567197355, 7.3873])
def test_pic50_must_be_exactly_the_rounded_value(pic50):
    assert pic50_of(41.0) == 7.3872
    bad(Activity, f.activity(pic50=pic50), "rounded to 4 decimals")


def test_pic50_from_the_stored_value_passes_at_awkward_magnitudes():
    for v in (1e-3, 0.123457, 3.0, 12345.7, 1e6):
        Activity.model_validate(f.activity(value_nm=v, pic50=pic50_of(v)))


def test_records_survive_deepcopy_pickle_and_deep_model_copy():
    m = Manifest.model_validate(f.manifest())
    a = Activity.model_validate(f.activity(flags=["QC5"], flag_reasons={"QC5": "censored"}))
    for obj in (m, a):
        assert copy.deepcopy(obj) == obj
        assert copy.copy(obj) == obj
        assert pickle.loads(pickle.dumps(obj)) == obj
        assert obj.model_copy(deep=True) == obj
    copied = copy.deepcopy(m)
    assert isinstance(copied.files, FrozenDict)
    with pytest.raises(TypeError):
        copied.files.clear()
    assert pickle.loads(pickle.dumps(m.files)) == m.files


@pytest.mark.parametrize(
    ("model", "row", "needle"),
    [
        (Manifest, f.manifest(schema_version="1.\u0660"), "schema_version"),
        (Manifest, f.manifest(snapshot="2026-11-19.\u0661"), "snapshot"),
        (AttackCase, f.attack(id="atk-llm02-\u0660\u0661"), "id"),
        (NormalQuestion, f.question(id="nq-\u0660\u0667"), "id"),
        (TrialResult, f.trial(case_id="atk-llm02-\u0660\u0661"), "case_id"),
    ],
)
def test_unicode_digits_are_rejected(model, row, needle):
    bad(model, row, needle)


@pytest.mark.parametrize("planted", [{}, [], "x", 0, {1: "x"}, {"b": b"\xff"}])
def test_llm04_planted_data_must_be_a_non_empty_json_mapping(planted):
    bad(AttackCase, f.poisoning(planted_data=planted))


@pytest.mark.parametrize(
    ("model", "row"),
    [
        (Activity, f.activity(value_nm=True, pic50=None)),
        (Activity, f.activity(value_nm="41")),
        (TrialResult, f.trial(cost_usd=True)),
        (Consensus, f.consensus(median_pic50="7.5")),
    ],
)
def test_floats_reject_booleans_and_strings(model, row):
    bad(model, row)


def test_equal_records_serialise_identically_whatever_the_key_order():
    a = Manifest.model_validate(f.manifest(rows_by_source={"chembl": 1, "pubchem": 2}))
    b = Manifest.model_validate(f.manifest(rows_by_source={"pubchem": 2, "chembl": 1}))
    assert a == b and a.model_dump_json() == b.model_dump_json()


def test_timestamps_are_normalised_to_utc():
    a = Manifest.model_validate(f.manifest(created_at="2026-11-19T13:30:00+05:30"))
    b = Manifest.model_validate(f.manifest(created_at="2026-11-19T08:00:00Z"))
    assert a.model_dump_json() == b.model_dump_json()
    assert '"2026-11-19T08:00:00Z"' in a.model_dump_json()


@pytest.mark.parametrize(
    ("row", "needle"),
    [
        (f.consensus(n_sources=2, n_records=1, member_uids=["0123456789abcdef"]), "n_sources"),
        (
            f.consensus(
                n_records=1,
                n_sources=1,
                member_uids=["0123456789abcdef"],
                spread_pic50=2.0,
                conflict=True,
            ),
            "at least two records",
        ),
    ],
)
def test_consensus_counts_are_consistent(row, needle):
    bad(Consensus, row, needle)


def test_impossible_snapshot_dates_are_rejected():
    bad(Manifest, f.manifest(snapshot="2026-13-45.1"))


@pytest.mark.parametrize("event", ["question", "query", "answer"])
def test_non_genesis_audit_lines_name_the_trial(event):
    bad(AuditLine, f.audit(event, trial_id=None), "names the model and the trial")


@pytest.mark.parametrize(
    ("row", "needle"),
    [
        (f.trial(case_id="atk-llm99-01"), "case_id"),
        (f.trial(stopping_layer="quality_rule"), "only for, LLM04"),
        (f.trial(case_id="atk-llm04-01", stopping_layer="guard"), "only for, LLM04"),
    ],
)
def test_trial_layers_follow_the_case_category(row, needle):
    bad(TrialResult, row, needle)


def test_llm04_trials_stopped_by_quality_rules_pass():
    t = f.trial(case_id="atk-llm04-01", stopping_layer="quality_rule")
    assert TrialResult.model_validate(t).stopping_layer == "quality_rule"
