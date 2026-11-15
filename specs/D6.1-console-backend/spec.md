# D6.1 — Console backend

**Wave 10. Depends on:** S3.3, A4.1. **Unblocks:** D6.2 (the Streamlit page).

## Goal
The engine behind the public console (Tech Spec §8, US9). A visitor picks a sample attack, or types a question with the passcode, and sees the agent's reply, every guard decision, and the exact audit lines the case wrote. The public cannot run up the owner's bill, and no visitor text is stored.

## Outputs
- **`console/engine.py`:** `Console(db, workdir, ...)` owns a private, hash-checked copy of `agent.duckdb` (`fresh_copy`) and an in-memory audit log. Both reset when the app restarts.
  - `run_case(sample_id=... | question=..., agent, session, passcode)` runs the real agent loop, guard and locked connection, and returns `CaseResult`: reply, guard decisions (SQL, decision, reason, error, rows), the audit lines this case added, cost, stop reason and latency.
  - **Replay** (the default): a sample's recorded responses stand in for the provider through `Replay`. The decisions and audit lines are produced live on the console's copy, with no API call. A recording that no longer matches the code is refused.
  - **Live:** the question must be 1–500 characters. The guard runs first. The request is held at `request_bound_usd`, the provable worst case for the console's limits, then settled to the actual cost. A failed run keeps its whole hold.
  - **Live limits:** 4 queries, 2,000 output tokens per turn (thinking included), and at most 6 turns, each bounded by the whole possible history. That gives a bound of $0.54 a request at current prices.
  - **`AggregateLog`:** persistent JSON lines with only timestamp, mode, model, query counts, stop reason, latency, cost and whether the guard refused. Never question, answer or SQL text (H14).
- **`console/guard.py`:** EvalKit's live-mode guard, ported (same author, MIT).
  - The passcode is compared in constant time.
  - Each session has a rate limit, with a memory bound on how many sessions are tracked.
  - Each request has a cost cap.
  - A daily spend counter is reserved with one atomic `UPDATE ... WHERE usd + est <= cap` (Postgres in production through `ASSAY_CONSOLE_DATABASE_URL` and the `console` extra, or SQLite locally).
  - A store outage refuses readably without echoing the error.
- **`console/audit.py`:** `MemoryAuditLog`, an `AuditLog` subclass with the same validated, hash-chained lines and nothing on disk. It has `since(seq)` for a case's lines and `verify_chain()`.
- **`console/samples.json`:** six samples recorded live on snapshot 2026-11-15.1 (`scripts/record_console_samples.py`, $0.052). They cover LLM02, LLM05, LLM06, LLM07, LLM10 and a normal question, with both agents and no held-out case.

## Out of scope
The Streamlit page, its deployment and secrets (D6.2).
