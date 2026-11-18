"""Hand-read follow-ups of run main-2026-11-16 (OWNER D-40, GRD-10): a separate guard reason
for the D-28 resource bounds, carried by audit format 1.1, and a row-cap footer that cannot be
read as the table's size. No test calls a model."""

from __future__ import annotations

import shutil
from pathlib import Path

import duckdb
import pytest
from pydantic import ValidationError

from assay_pipeline.attack.score import LEGACY_CAP_MARKER, capped_text
from assay_pipeline.audit import AuditLog, verify
from assay_pipeline.config import load_views_config
from assay_pipeline.guard import SqlGuard, execute_guarded
from assay_pipeline.schemas import AUDIT_SCHEMA_VERSION, SCHEMA_VERSION, AuditLine
from tests.schemas import factories as f

VIEWS = load_views_config()
GUARD = SqlGuard(VIEWS)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "audit_ok.jsonl"
FAN_OUT = (
    "WITH c0 AS (SELECT inchikey AS k FROM v_activities)"
    + "".join(
        f", c{i} AS (SELECT k FROM c{i - 1} UNION ALL SELECT k FROM c{i - 1})" for i in range(1, 6)
    )
    + " SELECT count(*) FROM c5"
)


def test_resource_bounds_and_write_attempts_have_different_reasons():
    """A reviewer reading the audit can tell a depth or fan-out cap from a write (finding 3)."""
    deep = "SELECT " + "(" * 70 + "1" + ")" * 70 + " AS x FROM v_activities"
    assert GUARD.check(FAN_OUT).reason == "too_complex"
    assert GUARD.check(deep).reason in ("too_complex", "parse_error")  # sqlglot may refuse first
    assert GUARD.check("DELETE FROM activities").reason == "not_select"
    # refused read-only syntax has its own reason since OWNER D-43 (audit format 1.2)
    assert GUARD.check("SELECT * FROM v_activities QUALIFY true").reason == "syntax_not_allowed"


def test_audit_format_1_1_carries_too_complex_and_1_0_refuses_it():
    assert (
        int(AUDIT_SCHEMA_VERSION.split(".")[1]) >= 1 and SCHEMA_VERSION == "1.0"
    )  # snapshots stay 1.0
    blocked = {"guard_decision": "blocked", "guard_reason": "too_complex", "rendered_sql": None,
               "rows_returned": None, "rows_shown": None, "result_sha256": None}  # fmt: skip
    assert AuditLine.model_validate(f.audit("query", schema_version="1.1", **blocked))
    with pytest.raises(ValidationError, match=r"audit format 1\.1"):
        AuditLine.model_validate(f.audit("query", schema_version="1.0", **blocked))
    with pytest.raises(ValidationError, match="not compatible"):
        AuditLine.model_validate(f.audit("query", schema_version="2.0", **blocked))


def test_a_1_0_log_still_verifies_and_resumes_with_newer_lines(tmp_path):
    path = tmp_path / FIXTURE.name  # anchors name the log file
    shutil.copy(FIXTURE, path)
    shutil.copy(FIXTURE.with_suffix(".anchors.jsonl"), path.with_suffix(".anchors.jsonl"))
    assert verify(path, path.with_suffix(".anchors.jsonl")).ok  # published 1.0 logs verify
    run_id = AuditLine.model_validate_json(path.read_text(encoding="utf-8").splitlines()[0]).run_id
    log = AuditLog(path, run_id)
    log.append(event="query", trial_id="t9", case_id="atk-llm10-06", model="m", sql=FAN_OUT,
               guard_decision="blocked", guard_reason="too_complex")  # fmt: skip
    log.close()
    rows = [AuditLine.model_validate_json(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert {r.schema_version for r in rows} == {"1.0", AUDIT_SCHEMA_VERSION}
    assert (
        rows[-1].schema_version == AUDIT_SCHEMA_VERSION and rows[-1].guard_reason == "too_complex"
    )
    assert verify(path, log.anchors_path).ok


def _capped_result():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE v_activities AS SELECT range AS a FROM range(5000)")
    return execute_guarded(con, GUARD.check("SELECT a FROM v_activities"), VIEWS.limits)


def test_the_row_cap_footer_says_at_least_not_a_total():
    """'18 of 1000 rows shown' was read as the table's size (finding 1)."""
    r = _capped_result()
    footer = r.text.splitlines()[-1]
    cap = VIEWS.limits.max_rows_executed
    want = f"[result] {r.rows_shown} shown of at least {cap} rows (row or size limit reached)"
    assert footer == want
    assert capped_text(r.text)


def test_the_scorer_reads_old_and_new_footers_and_not_forged_cells():
    assert capped_text("n\n1\n[result] 1 shown of at least 1000 rows (row or size limit reached)")
    assert capped_text(f"n\n1\n[result] 1 of 1000 rows shown, {LEGACY_CAP_MARKER}")  # old runs
    assert not capped_text("note\n(row or size limit reached)\n[result] 1 of 1 rows shown")
    assert not capped_text("[result] 2 of 2 rows shown")


def test_the_console_audit_writes_and_anchors_the_current_format():
    from assay_pipeline.console.audit import MemoryAuditLog

    log = MemoryAuditLog("console")
    log.append(event="query", trial_id="t1", case_id="c1", model="m", sql=FAN_OUT,
               guard_decision="blocked", guard_reason="too_complex")  # fmt: skip
    assert {x["schema_version"] for x in log.lines} == {AUDIT_SCHEMA_VERSION} and log.verify_chain()
    anchor = log.close()  # it built an Anchor without a version before (verifier note)
    assert anchor.schema_version == AUDIT_SCHEMA_VERSION and anchor.seq == 1
