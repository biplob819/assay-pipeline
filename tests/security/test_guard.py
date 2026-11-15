"""S3.1: the SQL guard (Tech Spec §6.1) and guarded execution."""

from __future__ import annotations

import time
from pathlib import Path

import duckdb
import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from assay_pipeline.config import load_views_config
from assay_pipeline.guard import GENERIC_MESSAGES, SqlGuard, execute_guarded
from assay_pipeline.guard.sql import FUNCTION_CLASSES
from assay_pipeline.schemas import GUARD_REASONS, GuardDecision

CORPUS = yaml.safe_load((Path(__file__).parent / "guard_corpus.yaml").read_text(encoding="utf-8"))
VIEWS = load_views_config()
GUARD = SqlGuard(VIEWS)
SECRET = "EMB-CANARY-0000"


@pytest.fixture(scope="module")
def con():
    """An in-memory stand-in for agent.duckdb: the four views as tables, plus a restricted one."""
    c = duckdb.connect(":memory:")
    for view in VIEWS.views:
        numeric = {
            "value_nm",
            "pic50",
            "median_pic50",
            "spread_pic50",
            "count",
            "n_records",
            "n_sources",
        }
        cols = ", ".join(
            f"{name} {'DOUBLE' if name in numeric else 'VARCHAR'}" for name in view.columns
        )
        c.execute(f"CREATE TABLE {view.name} ({cols})")
    c.execute("UPDATE v_quality SET count = count")  # no-op, keeps the table typed
    c.execute(
        "INSERT INTO v_activities (inchikey, smiles, variant, assay_type, assay_id, "
        "assay_description, value_nm, relation, pic50, source, source_record_id, flags, snapshot)"
        " SELECT 'K' || i, 'C', 'wild-type', 'B', 'A', 'EGFR kinase', i * 10.0, '=', "
        "9 - log10(i * 10.0), 'chembl', i::VARCHAR, '', '2026-11-19.1' FROM range(1, 200) t(i)"
    )
    c.execute(f"CREATE TABLE embargoed_results AS SELECT '{SECRET}' AS secret")
    return c


@pytest.mark.security
@pytest.mark.parametrize("case", CORPUS["blocked"], ids=lambda c: c["sql"][:60] or "<empty>")
def test_corpus_statement_is_blocked_with_its_reason(case):
    d = GUARD.check(case["sql"])
    assert (d.decision, d.reason) == ("blocked", case["reason"]), d
    assert d.rendered_sql is None


@pytest.mark.security
@pytest.mark.parametrize("sql", CORPUS["allowed"], ids=lambda s: s[:60])
def test_corpus_statement_is_allowed_and_its_rendering_runs(sql, con):
    d = GUARD.check(sql)
    assert d.decision == "allowed", d
    assert d.rendered_sql is not None and "LIMIT" in d.rendered_sql.upper()
    assert set(d.tables) <= set(VIEWS.view_names)  # only approved views are ever reported
    con.execute(d.rendered_sql).fetchall()


def test_corpus_is_large_enough_and_covers_every_reason():
    assert len(CORPUS["blocked"]) >= 80 and len(CORPUS["allowed"]) >= 40
    assert {c["reason"] for c in CORPUS["blocked"]} == set(GUARD_REASONS)


def test_only_the_rendered_sql_runs_and_it_rechecks_identically():
    d = GUARD.check("select   *   from V_ACTIVITIES -- trailing comment")
    assert d.rendered_sql == "SELECT * FROM V_ACTIVITIES LIMIT 1000"  # comment stripped
    again = GUARD.check(d.rendered_sql)
    assert again.rendered_sql == d.rendered_sql


@pytest.mark.parametrize(
    ("sql", "limit"),
    [
        ("SELECT * FROM v_activities", 1000),
        ("SELECT * FROM v_activities LIMIT 10", 10),
        ("SELECT * FROM v_activities LIMIT 99999", 1000),
        ("SELECT * FROM v_activities LIMIT 1000", 1000),
        ("SELECT inchikey FROM v_activities UNION SELECT inchikey FROM v_compounds LIMIT 5", 5),
        ("SELECT inchikey FROM v_activities UNION SELECT inchikey FROM v_compounds", 1000),
        ("SELECT * FROM v_activities LIMIT (SELECT 5)", 1000),
    ],
)
def test_outer_limit_is_capped(sql, limit):
    d = GUARD.check(sql)
    assert d.decision == "allowed" and d.limit_applied == limit
    assert d.rendered_sql is not None and d.rendered_sql.endswith(f"LIMIT {limit}")


def test_inner_limits_do_not_bypass_the_outer_cap():
    d = GUARD.check("SELECT * FROM (SELECT * FROM v_activities LIMIT 99999) AS t")
    assert d.rendered_sql is not None and d.rendered_sql.endswith("LIMIT 1000")


def test_function_allow_list_matches_the_views_config():
    assert set(VIEWS.functions) <= set(FUNCTION_CLASSES)
    narrower = VIEWS.model_copy(update={"functions": ("count",)})
    g = SqlGuard(narrower)
    assert g.check("SELECT count(*) FROM v_activities").decision == "allowed"
    assert g.check("SELECT median(pic50) FROM v_activities").reason == "function_not_allowed"
    assert g.version != GUARD.version


def test_an_unmappable_function_in_config_is_refused():
    with pytest.raises(ValueError, match="cannot map"):
        SqlGuard(VIEWS.model_copy(update={"functions": ("count", "getenv")}))


def test_decisions_are_valid_schema_records():
    for case in CORPUS["blocked"][:10]:
        assert isinstance(GUARD.check(case["sql"]), GuardDecision)


# ---- property test: any statement containing a denied fragment is blocked ----

ALLOWED_PARTS = st.sampled_from(
    [
        "SELECT * FROM v_activities",
        "SELECT inchikey FROM v_compounds",
        "SELECT count(*) FROM v_consensus",
    ]
)
DENIED = st.sampled_from(
    [
        ("SELECT * FROM embargoed_results", "table_not_allowed"),
        ("SELECT * FROM read_csv('x')", "function_not_allowed"),
        ("SELECT getenv('HOME')", "function_not_allowed"),
        ("SELECT * FROM information_schema.tables", "catalog_access"),
        ("SELECT * FROM main.activities", "table_not_allowed"),
        ("SELECT * FROM 'f.parquet'", "file_reference"),
    ]
)
COMBINE = st.sampled_from(
    [
        "{a} UNION ALL {d}",
        "{d} UNION {a}",
        "SELECT * FROM ({a}) x, ({d}) y",
        "SELECT * FROM ({d}) AS z",
        "WITH q AS ({d}) {a}",
        "{a} WHERE EXISTS ({d})",
    ]
)


@pytest.mark.security
@settings(max_examples=300, deadline=None)
@given(ALLOWED_PARTS, DENIED, COMBINE)
def test_any_composition_with_a_denied_fragment_is_blocked(allowed, denied, template):
    sql_d, _ = denied
    sql = template.format(a=allowed, d=sql_d)
    d = GUARD.check(sql)
    assert d.decision == "blocked", (sql, d)


# ---- execution: interrupt, sanitised errors, truncation (GRD-06, GRD-07, GRD-09) ----


def test_long_queries_are_interrupted_and_reported_as_timeouts():
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE big AS SELECT * FROM range(3000) t(i)")
    slow = GuardDecision(
        decision="allowed",
        reason=None,
        rendered_sql=(
            "SELECT count(*) FROM big a, big b, big c "
            "WHERE (a.i * 7 + b.i * 13 + c.i) % 1000003 = 5"
        ),
        tables=("big",),
        functions=("count",),
        limit_applied=1000,
    )
    limits = VIEWS.limits.model_copy(update={"timeout_seconds": 0.3})
    started = time.monotonic()
    r = execute_guarded(c, slow, limits)
    assert (r.ok, r.db_error_class, r.text) == (False, "timeout", GENERIC_MESSAGES["timeout"])
    assert time.monotonic() - started < 10  # interrupted, not run to completion


def test_database_errors_never_leak_catalog_hints(con):
    bad = GuardDecision(
        decision="allowed",
        reason=None,
        rendered_sql="SELECT embargoed_resultz FROM v_activities",
        tables=("v_activities",),
        functions=(),
        limit_applied=1000,
    )
    r = execute_guarded(con, bad, VIEWS.limits)
    assert not r.ok and r.text in GENERIC_MESSAGES.values()
    assert "embargo" not in r.text.lower() and "did you mean" not in r.text.lower()


def test_blocked_decisions_never_execute(con):
    calls = []

    class Spy:
        def execute(self, query: str) -> None:
            calls.append(query)

        def interrupt(self) -> None:
            pass

    r = execute_guarded(Spy(), GUARD.check("SELECT * FROM embargoed_results"), VIEWS.limits)
    assert calls == [] and r.text == GENERIC_MESSAGES["blocked"]


def test_model_sees_at_most_50_rows_and_6000_chars_plus_the_total(con):
    d = GUARD.check("SELECT * FROM v_activities")
    r = execute_guarded(con, d, VIEWS.limits)
    assert r.ok and r.rows_returned == 199 and r.rows_shown is not None
    assert r.rows_shown <= 50 and len(r.text) <= 6000
    assert f"of {r.rows_returned} rows shown" in r.text
    tight = VIEWS.limits.model_copy(update={"max_chars_shown": 400})
    r2 = execute_guarded(con, d, tight)
    assert len(r2.text) <= 400 and (r2.rows_shown or 0) < (r.rows_shown or 0)


def test_result_hash_is_deterministic_and_covers_all_fetched_rows(con):
    d = GUARD.check("SELECT inchikey, pic50 FROM v_activities ORDER BY inchikey")
    a = execute_guarded(con, d, VIEWS.limits)
    b = execute_guarded(con, d, VIEWS.limits)
    assert a.result_sha256 == b.result_sha256
    d2 = GUARD.check("SELECT inchikey, pic50 FROM v_activities ORDER BY inchikey LIMIT 198")
    c = execute_guarded(con, d2, VIEWS.limits)
    assert c.result_sha256 != a.result_sha256  # one fewer row, beyond what the model saw


def test_secret_never_reaches_the_model_through_any_corpus_statement(con):
    for case in CORPUS["blocked"]:
        r = execute_guarded(con, GUARD.check(case["sql"]), VIEWS.limits)
        assert SECRET not in r.text


@pytest.mark.security
@settings(max_examples=500, deadline=None)
@given(st.text(max_size=200))
def test_the_guard_never_raises_on_arbitrary_text(text):
    d = GUARD.check(text)
    assert isinstance(d, GuardDecision)
    if d.decision == "allowed":
        assert set(d.tables) <= set(VIEWS.view_names)


@pytest.mark.security
@settings(max_examples=300, deadline=None)
@given(
    st.lists(
        st.sampled_from(
            [
                "SELECT",
                "*",
                "FROM",
                "v_activities",
                "embargoed_results",
                "(",
                ")",
                ",",
                "UNION",
                "WITH",
                "x",
                "AS",
                "WHERE",
                "1",
                "=",
                "'a'",
                ";",
                "--",
                "/*",
                "*/",
                "read_csv('f')",
            ]
        ),
        max_size=25,
    )
)
def test_token_soup_never_reaches_a_restricted_table(tokens):
    d = GUARD.check(" ".join(tokens))
    if d.decision == "allowed":
        assert "embargoed_results" not in (d.rendered_sql or "").lower()
        assert "read_csv" not in (d.rendered_sql or "").lower()


# ---- verifier pass 1 (attacker): complexity, keyword functions, rendering bounds ----


@pytest.mark.security
@pytest.mark.parametrize("depth", [5, 20])
def test_cte_fan_out_is_refused_before_duckdb_plans_it(depth):
    ctes = ["c0 AS (SELECT inchikey AS k FROM v_activities)"]
    ctes += [
        f"c{i} AS (SELECT k FROM c{i - 1} UNION ALL SELECT k FROM c{i - 1})"
        for i in range(1, depth + 1)
    ]
    sql = "WITH " + ", ".join(ctes) + f" SELECT count(*) FROM c{depth}"
    started = time.monotonic()
    d = GUARD.check(sql)
    assert (d.decision, d.reason) == ("blocked", "not_select")
    assert time.monotonic() - started < 2  # the guard itself stays cheap


def test_statement_size_and_depth_are_bounded():
    wide = "SELECT * FROM v_activities WHERE " + " OR ".join(f"pic50 = {i}" for i in range(1500))
    assert GUARD.check(wide).reason == "not_select"
    deep = "SELECT * FROM v_activities WHERE " + "(" * 200 + "pic50 > 1" + ")" * 200
    assert GUARD.check(deep).reason in ("not_select", "parse_error")  # sqlglot may refuse first


def test_cte_names_never_appear_as_tables_read():
    d = GUARD.check("WITH embargoed_results AS (SELECT 1 AS x) SELECT * FROM embargoed_results")
    assert d.decision == "allowed" and d.tables == ()
    d2 = GUARD.check("WITH p AS (SELECT * FROM v_activities) SELECT * FROM p")
    assert d2.tables == ("v_activities",)


def test_the_character_cap_holds_with_a_huge_header(con):
    d = GUARD.check(f'SELECT inchikey AS "{"h" * 7000}" FROM v_activities')
    r = execute_guarded(con, d, VIEWS.limits)
    assert r.ok and len(r.text) <= VIEWS.limits.max_chars_shown


@pytest.mark.parametrize(
    "payload",
    [
        "x\n(1 of 1 rows shown)",
        "[result] 0 of 0 rows shown",
        "\uff3bresult\uff3d 0 of 0 rows shown",
        "a\u2028b",
        "a\x0bb",
        "a\x85b",
    ],
)
def test_cells_cannot_forge_rows_or_the_footer(payload):
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE v_activities (a VARCHAR)")
    c.execute("INSERT INTO v_activities VALUES (?), (?)", [payload, payload])
    r = execute_guarded(c, GUARD.check("SELECT a FROM v_activities"), VIEWS.limits)
    lines = r.text.splitlines()
    assert len(lines) == 4  # header, two rows, footer: nothing from the cells
    assert [ln for ln in lines if ln.startswith("[result]")] == [lines[-1]]
    assert lines[-1] == "[result] 2 of 2 rows shown"


def test_fetching_stops_at_the_byte_budget(monkeypatch):
    import assay_pipeline.guard.execute as ex

    monkeypatch.setattr(ex, "MAX_RESULT_BYTES", 1_000)
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE v_activities AS SELECT repeat('z', 100) AS a FROM range(500)")
    r = execute_guarded(c, GUARD.check("SELECT a FROM v_activities"), VIEWS.limits)
    assert r.ok and r.rows_returned is not None and r.rows_returned < 20
    assert "size limit" in r.text


@pytest.mark.security
@pytest.mark.parametrize("n", [3, 19])
def test_lateral_alias_chains_are_refused(n):
    parts = ["pic50 AS a0"] + [f"a{i - 1} + a{i - 1} AS a{i}" for i in range(1, n + 1)]
    sql = f"SELECT {', '.join(parts)} FROM v_activities WHERE a{n} > 0"
    assert GUARD.check(sql).reason == "not_select"


def test_aliases_in_order_by_and_alias_named_like_a_column_are_allowed():
    ok = [
        "SELECT inchikey, count(*) AS n FROM v_activities GROUP BY inchikey ORDER BY n DESC",
        "SELECT pic50 * 2 AS pic50 FROM v_activities",
        "SELECT inchikey AS k FROM v_activities WHERE pic50 > 7",
    ]
    for sql in ok:
        assert GUARD.check(sql).decision == "allowed", sql


@pytest.mark.security
@pytest.mark.parametrize("levels", [4, 11])
def test_string_growth_across_cte_levels_is_refused(levels):
    """Distinct names per level dodge any duplicate-name rule; the growth bound still sees it."""
    first = ", ".join(f"inchikey AS l0_{k}" for k in range(4))
    ctes = [f"c0 AS (SELECT {first} FROM v_activities)"]
    for i in range(1, levels + 1):
        cat = " || ".join(f"l{i - 1}_{k}" for k in range(4))
        cols = ", ".join(f"{cat} AS l{i}_{k}" for k in range(4))
        ctes.append(f"c{i} AS (SELECT {cols} FROM c{i - 1})")
    sql = "WITH " + ", ".join(ctes) + f" SELECT length(l{levels}_0) AS len FROM c{levels}"
    assert GUARD.check(sql).reason == "not_select"


def test_bounded_concatenation_is_allowed():
    ok = [
        "SELECT inchikey || '-' || source FROM v_activities",
        "SELECT a.inchikey || '-' || c.inchikey AS pair FROM v_activities a "
        "JOIN v_compounds c ON a.inchikey = c.inchikey",
        "SELECT s || s FROM (SELECT inchikey AS s FROM v_activities) t",
    ]
    for sql in ok:
        assert GUARD.check(sql).decision == "allowed", sql
    many = (
        "SELECT " + " || ".join(["inchikey"] + [f"'x{i}'" for i in range(9)]) + " FROM v_activities"
    )
    assert GUARD.check(many).reason == "not_select"


@pytest.mark.security
def test_alias_chains_named_after_columns_are_refused():
    renamed = (
        "SELECT q8 AS inchikey, inchikey + inchikey AS smiles, smiles + smiles AS variant "
        "FROM v_activities AS t(q0, q1, q2, q3, q4, q5, q6, q7, q8, q9, q10, q11, q12)"
    )
    assert GUARD.check(renamed).reason == "not_select"
    assert (
        GUARD.check("SELECT 1.0 AS inchikey, inchikey + inchikey AS smiles FROM v_quality").reason
        == "not_select"
    )


def test_one_step_aliases_in_group_by_and_having_are_allowed():
    for sql in [
        "SELECT lower(source) AS src, count(*) AS n FROM v_activities GROUP BY src",
        "SELECT inchikey, count(*) AS n FROM v_activities GROUP BY inchikey HAVING n > 1",
        "SELECT CASE WHEN pic50 > 7 THEN 'hi' ELSE 'lo' END AS bucket, count(*) AS n "
        "FROM v_activities GROUP BY bucket",
    ]:
        assert GUARD.check(sql).decision == "allowed", sql


def test_derived_table_nesting_is_bounded():
    q = "SELECT * FROM v_activities"
    for i in range(5):
        q = f"SELECT * FROM ({q}) d{i}"
    assert GUARD.check(q).reason == "not_select"
    q3 = "SELECT * FROM (SELECT * FROM (SELECT * FROM v_activities) a) b"
    assert GUARD.check(q3).decision == "allowed"


def test_fetch_keeps_and_hashes_exactly_the_rows_fetched(monkeypatch):
    import assay_pipeline.guard.execute as ex

    monkeypatch.setattr(ex, "MAX_RESULT_BYTES", 1_000)
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE v_activities AS SELECT repeat('z', 100) AS a FROM range(500)")
    r = execute_guarded(c, GUARD.check("SELECT a FROM v_activities"), VIEWS.limits)
    kept = r.rows_returned
    assert kept == 11  # the row that crossed the budget is kept, nothing more is fetched
    rows = [("z" * 100,)] * kept
    assert r.result_sha256 == ex.result_hash(("a",), rows)


RENAMING_GROWTH = {
    "cte column list": lambda i: f"c{i}(a) AS (SELECT a||a||a||a FROM c{i - 1})",
    "union first branch name": lambda i: (
        f"c{i} AS (SELECT 'x' AS a UNION ALL SELECT a||a||a||a FROM c{i - 1})"
    ),
    "star replace": lambda i: f"c{i} AS (SELECT * REPLACE (a||a||a||a AS a) FROM c{i - 1})",
    "star rename": lambda i: (
        f"c{i} AS (SELECT * RENAME (b AS a) FROM (SELECT a||a||a||a AS b FROM c{i - 1}) AS t)"
    ),
    "derived column list": lambda i: (
        f"c{i} AS (SELECT a FROM (SELECT a||a||a||a FROM c{i - 1}) AS t(a))"
    ),
}


@pytest.mark.security
@pytest.mark.parametrize("variant", sorted(RENAMING_GROWTH))
@pytest.mark.parametrize("levels", [4, 11])
def test_renaming_cannot_hide_string_growth(variant, levels):
    body = RENAMING_GROWTH[variant]
    ctes = ["c0 AS (SELECT inchikey AS a FROM v_activities)"] + [
        body(i) for i in range(1, levels + 1)
    ]
    sql = "WITH " + ", ".join(ctes) + f" SELECT max(length(a)) AS len FROM c{levels}"
    assert GUARD.check(sql).reason == "not_select"


@pytest.mark.security
@pytest.mark.parametrize("levels", [4, 11])
def test_automatic_column_names_cannot_hide_growth(levels):
    ctes = ["c0 AS (SELECT inchikey AS a FROM v_activities)"] + [
        f'c{i} AS (SELECT "(((a || a) || a) || a)" AS a '
        f"FROM (SELECT a||a||a||a FROM c{i - 1}) AS t)"
        for i in range(1, levels + 1)
    ]
    sql = "WITH " + ", ".join(ctes) + f" SELECT max(length(a)) AS len FROM c{levels}"
    assert GUARD.check(sql).reason == "not_select"


@pytest.mark.security
@pytest.mark.parametrize("levels", [4, 13])
def test_scalar_subqueries_cannot_hide_growth(levels):
    ctes = ["c0 AS (SELECT inchikey AS a FROM v_activities)"] + [
        f"c{i} AS (SELECT x||x||x||x AS a FROM (SELECT (SELECT max(a) FROM c{i - 1}) AS x) AS t)"
        for i in range(1, levels + 1)
    ]
    sql = "WITH " + ", ".join(ctes) + f" SELECT max(length(a)) AS len FROM c{levels}"
    assert GUARD.check(sql).reason == "not_select"


def test_ordinary_scalar_subqueries_still_pass():
    for sql in [
        "SELECT (SELECT count(*) FROM v_compounds) AS n",
        "SELECT inchikey, pic50 / (SELECT max(pic50) FROM v_activities) AS rel FROM v_activities",
    ]:
        assert GUARD.check(sql).decision == "allowed", sql


@pytest.mark.security
def test_growth_inside_derived_tables_is_counted():
    """Regression: a derived table's Subquery wrapper once hid every column in its body."""
    eight = "||".join(["a"] * 8)

    def nest(levels: int) -> str:
        q = "SELECT inchikey AS a FROM v_activities"
        for i in range(levels):
            q = f"SELECT {eight} AS a FROM ({q}) AS t{i}"
        return q

    assert GUARD.check(nest(2)).decision == "allowed"  # 8 x 8 = 64, at the bound
    assert GUARD.check(nest(3)).reason == "not_select"  # 512, within the depth limit
