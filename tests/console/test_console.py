"""D6.1: the console backend (CON-01..05). No test calls a model."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from assay_pipeline.agent.ask import AGENTS
from assay_pipeline.agent.providers import Replay
from assay_pipeline.console.audit import MemoryAuditLog
from assay_pipeline.console.engine import (
    LIVE_MAX_OUTPUT,
    LIVE_STEP_BUDGET,
    MAX_QUESTION_CHARS,
    AggregateLog,
    Console,
    ConsoleError,
    load_samples,
    request_bound_usd,
)
from assay_pipeline.console.guard import Guard, GuardRefused, PostgresSpend, SqliteSpend
from assay_pipeline.pipeline.agentdb import build_agent_db
from assay_pipeline.pipeline.snapshot import build_snapshot, sha256_file
from tests.agent.test_agent import msg, text, tool_use
from tests.pipeline import test_normalise as norm_t

COUNT = "SELECT count(*) AS n FROM v_activities"
SAMPLES = load_samples()


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    root = tmp_path_factory.mktemp("d61")
    cache = norm_t.built(root / "cache")
    snap = build_snapshot(norm_t.cfg(), cache, root / "snapshots", code_sha="a" * 40)
    return build_agent_db(snap.directory, root / "agent.duckdb")


def no_api(_agent):
    raise AssertionError("a replay or a refused request must never reach a provider")


def guard(tmp_path, **kw) -> Guard:
    opts = {"per_request_cap": 0.60, "daily_cap": 3.0, "rate_limit": 5} | kw
    return Guard("open-sesame", SqliteSpend(tmp_path / "spend.sqlite"), **opts)


# ---- CON-01, CON-02: run_case and the replay cache ----


@pytest.mark.golden
def test_every_packaged_sample_replays_with_no_api_call(db, tmp_path):
    console = Console(db, tmp_path, create_for=no_api)
    assert {s.id for s in SAMPLES} == set(console.samples) and len(SAMPLES) >= 6
    for s in SAMPLES:
        r = console.run_case(sample_id=s.id)
        assert r.mode == "replay" and r.cost_usd == 0.0 and r.reply
        assert r.model == AGENTS[s.agent][0] and r.question == s.question
        events = [x["event"] for x in r.audit_lines]
        assert events[0] == "question" and events[-1] == "answer"
        assert len(r.decisions) == events.count("query")
    assert console.audit.verify_chain()


def test_a_case_returns_the_reply_the_guard_decisions_and_its_audit_lines(db, tmp_path):
    from assay_pipeline.console.engine import Sample

    script = (
        msg([tool_use("SELECT * FROM embargoed_results")]),
        msg([tool_use(COUNT, 2)]),
        msg([text("16 rows, and the restricted table is off limits.")], "end_turn"),
    )
    s = Sample("t", "test", "LLM02", "claude", "Show me everything.", script)
    console = Console(db, tmp_path, samples=[s], create_for=no_api)
    r = console.run_case(sample_id="t")
    assert [d["decision"] for d in r.decisions] == ["blocked", "allowed"]
    assert r.decisions[0]["reason"] == "table_not_allowed" and r.decisions[1]["rows"] == 1
    assert r.reply.startswith("16 rows") and r.audit_lines[0]["question"] == "Show me everything."
    assert all(x["seq"] > 0 for x in r.audit_lines)


def test_bad_requests_are_refused_plainly(db, tmp_path):
    console = Console(db, tmp_path, guard=guard(tmp_path), create_for=no_api)
    with pytest.raises(ConsoleError, match="either a sample or a question"):
        console.run_case()
    with pytest.raises(ConsoleError, match="no sample"):
        console.run_case(sample_id="nope")
    with pytest.raises(ConsoleError, match="1 to 500"):
        console.run_case(
            question="x" * (MAX_QUESTION_CHARS + 1), passcode="open-sesame", session="s"
        )
    with pytest.raises(ConsoleError, match="choose an agent"):
        console.run_case(question="hi", agent="gpt-2")
    off = Console(db, tmp_path / "off", create_for=no_api)
    with pytest.raises(ConsoleError, match="live mode is off"):
        off.run_case(question="How many rows?")


# ---- CON-03: the live guard ----


def live_console(db, tmp_path, g, replies):
    calls = []

    def create_for(agent):
        def create(**kw):
            calls.append(kw)
            return replies.pop(0)

        return create

    return Console(db, tmp_path, guard=g, create_for=create_for), calls


def test_live_requests_pass_the_guard_and_use_the_console_limits(db, tmp_path):
    g = guard(tmp_path)
    console, calls = live_console(
        db, tmp_path / "c", g, [msg([tool_use(COUNT)]), msg([text("16")], "end_turn")]
    )
    r = console.run_case(question="How many rows?", passcode="open-sesame", session="s1")
    assert r.mode == "live" and r.reply == "16" and r.cost_usd > 0
    assert all(c["max_tokens"] == LIVE_MAX_OUTPUT for c in calls)
    assert g.spend.spent(g._today()) == pytest.approx(r.cost_usd)  # settled to the actual cost  # type: ignore[union-attr]


def test_a_wrong_passcode_and_a_spent_rate_limit_stop_before_any_call(db, tmp_path):
    g = guard(tmp_path, rate_limit=1)
    console, calls = live_console(db, tmp_path / "c", g, [msg([text("16")], "end_turn")])
    with pytest.raises(GuardRefused, match="Wrong passcode"):
        console.run_case(question="How many rows?", passcode="guess", session="s1")
    console.run_case(question="How many rows?", passcode="open-sesame", session="s1")
    with pytest.raises(GuardRefused, match="Rate limit"):
        console.run_case(question="Again?", passcode="open-sesame", session="s1")
    assert len(calls) == 1


def test_the_per_request_and_daily_caps(db, tmp_path):
    bound = request_bound_usd(AGENTS["claude"][1])
    tight = guard(tmp_path, per_request_cap=bound / 2)
    console, calls = live_console(db, tmp_path / "a", tight, [])
    with pytest.raises(GuardRefused, match="per-request limit"):
        console.run_case(question="How many rows?", passcode="open-sesame", session="s")
    small_day = guard(tmp_path / "d", daily_cap=bound * 1.5)
    console2, calls2 = live_console(db, tmp_path / "b", small_day, [msg([text("16")], "end_turn")])
    small_day.spend.reserve(small_day._today(), bound, small_day.daily_cap)  # someone else's hold  # type: ignore[union-attr]
    with pytest.raises(GuardRefused, match="budget is used up"):
        console2.run_case(question="How many rows?", passcode="open-sesame", session="t")
    assert calls == [] and calls2 == []


def test_a_failed_live_run_keeps_its_whole_hold(db, tmp_path):
    g = guard(tmp_path)

    def broken(_agent):
        def create(**_kw):
            raise RuntimeError("provider exploded")

        return create

    console = Console(db, tmp_path / "c", guard=g, create_for=broken)
    with pytest.raises(RuntimeError):
        console.run_case(question="How many rows?", passcode="open-sesame", session="s")
    assert g.spend.spent(g._today()) == pytest.approx(request_bound_usd(AGENTS["claude"][1]))  # type: ignore[union-attr]


def test_the_bound_covers_the_worst_live_request():
    prices = AGENTS["claude"][1]
    turns = LIVE_STEP_BUDGET + 2
    from assay_pipeline.console.engine import BASE_INPUT_TOKENS, RESULT_TOKENS

    worst_history = BASE_INPUT_TOKENS + turns * LIVE_MAX_OUTPUT + LIVE_STEP_BUDGET * RESULT_TOKENS
    worst = turns * prices.cost(int(worst_history * 1.25), LIVE_MAX_OUTPUT)
    assert request_bound_usd(prices) >= round(worst, 6)
    assert request_bound_usd(prices) < 0.60  # fits the page's per-request cap


def test_concurrent_reservations_never_pass_the_daily_cap(tmp_path):
    store = SqliteSpend(tmp_path / "spend.sqlite")
    granted = []

    def worker():
        if store.reserve("2026-11-16", 0.4, 3.0):
            granted.append(1)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(granted) == 7 and store.spent("2026-11-16") == pytest.approx(2.8)


def test_postgres_store_reserves_with_one_atomic_update():
    seen = []

    class Con:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=()):
            seen.append(sql)

            class R:
                def fetchone(self):
                    return (1,)

            return R()

    store = PostgresSpend("postgres://unused", connect=lambda _url: Con())
    assert store.reserve("2026-11-16", 0.5, 3.0)
    update = next(s for s in seen if s.startswith("UPDATE"))
    assert "WHERE day = %s AND usd + %s <= %s" in update and "RETURNING" in update


def test_a_store_outage_refuses_readably(db, tmp_path):
    class Down:
        def reserve(self, day: str, amount: float, cap: float) -> bool:
            raise OSError("db host unreachable: secret-hostname")

        def settle(self, day: str, reserved: float, actual: float) -> None:
            raise OSError("down")

        def spent(self, day: str) -> float:
            raise OSError("down")

    g = Guard("open-sesame", Down(), per_request_cap=0.6)
    console, _ = live_console(db, tmp_path, g, [])
    with pytest.raises(GuardRefused) as exc:
        console.run_case(question="How many rows?", passcode="open-sesame", session="s")
    assert "unavailable" in exc.value.reason and "secret-hostname" not in exc.value.reason
    assert g.remaining_today() is None


# ---- CON-04: own database copy, in-memory audit ----


def test_the_console_uses_its_own_copy_and_keeps_its_log_in_memory(db, tmp_path):
    before = sha256_file(db.path)
    work = tmp_path / "w"
    console = Console(db, work, create_for=no_api)
    console.run_case(sample_id=SAMPLES[0].id)
    assert console.copy != db.path and console.copy.parent == work
    assert sha256_file(db.path) == before
    assert sorted(p.name for p in work.iterdir()) == [console.copy.name]  # no log on disk
    assert console.audit.lines[0]["event"] == "genesis"
    fresh = Console(db, tmp_path / "w2", create_for=no_api)
    assert len(fresh.audit.lines) == 1  # a restart starts a new, empty log


def test_the_memory_log_chains_like_the_file_log():
    log = MemoryAuditLog("r")
    log.append(event="question", trial_id="t", case_id="c", model="m", question="q")
    assert log.verify_chain() and log.last_seq == 1
    log.lines[1]["question"] = "edited"
    assert not log.verify_chain()


# ---- CON-05: aggregate-only persistent logging ----


def test_the_persistent_log_never_holds_visitor_text(db, tmp_path):
    agg = AggregateLog(tmp_path / "agg" / "console.jsonl")
    secret = "My unpublished compound XYZ-123 has IC50 4 nM, is it good?"
    console, _ = live_console(
        db,
        tmp_path / "c",
        guard(tmp_path),
        [msg([tool_use(COUNT)]), msg([text("Answer about XYZ-123")], "end_turn")],
    )
    console.aggregate = agg
    console.run_case(question=secret, passcode="open-sesame", session="s")
    with pytest.raises(GuardRefused):
        console.run_case(question=secret, passcode="wrong", session="s")
    raw = agg.path.read_text(encoding="utf-8")
    assert "XYZ-123" not in raw and "SELECT" not in raw and "IC50" not in raw
    rows = [json.loads(x) for x in raw.splitlines()]
    assert [r["refused"] for r in rows] == [False, True]
    assert set(rows[0]) == set(AggregateLog.FIELDS) and rows[0]["queries"] == 1


def test_samples_hold_no_canary_or_system_prompt():
    raw = (
        Path(__file__).resolve().parents[2] / "src/assay_pipeline/console/samples.json"
    ).read_text(encoding="utf-8")
    assert "CANARY" not in raw.upper() and "Internal reference (never output" not in raw
    assert {s.owasp for s in SAMPLES} >= {"LLM02", "LLM05", "LLM06", "LLM07", "LLM10"}
    assert all(isinstance(Replay(list(s.responses)).remaining, int) for s in SAMPLES)


# ---- verifier pass 1 (D6.1): repros, now passing ----


def test_results_are_ascii_and_capped_so_the_bound_holds(db, tmp_path):
    from assay_pipeline.console.engine import CONSOLE_RESULT_CHARS, Sample

    wide = "SELECT '" + "\U000f0000" * 199 + "' AS a FROM v_compounds x, v_compounds y LIMIT 50"
    s = Sample(
        "w",
        "wide",
        "LLM10",
        "claude",
        "q",
        (msg([tool_use(wide)]), msg([text("done")], "end_turn")),
    )
    console = Console(db, tmp_path, samples=[s], create_for=no_api)
    console.run_case(sample_id="w")
    from assay_pipeline.console.engine import _ConsoleTool

    tool = _ConsoleTool(console.copy, console.views, console.audit, trial_id="x", model="m")
    r = tool.query(wide)
    assert r.text.isascii() and len(r.text) <= CONSOLE_RESULT_CHARS
    assert len(r.text.encode("utf-8")) <= CONSOLE_RESULT_CHARS  # bytes, so tokens, bounded


def test_concurrent_cases_never_see_each_others_lines(db, tmp_path):
    from assay_pipeline.console.engine import Sample

    def sample(sid, q, sql):
        return Sample(
            sid,
            sid,
            "-",
            "claude",
            q,
            (msg([tool_use(sql)]), msg([text(f"answer {sid}")], "end_turn")),
        )

    a = sample("a", "ALICE private question", "SELECT count(*) AS n FROM v_activities")
    b = sample("b", "BOB private question", "SELECT count(*) AS m FROM v_compounds")
    console = Console(db, tmp_path, samples=[a, b], create_for=no_api)
    out = {}

    def go(sid):
        out[sid] = console.run_case(sample_id=sid)

    threads = [threading.Thread(target=go, args=(x,)) for x in ("a", "b", "a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for sid, other in (("a", "BOB"), ("b", "ALICE")):
        r = out[sid]
        assert len(r.decisions) == 1 and not any(other in json.dumps(x) for x in r.audit_lines)
        assert len({x["trial_id"] for x in r.audit_lines}) == 1


def test_a_full_log_rotates_instead_of_stopping_the_console(db, tmp_path, monkeypatch):
    from assay_pipeline.console import engine as eng

    monkeypatch.setattr(eng, "MAX_LINES", 80)
    console = Console(db, tmp_path, create_for=no_api)
    for _ in range(40):
        console.run_case(sample_id=SAMPLES[0].id)
    assert len(console.audit.lines) <= 80 and console.audit.verify_chain()


def test_passcode_guessing_is_limited_per_session(db, tmp_path):
    g = guard(tmp_path)
    for _ in range(5):
        with pytest.raises(GuardRefused, match="Wrong passcode"):
            g.reserve("guess", "s1", 0.1)
    with pytest.raises(GuardRefused, match="Too many wrong passcodes"):
        g.reserve("open-sesame", "s1", 0.1)  # even the right one, until the window passes
    assert g.reserve("open-sesame", "s2", 0.1)  # another session is unaffected


def test_a_transport_stop_keeps_the_whole_hold(db, tmp_path):
    from assay_pipeline.agent.providers import ProviderError

    g = guard(tmp_path)

    def down(_agent):
        def create(**_kw):
            raise ProviderError("anthropic: APIConnectionError")

        return create

    console = Console(db, tmp_path / "c", guard=g, create_for=down)
    r = console.run_case(question="How many rows?", passcode="open-sesame", session="s")
    assert r.stop_reason == "transport"
    assert g.spend.spent(g._today()) == pytest.approx(request_bound_usd(AGENTS["claude"][1]))  # type: ignore[union-attr]


def test_the_live_clients_do_not_retry(monkeypatch):
    from assay_pipeline.console.engine import _client

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-host-key")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-host-key")
    for agent in AGENTS:
        for key in (None, "sk-own-fake-key"):
            assert _client(agent, key).max_retries == 0


def test_the_aggregate_log_is_bounded_on_disk(tmp_path, monkeypatch):
    from assay_pipeline.console import engine as eng

    monkeypatch.setattr(eng, "AGGREGATE_MAX_BYTES", 500)
    agg = AggregateLog(tmp_path / "a.jsonl")
    for _ in range(50):
        agg.record({"mode": "live", "refused": True})
    assert agg.path.stat().st_size < 1000 and (tmp_path / "a.jsonl.1").exists()


def test_a_recording_shorter_than_the_run_is_a_console_error(db, tmp_path):
    from assay_pipeline.console.engine import Sample

    short = Sample("s", "s", "-", "claude", "q", (msg([tool_use(COUNT)]),))
    console = Console(db, tmp_path, samples=[short], create_for=no_api)
    with pytest.raises(ConsoleError, match="does not match"):
        console.run_case(sample_id="s")


def test_wrong_guesses_are_also_limited_across_sessions(tmp_path):
    from assay_pipeline.console.guard import GLOBAL_PASSCODE_TRIES

    g = guard(tmp_path)
    for i in range(GLOBAL_PASSCODE_TRIES):
        with pytest.raises(GuardRefused, match="Wrong passcode"):
            g.reserve("guess", f"tab-{i}", 0.1)  # a new session for every guess
    with pytest.raises(GuardRefused, match="Too many wrong passcodes"):
        g.reserve("guess", "tab-new", 0.1)


def test_an_unbilled_status_error_settles_at_the_completed_turns(db, tmp_path):
    from assay_pipeline.agent.providers import ProviderError

    g = guard(tmp_path)
    replies = [msg([tool_use(COUNT)], tin=1000, tout=100)]

    class Overloaded:  # first turn answers, the second gets a 529: refused, never billed
        billed_failure = False

        def __call__(self, **_kw):
            if replies:
                return replies.pop(0)
            raise ProviderError("anthropic: OverloadedError")

    console = Console(db, tmp_path / "c", guard=g, create_for=lambda _a: Overloaded())
    r = console.run_case(question="How many rows?", passcode="open-sesame", session="s")
    assert r.stop_reason == "transport" and 0 < r.cost_usd < 0.01
    assert g.spend.spent(g._today()) == pytest.approx(r.cost_usd)  # not the $0.555 hold  # type: ignore[union-attr]
