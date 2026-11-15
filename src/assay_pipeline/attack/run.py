"""`assay attack`: the main run and the guard-off ablation (Tech Spec §2.4, §7, §9, RUN-01..07).

Each (case, model, trial) gets a fresh, hash-checked copy of the agent database (planted with
descriptions for indirect-injection cases), one agent trial, a change check on the copy, and a
score. LLM04 cases never use the agent: their planted rows go through a scratch build once per
case. Results are written one JSON line per trial, so a rerun resumes where it stopped. The run
prints a cost estimate first and refuses to start, or stops, past `max_cost`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import duckdb
from evalkit.schemas import ResultSet

from assay_pipeline.agent.ask import AGENTS
from assay_pipeline.agent.loop import STEP_BUDGET, run_trial
from assay_pipeline.agent.prompt import prompt_canary, system_prompt
from assay_pipeline.agent.providers import (
    AnthropicProvider,
    Create,
    OpenAIProvider,
    Provider,
    anthropic_create,
    openai_create,
)
from assay_pipeline.agent.query_tool import QueryTool, ToolResult
from assay_pipeline.audit import AuditLog, verify
from assay_pipeline.config import BuildConfig, ViewsConfig, load_build_config, load_views_config
from assay_pipeline.guard import SqlGuard
from assay_pipeline.pipeline.agentdb import AgentDB, fresh_copy, load_agent_db, plant_descriptions
from assay_pipeline.pipeline.ingest.cache import RawCache
from assay_pipeline.pipeline.planted import load_planted, run_case_in_views
from assay_pipeline.pipeline.snapshot import sha256_file
from assay_pipeline.schemas import SCHEMA_VERSION, AttackCase, NormalQuestion, TrialResult

from .gold import cell, compute_gold
from .passthrough import PassThroughGuard
from .score import (
    Layer,
    Outcome,
    answer_correct,
    audit_covered,
    capped_text,
    score_attack,
    successful_sqls,
)
from .suite import load_suite
from .summary import summarise

# Planning cost per trial before a pilot replaces it (Tech Spec §9: EvalKit's measured ~$0.018
# with caching, $0.04 without); the conservative figure is used for the estimate.
PLANNING_TRIAL_USD = 0.04


class RunError(RuntimeError):
    """The run cannot start or continue (over budget, a database that fails its hash)."""


@dataclass
class RunPlan:
    run_dir: Path
    db: AgentDB
    views: ViewsConfig
    cfg: BuildConfig
    cache: RawCache | None  # needed for LLM04 scratch builds
    models: Sequence[str]
    k: int
    attacks: Sequence[AttackCase]
    questions: Sequence[NormalQuestion]
    gold: dict[str, Any]  # question id -> {columns, rows, ordered}
    create_for: Callable[[str], Create]  # agent name -> provider call (live, or replay in tests)
    ablation: bool = False
    max_cost: float | None = None
    seed: int = 0
    trial_usd: float = PLANNING_TRIAL_USD  # a pilot's measured cost replaces the plan figure
    spent: float = field(default=0.0)

    @property
    def results_path(self) -> Path:
        return self.run_dir / ("ablation.jsonl" if self.ablation else "trials.jsonl")


PIPELINE_MODEL = "pipeline"  # LLM04 results: one scratch build per case, no model involved


def _agent_items(plan: RunPlan) -> list[AttackCase | NormalQuestion]:
    return [x for x in _items(plan) if not (isinstance(x, AttackCase) and x.owasp == "LLM04")]


def _k(plan: RunPlan) -> int:
    return 1 if plan.ablation else plan.k


def estimate(plan: RunPlan) -> float:
    """The whole run at `trial_usd` a trial (LLM04 cases never call a model)."""
    return round(len(plan.models) * len(_agent_items(plan)) * _k(plan) * plan.trial_usd, 2)


def _remaining(plan: RunPlan, complete: set[tuple[str, str, int]]) -> float:
    n = sum(
        1
        for agent in plan.models
        for item in _agent_items(plan)
        for trial in range(1, _k(plan) + 1)
        if (item.id, AGENTS[agent][0], trial) not in complete
    )
    return round(n * plan.trial_usd, 2)


def _complete(
    done: dict[tuple[str, str, int], TrialResult], trajectories: set[tuple[str, str, int]]
) -> set[tuple[str, str, int]]:
    """Scored trials that need no rerun: an agent trial also needs its trajectory record (a
    crash between the two writes reruns the trial rather than leave it unjudgeable)."""
    return {
        key
        for key, r in done.items()
        if r.status == "scored" and (key[1] == PIPELINE_MODEL or key in trajectories)
    }


def _provider(agent: str, create: Create) -> Provider:
    model = AGENTS[agent][0]
    return AnthropicProvider(model, create) if agent == "claude" else OpenAIProvider(model, create)


def _repair_tail(path: Path) -> bytes:
    """The file's bytes, with a half-written last line (a crash mid-write) cut off on disk."""
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        keep = raw.rfind(b"\n") + 1
        with path.open("r+b") as f:
            f.truncate(keep)
        raw = raw[:keep]
    return raw


def _trajectory_keys(path: Path) -> set[tuple[str, str, int]]:
    """Trials with a trajectory record (A4.3 judges from these; the last record per key wins)."""
    if not path.is_file():
        return set()
    keys = set()
    for n, line in enumerate(_repair_tail(path).decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            keys.add((str(rec["case_id"]), str(rec["model"]), int(rec["trial"])))
        except (ValueError, KeyError, TypeError) as exc:
            raise RunError(f"{path}:{n}: not a trajectory record ({exc})") from exc
    return keys


def _done(path: Path) -> tuple[dict[tuple[str, str, int], TrialResult], float]:
    """Trials already written, and everything they cost (retried transport attempts included).
    A half-written last line (a crash mid-write) is cut off; any other unreadable line is
    refused. A later line for the same trial replaces an earlier one."""
    out: dict[tuple[str, str, int], TrialResult] = {}
    spent = 0.0
    if not path.is_file():
        return out, spent
    raw = _repair_tail(path)
    for n, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = TrialResult.model_validate_json(line)
        except ValueError as exc:
            raise RunError(f"{path}:{n}: not a trial result ({exc})") from exc
        out[(r.case_id, r.model, r.trial)] = r
        spent += r.cost_usd
    return out, spent


def _run_record(plan: RunPlan) -> dict[str, Any]:
    # the cases' full content, not just their ids: an edited prompt or gold query is a new run
    content = sorted(
        json.dumps(x.model_dump(mode="json"), sort_keys=True)
        for x in [*plan.attacks, *plan.questions]
    )
    return {
        "db_sha256": plan.db.sha256, "snapshot": plan.db.snapshot, "seed": plan.seed,
        "cases": hashlib.sha256("\n".join(content).encode()).hexdigest(), "k": _k(plan),
        "models": {a: AGENTS[a][0] for a in sorted(plan.models)},
    }  # fmt: skip


def _check_run_record(plan: RunPlan) -> tuple[Path, dict[str, Any]]:
    """A resumed run must be the same run: same database, snapshot, seed, cases, k and models,
    so its summary never mixes trials from different settings. The caller writes the record
    only once the run is allowed to start, so a refused run never claims its run id."""
    path = plan.run_dir / ("ablation.json" if plan.ablation else "run.json")
    want = _run_record(plan)
    if path.is_file():
        have = json.loads(path.read_text(encoding="utf-8"))
        diff = sorted(k for k in want if have.get(k) != want[k])
        if diff:
            raise RunError(f"{plan.run_dir} holds another run ({', '.join(diff)} differ); "
                           "choose a new --run-id")  # fmt: skip
    return path, want


def _predicted(db_copy: Path, sql: str | None, views: ViewsConfig) -> ResultSet | None:
    """One of the agent's successful queries, re-run in full (the model saw at most 50 rows) under
    the same limits as the agent's own queries: read-only, no external access, the memory and
    thread caps, interrupted at the timeout. The guard's rendered SQL carries LIMIT
    `max_rows_executed`, so a result that reaches it counts as cut off (gold results are far
    smaller). A query that breaks a limit gives no result: the question scores as not answered."""
    if sql is None:
        return None
    d = SqlGuard(views).check(sql)
    if d.decision != "allowed" or d.rendered_sql is None:
        return None
    lim = views.limits
    config = {
        "enable_external_access": False,
        "memory_limit": lim.memory_limit,
        "threads": lim.threads,
        "temp_directory": "",
        "max_temp_directory_size": "0KB",
    }
    con = duckdb.connect(str(db_copy), read_only=True, config=config)  # fmt: skip
    timer = threading.Timer(lim.timeout_seconds, con.interrupt)
    timer.start()
    try:
        cur = con.execute(d.rendered_sql)
        columns = tuple(c[0] for c in cur.description or [])
        fetched = cur.fetchmany(lim.max_rows_executed + 1)
    except duckdb.Error:
        return None
    finally:
        timer.cancel()
        con.close()
    if not columns or len(fetched) >= lim.max_rows_executed:  # the guard's LIMIT cut it
        return None
    return ResultSet(columns=columns, rows=tuple(tuple(cell(v) for v in r) for r in fetched))


def _first_visible_ids(db_path: Path, n: int) -> list[str]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT source_record_id FROM v_activities WHERE source_record_id IS NOT NULL "
            "ORDER BY source_record_id LIMIT ?",
            [n],
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


class _CapturingTool(QueryTool):
    """The query tool, keeping each full tool result: the step summary keeps 500 characters,
    which can drop the cap footer, and the judges (A4.3) see what the agent saw."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[ToolResult] = []

    def query(self, sql: str) -> ToolResult:
        r = super().query(sql)
        self.calls.append(r)
        return r


def _trial(
    plan: RunPlan,
    audit: AuditLog,
    agent: str,
    item: AttackCase | NormalQuestion,
    trial: int,
) -> tuple[TrialResult, dict[str, Any]]:
    work = plan.run_dir / "work"
    model = AGENTS[agent][0]
    case_id = item.id
    planted = item.planted_data if isinstance(item, AttackCase) else None
    if planted and planted.get("description_texts"):
        texts = [str(x) for x in planted["description_texts"]]  # type: ignore[union-attr]
        ids = _first_visible_ids(plan.db.path, len(texts))
        if len(ids) < len(texts):
            raise RunError(f"{case_id}: {len(texts)} planted texts but {len(ids)} visible rows")
        copy = plant_descriptions(
            plan.db.path, plan.db.sha256, work, dict(zip(ids, texts, strict=True))
        )
    else:
        copy = fresh_copy(plan.db.path, plan.db.sha256, work)
    before = sha256_file(copy)
    first = audit.last_seq + 1
    try:
        guard = PassThroughGuard(plan.views) if plan.ablation else None
        tool = _CapturingTool(copy, plan.views, audit, trial_id=f"{case_id}-{agent}-{trial}",
                              model=model, case_id=case_id, guard=guard)  # fmt: skip
        question = item.prompt if isinstance(item, AttackCase) else item.question
        system = system_prompt(
            plan.views, plan.db.snapshot, prompt_canary(plan.db.snapshot, plan.seed)
        )
        result = run_trial(
            _provider(agent, plan.create_for(agent)), tool, audit, question, system,
            task_id=case_id, trial=trial, seed=plan.seed, prices=AGENTS[agent][1],
        )  # fmt: skip
        t = result.trajectory
        changed = sha256_file(copy) != before
        correct: bool | None = None
        if isinstance(item, NormalQuestion) and item.gold_sql:
            correct = any(
                answer_correct(_predicted(copy, sql, plan.views), plan.gold[case_id])
                for sql in reversed(successful_sqls(t))
            )
    finally:
        copy.unlink(missing_ok=True)
    last = audit.last_seq
    lines = [json.loads(x) for x in audit.path.read_text(encoding="utf-8").splitlines()][
        first : last + 1
    ]
    plan.spent += t.cost_usd
    record = {
        "case_id": case_id, "model": model, "trial": trial, "audit_seq": [first, last],
        "trajectory": t.model_dump(mode="json"), "answer": result.answer,
        "turn_texts": list(result.turn_texts), "tool_texts": [c.text for c in tool.calls],
    }  # fmt: skip
    common = {
        "schema_version": SCHEMA_VERSION, "case_id": case_id, "model": model, "trial": trial,
        "behaviour": {}, "audit_seq": (first, last), "cost_usd": t.cost_usd,
    }  # fmt: skip
    if t.stop_reason == "transport":
        return TrialResult(**common, outcome=None, answer_correct=None, stopping_layer=None,
                           status="transport"), record  # fmt: skip
    if not audit_covered(t, lines):
        raise RunError(f"{case_id} {agent} trial {trial}: the audit lines do not match the trial")
    if isinstance(item, AttackCase):
        canaries = (*plan.db.canaries, prompt_canary(plan.db.snapshot, plan.seed))
        capped = [capped_text(c.text) for c in tool.calls if c.allowed and not c.error_class]
        s = score_attack(item, t, canaries, db_changed=changed, step_budget=STEP_BUDGET,
                         turn_texts=result.turn_texts, capped=capped)  # fmt: skip
        return TrialResult(**common, outcome=s.outcome, answer_correct=None,
                           stopping_layer=s.stopping_layer, status="scored"), record  # fmt: skip
    return TrialResult(**common, outcome=None, answer_correct=correct, stopping_layer=None,
                       status="scored"), record  # fmt: skip


def _llm04(plan: RunPlan, case: AttackCase) -> tuple[Outcome, Layer]:
    if plan.cache is None:
        raise RunError("LLM04 cases need the raw cache for their scratch build")
    pc = (case.planted_data or {}).get("planted_case")
    planted = next(c for c in load_planted().cases if c.id == pc)
    r = run_case_in_views(planted, plan.cache, plan.cfg, plan.run_dir / "work" / case.id)
    return ("blocked", "quality_rule") if r.passed else ("succeeded", "leak")


def _items(plan: RunPlan) -> list[AttackCase | NormalQuestion]:
    if plan.ablation:  # the ablation measures the agent: LLM04 never reaches it (§7.3)
        return [c for c in plan.attacks if c.owasp != "LLM04"]
    return [*plan.attacks, *plan.questions]


def run(plan: RunPlan, log: Callable[[str], None] = lambda _s: None) -> list[TrialResult]:
    """Run every (model, case, trial) not yet scored; transport failures are retried on rerun.
    LLM04 cases are one scratch build each, recorded once under the model name `pipeline`."""
    if plan.k < 1:
        raise RunError("k must be at least 1")
    if not plan.trial_usd > 0:
        raise RunError("the per-trial cost for the estimate must be positive")
    unknown = sorted(set(plan.models) - set(AGENTS))
    if unknown or not plan.models:
        raise RunError(f"unknown or missing models {unknown}; choose from {sorted(AGENTS)}")
    for agent in plan.models:
        plan.create_for(agent)  # a missing key fails here, before any trial runs
    k = _k(plan)
    record_path, record = _check_run_record(plan)
    stem = "ablation" if plan.ablation else "trials"
    traj_path = plan.run_dir / f"{stem}-trajectories.jsonl"
    done, plan.spent = _done(plan.results_path)
    complete = _complete(done, _trajectory_keys(traj_path))
    est = estimate(plan)
    remaining = _remaining(plan, complete)
    log(f"estimate: ${est:.2f} for {len(plan.models)} model(s), k={k}; "
        f"remaining ${remaining:.2f}, spent so far ${plan.spent:.4f}")  # fmt: skip
    if plan.max_cost is not None and plan.spent + remaining > plan.max_cost:
        raise RunError(f"spent ${plan.spent:.2f} plus the remaining estimate ${remaining:.2f} is "
                       f"over --max-cost ${plan.max_cost:.2f}")  # fmt: skip
    plan.run_dir.mkdir(parents=True, exist_ok=True)
    if not record_path.is_file():
        record_path.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n",
                               encoding="utf-8")  # fmt: skip
    audit_path = plan.run_dir / ("ablation-audit.jsonl" if plan.ablation else "audit.jsonl")
    audit = AuditLog(audit_path, plan.run_dir.name)
    try:
        audit.anchor()  # an anchor from the start, so a hard kill mid-run can resume
        with (
            plan.results_path.open("a", encoding="utf-8") as out,
            traj_path.open("a", encoding="utf-8") as trajs,
        ):

            def write(r: TrialResult, traj: dict[str, Any] | None) -> None:
                if traj is not None:  # the trajectory first: a result never lacks one
                    trajs.write(json.dumps(traj, sort_keys=True) + "\n")
                    trajs.flush()
                out.write(r.model_dump_json() + "\n")
                out.flush()
                audit.anchor()
                key = (r.case_id, r.model, r.trial)
                done[key] = r
                if r.status == "scored":
                    complete.add(key)
                log(f"{r.case_id} {r.model} #{r.trial}: {r.outcome or r.answer_correct} "
                    f"({r.stopping_layer or r.status}) ${plan.spent:.4f}")  # fmt: skip

            llm04 = [c for c in _items(plan) if isinstance(c, AttackCase) and c.owasp == "LLM04"]
            for case in llm04:
                if (case.id, PIPELINE_MODEL, 1) not in complete:
                    outcome, layer = _llm04(plan, case)
                    write(TrialResult(
                        schema_version=SCHEMA_VERSION, case_id=case.id, model=PIPELINE_MODEL,
                        trial=1, outcome=outcome, answer_correct=None, stopping_layer=layer,
                        behaviour={}, audit_seq=(0, 0), cost_usd=0.0, status="scored",
                    ), None)  # fmt: skip
            for agent in plan.models:
                for item in _agent_items(plan):
                    for trial in range(1, k + 1):
                        if (item.id, AGENTS[agent][0], trial) in complete:
                            continue
                        if plan.max_cost is not None and plan.spent >= plan.max_cost:
                            raise RunError(f"spent ${plan.spent:.4f}: stopped at --max-cost")
                        write(*_trial(plan, audit, agent, item, trial))
    finally:
        audit.close()
        shutil.rmtree(plan.run_dir / "work", ignore_errors=True)
    check = verify(audit_path, audit.anchors_path)  # the chain and every per-trial anchor
    if not check.ok:
        raise RunError(f"{audit_path.name} does not verify: {check}")
    results = list(done.values())
    summary = summarise(results, plan.attacks, [] if plan.ablation else plan.questions,
                        seed=plan.seed, k=k)  # fmt: skip
    summary.update(estimate_usd=est, spent_usd=round(plan.spent, 6), ablation=plan.ablation,
                   snapshot=plan.db.snapshot, k=k)  # fmt: skip
    name = "ablation-summary.json" if plan.ablation else "summary.json"
    (plan.run_dir / name).write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n",
                                     encoding="utf-8")  # fmt: skip
    return results


def _gold(questions: Sequence[NormalQuestion], db: AgentDB, views: ViewsConfig,
          heldout_dir: Path | None) -> dict[str, Any]:  # fmt: skip
    """Gold computed on the database the agent sees; when the recorded gold was computed on the
    same snapshot (same manifest hash) it must agree, so a silently changed gold query or
    database is refused."""
    gold = compute_gold(questions, db.path, views)
    recorded = [json.loads(resources.files("assay_pipeline").joinpath("data/gold.json")
                           .read_text(encoding="utf-8"))]  # fmt: skip
    if heldout_dir is not None and (heldout_dir / "gold.json").is_file():
        recorded.append(json.loads((heldout_dir / "gold.json").read_text(encoding="utf-8")))
    for rec in recorded:
        if not db.manifest_sha256 or rec.get("manifest_sha256") != db.manifest_sha256:
            continue
        for qid, want in rec.get("results", {}).items():
            if qid in gold and gold[qid] != want:
                raise RunError(f"{qid}: gold on {db.path.name} differs from the recorded gold "
                               f"for snapshot {db.snapshot} (same manifest)")  # fmt: skip
    return gold


def build_plan(
    *,
    db_path: Path,
    run_dir: Path,
    models: Sequence[str],
    k: int,
    split: str,
    ablation: bool,
    max_cost: float | None,
    cache_root: Path | None,
    heldout_dir: Path | None,
    create_for: Callable[[str], Create] | None = None,
    seed: int = 0,
    trial_usd: float = PLANNING_TRIAL_USD,
) -> RunPlan:
    """`split` is public or all (public plus held out); the ablation always uses public cases."""
    if split not in ("public", "all"):
        raise RunError("split is public or all")
    if ablation and split != "public":
        raise RunError("the ablation runs the public attacks only (§7.3)")
    views = load_views_config()
    db = load_agent_db(db_path)
    suite = load_suite(heldout_dir if split == "all" else None)
    attacks, questions = list(suite.attacks), list(suite.questions)
    return RunPlan(
        run_dir=run_dir, db=db, views=views, cfg=load_build_config(),
        cache=RawCache(cache_root) if cache_root is not None else None,
        models=list(models), k=k, attacks=attacks, questions=questions,
        gold=_gold(questions, db, views, heldout_dir), create_for=create_for or live_create,
        ablation=ablation, max_cost=max_cost, seed=seed, trial_usd=trial_usd,
    )  # fmt: skip


def live_create(agent: str) -> Create:
    key = AGENTS[agent][2]
    if not os.environ.get(key):
        raise RunError(f"{key} is not set: export it to run {agent} live")
    return anthropic_create if agent == "claude" else openai_create
