import pytest
from pydantic import ValidationError

from assay_pipeline.schemas import AttackCase, NormalQuestion, TrialResult
from tests.schemas import factories as f


def bad(model, row, needle: str) -> None:
    with pytest.raises(ValidationError) as exc:
        model.model_validate(row)
    assert needle in str(exc.value), str(exc.value)


def test_cases_parse():
    assert AttackCase.model_validate(f.attack()).owasp == "LLM02"
    assert AttackCase.model_validate(f.poisoning()).attacker_sql == ()
    secondary = f.attack(id="atk-llm01-03", owasp="LLM01", owasp_secondary=["LLM07"])
    assert AttackCase.model_validate(secondary).owasp_secondary == ("LLM07",)
    assert NormalQuestion.model_validate(f.question()).kind == "stat"


@pytest.mark.parametrize(
    ("row", "needle"),
    [
        (f.attack(id="atk-llm05-01"), "does not match category"),
        (f.attack(id="attack-1"), "id"),
        (f.attack(id="atk-llm03-01"), "id"),
        (f.attack(owasp_secondary=["LLM02"]), "must not repeat"),
        (f.attack(owasp_secondary=["LLM07", "LLM07"]), "owasp_secondary must be unique"),
        (f.attack(owasp_secondary=["LLM11"]), "owasp_secondary"),
        (f.attack(expected_stop=[]), "at least one layer"),
        (f.attack(expected_stop=["guard", "guard"]), "expected_stop must be unique"),
        (f.attack(attacker_sql=[]), "lists the SQL"),
        (f.attack(expected_stop=["quality_rule"]), "only LLM04"),
        (f.poisoning(planted_data=None), "plants data"),
        (f.attack(id="atk-llm04-02", owasp="LLM04", planted_data={"rows": []}), "plants data"),
        (
            f.attack(
                id="atk-llm04-02",
                owasp="LLM04",
                planted_data={"rows": []},
                attacker_sql=[],
                expected_stop=["guard"],
            ),
            "stopped by the quality rules",
        ),
        (f.attack(split="private"), "split"),
    ],
)
def test_attack_case_rules(row, needle):
    bad(AttackCase, row, needle)


@pytest.mark.parametrize(
    ("row", "needle"),
    [
        (f.question(gold_sql=None), "gold_sql is required"),
        (f.question(kind="censored", gold_sql=None), "gold_sql is required"),
        (f.question(kind="ambiguous"), "gold_sql is required"),
        (f.question(kind="off_scope"), "gold_sql is required"),
        (f.question(rules=[]), "at least one behaviour rule"),
        (f.question(rules=["B1", "B1"]), "rules must be unique"),
        (f.question(rules=["B11"]), "rules"),
        (f.question(id="q-1"), "id"),
    ],
)
def test_normal_question_rules(row, needle):
    bad(NormalQuestion, row, needle)


def test_trials_parse():
    assert TrialResult.model_validate(f.trial()).stopping_layer == "guard"
    leak = f.trial(outcome="succeeded", stopping_layer="leak")
    assert TrialResult.model_validate(leak).outcome == "succeeded"
    normal = f.trial(case_id="nq-01", outcome=None, stopping_layer=None, answer_correct=True)
    assert TrialResult.model_validate(normal).answer_correct is True
    ambiguous = f.trial(case_id="nq-02", outcome=None, stopping_layer=None)
    assert TrialResult.model_validate(ambiguous).answer_correct is None
    void = f.trial(status="void", outcome=None, stopping_layer=None)
    assert TrialResult.model_validate(void).status == "void"


@pytest.mark.parametrize(
    ("row", "needle"),
    [
        (f.trial(status="transport"), "carries no outcome"),
        (
            f.trial(status="void", outcome=None, stopping_layer=None, answer_correct=False),
            "carries no outcome",
        ),
        (f.trial(outcome=None), "outcome and a stopping layer"),
        (f.trial(stopping_layer=None), "outcome and a stopping layer"),
        (f.trial(answer_correct=True), "outcome and a stopping layer"),
        (f.trial(outcome="succeeded"), "'leak' exactly when"),
        (f.trial(stopping_layer="leak"), "'leak' exactly when"),
        (f.trial(case_id="nq-01"), "no attack outcome"),
        (f.trial(behaviour={"B11": True}), "unknown behaviour rules"),
        (f.trial(audit_seq=[9, 3]), "first <= last"),
        (f.trial(trial=0), "trial"),
        (f.trial(cost_usd=-0.01), "cost_usd"),
        (
            f.trial(answer_correct="yes", case_id="nq-01", outcome=None, stopping_layer=None),
            "answer_correct",
        ),
    ],
)
def test_trial_result_rules(row, needle):
    bad(TrialResult, row, needle)
