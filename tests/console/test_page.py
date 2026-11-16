"""D6.2: the console page (DEM-01..04). No test calls a model."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from assay_pipeline.agent.ask import AGENTS
from assay_pipeline.agent.loop import Prices
from assay_pipeline.agent.providers import Replay
from assay_pipeline.console.engine import Console, request_bound_usd
from assay_pipeline.console.guard import DAILY_CAP_USD, Guard, PostgresSpend, SqliteSpend
from assay_pipeline.console.page import (
    BUDGET_TTL_S,
    GENERIC_ERROR,
    BudgetLine,
    Settings,
    SettingsError,
    audit_view,
    budget_line,
    build_console,
    decision_rows,
    live_bound_usd,
    money,
    sample_label,
)
from tests.agent.test_agent import msg, text, tool_use
from tests.console.test_console import COUNT, SAMPLES, db, no_api  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "apps/console/streamlit_app.py"
KEYS = {"ANTHROPIC_API_KEY": "fake-key-for-tests", "OPENAI_API_KEY": "fake-key-for-tests"}
STORE = "postgresql://nobody@127.0.0.1:9/none"
LIVE_ENV = {"ASSAY_CONSOLE_PASSCODE": "open-sesame", "ASSAY_CONSOLE_DATABASE_URL": STORE, **KEYS}


# ---- settings and wiring (DEM-02, DEM-04) ----


def test_live_needs_the_passcode_the_spend_store_and_a_key():
    s = Settings.from_env({})
    assert s.db == Path("agent.duckdb") and s.daily_cap == DAILY_CAP_USD and not s.live
    assert "it needs a passcode, a spend store and a provider key" in s.live_off_reason()
    for drop in LIVE_ENV:
        if drop in KEYS:
            continue
        assert not Settings.from_env({k: v for k, v in LIVE_ENV.items() if k != drop}).live
    assert not Settings.from_env({k: v for k, v in LIVE_ENV.items() if k not in KEYS}).live
    only_claude = Settings.from_env({**LIVE_ENV, "OPENAI_API_KEY": " "})
    assert only_claude.live and only_claude.agents == ("claude",)
    assert not Settings.from_env({**LIVE_ENV, "ASSAY_CONSOLE_PASSCODE": "   "}).live
    assert Settings.from_env({**LIVE_ENV, "ASSAY_CONSOLE_PASSCODE": "open-sesame\n"}).passcode == (
        "open-sesame"
    )


def test_the_daily_cap_is_validated_and_secrets_stay_out_of_the_repr():
    assert Settings.from_env({"ASSAY_CONSOLE_DAILY_CAP": "1.5"}).daily_cap == 1.5
    for bad in ("lots", "0", "-1", "0.10", "5000", "nan", "inf"):
        with pytest.raises(SettingsError):
            Settings.from_env({"ASSAY_CONSOLE_DAILY_CAP": bad})
    r = repr(Settings.from_env(LIVE_ENV))
    assert "open-sesame" not in r and "nobody@" not in r


def test_the_per_request_cap_is_the_largest_agents_bound():
    assert live_bound_usd() == max(request_bound_usd(p) for _m, p, _k in AGENTS.values())
    assert live_bound_usd() == pytest.approx(0.555)
    table = {"cheap": ("m1", Prices(1.0, 1.0), "K1"), "dear": ("m2", Prices(9.0, 30.0), "K2")}
    assert live_bound_usd(table) == request_bound_usd(Prices(9.0, 30.0))
    assert money(0.555) == "$0.56" and money(3.0) == "$3.00" and money(0.551) == "$0.56"


def test_build_console_wires_live_only_with_every_secret(db, tmp_path, monkeypatch):  # noqa: F811
    with pytest.raises(SettingsError, match="ASSAY_CONSOLE_DB") as missing:
        build_console(Settings.from_env({"ASSAY_CONSOLE_DB": str(tmp_path / "none.duckdb")}))
    assert str(tmp_path) not in str(missing.value)  # no host paths for visitors
    base = {"ASSAY_CONSOLE_DB": str(db.path)}
    off = build_console(Settings.from_env(base), tmp_path / "a")
    assert off.guard is None
    half = build_console(Settings.from_env({**base, "ASSAY_CONSOLE_PASSCODE": "x"}), tmp_path / "b")
    assert half.guard is None  # a passcode alone never wires a guard on an empty store URL
    on = build_console(Settings.from_env({**base, **LIVE_ENV}), tmp_path / "c")
    assert on.guard is not None and isinstance(on.guard.spend, PostgresSpend)
    assert on.guard.per_request_cap == live_bound_usd()
    monkeypatch.setitem(sys.modules, "psycopg", None)  # the console extra is not installed
    s = Settings.from_env({**base, **LIVE_ENV})
    no_client = build_console(s, tmp_path / "d")
    assert no_client.guard is None and "client is missing" in budget_line(no_client, s)


def test_a_damaged_database_is_a_clean_settings_error(db, tmp_path):  # noqa: F811
    import shutil

    copy = tmp_path / "agent.duckdb"
    shutil.copy(db.path, copy)  # no build record beside it
    with pytest.raises(SettingsError, match="integrity") as err:
        build_console(Settings.from_env({"ASSAY_CONSOLE_DB": str(copy)}), tmp_path / "w")
    assert str(tmp_path) not in str(err.value)


# ---- the budget line (DEM-02) ----


class _CountingSpend(SqliteSpend):
    calls = 0

    def spent(self, day: str) -> float:
        type(self).calls += 1
        return super().spent(day)


def test_the_budget_line_shows_the_hold_what_is_left_and_when_it_is_used_up(db, tmp_path):  # noqa: F811
    spend = _CountingSpend(tmp_path / "spend.sqlite")
    g = Guard("open-sesame", spend, per_request_cap=live_bound_usd(), daily_cap=3.0)
    console = Console(db, tmp_path / "w", guard=g, create_for=no_api)
    now = [0.0]
    line = BudgetLine(clock=lambda: now[0])
    assert line(console) == ("Each live request is held at up to $0.56 and settled to its actual "
                             "cost; about $3.00 of today's $3.00 budget is left.")  # fmt: skip
    spend.reserve(g._today(), 1.25, 3.0)
    for _ in range(5):  # reruns inside the window reuse the reading: no new connection
        assert "$3.00 of today's" in line(console)
    assert _CountingSpend.calls == 1
    now[0] = BUDGET_TTL_S + 1
    assert "about $1.75 of today's $3.00 budget is left" in line(console)
    spend.reserve(g._today(), 1.45, 3.0)  # $0.30 left: less than one request's hold
    line.invalidate()
    assert "today's $3.00 budget is used up" in line(console)
    assert _CountingSpend.calls == 3


# ---- decisions beside audit lines (DEM-01, DEM-03) ----


def test_each_decision_sits_beside_the_audit_line_it_wrote(db, tmp_path):  # noqa: F811
    console = Console(db, tmp_path, create_for=no_api)
    blocked = set()
    for s in SAMPLES:
        r = console.run_case(sample_id=s.id)
        rows = decision_rows(r)
        queries = [x for x in r.audit_lines if x["event"] == "query"]
        assert len(rows) == len(r.decisions) == len(queries)
        for row, line in zip(rows, queries, strict=True):
            assert (row["sql"], row["decision"]) == (line["sql"], line["guard_decision"])
            assert row["audit_seq"] == str(line["seq"]) and line["hash"].startswith(
                row["audit_hash"]
            )
            if row["decision"] == "blocked":
                blocked.add(s.id)
        view = audit_view(r)
        assert all("question" not in v and "sql" not in v for v in view)
        assert all(isinstance(c, str) for v in view for c in v.values())  # never NaN on the page
        assert [v["seq"] for v in view] == [str(x["seq"]) for x in r.audit_lines]
    # the packaged scripted demos show the guard blocking; the recorded runs were refused or
    # allowed by the models themselves
    assert blocked == {"guard-restricted", "guard-stacked"}
    assert all(sample_label(s).startswith(s.owasp) for s in SAMPLES)
    assert all(SAMPLES_BY_ID[i].title.startswith("Scripted, no model:") for i in blocked)


SAMPLES_BY_ID = {s.id: s for s in SAMPLES}


# ---- no telemetry and no tracebacks (DEM-04) ----


def test_streamlit_loads_the_config_with_statistics_off_and_no_error_details(monkeypatch):
    from streamlit import config

    assert (ROOT / ".streamlit/config.toml").read_text(encoding="utf-8") == (
        APP.parent / ".streamlit/config.toml"
    ).read_text(encoding="utf-8")  # the same file where Community Cloud and Streamlit look
    monkeypatch.chdir(ROOT / "docs")  # not the repo root: only the file beside the app applies
    monkeypatch.setattr(config, "_main_script_path", str(APP))
    try:
        config.get_config_options(force_reparse=True)
        assert config.get_option("browser.gatherUsageStats") is False
        assert config.get_option("client.showErrorDetails") == "none"
        assert config.get_option("logger.hideWelcomeMessage") is True
    finally:
        monkeypatch.undo()
        config.get_config_options(force_reparse=True)


# ---- the page itself, under Streamlit's AppTest ----


def _app(monkeypatch, db_path, **env):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    st.cache_resource.clear()
    for k in ("ASSAY_CONSOLE_PASSCODE", "ASSAY_CONSOLE_DATABASE_URL", "ASSAY_CONSOLE_DAILY_CAP",
              "ASSAY_CONSOLE_WORKDIR", *KEYS):  # fmt: skip
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ASSAY_CONSOLE_DB", str(db_path))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    return at


def test_replay_shows_the_picked_samples_reply_decisions_and_audit_lines(db, tmp_path, monkeypatch):  # noqa: F811
    at = _app(monkeypatch, db.path)
    assert not at.exception and [t.label for t in at.tabs] == ["Replay a recorded attack", "Live"]
    assert len(at.selectbox[0].options) == len(SAMPLES)
    sample = SAMPLES_BY_ID["guard-restricted"]
    at.selectbox[0].select(sample).run()
    assert sample.question in [t.value for t in at.text]
    at.button(key="replay").click().run()
    assert not at.exception and not at.error
    want = Console(db, tmp_path, create_for=no_api).run_case(sample_id=sample.id)
    assert want.reply in [t.value for t in at.text]  # this sample's reply, as plain text
    decisions = at.dataframe[0].value
    assert list(decisions["decision"]) == ["blocked"]
    assert list(decisions["reason"]) == ["table_not_allowed"]
    assert [h.value for h in at.subheader] == ["Guard decisions", "New audit lines"]
    assert "Live mode is off" in at.info[0].value and "Replay mode always works" in at.info[0].value


def _live(db, tmp_path, monkeypatch, create_for):  # noqa: F811
    """The page with a working live mode: a local spend store and a scripted provider."""
    import assay_pipeline.console.page as page

    guard = Guard("open-sesame", SqliteSpend(tmp_path / "spend.sqlite"),
                  per_request_cap=live_bound_usd(), daily_cap=3.0)  # fmt: skip
    console = Console(db, tmp_path / "w", guard=guard, create_for=create_for)
    seen = []
    real = console.run_case

    def spy(**kw):
        seen.append(kw)
        return real(**kw)

    console.run_case = spy  # type: ignore[method-assign]
    monkeypatch.setattr(page, "build_console", lambda settings, workdir=None: console)
    return _app(monkeypatch, db.path, **LIVE_ENV), seen, console


HOSTILE = "<script>alert(1)</script> **not bold** <b>x</b>"


def test_live_runs_the_typed_question_for_the_chosen_agent_and_shows_text_only(
    db,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    agents = []

    def create_for(agent):
        agents.append(agent)
        return Replay([msg([tool_use(COUNT)]), msg([text(HOSTILE)], "end_turn")])

    at, seen, console = _live(db, tmp_path, monkeypatch, create_for)
    assert not at.exception
    assert at.radio[0].options == ["claude-sonnet-5-5", "gpt-6.1-sol"]
    assert at.text_input[0].proto.type == at.text_input[0].proto.PASSWORD
    assert at.text_area[0].max_chars == 500
    assert "about $3.00 of today's $3.00 budget is left" in at.info[0].value
    at.text_area[0].input("How many rows are there?")
    at.text_input[0].input("open-sesame")
    at.button(key="ask").click().run()
    assert not at.exception and not at.error
    assert seen[-1] == {"question": "How many rows are there?", "agent": "claude",
                        "session": seen[-1]["session"], "passcode": "open-sesame"}  # fmt: skip
    assert len(seen[-1]["session"]) == 32 and agents == ["claude"]
    assert HOSTILE in [t.value for t in at.text]  # the reply is plain text...
    assert all(HOSTILE not in m.value and "alert(1)" not in m.value for m in at.markdown)  # ...only
    spent = 3.0 - console.guard.remaining_today()  # type: ignore[union-attr, operator]
    assert 0 < spent < 0.05  # settled to the actual cost, not the $0.555 hold


def test_a_wrong_passcode_is_refused_and_an_unexpected_error_shows_no_traceback(
    db,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    def boom(_agent):
        raise RuntimeError("provider client could not start: /secret/host/path")

    at, _seen, _console = _live(db, tmp_path, monkeypatch, boom)
    at.text_area[0].input("How many rows are there?")
    at.text_input[0].input("wrong")
    at.button(key="ask").click().run()
    assert not at.exception and at.error[0].value == "Wrong passcode."
    at.text_input[0].input("open-sesame")
    at.button(key="ask").click().run()
    assert not at.exception and at.error[0].value == GENERIC_ERROR
    assert all("/secret/host/path" not in e.value for e in at.error)


def test_the_page_reports_a_missing_database_instead_of_crashing(tmp_path, monkeypatch):
    at = _app(monkeypatch, tmp_path / "missing.duckdb")
    assert not at.exception and "ASSAY_CONSOLE_DB" in at.error[0].value


def test_live_mode_says_why_it_is_off_and_offers_only_keyed_agents(db, monkeypatch):  # noqa: F811
    at = _app(monkeypatch, db.path, **{k: v for k, v in LIVE_ENV.items() if k not in KEYS})
    assert not at.exception and "it needs a provider key" in at.info[0].value
    assert not at.radio and not at.text_input
