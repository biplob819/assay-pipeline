"""Hand-read follow-up of run main-2026-11-18 (OWNER D-43, GRD-11): the guard allows an
aggregate's FILTER (WHERE ...) and GROUP BY ROLLUP(...), and names refused read-only syntax
`syntax_not_allowed` (audit format 1.2) so it is no longer reported as "not a SELECT"."""

from __future__ import annotations

import duckdb
import pytest
from pydantic import ValidationError

from assay_pipeline.config import load_views_config
from assay_pipeline.console.audit import MemoryAuditLog
from assay_pipeline.guard import SqlGuard
from assay_pipeline.guard.sql import MAX_ROLLUP_COLUMNS
from assay_pipeline.schemas import AUDIT_SCHEMA_VERSION, AuditLine
from tests.schemas import factories as f

GUARD = SqlGuard(load_views_config())
# Real queries the agents wrote in main-2026-11-18 (nq-17, nq-04 and nq-03, all public) that the
# guard refused as not_select before D-43.
ROLLUP_Q = (
    "SELECT relation, count(*) AS n, sum(CASE WHEN length(flags) > 0 THEN 1 ELSE 0 END) "
    "AS n_flagged FROM v_activities GROUP BY ROLLUP(relation) ORDER BY relation"
)
FILTER_Q = ("SELECT count(*) AS n, count(*) FILTER (WHERE length(flags) > 0) AS n_flagged, "
            "median(pic50) FILTER (WHERE length(flags) = 0) AS med_unflagged "
            "FROM v_activities WHERE relation = '='")  # fmt: skip


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE v_activities (relation VARCHAR, pic50 DOUBLE, flags VARCHAR[])")
    c.execute("INSERT INTO v_activities VALUES ('=', 7, []), ('=', 9, ['QC2']), ('=', 8, []),"
              " ('<', 5, ['QC5']), ('unknown', 6, ['QC5'])")  # fmt: skip
    c.execute("CREATE TABLE embargoed_results AS SELECT 'secret' AS canary")
    return c


@pytest.mark.security
def test_the_agents_filter_and_rollup_queries_are_allowed_and_give_the_right_numbers(con):
    rollup = GUARD.check(ROLLUP_Q)
    assert rollup.decision == "allowed" and rollup.limit_applied == 1000
    rows = con.execute(rollup.rendered_sql).fetchall()
    assert (None, 5, 3) in rows and ("=", 3, 1) in rows  # the ROLLUP total row and a group
    filt = GUARD.check(FILTER_Q)
    assert filt.decision == "allowed" and "FILTER(WHERE" in (filt.rendered_sql or "")
    assert con.execute(filt.rendered_sql).fetchall() == [(3, 1, 7.5)]


@pytest.mark.security
def test_syntax_and_write_attempts_have_different_reasons():
    for sql in ("SELECT * FROM v_activities QUALIFY true",
                "SELECT * FROM (PIVOT v_activities ON source USING count(*))",
                "SELECT relation FROM v_activities GROUP BY CUBE(relation)",
                "SELECT relation FROM v_activities GROUP BY GROUPING SETS ((relation), ())",
                "SELECT CAST(count(*) AS HUGEINT) FROM v_activities",
                "SELECT count(*) OVER () FROM v_activities"):  # fmt: skip
        assert GUARD.check(sql).reason == "syntax_not_allowed", sql
    for sql in ("DELETE FROM v_activities", "SELECT * INTO v_quality FROM v_activities",
                "INSTALL httpfs", "CREATE TABLE t AS SELECT * FROM v_activities",
                # a write behind shallower refused syntax, or in a DML CTE (verifier)
                "WITH d AS (INSERT INTO v_quality SELECT * FROM v_quality RETURNING *) "
                "SELECT * FROM d QUALIFY true",
                "SELECT * FROM v_activities QUALIFY true UNION ALL "
                "SELECT * INTO v_quality FROM v_activities",
                "WITH d AS (DELETE FROM v_activities RETURNING *) SELECT * FROM d",
                "SELECT 1 FROM v_activities FOR UPDATE"):  # fmt: skip
        assert GUARD.check(sql).reason == "not_select", sql


@pytest.mark.security
def test_filter_and_rollup_are_only_what_they_claim_to_be():
    """FILTER only on an aggregate and only with WHERE; ROLLUP only in GROUP BY, bounded."""
    assert GUARD.check("SELECT abs(pic50) FILTER (WHERE pic50 > 1) FROM v_activities").reason == (
        "syntax_not_allowed")  # fmt: skip
    assert GUARD.check("SELECT count(*) FILTER (WHERE true) OVER () FROM v_activities").reason == (
        "syntax_not_allowed")  # fmt: skip
    cols = ["relation", "assay_type", "variant", "source", "snapshot", "inchikey", "flags",
            "pic50", "value_nm"][: MAX_ROLLUP_COLUMNS + 1]  # fmt: skip
    over = f"SELECT count(*) FROM v_activities GROUP BY ROLLUP({', '.join(cols)})"
    at = f"SELECT count(*) FROM v_activities GROUP BY ROLLUP({', '.join(cols[:-1])})"
    assert GUARD.check(over).reason == "too_complex"
    assert GUARD.check(at).decision == "allowed"
    # one ROLLUP per GROUP BY: several multiply (ROLLUP(a), ROLLUP(b) is CUBE(a, b)), and ten
    # of them reached 4 GB before reading a row (verifier); plain columns beside one are fine
    stacked = "SELECT count(*) FROM v_activities GROUP BY ROLLUP(relation), ROLLUP(source)"
    assert GUARD.check(stacked).reason == "too_complex"
    beside = "SELECT relation, count(*) FROM v_activities GROUP BY relation, ROLLUP(source)"
    assert GUARD.check(beside).decision == "allowed"
    for star in ("ROLLUP(*)", "ROLLUP(v_activities.*)"):  # DuckDB reads these as GROUP BY ALL
        sql = f"SELECT relation, count(*) FROM v_activities GROUP BY {star}"
        assert GUARD.check(sql).reason == "syntax_not_allowed"


@pytest.mark.security
def test_nothing_hidden_inside_filter_or_rollup_escapes_the_other_checks(con):
    cases = {
        "SELECT count(*) FILTER (WHERE relation IN (SELECT canary FROM embargoed_results)) "
        "FROM v_activities": "table_not_allowed",
        "SELECT count(*) FILTER (WHERE relation = (SELECT max(x) FROM read_csv('a.csv'))) "
        "FROM v_activities": "function_not_allowed",
        "SELECT count(*) FILTER (WHERE current_user = 'x') FROM v_activities": (
            "function_not_allowed"),
        "SELECT relation FROM v_activities "
        "GROUP BY ROLLUP(relation, (SELECT max(canary) FROM embargoed_results))": (
            "table_not_allowed"),
        "SELECT count(*) FILTER (WHERE relation IN (SELECT table_name FROM "
        "information_schema.tables)) FROM v_activities": "catalog_access",
    }  # fmt: skip
    for sql, reason in cases.items():
        assert (GUARD.check(sql).decision, GUARD.check(sql).reason) == ("blocked", reason), sql


def test_a_1_1_log_resumes_with_1_2_lines_and_still_verifies(tmp_path, monkeypatch):
    """A run started by v0.1.1 or for main-2026-11-18 (format 1.1) resumes under 1.2."""
    from assay_pipeline.audit import AuditLog, verify
    from assay_pipeline.audit import log as log_module

    path = tmp_path / "run.jsonl"
    with monkeypatch.context() as m:
        m.setattr(log_module, "AUDIT_SCHEMA_VERSION", "1.1")
        old = AuditLog(path, "r1")
        old.append(event="query", trial_id="t1", case_id="c1", model="m", sql="SELECT ?",
                   guard_decision="blocked", guard_reason="too_complex")  # fmt: skip
        old.close()
    resumed = AuditLog(path, "r1")
    resumed.append(event="query", trial_id="t2", case_id="c1", model="m", sql="SELECT ?",
                   guard_decision="blocked", guard_reason="syntax_not_allowed")  # fmt: skip
    resumed.close()
    rows = [AuditLine.model_validate_json(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert {r.schema_version for r in rows} == {"1.1", "1.2"}
    assert rows[-1].guard_reason == "syntax_not_allowed" and verify(path, resumed.anchors_path).ok


def test_audit_format_1_2_carries_syntax_not_allowed_and_1_1_refuses_it():
    assert AUDIT_SCHEMA_VERSION == "1.2"
    blocked = {"guard_decision": "blocked", "guard_reason": "syntax_not_allowed",
               "rendered_sql": None, "rows_returned": None, "rows_shown": None,
               "result_sha256": None}  # fmt: skip
    assert AuditLine.model_validate(f.audit("query", schema_version="1.2", **blocked))
    with pytest.raises(ValidationError, match=r"audit format 1\.2"):
        AuditLine.model_validate(f.audit("query", schema_version="1.1", **blocked))
    too = {**blocked, "guard_reason": "too_complex"}
    assert AuditLine.model_validate(f.audit("query", schema_version="1.1", **too))  # unchanged
    log = MemoryAuditLog("console")
    log.append(event="query", trial_id="t1", case_id="c1", model="m", sql="SELECT ?",
               guard_decision="blocked", guard_reason="syntax_not_allowed")  # fmt: skip
    assert log.verify_chain() and log.close().schema_version == "1.2"
