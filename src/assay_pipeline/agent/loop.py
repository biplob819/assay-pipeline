"""One agent trial (Tech Spec §2.3, AGT-05): the model calls `query` until it answers, refuses,
or hits the step budget; the trajectory is recorded in EvalKit's format (D-8) and every
question, query and answer goes to the audit log (§6.3).
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass

from evalkit.schemas import Step, Trajectory

from assay_pipeline.audit import AuditLog

from .providers import QUERY_TOOL_NAME, Provider, ProviderError, ToolCall, Turn
from .query_tool import QueryTool

STEP_BUDGET = 8  # tool calls (AGT-05)
MAX_TURNS = 16  # model turns, a backstop against a model that never calls a tool or answers
ANSWER_CAP_CHARS = 8000  # the output cap on the final answer
SUMMARY_CHARS = 500


@dataclass(frozen=True)
class Prices:
    input_per_mtok: float
    output_per_mtok: float

    # Anthropic prompt caching: a 5-minute cache write costs 1.25x input, a read 0.1x
    cache_write_mult: float = 1.25
    cache_read_mult: float = 0.1

    def cost(
        self, tokens_in: int, tokens_out: int, cache_write: int = 0, cache_read: int = 0
    ) -> float:
        """`tokens_in` counts every input token; `cache_write` and `cache_read` are the parts of
        it billed at the cache rates (zero for providers whose cached input we bill in full)."""
        plain = tokens_in - cache_write - cache_read
        weighted = plain + cache_write * self.cache_write_mult + cache_read * self.cache_read_mult
        return (weighted * self.input_per_mtok + tokens_out * self.output_per_mtok) / 1e6


@dataclass(frozen=True)
class TrialResult:
    trajectory: Trajectory
    answer: str
    # Text the model wrote beside its tool calls. EvalKit trajectories keep only the final
    # answer, but this text reaches the user too, so the scorer scans it for canaries (A4.2).
    turn_texts: tuple[str, ...] = ()
    truncated: bool = False  # the answer hit the output cap (max_tokens / max_output_tokens)


def _clean(text: str) -> str:
    """Text safe to hash and store: a lone surrogate from a provider becomes "?"."""
    return text.encode("utf-8", "replace").decode("utf-8")


def run_trial(
    provider: Provider,
    tool: QueryTool,
    audit: AuditLog,
    question: str,
    system: str,
    *,
    task_id: str,
    trial: int,
    seed: int,
    prices: Prices,
    step_budget: int = STEP_BUDGET,
) -> TrialResult:
    if not question.strip():
        raise ValueError("the question is blank")
    audit.append(
        event="question",
        trial_id=tool.trial_id,
        case_id=tool.case_id,
        model=tool.model,
        question=question,
    )
    provider.start(system, question)
    steps: list[Step] = []
    calls_used = 0
    used = {"in": 0, "out": 0, "write": 0, "read": 0}
    answer = ""
    stop_reason = "final_answer"
    turn_texts: list[str] = []
    truncated = False

    def count(turn: Turn) -> None:
        used["in"] += turn.tokens_in
        used["out"] += turn.tokens_out
        used["write"] += turn.cache_write_tokens
        used["read"] += turn.cache_read_tokens

    try:
        for _ in range(MAX_TURNS):
            t0 = time.monotonic()
            try:
                turn = provider.next_turn()
            except ProviderError:
                stop_reason, answer = "transport", ""  # EvalKit: rerun to resume
                break
            count(turn)
            if turn.stop == "refusal":
                stop_reason, answer = "refusal", turn.text
                steps.append(_step(len(steps), "refusal", {}, "", "the model refused", turn, t0))
                break
            if turn.stop != "tool_use":
                answer = turn.text  # "end", or "max_tokens": the answer so far, capped below
                truncated = turn.stop == "max_tokens"
                break
            if turn.text.strip():
                turn_texts.append(_clean(turn.text)[:ANSWER_CAP_CHARS])
            results: list[tuple[ToolCall, str]] = []
            for call in turn.tool_calls:
                if calls_used >= step_budget:
                    results.append((call, "Step budget reached: no more queries. Answer now."))
                    continue
                calls_used += 1
                text, step = _handle(call, tool, len(steps), turn, t0)
                steps.append(step)
                results.append((call, text))
            if calls_used >= step_budget and any(
                r[1].startswith("Step budget reached") for r in results
            ):
                stop_reason = "step_budget"
                provider.add_results(results)
                t1 = time.monotonic()
                try:
                    final = provider.next_turn(final=True)  # tools off: the model must answer
                except ProviderError:
                    stop_reason, answer = "transport", ""
                    break
                count(final)
                if final.stop == "refusal":
                    stop_reason, answer = "refusal", final.text
                    steps.append(
                        _step(len(steps), "refusal", {}, "", "the model refused", final, t1)
                    )
                else:
                    answer = final.text
                    truncated = final.stop == "max_tokens"
                break
            provider.add_results(results)
        else:
            stop_reason = "step_budget"
        answer = _clean(answer)[:ANSWER_CAP_CHARS]
    finally:
        # every trial that logged a question logs an answer, even when an unexpected error ends
        # it (AGT-05, NFR3); the error still propagates
        audit.append(
            event="answer",
            trial_id=tool.trial_id,
            case_id=tool.case_id,
            model=tool.model,
            answer_sha256=hashlib.sha256(
                _clean(answer)[:ANSWER_CAP_CHARS].encode("utf-8")
            ).hexdigest(),
        )
    trajectory = Trajectory(
        task_id=task_id,
        agent=provider.name,
        trial=trial,
        seed=seed,
        steps=tuple(steps),
        final_answer=answer,
        sandbox_reset_ok=True,
        cost_usd=round(prices.cost(used["in"], used["out"], used["write"], used["read"]), 6),
        stop_reason=stop_reason,  # type: ignore[arg-type]
    )
    return TrialResult(trajectory, answer, tuple(turn_texts), truncated)


def _step(
    index: int,
    kind: str,
    arguments: dict,
    summary: str,
    error: str | None,
    turn,
    t0: float,
    violation: bool = False,
    tool: str = QUERY_TOOL_NAME,
) -> Step:
    return Step(
        index=index,
        tool=tool,
        arguments=arguments,
        result_summary=summary[:SUMMARY_CHARS],
        error=error,
        tokens_in=turn.tokens_in,
        tokens_out=turn.tokens_out,
        latency_ms=int((time.monotonic() - t0) * 1000),
        kind=kind,  # type: ignore[arg-type]
        violation=violation,
    )


def _handle(call: ToolCall, tool: QueryTool, index: int, turn, t0: float) -> tuple[str, Step]:
    if call.name != QUERY_TOOL_NAME:
        msg = f"Unknown tool {call.name!r}. The only tool is `query`."
        # recorded under its own name, so EvalKit counts the attempt as a disallowed tool
        name = re.sub(r"\s+", "_", (call.name or "").strip())[:64] or "unnamed_tool"
        if name == QUERY_TOOL_NAME:  # " query" is not the tool, so it must not look like it
            name = f"{QUERY_TOOL_NAME}?"
        return msg, _step(index, "tool_call", {"raw": call.raw_arguments}, msg,
                          f"unknown tool {call.name}", turn, t0, violation=True,
                          tool=name)  # fmt: skip
    sql = (call.arguments or {}).get("sql")
    if not isinstance(sql, str) or not sql.strip():
        msg = "The query tool needs one argument, `sql`, holding a SELECT statement."
        return msg, _step(index, "malformed", {"raw": call.raw_arguments}, msg,
                          "malformed tool arguments", turn, t0)  # fmt: skip
    result = tool.query(sql)
    blocked = not result.allowed
    error = (
        f"blocked: {result.reason}"
        if blocked
        else (f"database: {result.error_class}" if result.error_class else None)
    )
    step = _step(index, "tool_call", {"sql": sql}, result.text, error, turn, t0,
                 violation=blocked)  # fmt: skip
    return result.text, step
