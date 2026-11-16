"""The console page's logic (D6.2, DEM-01..04), kept apart from Streamlit so it is testable.

`apps/console/streamlit_app.py` is a thin Streamlit script over these functions:
- **Replay** (the default) runs a sample through the real guard and a fresh audit log, with no
  API call (DEM-01). Most samples are recorded model runs; two are scripted (labelled so) to
  show the guard blocking a query an agent does send.
- **Live** asks a typed question behind the passcode, with a line showing what one request is
  held at and what is left of today's budget (DEM-02). A visitor may bring their own provider
  key instead: the passcode is still required, and nothing is held against the host's budget
  (DEM-05, OWNER D-37). A console with a passcode but no hosted keys serves own-key requests only.
- Each query's guard decision is shown next to the audit line it wrote (DEM-03).

Settings come from the host's secret store as environment variables (DEM-04, NFR1); nothing is
read from files in the repository:

| Variable | Meaning |
| --- | --- |
| `ASSAY_CONSOLE_DB` | the agent database (`agent.duckdb`, with its `.json` beside it) |
| `ASSAY_CONSOLE_PASSCODE` | live mode's passcode; live mode is off when unset |
| `ASSAY_CONSOLE_DATABASE_URL` | Postgres daily spend counter (needed for hosted keys) |
| `ASSAY_CONSOLE_DAILY_CAP` | daily live budget in USD (default 3.00, at least one request) |
| `ASSAY_CONSOLE_WORKDIR` | where the database copy and aggregate log go (default: a temp dir) |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | hosted keys (optional: visitors can bring their own) |
"""

from __future__ import annotations

import math
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from assay_pipeline.agent.ask import AGENTS
from assay_pipeline.pipeline.agentdb import AgentDBError, load_agent_db

from .engine import AggregateLog, CaseResult, Console, Sample, request_bound_usd
from .guard import DAILY_CAP_USD, Guard, PostgresSpend

AUDIT_FIELDS = ("seq", "event", "guard_decision", "guard_reason", "rows_returned", "hash")
BUDGET_TTL_S = 15.0  # the budget line reads the shared store at most this often
CONNECT_TIMEOUT_S = 3  # an unreachable store must not hang every page rerun
GENERIC_ERROR = "Something went wrong and the request did not complete. Replay mode still works."
PROVIDERS = {"ANTHROPIC_API_KEY": "Anthropic", "OPENAI_API_KEY": "OpenAI"}


class SettingsError(ValueError):
    """The host's settings cannot start the console; safe to show the visitor (no paths)."""


def live_bound_usd(agents: Mapping[str, Any] | None = None) -> float:
    """The most any one live request can cost, whichever agent the visitor picks. The guard's
    per-request cap is set to exactly this, so no request it admits can exceed it."""
    table = AGENTS if agents is None else agents
    return max(request_bound_usd(prices) for _model, prices, _key in table.values())


def money(x: float) -> str:
    """Dollars as the page shows them: cents, rounded up, so a hold is never understated."""
    return f"${math.ceil(round(x * 100, 6)) / 100:.2f}"


@dataclass(frozen=True)
class Settings:
    db: Path
    passcode: str = field(repr=False)
    database_url: str = field(repr=False)
    daily_cap: float
    agents: tuple[str, ...] = ()  # agents whose provider key is set
    workdir: Path | None = None

    @property
    def live(self) -> bool:
        """Live mode is on: with a passcode, visitors can at least bring their own key."""
        return bool(self.passcode)

    @property
    def hosted(self) -> bool:
        """The host's keys and daily budget are offered too."""
        return bool(self.passcode and self.database_url and self.agents)

    def live_off_reason(self) -> str:
        return "Live mode is off on this console: it needs a passcode."

    def hosted_off_reason(self) -> str:
        needs = (("a spend store", self.database_url), ("a provider key", self.agents))
        missing = [name for name, ok in needs if not ok]
        if not missing:
            return "This console's hosted keys are off (its spend store client is missing)"
        return f"This console's hosted keys are off (it needs {' and '.join(missing)})"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        raw = env.get("ASSAY_CONSOLE_DAILY_CAP", "").strip()
        try:
            cap = float(raw) if raw else DAILY_CAP_USD
        except ValueError as exc:
            raise SettingsError("ASSAY_CONSOLE_DAILY_CAP must be a number of dollars") from exc
        if not live_bound_usd() <= cap < 1_000:
            raise SettingsError(f"ASSAY_CONSOLE_DAILY_CAP must be at least one live request "
                                f"({money(live_bound_usd())}) and below 1000")  # fmt: skip
        work = env.get("ASSAY_CONSOLE_WORKDIR", "").strip()
        return cls(
            db=Path(env.get("ASSAY_CONSOLE_DB", "agent.duckdb")),
            passcode=env.get("ASSAY_CONSOLE_PASSCODE", "").strip(),  # a stray newline in a store
            database_url=env.get("ASSAY_CONSOLE_DATABASE_URL", "").strip(),
            daily_cap=cap,
            agents=tuple(a for a, (_m, _p, key) in AGENTS.items() if env.get(key, "").strip()),
            workdir=Path(work) if work else None,
        )


def _postgres_connect(url: str) -> Any:
    import psycopg  # the `console` extra

    return psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT_S)


def provider_name(agent: str) -> str:
    return PROVIDERS[AGENTS[agent][2]]


def build_console(settings: Settings, workdir: Path | None = None) -> Console:
    """One console per app process: its own database copy and in-memory audit log. Live mode
    is wired when the passcode is set; the host's keys and budget only when the shared spend
    store, a provider key and the Postgres client are there too. Otherwise the guard has no
    spend store and admits own-key requests only."""
    if not settings.db.is_file():
        raise SettingsError("No agent database is configured (set ASSAY_CONSOLE_DB).")
    try:
        db = load_agent_db(settings.db)
    except AgentDBError as exc:  # missing build record, hash mismatch: no host paths shown
        raise SettingsError("The agent database failed its integrity check; rebuild it with "
                            "`assay build-db`.") from exc  # fmt: skip
    work = workdir or settings.workdir or Path(tempfile.mkdtemp(prefix="assay-console-"))
    guard = None
    if settings.live:
        spend = None
        if settings.hosted:
            try:
                import psycopg  # noqa: F401  (the `console` extra)
            except ImportError:
                spend = None  # hosted keys stay off; the page says why
            else:
                spend = PostgresSpend(settings.database_url, connect=_postgres_connect)
        guard = Guard(settings.passcode, spend, per_request_cap=live_bound_usd(),
                      daily_cap=settings.daily_cap)  # fmt: skip
    return Console(db, work, guard=guard, aggregate=AggregateLog(work / "aggregate.jsonl"),
                   host_agents=settings.agents)  # fmt: skip


def sample_label(s: Sample) -> str:
    return f"{s.owasp}: {s.title}"


class BudgetLine:
    """The live budget line, reading the shared store at most every BUDGET_TTL_S seconds so a
    rerun (any click, by any visitor) does not open a database connection each time."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._at = -math.inf
        self._left: float | None = None

    def invalidate(self) -> None:
        self._at = -math.inf

    def __call__(self, console: Console, settings: Settings | None = None) -> str:
        if console.guard is None:
            off = settings.live_off_reason() if settings else "Live mode is off on this console."
            return off + " Replay mode always works."
        own = (f"With your own API key, a request is capped at {money(live_bound_usd())} on your "
               "account and uses none of this console's budget.")  # fmt: skip
        if console.guard.spend is None:
            why = settings.hosted_off_reason() if settings else "This console's hosted keys are off"
            return f"{why}: live requests need your own API key. {own}"
        now = self._clock()
        if now - self._at > BUDGET_TTL_S:
            self._left, self._at = console.guard.remaining_today(), now
        bound, left = live_bound_usd(), self._left
        hold = f"Each live request is held at up to {money(bound)} and settled to its actual cost; "
        if left is None:
            return hold + f"today's remaining budget is unavailable right now. {own}"
        if left < bound:
            return hold + f"today's {money(console.guard.daily_cap)} budget is used up. {own}"
        return hold + (f"about ${left:.2f} of today's {money(console.guard.daily_cap)} "
                       f"budget is left. {own}")  # fmt: skip


def budget_line(console: Console, settings: Settings | None = None) -> str:
    """Uncached, for tests and one-off use."""
    return BudgetLine()(console, settings)


def decision_rows(result: CaseResult) -> list[dict[str, Any]]:
    """Each query beside the audit line it wrote (DEM-03): the decision and the line come from
    the same case, in order, so the page can show them together."""
    lines = [x for x in result.audit_lines if x.get("event") == "query"]
    rows = []
    for d, line in zip(result.decisions, lines, strict=True):
        rows.append({
            "sql": d["sql"], "decision": d["decision"], "reason": d["reason"] or "",
            "rows": "" if d["rows"] is None else str(d["rows"]),
            "audit_seq": str(line["seq"]), "audit_hash": line["hash"][:12],
        })  # fmt: skip
    return rows


def audit_view(result: CaseResult) -> list[dict[str, str]]:
    """The case's audit lines without question or answer text (they are on the page already),
    as text cells: a missing value is blank, never NaN."""
    return [{k: "" if x.get(k) is None else str(x.get(k)) for k in AUDIT_FIELDS}
            for x in result.audit_lines]  # fmt: skip
