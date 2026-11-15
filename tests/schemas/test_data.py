import pytest
from pydantic import ValidationError

from assay_pipeline.schemas import (
    Activity,
    Consensus,
    Drop,
    FrozenDict,
    Manifest,
    RawResponse,
    pic50_of,
)
from tests.schemas import factories as f


def bad(model, row, needle: str) -> None:
    with pytest.raises(ValidationError) as exc:
        model.model_validate(row)
    assert needle in str(exc.value), str(exc.value)


def test_valid_rows_parse():
    assert Activity.model_validate(f.activity()).pic50 == 7.3872
    assert Activity.model_validate(f.pubchem_activity()).depositor == "BindingDB"
    assert Consensus.model_validate(f.consensus()).conflict is False
    assert Manifest.model_validate(f.manifest()).planted is False
    assert RawResponse.model_validate(f.raw_response()).status == 200
    assert Drop(source="pubchem", source_record_id="1:2", stage="ingest", reason="not_ic50")


def test_pic50_of_rounds_to_four_decimals():
    assert pic50_of(41.0) == 7.3872
    assert pic50_of(1.0) == 9.0
    assert pic50_of(10_000.0) == 5.0


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"pic50": None}, "pic50 is required"),
        ({"pic50": 7.5}, "does not equal 9"),
        ({"value_nm": None, "pic50": 7.3872}, "pic50 must be null"),
        ({"value_nm": 0.0, "pic50": 7.0}, "pic50 must be null"),
        ({"assay_type": "U"}, "assay_type 'U'"),
        ({"depositor": "ChEMBL"}, "depositor is set for, and only for, PubChem"),
        ({"flags": ["QC5", "QC1"], "flag_reasons": {"QC1": "x", "QC5": "y"}}, "sorted and unique"),
        ({"flags": ["QC1", "QC1"], "flag_reasons": {"QC1": "x"}}, "sorted and unique"),
        ({"flags": ["QC1"], "flag_reasons": {}}, "one entry per flag"),
        ({"flags": [], "flag_reasons": {"QC3": "x"}}, "one entry per flag"),
        ({"flags": ["QC9"], "flag_reasons": {"QC9": "x"}}, "QC5"),
        ({"inchikey": "not-a-key"}, "inchikey"),
        ({"snapshot": "2026-11-19"}, "snapshot"),
        ({"snapshot": "2026-11-19.0"}, "snapshot"),
        ({"activity_uid": "XYZ"}, "activity_uid"),
        ({"relation": "~"}, "relation"),
        ({"value_nm": -1.0, "pic50": None}, "value_nm"),
        ({"value_nm": float("nan"), "pic50": None}, "value_nm"),
    ],
)
def test_activity_rules(over, needle):
    bad(Activity, f.activity(**over), needle)


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"assay_type": "B"}, "assay_type 'U'"),
        ({"depositor": None}, "depositor is set for, and only for, PubChem"),
        ({"chembl_src_id": 7}, "ChEMBL-only"),
        ({"data_validity_comment": "Outside typical range"}, "ChEMBL-only"),
    ],
)
def test_pubchem_activity_rules(over, needle):
    bad(Activity, f.pubchem_activity(**over), needle)


def test_unknown_relation_is_allowed_for_either_source():
    row = f.activity(relation="unknown", flags=["QC5"], flag_reasons={"QC5": "no relation"})
    assert Activity.model_validate(row).relation == "unknown"


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"median_pic50": None}, "both null or both set"),
        ({"spread_pic50": 1.5}, "conflict must be true"),
        ({"spread_pic50": 1.0, "conflict": True}, "conflict must be true"),
        ({"member_uids": ["0123456789abcdef", "0123456789abcdef"]}, "unique"),
        ({"n_records": 3}, "n_records must equal"),
        ({"n_sources": 3}, "n_sources"),
        ({"conflict": "yes"}, "conflict"),
    ],
)
def test_consensus_rules(over, needle):
    bad(Consensus, f.consensus(**over), needle)


def test_consensus_spread_exactly_one_is_not_a_conflict():
    assert Consensus.model_validate(f.consensus(spread_pic50=1.0)).conflict is False
    row = f.consensus(spread_pic50=1.0001, conflict=True)
    assert Consensus.model_validate(row).conflict is True


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"flags_by_rule": {"QC1": 1}}, "flags_by_rule must have exactly"),
        ({"content_sha256": {"other.parquet": f.H}}, "same paths"),
        ({"files": {}, "content_sha256": {}}, "at least one file"),
        ({"files": {"a.parquet": "ABC"}, "content_sha256": {"a.parquet": f.H}}, "files"),
        ({"planted": "false"}, "planted"),
        ({"code_sha": "xyz"}, "code_sha"),
    ],
)
def test_manifest_rules(over, needle):
    bad(Manifest, f.manifest(**over), needle)


def test_manifest_planted_has_no_default():
    row = f.manifest()
    del row["planted"]
    bad(Manifest, row, "planted")


def test_records_are_deeply_immutable_and_hashable():
    a = Activity.model_validate(f.activity(flags=["QC5"], flag_reasons={"QC5": "censored"}))
    with pytest.raises(ValidationError):
        a.pic50 = 1.0  # type: ignore[misc]
    assert isinstance(a.flag_reasons, FrozenDict)
    with pytest.raises(TypeError):
        a.flag_reasons["QC1"] = "x"  # type: ignore[index]
    assert isinstance(a.flags, tuple)
    m = Manifest.model_validate(f.manifest())
    with pytest.raises(TypeError):
        m.files["x"] = f.H  # type: ignore[index]
    assert hash(a) == hash(Activity.model_validate(a.model_dump()))


def test_drop_codes_are_snake_case():
    bad(
        Drop,
        {"source": "chembl", "source_record_id": None, "stage": "ingest", "reason": "Not IC50"},
        "reason",
    )
