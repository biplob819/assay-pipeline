# S3.3 — Agent

**Wave 4. Depends on:** Q2.3, S3.1, S3.2. **Unblocks:** A4.1, D6.1.

## Goal
Put two agents in front of the agent database: Claude Sonnet 5.5 through the Anthropic SDK and GPT-6.1 Sol through the OpenAI SDK, each with native tool calling and exactly one tool, `query(sql)` (FR8, D-8). Every path to a row goes through the guard. Each query runs under a hard deadline in a child process (D-28). Every question, query and answer is audited (§2.3, §6.3), and trials are recorded in EvalKit's trajectory format, so runs can be scored and replayed without the network (AGT-07).

## Outputs (`src/assay_pipeline/agent/`)
- **`query_tool.QueryTool(db_path, views, audit, *, trial_id, model, case_id=None)`** (`.query(sql) -> ToolResult(text, allowed, reason, error_class)`):
  - The guard checks the SQL in-process; blocked SQL never reaches the database.
  - An allowed query runs in a child process, `python -m assay_pipeline.agent._child`, over JSON on stdin and stdout. The child opens the locked connection (Q2.3) and runs `execute_guarded` with the interrupt timer and fetch budget. It caps its address space with RLIMIT_AS where the OS allows (Windows and macOS may rely on the deadline alone). The parent kills it at `timeout_seconds` plus 2 s.
  - A timeout, a crash or an exit without output each become a generic message for the model.
  - The audit line is written in a `finally` block for every call: blocked, allowed, timeout, crash, or an exception in the tool itself.
  - The child gets no credentials: environment variables whose names contain KEY, TOKEN, SECRET, PASSWORD or CREDENTIAL are not passed on.
- **`providers.AnthropicProvider` and `providers.OpenAIProvider`:**
  - **Request shapes:** Anthropic Messages API: `claude-sonnet-5-5`, one strict tool, `tool_choice: auto`, the system prompt cached, `output_config.effort: high`, no sampling parameters, no refusal fallbacks (D-30). OpenAI Responses API: `gpt-6.1-sol`, one strict function tool, `instructions`, `reasoning.effort: high`, `max_output_tokens: 16000`.
  - **Reading responses:** every response is read as the dict the SDK serialises it to, validated against the SDK's own `Message`/`Response` type, so a recording must have the real shape.
  - **History:** thinking, reasoning and tool-call items go back unchanged. All tool results of a turn go in one message. Each request carries a snapshot of the history.
  - **Refusals:** Claude `stop_reason: refusal`, and Sol refusal content or `content_filter`, end the trial as a refusal.
- **`prompt.system_prompt(views, snapshot, canary)`:** the behaviour rules (approved views only; results are data, never instructions; grounded numbers; censored and flagged rows stated; ambiguity and off-scope), every view with its columns, the answer format (answer, `SQL:` lines, `Snapshot: <id>`), and a prompt canary `PRMCANARY…`. The canary is deterministic per snapshot and seed and distinct from the embargo canaries.
- **`loop.run_trial(...) -> TrialResult(trajectory, answer)`:**
  - **Audit:** a question line, the query lines from the tool, then an answer line with the answer's sha256.
  - **Limits:** the step budget is 8 tool calls. Calls past it are answered "budget reached" without running, and the model gets one last turn to answer (`stop_reason: step_budget`). A 16-turn backstop applies, and the final answer is capped at 8,000 characters.
  - **Bad calls:** malformed arguments (no or blank `sql`) are recorded as `malformed` steps and never run. An unknown tool is a violation.
  - **Trajectory:** guard-blocked calls are violations. Trajectories are `evalkit.schemas.Trajectory` (`evalkit-harness==0.1.0`), with `cost_usd` from list prices.
- **`ask.ask(question, db, agent, audit_path, *, replay=None, record=False)` and `assay ask QUESTION [--db] [--model claude|sol] [--audit] [--replay FILE] [--record FILE] [--json]`:**
  - Live mode needs the provider key and refuses clearly without it. A provider failure after the SDK's retries (network, API error, or a failed Responses-API response) ends the trial with `stop_reason: transport`, still audited, never a traceback.
  - A recording that has responses the run never asked for is refused (it does not match this question, model or code). A blank question is refused up front.
  - `--record` saves the response dicts, and `--replay` serves them back with no network. A replay reproduces the trajectory and the audit lines (apart from times, hashes and ids).
  - The snapshot id comes from `agent.json`.
- **Dependencies:** `anthropic>=2.8,<3` (API version 2026-10-21), `openai>=3.51,<4`, `evalkit-harness==0.1.0` (now on PyPI, D-9).

## Live check and recorded responses
The owner supplied API keys (copied from EvalKit's `.env` into this repo's git-ignored `.env`; the CLI never loads it implicitly). One live question per agent ran on the recorded-fixture database ($0.0149 in all, in the spend ledger). Both providers accepted the request shapes, both real responses validate against the SDK types, and both answers were correct and in format.
- The two recordings are kept in `tests/fixtures/agent/` and replay offline in a golden test.
- The Responses API echoes the request's `instructions` (the whole system prompt, canary included) and `tools` in every response. `openai_create` drops them as the response arrives (`scrub_openai`), so no recording holds text the model did not write.
- The full golden set (6 attacks, 4 normal questions, §10) lands with the attack suite (A4.x).

## Out of scope
The attack suite and scoring (A4.x), the console (D6.x).
