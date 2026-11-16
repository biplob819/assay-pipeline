"""The console page's logic (D6.2, DEM-01..04), kept apart from Streamlit so it is testable.

`apps/console/streamlit_app.py` is a thin Streamlit script over these functions:
- **Replay** (the default) runs a sample through the real guard and a fresh audit log, with no
  API call (DEM-01). Most samples are recorded model runs; two are scripted (labelled so) to
  show the guard blocking a query an agent does send.
- **Live** asks a typed question behind the passcode, with a line showing what one request is
  held at and what is left of today's budget (DEM-02).
- Each query's guard decision is shown next to the audit line it wrote (DEM-03).

Settings come from the host's secret store as environment variables (DEM-04, NFR1); nothing is
read from files in the repository:

| Variable | Meaning |
| --- | --- |
| `ASSAY_CONSOLE_DB` | the agent database (`agent.duckdb`, with its `.json` beside it) |
| `ASSAY_CONSOLE_PASSCODE` | live mode's passcode; live mode is off when unset |
| `ASSAY_CONSOLE_DATABASE_URL` | Postgres for the shared daily spend counter (needed for live) |
| `ASSAY_CONSOLE_DAILY_CAP` | daily live budget in USD (default 3.00, at least one request) |
| `ASSAY_CONSOLE_WORKDIR` | where the database copy and aggregate log go (default: a temp dir) |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | provider keys; live offers only agents whose key is set |
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
        return bool(self.passcode and self.database_url and self.agents)

    def live_off_reason(self) -> str:
        needs = (("a passcode", self.passcode), ("a spend store", self.database_url),
                 ("a provider key", self.agents))  # fmt: skip
        missing = [name for name, ok in needs if not ok]
        need = missing[0] if len(missing) == 1 else ", ".join(missing[:-1]) + " and " + missing[-1]
        return f"Live mode is off on this console: it needs {need}."

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


def build_console(settings: Settings, workdir: Path | None = None) -> Console:
    """One console per app process: its own database copy and in-memory audit log. Live mode
    is wired only when the passcode, the shared spend store and a provider key are all set."""
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
        try:
            import psycopg  # noqa: F401  (the `console` extra)
        except ImportError:
            guard = None  # live stays off; the page says why
        else:
            spend = PostgresSpend(settings.database_url, connect=_postgres_connect)
            guard = Guard(settings.passcode, spend, per_request_cap=live_bound_usd(),
                          daily_cap=settings.daily_cap)  # fmt: skip
    return Console(db, work, guard=guard, aggregate=AggregateLog(work / "aggregate.jsonl"))


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
            if settings is not None and settings.live:
                return "Live mode is off on this console: its spend store client is missing."
            off = settings.live_off_reason() if settings else "Live mode is off on this console."
            return off + " Replay mode always works."
        now = self._clock()
        if now - self._at > BUDGET_TTL_S:
            self._left, self._at = console.guard.remaining_today(), now
        bound, left = live_bound_usd(), self._left
        hold = f"Each live request is held at up to {money(bound)} and settled to its actual cost; "
        if left is None:
            return hold + "today's remaining budget is unavailable right now."
        if left < bound:
            return hold + f"today's {money(console.guard.daily_cap)} budget is used up."
        return hold + (f"about ${left:.2f} of today's {money(console.guard.daily_cap)} "
                       "budget is left.")  # fmt: skip


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
