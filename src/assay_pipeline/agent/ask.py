"""`assay ask`: one question to one agent, live or replayed from a recording (AGT-07)."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from assay_pipeline.audit import AuditLog
from assay_pipeline.config import ViewsConfig, load_views_config

from .loop import Prices, TrialResult, run_trial
from .prompt import prompt_canary, system_prompt
from .providers import (
    AnthropicProvider,
    OpenAIProvider,
    Provider,
    Recorder,
    Replay,
    anthropic_create,
    openai_create,
)
from .query_tool import QueryTool

AGENTS: dict[str, tuple[str, Prices, str]] = {
    # name: (model id, list prices per 1M tokens, key environment variable)
    "claude": ("claude-sonnet-5-5", Prices(2.0, 10.0), "ANTHROPIC_API_KEY"),
    "sol": ("gpt-6.1-sol", Prices(2.0, 10.0), "OPENAI_API_KEY"),
}


class AskError(RuntimeError):
    """The question cannot be asked (no key, no database, a recording that does not fit)."""


@dataclass(frozen=True)
class AskResult:
    trial: TrialResult
    model: str
    snapshot: str
    audit_path: Path
    recording: list[dict[str, Any]] | None


def _snapshot_of(db: Path) -> str:
    info = db.with_suffix(".json")
    try:
        return str(json.loads(info.read_text(encoding="utf-8"))["snapshot"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AskError(f"{info}: cannot read the snapshot id ({exc})") from exc


def ask(
    question: str,
    db: Path | str,
    agent: str,
    audit_path: Path | str,
    *,
    replay: list[dict[str, Any]] | None = None,
    record: bool = False,
    record_path: Path | None = None,
    views: ViewsConfig | None = None,
    run_id: str | None = None,
    seed: int = 0,
) -> AskResult:
    if agent not in AGENTS:
        raise AskError(f"unknown agent {agent!r}; choose from {sorted(AGENTS)}")
    db = Path(db)
    if not db.is_file():
        raise AskError(f"{db} is not an agent database (build one with `assay build-db`)")
    model, prices, key_var = AGENTS[agent]
    if replay is None and not os.environ.get(key_var):
        raise AskError(
            f"{key_var} is not set: export it to ask {agent} live, or pass --replay FILE"
        )
    views = views or load_views_config()
    snapshot = _snapshot_of(db)
    live = anthropic_create if agent == "claude" else openai_create
    recorder = Recorder(live) if replay is None and record else None
    create = Replay(replay) if replay is not None else (recorder or live)
    provider: Provider = (
        AnthropicProvider(model, create) if agent == "claude" else OpenAIProvider(model, create)
    )
    trial_id = f"ask-{uuid.uuid4().hex[:8]}" if replay is None else "ask-replay"
    # one run id per audit file, so every later ask (live or replay) appends to the same log
    audit = AuditLog(audit_path, run_id or "ask")
    try:
        tool = QueryTool(db, views, audit, trial_id=trial_id, model=model, case_id="ask")
        result = run_trial(
            provider,
            tool,
            audit,
            question,
            system_prompt(views, snapshot, prompt_canary(snapshot, seed)),
            task_id="ask",
            trial=1,
            seed=seed,
            prices=prices,
        )
    finally:
        audit.close()
        if recorder is not None and record_path is not None:  # paid-for responses survive errors
            record_path.write_text(json.dumps(recorder.responses, indent=1), encoding="utf-8")
    if isinstance(create, Replay) and create.remaining:
        raise AskError(
            f"the recording has {create.remaining} responses this run never asked for: it does "
            "not match this question, model or code"
        )
    return AskResult(
        result, model, snapshot, Path(audit_path), recorder.responses if recorder else None
    )
