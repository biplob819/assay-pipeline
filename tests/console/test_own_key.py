"""DEM-05 (OWNER D-37): live mode on the visitor's own provider key. The passcode, the session
rate limit and the per-request cap still apply; nothing is held against the host's budget; the
key reaches only that request's SDK client and never the environment, a log or the page.
No test calls a model."""

from __future__ import annotations

import json
import os

import pytest

from assay_pipeline.agent.ask import AGENTS
from assay_pipeline.agent.providers import ProviderError
from assay_pipeline.console.engine import (
    KEY_REFUSED,
    AggregateLog,
    Console,
    ConsoleError,
    _LiveCall,
    check_key,
)
from assay_pipeline.console.guard import NO_HOSTED, Guard, GuardRefused
from tests.agent.test_agent import msg, text, tool_use
from tests.console.test_console import COUNT, db, guard, no_api  # noqa: F401  (fixture)

OWN = "sk-own-test-key-0123456789abcdef"  # not a real key: shape only


def _own_console(db, tmp_path, g, script=None):  # noqa: F811
    """A console whose provider records which key each request was built with."""
    seen: list[tuple[str, str | None]] = []

    def create_for(agent, api_key=None):
        seen.append((agent, api_key))
        replies = list(script or [msg([tool_use(COUNT)]), msg([text("16 rows.")], "end_turn")])
        return lambda **_kw: replies.pop(0)

    agg = AggregateLog(tmp_path / "agg.jsonl")
    return Console(db, tmp_path / "w", guard=g, create_for=create_for, aggregate=agg), seen, agg


# ---- the guard ----


def test_an_own_key_request_still_needs_the_passcode_and_holds_no_budget(tmp_path):
    g = guard(tmp_path)
    with pytest.raises(GuardRefused, match="Wrong passcode"):
        g.admit_own_key("guess", "s", 0.5)
    g.admit_own_key("open-sesame", "s", 0.5)
    assert g.spend.spent(g._today()) == 0.0  # type: ignore[union-attr]


def test_own_key_requests_keep_the_rate_limit_and_the_per_request_cap(tmp_path):
    g = guard(tmp_path, rate_limit=2)
    with pytest.raises(GuardRefused, match="per-request limit"):
        g.admit_own_key("open-sesame", "s", 0.61)
    for _ in range(2):
        g.admit_own_key("open-sesame", "s", 0.5)
    with pytest.raises(GuardRefused, match="Rate limit"):
        g.admit_own_key("open-sesame", "s", 0.5)
    with pytest.raises(GuardRefused, match="Rate limit"):  # the host's key shares the limit
        g.reserve("open-sesame", "s", 0.5)


def test_a_guard_with_no_spend_store_serves_own_keys_only(tmp_path):
    g = Guard("open-sesame", None, per_request_cap=0.6)
    g.admit_own_key("open-sesame", "s", 0.5)
    with pytest.raises(GuardRefused, match="Wrong passcode"):  # the passcode is still first
        g.reserve("guess", "s", 0.5)
    with pytest.raises(GuardRefused) as no_hosted:
        g.reserve("open-sesame", "s", 0.5)
    assert no_hosted.value.reason == NO_HOSTED
    assert g.remaining_today() is None


# ---- the engine ----


def test_the_key_goes_to_that_requests_client_and_nothing_is_held(db, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ANTHROPIC_API_KEY", "host-key-unchanged")
    monkeypatch.setenv("OPENAI_API_KEY", "host-key-unchanged")
    g = guard(tmp_path)
    console, seen, agg = _own_console(db, tmp_path, g)
    r = console.run_case(question="How many rows?", passcode="open-sesame", session="s",
                         api_key=f"  {OWN}\n")  # fmt: skip
    assert seen == [("claude", OWN)]  # trimmed, and only to this request's client
    assert r.mode == "live" and r.reply == "16 rows." and r.cost_usd > 0
    assert g.spend.spent(g._today()) == 0.0  # type: ignore[union-attr]
    assert os.environ["ANTHROPIC_API_KEY"] == os.environ["OPENAI_API_KEY"] == "host-key-unchanged"
    assert all(OWN not in v for v in os.environ.values())
    assert OWN not in json.dumps([dict(x) for x in r.audit_lines]) and OWN not in repr(r)
    assert OWN not in json.dumps(console.audit.lines)
    rows = [json.loads(x) for x in agg.path.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["key"] == "own" and OWN not in agg.path.read_text(encoding="utf-8")


def test_a_host_key_request_is_logged_as_host_and_holds_the_budget(db, tmp_path):  # noqa: F811
    g = guard(tmp_path)
    console, seen, agg = _own_console(db, tmp_path, g)
    r = console.run_case(question="How many rows?", passcode="open-sesame", session="s")
    assert seen == [("claude", None)]
    assert g.spend.spent(g._today()) == pytest.approx(r.cost_usd)  # type: ignore[union-attr]
    assert json.loads(agg.path.read_text(encoding="utf-8").splitlines()[-1])["key"] == "host"


def test_a_refused_own_key_request_never_builds_a_client(db, tmp_path):  # noqa: F811
    g = guard(tmp_path)
    console, seen, agg = _own_console(db, tmp_path, g)
    with pytest.raises(GuardRefused, match="Wrong passcode"):
        console.run_case(question="How many rows?", passcode="nope", session="s", api_key=OWN)
    assert seen == []
    row = json.loads(agg.path.read_text(encoding="utf-8").splitlines()[-1])
    assert row["refused"] is True and row["key"] == "own"


@pytest.mark.parametrize("bad", ["has space", "x" * 513, "kéy", "tab\there"])
def test_a_key_of_the_wrong_shape_is_refused_without_repeating_it(bad):
    with pytest.raises(ConsoleError) as err:
        check_key(bad)
    assert bad not in str(err.value) and "does not look like an API key" in str(err.value)
    assert check_key(f" {OWN} ") == OWN


def test_a_key_the_provider_rejects_is_a_plain_message(db, tmp_path):  # noqa: F811
    g = guard(tmp_path)

    class Rejected:
        billed_failure = False
        last_error = "AuthenticationError"

        def __call__(self, **_kw):
            raise ProviderError("anthropic: AuthenticationError")

    console = Console(db, tmp_path / "w", guard=g, create_for=lambda _a, _k=None: Rejected())
    with pytest.raises(ConsoleError) as err:
        console.run_case(question="How many rows?", passcode="open-sesame", session="s",
                         api_key=OWN)  # fmt: skip
    assert str(err.value) == KEY_REFUSED and OWN not in str(err.value)


def test_live_needs_a_guard_even_with_an_own_key(db, tmp_path):  # noqa: F811
    off = Console(db, tmp_path, create_for=no_api)
    with pytest.raises(ConsoleError, match="live mode is off"):
        off.run_case(question="How many rows?", passcode="open-sesame", api_key=OWN)


# ---- the SDK clients ----


def test_the_live_client_gets_the_key_directly_and_does_not_chain_sdk_errors(monkeypatch):
    import anthropic
    import openai

    built: list[dict] = []

    def fake_client(sdk_error):
        class Client:
            def __init__(self, **kw):
                built.append(kw)
                self.messages = self.responses = self

            def create(self, **_kw):
                raise sdk_error

        return Client

    import httpx2

    req = httpx2.Request("POST", "https://example.invalid")
    resp = httpx2.Response(401, request=req)
    a_err = anthropic.AuthenticationError(f"bad key {OWN}", response=resp, body=None)
    o_err = openai.AuthenticationError(f"Incorrect API key provided: {OWN}", response=resp,
                                       body=None)  # fmt: skip
    monkeypatch.setattr(anthropic, "Anthropic", fake_client(a_err))
    monkeypatch.setattr(openai, "OpenAI", fake_client(o_err))
    for agent in AGENTS:
        call = _LiveCall(agent, OWN)
        assert OWN not in repr(call)
        with pytest.raises(ProviderError) as err:
            call(model="m")
        assert OWN not in str(err.value) and err.value.__cause__ is None
        assert err.value.__context__ is None  # not even the implicit context holds the request
        assert call.last_error == "AuthenticationError"
    assert [b["api_key"] for b in built] == [OWN, OWN]
    assert all(b["max_retries"] == 0 for b in built)
    host = _LiveCall("claude")
    with pytest.raises(ProviderError):
        host(model="m")
    assert built[-1].get("api_key") is None  # the SDK falls back to the host's environment


HOST_ENV = {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9/host-gateway",
    "ANTHROPIC_CUSTOM_HEADERS": "X-Host-Secret: host-only\nX-Host-Team: ops",
    "OPENAI_BASE_URL": "http://127.0.0.1:9/host-gateway",
    "OPENAI_ORG_ID": "org-host-only",
    "OPENAI_PROJECT_ID": "proj-host-only",
    "OPENAI_CUSTOM_HEADERS": "X-Host-O: host-only-o",
    "HTTPS_PROXY": "http://127.0.0.1:9",
    "ALL_PROXY": "http://127.0.0.1:9",
}


def test_an_own_key_request_carries_none_of_the_hosts_sdk_settings(monkeypatch):
    """The real SDK clients, over a mock transport: an own-key request goes to the provider's
    public endpoint with the visitor's credential and none of the host's URL, headers,
    organization or project (verifier finding)."""
    import httpx2

    from assay_pipeline.console.engine import PUBLIC_URL, _client

    for k, v in {**HOST_ENV, "OPENAI_API_KEY": "fake-host-key"}.items():
        monkeypatch.setenv(k, v)
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(401, json={"error": {"type": "authentication_error"}})

    for agent in AGENTS:
        own = _client(
            agent, OWN, http_client=httpx2.Client(transport=httpx2.MockTransport(handler))
        )
        with pytest.raises(Exception):  # noqa: B017  (the 401 the mock returns)
            if agent == "claude":
                own.messages.create(
                    model="m", max_tokens=1, messages=[{"role": "user", "content": "q"}]
                )
            else:
                own.responses.create(model="m", input="q")
        req = sent[-1]
        assert str(req.url).startswith(PUBLIC_URL[agent])
        headers = "\n".join(f"{k}: {v}" for k, v in req.headers.items()).lower()
        assert OWN.lower() in headers  # the visitor's key is the credential
        for host_value in ("host-only", "ops", "org-host", "proj-host", "127.0.0.1"):
            assert host_value not in headers, (agent, host_value)
    mock = httpx2.Client(transport=httpx2.MockTransport(handler))
    host = _client("sol", None, http_client=mock)
    assert host.organization == "org-host-only"  # the host's own requests keep its settings


def test_a_host_key_request_for_an_agent_without_a_host_key_holds_nothing(db, tmp_path):  # noqa: F811
    g = guard(tmp_path)
    console = Console(db, tmp_path / "w", guard=g, create_for=no_api, host_agents=("claude",))
    with pytest.raises(ConsoleError, match="no key for that agent"):
        console.run_case(
            question="How many rows?", agent="sol", passcode="open-sesame", session="s"
        )
    assert g.spend.spent(g._today()) == 0.0  # type: ignore[union-attr]


def test_the_spend_table_is_not_evalkits_so_the_two_demos_can_share_a_database(tmp_path):
    """The console may use EvalKit's demo Postgres (owner, 17 Nov 2026). EvalKit's guard keeps
    its counter in `demo_spend`; sharing that table would merge the two daily budgets."""
    import inspect

    from assay_pipeline.console import guard as guard_mod
    from assay_pipeline.console.guard import PostgresSpend, SqliteSpend

    assert "assay_spend" in PostgresSpend.TABLE
    assert "demo_spend" not in inspect.getsource(guard_mod).replace("`demo_spend`", "")
    import sqlite3

    SqliteSpend(tmp_path / "s.sqlite")
    names = {r[0] for r in sqlite3.connect(tmp_path / "s.sqlite").execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}  # fmt: skip
    assert names == {"assay_spend"}


@pytest.mark.parametrize("agent", list(AGENTS))
def test_host_custom_headers_with_a_credential_turn_own_keys_off_before_the_guard(
    agent,
    db,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    """If the host's custom headers carry a credential, it would travel in place of the
    visitor's key, billed to the host with nothing held: refuse own keys (verifier finding)."""
    from assay_pipeline.console.engine import CUSTOM_HEADERS_ENV, _client, own_key_unavailable

    monkeypatch.setenv(CUSTOM_HEADERS_ENV[agent], "X-Team: ops\nAuthorization: Bearer host-cred")
    assert own_key_unavailable(agent)
    with pytest.raises(ConsoleError, match="own keys are off"):
        _client(agent, OWN)
    g = guard(tmp_path, rate_limit=1)
    console, seen, _agg = _own_console(db, tmp_path, g)
    with pytest.raises(ConsoleError, match="own keys are off"):
        console.run_case(question="How many rows?", agent=agent, passcode="open-sesame",
                         session="s", api_key=OWN)  # fmt: skip
    assert seen == [] and g.spend.spent(g._today()) == 0.0  # type: ignore[union-attr]
    g.admit_own_key("open-sesame", "s", 0.1)  # the refusal used no rate-limit slot


def test_an_own_key_client_ignores_the_hosts_proxy_variables(monkeypatch):
    from assay_pipeline.console.engine import _client

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-host-key")
    for agent in AGENTS:
        assert _client(agent, OWN)._client.trust_env is False
    assert _client("sol", None)._client.trust_env is True  # the host's own requests are unchanged
