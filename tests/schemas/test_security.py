import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from assay_pipeline.schemas import GUARD_REASONS, Anchor, AuditLine, GuardDecision
from tests.schemas import factories as f

SPEC = Path(__file__).resolve().parents[2] / "Trusted Assay Pipeline Tech Spec.md"


def bad(model, row, needle: str) -> None:
    with pytest.raises(ValidationError) as exc:
        model.model_validate(row)
    assert needle in str(exc.value), str(exc.value)


def test_guard_decisions_parse():
    assert GuardDecision.model_validate(f.guard_allowed()).limit_applied == 1000
    assert GuardDecision.model_validate(f.guard_blocked()).reason == "table_not_allowed"


@pytest.mark.parametrize(
    ("row", "needle"),
    [
        (f.guard_allowed(reason="not_select"), "allowed decision"),
        (f.guard_allowed(rendered_sql=None), "allowed decision"),
        (f.guard_allowed(limit_applied=None), "allowed decision"),
        (f.guard_blocked(reason=None), "blocked decision"),
        (f.guard_blocked(rendered_sql="SELECT 1"), "blocked decision"),
        (f.guard_blocked(limit_applied=1000), "blocked decision"),
        (f.guard_blocked(reason="looked_suspicious"), "reason"),
    ],
)
def test_guard_decision_pairing(row, needle):
    bad(GuardDecision, row, needle)


def test_guard_reasons_match_tech_spec_section_6_1():
    """Independent list: the reason codes in the §6.1 table, in order (EvalKit L-10)."""
    text = SPEC.read_text(encoding="utf-8")
    section = text.split("### 6.1 SQL guard")[1].split("### 6.2")[0]
    codes: list[str] = []
    for cell in re.findall(r"^\| \d \| .+? \| (.+?) \|$", section, re.M):
        codes += re.findall(r"`([a-z_]+)`", cell)
    assert tuple(codes) == GUARD_REASONS


@pytest.mark.parametrize("event", ["genesis", "question", "query", "answer"])
def test_audit_lines_parse(event):
    assert AuditLine.model_validate(f.audit(event)).event == event


def test_blocked_and_failed_query_lines_parse():
    blocked = f.audit(
        "query",
        guard_decision="blocked",
        guard_reason="table_not_allowed",
        rendered_sql=None,
        rows_returned=None,
        rows_shown=None,
        result_sha256=None,
    )
    assert AuditLine.model_validate(blocked).guard_reason == "table_not_allowed"
    failed = f.audit(
        "query", rows_returned=None, rows_shown=None, result_sha256=None, db_error_class="timeout"
    )
    assert AuditLine.model_validate(failed).db_error_class == "timeout"


@pytest.mark.parametrize(
    ("row", "needle"),
    [
        (f.audit("genesis", seq=1), "seq 0 is the genesis line"),
        (f.audit("question", seq=0), "seq 0 is the genesis line"),
        (f.audit("genesis", prev_hash=f.H), "zero prev_hash"),
        (f.audit("genesis", model="m"), "only run_id"),
        (f.audit("genesis", sql="SELECT 1"), "only run_id"),
        (f.audit("question", model=None), "names the model"),
        (f.audit("question", question=None), "question lines only"),
        (f.audit("answer", question="leaked text"), "question lines only"),
        (f.audit("answer", answer_sha256=None), "answer lines only"),
        (f.audit("question", sql="SELECT 1"), "no SQL or guard fields"),
        (f.audit("answer", guard_decision="allowed"), "no SQL or guard fields"),
        (f.audit("query", sql=None), "model's SQL and the guard decision"),
        (f.audit("query", guard_decision=None), "model's SQL and the guard decision"),
        (
            f.audit(
                "query",
                guard_decision="blocked",
                rendered_sql=None,
                rows_returned=None,
                rows_shown=None,
                result_sha256=None,
            ),
            "names the guard reason",
        ),
        (
            f.audit("query", guard_decision="blocked", guard_reason="not_select"),
            "never reached the database",
        ),
        (f.audit("query", guard_reason="not_select"), "no guard reason"),
        (f.audit("query", rendered_sql=None), "no guard reason"),
        (f.audit("query", db_error_class="timeout"), "returned no result"),
        (f.audit("query", result_sha256=None), "records rows_returned"),
        (f.audit("query", rows_shown=5, rows_returned=2), "cannot exceed"),
        (f.audit("query", hash="xyz"), "hash"),
    ],
)
def test_audit_line_rules(row, needle):
    bad(AuditLine, row, needle)


def test_anchor_rules():
    assert Anchor.model_validate(f.anchor()).seq == 42
    bad(Anchor, f.anchor(seq=-1), "seq")
    bad(Anchor, f.anchor(at="2026-11-19T08:00:00"), "timezone")
