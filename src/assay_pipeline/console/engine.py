"""`run_case()` behind the console page (Tech Spec §8, CON-01..05).

A `Console` owns a private, hash-checked copy of the agent database and an in-memory audit log,
both gone when the app restarts. Every case runs the real agent loop, guard and locked
connection against that copy:

- **Replay** (the default) plays a recorded sample attack: the recorded model responses stand in
  for the provider, so the guard decisions and audit lines are produced live, with no API call.
- **Live** needs the passcode and passes the guard first (session rate limit, a per-request
  cost cap and the daily spend counter). The request is held at its upper bound, computed from
  the console's tighter limits (fewer steps, smaller outputs, a short question), and settled to
  the actual cost after.

A persistent aggregate log keeps only timestamp, mode, model, guard counts, stop reason,
latency and cost, never question, answer or SQL text (H14).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from assay_pipeline.agent.ask import AGENTS
from assay_pipeline.agent.loop import Prices, run_trial
from assay_pipeline.agent.prompt import prompt_canary, system_prompt
from assay_pipeline.agent.providers import (
    AnthropicProvider,
    Create,
    OpenAIProvider,
    Provider,
    ProviderError,
    Replay,
    scrub_openai,
)
from assay_pipeline.agent.query_tool import QueryTool, ToolResult
from assay_pipeline.audit import AuditError
from assay_pipeline.config import ViewsConfig, load_views_config
from assay_pipeline.pipeline.agentdb import AgentDB, fresh_copy

from .audit import MAX_LINES, MemoryAuditLog
from .guard import Guard, GuardRefused

LIVE_STEP_BUDGET = 4  # queries per live request
LIVE_MAX_OUTPUT = 2_000  # output tokens per model turn, thinking included
MAX_QUESTION_CHARS = 500
# Tokens are bounded by UTF-8 bytes (a byte-level tokenizer never spends more than one token
# per byte). The prompt and tool schema are ASCII; a 500-character question is at most 2,000
# bytes. Tool results are escaped to ASCII and cut to CONSOLE_RESULT_CHARS, so each is at most
# that many tokens whatever characters the data or a crafted literal holds (D6.1 verifier).
BASE_INPUT_TOKENS = 5_000
CONSOLE_RESULT_CHARS = 3_000
RESULT_TOKENS = CONSOLE_RESULT_CHARS
ROTATE_MARGIN = 64  # lines a case can add at most is far below this
AGGREGATE_MAX_BYTES = 5_000_000
CACHE_WRITE_MULT = 1.25  # the system prompt is cached: bound input at the write price


class ConsoleError(RuntimeError):
    """The case cannot run (unknown sample, bad question). Safe to show the visitor."""


@dataclass(frozen=True)
class Sample:
    id: str
    title: str
    owasp: str
    agent: str
    question: str
    responses: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class CaseResult:
    mode: str  # replay | live
    model: str
    question: str
    reply: str
    decisions: tuple[dict[str, Any], ...]  # per query: sql, decision, reason, error, rows
    audit_lines: tuple[dict[str, Any], ...]  # the lines this case added to the console's log
    cost_usd: float
    stop_reason: str
    latency_ms: int


def load_samples(path: Path | None = None) -> list[Sample]:
    text = (
        path.read_text(encoding="utf-8")
        if path is not None
        else resources.files("assay_pipeline").joinpath("console/samples.json").read_text(
            encoding="utf-8")
    )  # fmt: skip
    return [Sample(**{**s, "responses": tuple(s["responses"])}) for s in json.loads(text)]


def request_bound_usd(prices: Prices, step_budget: int = LIVE_STEP_BUDGET) -> float:
    """The most one live request can cost. At most `step_budget + 2` model turns run (tool turns,
    one turn whose calls hit the budget, and the forced answer turn), every turn hits its output
    cap, and no turn's input can exceed the whole history: the base prompt, every turn's output
    and every result the budget allows, however the calls are spread over turns."""
    turns = step_budget + 2
    history = BASE_INPUT_TOKENS + turns * LIVE_MAX_OUTPUT + step_budget * RESULT_TOKENS
    tokens_in = turns * history * CACHE_WRITE_MULT
    tokens_out = turns * LIVE_MAX_OUTPUT
    return round((tokens_in * prices.input_per_mtok + tokens_out * prices.output_per_mtok) / 1e6, 6)


class AggregateLog:
    """JSON lines of aggregates only (CON-05): no visitor text ever reaches disk."""

    FIELDS = ("at", "mode", "model", "queries", "blocked", "allowed", "stop_reason",
              "latency_ms", "cost_usd", "refused")  # fmt: skip

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, row: dict[str, Any]) -> None:
        clean = {k: row.get(k) for k in self.FIELDS}
        with self._lock:  # one rotation at a time; the previous file is kept as .1 (bounded disk)
            if self.path.is_file() and self.path.stat().st_size > AGGREGATE_MAX_BYTES:
                self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(clean, sort_keys=True) + "\n")


class _LiveCall:
    """One provider's calls with SDK retries off: a retried request the server had already
    processed would be billed but invisible to the settlement. It remembers whether the last
    failure could have been billed: an HTTP status error (429, 529, 4xx) was refused unbilled,
    a connection error or timeout may have been processed."""

    def __init__(self, agent: str) -> None:
        self.agent = agent
        self.billed_failure = False

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        if self.agent == "claude":
            import anthropic

            try:
                return anthropic.Anthropic(max_retries=0).messages.create(**kwargs).to_dict()
            except anthropic.APIError as exc:
                self.billed_failure = not isinstance(exc, anthropic.APIStatusError)
                raise ProviderError(f"anthropic: {type(exc).__name__}") from exc
        import openai

        try:
            raw = openai.OpenAI(max_retries=0).responses.create(**kwargs).to_dict()
        except openai.APIError as exc:
            self.billed_failure = not isinstance(exc, openai.APIStatusError)
            raise ProviderError(f"openai: {type(exc).__name__}") from exc
        return scrub_openai(raw)


def live_create(agent: str) -> Create:
    return _LiveCall(agent)


class _ConsoleTool(QueryTool):
    """The query tool with each result escaped to ASCII and cut to CONSOLE_RESULT_CHARS, so a
    result's token count is bounded by its length (the live cost bound relies on it)."""

    def query(self, sql: str) -> ToolResult:
        r = super().query(sql)
        text = r.text.encode("ascii", "backslashreplace").decode("ascii")
        if len(text) > CONSOLE_RESULT_CHARS:
            note = "\n[result cut to fit the console's limit]"
            text = text[: CONSOLE_RESULT_CHARS - len(note)] + note
        return replace(r, text=text)


class Console:
    def __init__(
        self,
        db: AgentDB,
        workdir: Path,
        *,
        samples: Sequence[Sample] | None = None,
        guard: Guard | None = None,
        aggregate: AggregateLog | None = None,
        views: ViewsConfig | None = None,
        create_for: Callable[[str], Create] = live_create,
        seed: int = 0,
    ) -> None:
        self.db = db
        self.views = views or load_views_config()
        self.copy = fresh_copy(db.path, db.sha256, workdir)  # the console's own copy (CON-04)
        self.audit = MemoryAuditLog("console")
        self._lock = threading.Lock()
        self.samples = {s.id: s for s in (samples if samples is not None else load_samples())}
        self.guard = guard
        self.aggregate = aggregate
        self.create_for = create_for
        self.system = system_prompt(self.views, db.snapshot, prompt_canary(db.snapshot, seed))
        self._n = 0

    def run_case(
        self,
        *,
        sample_id: str | None = None,
        question: str | None = None,
        agent: str = "claude",
        session: str = "",
        passcode: str = "",
    ) -> CaseResult:
        """A sample replays (no API call); a question runs live behind the guard."""
        if (sample_id is None) == (question is None):
            raise ConsoleError("give either a sample or a question")
        if sample_id is not None:
            sample = self.samples.get(sample_id)
            if sample is None:
                raise ConsoleError(f"no sample {sample_id!r}")
            return self._run(
                "replay", sample.agent, sample.question, Replay(list(sample.responses))
            )
        if agent not in AGENTS:
            raise ConsoleError(f"choose an agent from {sorted(AGENTS)}")
        q = (question or "").strip()
        if not q or len(q) > MAX_QUESTION_CHARS:
            raise ConsoleError(f"ask a question of 1 to {MAX_QUESTION_CHARS} characters")
        if self.guard is None:
            raise ConsoleError("live mode is off on this console; replay mode always works")
        bound = request_bound_usd(AGENTS[agent][1])
        try:
            ticket = self.guard.reserve(passcode, session, bound)
        except GuardRefused:
            self._log({"mode": "live", "model": AGENTS[agent][0], "refused": True})
            raise
        result = None
        create = self.create_for(agent)
        try:
            result = self._run("live", agent, q, create)
            return result
        finally:
            # settle to the actual cost; keep the whole hold when usage may be unseen: a crash,
            # or a transport stop whose failure could have been billed (an HTTP status error
            # was refused unbilled, so only the completed turns count)
            unseen = result is None or (
                result.stop_reason == "transport" and getattr(create, "billed_failure", True)
            )
            self.guard.settle(ticket, bound if unseen or result is None else result.cost_usd)

    def _run(self, mode: str, agent: str, question: str, create: Create) -> CaseResult:
        model, prices, _key = AGENTS[agent]
        provider: Provider = (
            AnthropicProvider(model, create, max_tokens=LIVE_MAX_OUTPUT)
            if agent == "claude"
            else OpenAIProvider(model, create, max_output_tokens=LIVE_MAX_OUTPUT)
        )
        with self._lock:  # a unique trial id per case, and a log with room for it
            self._n += 1
            trial_id = f"console-{self._n}"
            if len(self.audit.lines) > MAX_LINES - ROTATE_MARGIN:
                self.audit = MemoryAuditLog("console")  # rotate: the page says logs are temporary
            audit = self.audit
        t0 = time.monotonic()
        tool = _ConsoleTool(self.copy, self.views, audit, trial_id=trial_id, model=model,
                            case_id="console")  # fmt: skip
        try:
            trial = run_trial(
                provider, tool, audit, question, self.system, task_id="console", trial=1,
                seed=0, prices=prices, step_budget=LIVE_STEP_BUDGET,
            )  # fmt: skip
        except RuntimeError as exc:
            if (
                isinstance(create, Replay)
                and not create.remaining
                and not isinstance(exc, AuditError)
            ):
                raise ConsoleError(
                    "the recorded sample does not match this console's code"
                ) from exc
            raise
        if isinstance(create, Replay) and create.remaining:
            raise ConsoleError("the recorded sample does not match this console's code")
        # only this case's lines: other visitors' cases may write to the log at the same time
        lines = tuple(x for x in list(audit.lines) if x.get("trial_id") == trial_id)
        decisions = tuple(
            {"sql": x["sql"], "decision": x["guard_decision"], "reason": x["guard_reason"],
             "error": x["db_error_class"], "rows": x["rows_returned"]}
            for x in lines if x["event"] == "query"
        )  # fmt: skip
        t = trial.trajectory
        result = CaseResult(mode, model, question, trial.answer, decisions, lines,
                            t.cost_usd if mode == "live" else 0.0, t.stop_reason,
                            int((time.monotonic() - t0) * 1000))  # fmt: skip
        self._log({
            "mode": mode, "model": model, "queries": len(decisions),
            "blocked": sum(d["decision"] == "blocked" for d in decisions),
            "allowed": sum(d["decision"] == "allowed" for d in decisions),
            "stop_reason": t.stop_reason, "latency_ms": result.latency_ms,
            "cost_usd": result.cost_usd, "refused": False,
        })  # fmt: skip
        return result

    def _log(self, row: dict[str, Any]) -> None:
        if self.aggregate is not None:
            self.aggregate.record({"at": datetime.now(UTC).isoformat(), **row})
