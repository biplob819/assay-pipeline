"""Q2.1: quality rules QC1–QC5, the consensus table and the LLM04 planted rows."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from assay_pipeline.config import ConfigError, load_build_config
from assay_pipeline.pipeline.normalise import activity_uid, normalise, pic50_for, standardise
from assay_pipeline.pipeline.planted import load_planted, run_case, run_stealth
from assay_pipeline.pipeline.quality import apply_quality, key_of
from assay_pipeline.schemas import Activity
from tests.pipeline import test_normalise as norm_t

SNAP = "2026-11-15.1"
CFG = load_build_config()
SMILES, KEY = "CCOc1ccccc1", standardise("CCOc1ccccc1")[1]
_n = iter(range(10**6))


def act(
    value_nm: float | None = 10.0,
    relation: str = "=",
    *,
    key: str = KEY,
    variant: str = "wild-type/unspecified",
    assay_type: str = "B",
    record: str | None = "auto",
    source: str = "chembl",
    value_original: float | None = "auto",  # type: ignore[assignment]
    units: str | None = "nM",
    snapshot: str = SNAP,
) -> Activity:
    i = next(_n)
    rid = f"r{i}" if record == "auto" else record
    pub = source == "pubchem"
    return Activity(
        activity_uid=activity_uid(source, f"r{i}"),  # type: ignore[arg-type]
        inchikey=key,
        source_inchikey=None,
        smiles=SMILES,
        target="EGFR",
        variant=variant,
        assay_type="U" if pub else assay_type,  # type: ignore[arg-type]
        assay_id="A1",
        assay_description="",
        value_original=value_nm if value_original == "auto" else value_original,
        units_original=units,
        value_nm=value_nm,
        relation=relation,  # type: ignore[arg-type]
        pic50=pic50_for(value_nm),
        source=source,  # type: ignore[arg-type]
        source_record_id=rid,
        depositor="BindingDB" if pub else None,
        chembl_src_id=None,
        data_validity_comment=None,
        document_id=None,
        flags=(),
        flag_reasons={},
        snapshot=snapshot,
    )


def flags(rows):
    return [a.flags for a in apply_quality(rows, CFG).activities]


# ---- QC1 ----


def test_qc1_passes_a_converted_value_and_flags_each_cause():
    rows = [
        act(10.0),
        act(None, value_original=None),
        act(None, value_original=5.0, units=None),
        act(None, value_original=5.0, units="ug.mL-1"),
    ]
    out = apply_quality(rows, CFG).activities
    assert "QC1" not in out[0].flags
    assert all("QC1" in a.flags for a in out[1:])
    assert [a.flag_reasons["QC1"] for a in out[1:]] == [
        "no numeric value",
        "no units",
        "value 5 'ug.mL-1' cannot be converted to nM",
    ]


def test_qc1_boundary_a_zero_value_is_converted_not_flagged():
    (a,) = apply_quality([act(0.0)], CFG).activities
    assert "QC1" not in a.flags and a.pic50 is None


# ---- QC2 ----


def test_qc2_flags_every_member_when_the_spread_is_above_one():
    rows = [act(10.0), act(101.0), act(5.0, ">")]  # pIC50 8.0 and 6.9957; a censored member
    out = flags(rows)
    assert all("QC2" in f for f in out)


def test_qc2_boundary_a_spread_of_exactly_one_is_not_a_conflict():
    assert all("QC2" not in f for f in flags([act(10.0), act(100.0)]))  # 8.0 and 7.0
    # 7.3 − 6.3 is 1.0000000000000009 in floats; compared at pIC50 precision it is 1.0
    hi, lo = act(50.1187), act(501.187)
    assert (hi.pic50, lo.pic50) == (7.3, 6.3)
    assert all("QC2" not in f for f in flags([hi, lo]))


def test_qc2_needs_two_exact_rows_and_the_same_key():
    assert all("QC2" not in f for f in flags([act(10.0), act(100000.0, ">")]))  # one '=' row
    other = standardise("CCCOc1ccccc1")[1]
    assert all("QC2" not in f for f in flags([act(10.0), act(10000.0, key=other)]))
    assert all("QC2" not in f for f in flags([act(10.0), act(10000.0, variant="L858R")]))
    assert all("QC2" not in f for f in flags([act(10.0), act(10000.0, assay_type="F")]))


def test_qc2_never_spans_sources_because_pubchem_rows_are_type_u():
    rows = [act(10.0), act(10000.0, source="pubchem")]
    assert all("QC2" not in f for f in flags(rows))
    keys = {key_of(a) for a in rows}
    assert len(keys) == 2  # the stated consequence: no cross-source QC2 key


# ---- QC3 ----


@pytest.mark.parametrize(
    ("value_nm", "flagged"),
    [
        (1e6, False),  # pIC50 exactly 3
        (0.001, False),  # pIC50 exactly 12
        (1.0002e6, True),  # 2.9999
        (0.00099977, True),  # 12.0001
        (10.0, False),
    ],
)
def test_qc3_bounds_are_inclusive(value_nm, flagged):
    (f,) = flags([act(value_nm)])
    assert ("QC3" in f) == flagged


def test_qc3_applies_to_censored_rows_and_uses_the_configured_bounds():
    (f,) = flags([act(0.00001, "<")])
    assert {"QC3", "QC5"} <= set(f)
    narrow = CFG.model_copy(
        update={"quality": CFG.quality.model_copy(update={"pic50_min": 5.0, "pic50_max": 9.0})}
    )
    (a,) = apply_quality([act(100000.0)], narrow).activities  # pIC50 4
    assert "QC3" in a.flags and "[5, 9]" in a.flag_reasons["QC3"]


# ---- QC4 and QC5 ----


def test_qc4_flags_a_missing_record_id_only():
    out = flags([act(10.0), act(10.0, record=None)])
    assert "QC4" not in out[0] and "QC4" in out[1]


@pytest.mark.parametrize(
    ("relation", "flagged"),
    [("=", False), ("<", True), (">", True), ("<=", True), (">=", True), ("unknown", True)],
)
def test_qc5_flags_every_relation_but_equals(relation, flagged):
    (f,) = flags([act(10.0, relation)])
    assert ("QC5" in f) == flagged


# ---- flags, never delete (D-6) ----


def test_rules_flag_and_never_delete_or_reorder():
    rows = [act(10.0), act(None, value_original=None), act(10.0, record=None), act(5.0, "<")]
    out = apply_quality(rows, CFG).activities
    assert [a.activity_uid for a in out] == [a.activity_uid for a in rows]
    for a in out:
        assert list(a.flags) == sorted(set(a.flags)) and set(a.flag_reasons) == set(a.flags)


def test_reapplying_the_rules_gives_the_same_flags():
    once = apply_quality([act(10.0), act(1000.0), act(5.0, "<")], CFG).activities
    assert apply_quality(once, CFG).activities == once


def test_a_table_may_not_mix_snapshots_even_across_keys():
    with pytest.raises(ValueError, match="mixes snapshots"):
        apply_quality([act(10.0), act(20.0, snapshot="2026-11-16.1")], CFG)
    other = standardise("CCCOc1ccccc1")[1]
    with pytest.raises(ValueError, match="mixes snapshots"):
        apply_quality([act(10.0), act(20.0, key=other, snapshot="2026-11-16.1")], CFG)


# ---- consensus (D-6) ----


def test_consensus_medians_are_over_exact_rows_and_list_every_member():
    rows = [act(10.0), act(100.0), act(1000.0), act(1.0, ">"), act(None, value_original=None)]
    result = apply_quality(rows, CFG)
    (c,) = result.consensus
    assert c.median_pic50 == 7.0 and c.spread_pic50 == 2.0 and c.conflict
    assert c.n_records == 5 and c.n_sources == 1
    assert c.member_uids == tuple(sorted(a.activity_uid for a in rows))


def test_consensus_without_exact_rows_has_no_median():
    (c,) = apply_quality([act(5.0, "<"), act(None, value_original=None)], CFG).consensus
    assert c.median_pic50 is None and c.spread_pic50 is None and not c.conflict


def test_consensus_is_one_row_per_key_sorted():
    other = standardise("CCCOc1ccccc1")[1]
    rows = [act(10.0, key=other), act(10.0), act(10.0, variant="L858R")]
    keys = [
        (c.inchikey, c.target, c.variant, c.assay_type) for c in apply_quality(rows, CFG).consensus
    ]
    assert keys == sorted(keys) and len(keys) == 3


rows_strategy = st.lists(
    st.tuples(
        st.sampled_from([0.001, 0.01, 1.0, 10.0, 50.1187, 100.0, 501.187, 1e4, 1e6, None]),
        st.sampled_from(["=", "=", "=", "<", ">", "unknown"]),
        st.sampled_from(["wild-type/unspecified", "L858R"]),
        st.sampled_from(["B", "F"]),
    ),
    min_size=1,
    max_size=12,
)


@settings(max_examples=300, deadline=None)
@given(rows_strategy)
def test_qc2_flags_exactly_the_keys_whose_consensus_is_in_conflict(spec):
    rows = [act(v, r, variant=var, assay_type=t, value_original=v) for v, r, var, t in spec]
    result = apply_quality(rows, CFG)
    conflicted = {
        (c.inchikey, c.target, c.variant, c.assay_type) for c in result.consensus if c.conflict
    }
    for a in result.activities:
        assert ("QC2" in a.flags) == (key_of(a) in conflicted)
    assert sum(c.n_records for c in result.consensus) == len(rows)


# ---- the recorded sources end to end ----


def test_quality_on_both_recorded_sources(tmp_path):
    base = normalise(norm_t.cfg(), norm_t.built(tmp_path), SNAP).activities
    result = apply_quality(base, CFG)
    by_rule = result.flags_by_rule()
    assert set(by_rule) == {"QC1", "QC2", "QC3", "QC4", "QC5"}
    assert by_rule["QC5"] == 2 + 3  # two censored ChEMBL rows, every PubChem row
    assert by_rule["QC4"] == 0 and len(result.activities) == len(base)
    assert sum(c.n_records for c in result.consensus) == len(base)


# ---- LLM04 planted rows (QCR-07) ----


def test_the_suite_has_ten_cases_over_qc1_to_qc4_and_three_stealth_rows():
    suite = load_planted()
    assert len(suite.cases) == 10 and len(suite.stealth) == 3
    assert {c.rule for c in suite.cases} == {"QC1", "QC2", "QC3", "QC4"}


def test_every_planted_case_is_caught_in_a_scratch_build_of_real_rows(tmp_path):
    base = normalise(norm_t.cfg(), norm_t.built(tmp_path), SNAP).activities
    results = [run_case(c, base, CFG) for c in load_planted().cases]
    assert all(r.passed for r in results), [r for r in results if not r.passed]


def test_stealth_rows_are_known_misses(tmp_path):
    base = normalise(norm_t.cfg(), norm_t.built(tmp_path), SNAP).activities
    assert all(run_stealth(s, base, CFG) == () for s in load_planted().stealth)


@pytest.mark.parametrize("rule", ["QC1", "QC2", "QC3", "QC4"])
def test_a_switched_off_rule_fails_exactly_its_own_planted_cases(monkeypatch, rule):
    from assay_pipeline.pipeline import quality

    real = quality.apply_quality

    def without(rows, cfg):
        out = real(rows, cfg)
        stripped = []
        for a in out.activities:
            data = a.model_dump()
            data["flags"] = tuple(f for f in a.flags if f != rule)
            data["flag_reasons"] = {k: v for k, v in a.flag_reasons.items() if k != rule}
            stripped.append(Activity.model_validate(data))
        return quality.QualityResult(tuple(stripped), out.consensus)

    from assay_pipeline.pipeline import planted

    monkeypatch.setattr(planted, "apply_quality", without)
    suite = load_planted()
    failing = {c.id for c in suite.cases if not run_case(c, [], CFG, SNAP).passed}
    assert failing == {c.id for c in suite.cases if c.rule == rule}


def test_a_planted_structure_may_not_collide_with_a_real_compound():
    case = load_planted().cases[0]
    real = act(10.0, key=standardise(case.rows[0].smiles)[1])
    with pytest.raises(ValueError, match="collides"):
        run_case(case, [real], CFG)


@pytest.mark.parametrize(
    "text",
    [
        "cases: []\nstealth: []\ncases: []\n",
        "cases:\n  - {id: llm04-a, rule: QC5, rows: [{smiles: C, value: 1, units: nM, relation: '=', expect: [QC5]}]}\nstealth: []\n",  # noqa: E501
        "cases:\n  - {id: llm04-a, rule: QC1, rows: []}\nstealth: []\n",
        "cases:\n  - {id: llm04-a, rule: QC1, rows: [{smiles: C, value: 1, units: nM, relation: '=', expect: []}]}\nstealth: []\n",  # noqa: E501
        "cases: []\nstealth:\n  - {id: llm04-s, smiles: C, value: 1, units: nM, relation: '='}\n  - {id: llm04-s, smiles: C, value: 1, units: nM, relation: '='}\n",  # noqa: E501
    ],
    ids=["duplicate-key", "rule-qc5", "no-rows", "no-expect", "duplicate-id"],
)
def test_a_malformed_planted_suite_is_refused(tmp_path, text):
    p = tmp_path / "planted.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_planted(p)


# ---- verifier pass 1 nice-to-haves ----


def test_a_zero_value_has_no_finite_pic50_and_is_out_of_range():
    (a,) = apply_quality([act(0.0)], CFG).activities
    assert "QC1" not in a.flags and "QC3" in a.flags and "infinite" in a.flag_reasons["QC3"]


def test_a_stealth_row_may_not_collide_with_a_real_compound():
    row = load_planted().stealth[0]
    real = act(10.0, key=standardise(row.smiles)[1])
    with pytest.raises(ValueError, match="collides"):
        run_stealth(row, [real], CFG)


@pytest.mark.parametrize(
    "row",
    [
        "{smiles: C, value: 1, units: nM, relation: '=', expect: [QC5]}",  # not its rule
        "{smiles: 'C1CC', value: 1, units: nM, relation: '=', expect: [QC1]}",  # bad SMILES
        "{smiles: C, value: true, units: nM, relation: '=', expect: [QC1]}",
        "{smiles: C, value: '5', units: nM, relation: '=', expect: [QC1]}",
        "{smiles: C, value: 1, units: nM, relation: '=', expect: [QC1, QC1]}",
    ],
    ids=["expect-misses-rule", "bad-smiles", "bool-value", "string-value", "repeated-flag"],
)
def test_a_planted_row_that_could_pass_for_the_wrong_reason_is_refused(tmp_path, row):
    p = tmp_path / "planted.yaml"
    p.write_text(
        f"cases:\n  - {{id: llm04-a, rule: QC1, rows: [{row}]}}\nstealth: []\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load_planted(p)


def test_a_missing_planted_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_planted(tmp_path / "nope.yaml")
