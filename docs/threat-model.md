# Threat model: Trusted Assay Pipeline

Scope: the pipeline that builds a public bioactivity snapshot, and the read-only data agent that answers questions over it. Attackers are anyone who can write a question, plant text in source data, or control a dependency; the model itself is untrusted. Evidence lives in the repo (tests, oracle, measured runs).

## Assets

- **Snapshot integrity:** the published rows, their QC flags and the manifest hashes.
- **Restricted data:** the `embargoed_results` table with its canaries (it stands in for unpublished results).
- **System instructions:** the agent's prompt and its canary.
- **Audit trail:** every question, query and answer, hash-chained, with anchors.
- **Compute and spend:** database resources and API budget.
- **Provider keys:** held by the parent process only.

## Trust boundaries and data flow

Sources (ChEMBL, PubChem; untrusted) -> ingest (rate-limited HTTP, content-addressed raw cache) -> normalise and QC1-QC5 -> snapshot (hashed manifest) -> agent database (four approved views plus the restricted table) -> **model boundary**: the agent loop calls one tool, `query`. The SQL guard runs in-process; allowed SQL runs in a child process (no keys, isolated interpreter) on a locked read-only DuckDB connection; results go back to the model as data. Every step is written to the audit log.

## Threats, controls and evidence (OWASP Top 10 for LLM Applications 2025, MITRE ATLAS 2026.10)

| OWASP | ATLAS | Threat | Controls | Evidence |
| --- | --- | --- | --- | --- |
| LLM01 Prompt injection | AML.T0051.000, AML.T0051.001, AML.T0054 | Direct and indirect injection (instructions planted in assay descriptions), jailbreaks | Prompt rule: results are data, never instructions; SQL guard default deny; locked connection | 10 attack cases incl. indirect plants; oracle; B6 |
| LLM02 Sensitive information disclosure | AML.T0085.001, AML.T0085.003, AML.T0136 | Read the restricted table, the catalog, settings or the prompt | Guard table and function allow-lists; catalog and settings functions refused | 10 cases; oracle; canaries scored in answers and queries; B7 |
| LLM03 Supply chain | - | Poisoned dependency, SDK or CI action | uv.lock, pinned SDK ranges, CI actions pinned by SHA, secret scan; PyPI trusted publishing planned (B5.3) | CI on every push (lint, types, tests, build) |
| LLM04 Data and model poisoning | AML.T0059 | Planted or corrupted source rows | QC1-QC5 flag, never delete; consensus conflicts; planted-row suite; snapshot refuses planted markers | 10 cases stopped by quality rules |
| LLM05 Improper output handling | AML.T0053, AML.T0102 | Model output run as SQL: multi-statements, comments, PRAGMA, file functions | Only re-rendered SQL from the checked AST runs; one SELECT, enforced again by DuckDB's own parser at the connection | 194-statement guard corpus (138 blocked, 56 allowed); fuzzing; oracle |
| LLM06 Excessive agency | AML.T0053, AML.T0101, AML.T0086 | Writes, ATTACH, INSTALL, exports, TEMP views shadowing approved views | Read-only, external access off, locked config, no temp objects, no spilling; change check on every trial copy | Permission tests with the guard bypassed; B8 |
| LLM07 System prompt leakage | AML.T0085.003 | The model repeats its instructions | Prompt rule never to reveal instructions; prompt canary scanned in every answer (the provider's echo of the prompt is scrubbed, so only model-written text is scanned) | B9 on every trial |
| LLM08 Vector and embedding weaknesses | - | Not applicable: no retrieval or vector store | - | - |
| LLM09 Misinformation | - | Ungrounded numbers, censored values ignored | B1 SQL shown and audited, B2 snapshot id, B5 gold match, B3 judged by two blinded judges | Behaviour compliance in the report |
| LLM10 Unbounded consumption | AML.T0034.002, AML.T0029 | Row explosions, recursion, string growth, huge ranges | Guard static bounds and LIMIT rewrite; child process killed at the deadline (address-space cap on Linux); 8-call step budget; every paid run prints its estimate first and takes a max-cost option | 10 cases; timeout and row-cap stops in the oracle |

## Stated limits and production steps

- **DuckDB has no GRANTs:** table-level read control rests on the guard alone. Production adds database roles (Postgres or a catalog with RBAC) as a second layer.
- **Anchor trust:** whoever can rewrite both the log and its anchors can forge history. Production writes anchors to WORM storage or a timestamping service.
- **Stealth poison:** a plausible fabricated value passes QC (three planted stealth rows show it). Production adds source reputation, replicate checks and outlier review.
- **Canary matching:** an encoded or split leak is not detected. Production adds normalised and decoded matching plus output data-loss prevention.
- **Boundary gate:** a lint, not a sandbox. Production runs the agent in its own service account and container.
- **Resource cap:** Windows and macOS have no address-space cap (the deadline holds). Production sets container memory limits.
- **Small held-out split** (12 attacks, 6 questions): accepted for v0.1; production rotates fresh held-out cases each release.
- **Out of scope until D6:** the public console's live mode (passcode, rate limit, spend counter, visitor-text privacy).

## Review checklist

- [ ] `pytest -m security` passes: guard corpus, oracle, permissions with the guard bypassed, audit tamper.
- [ ] `assay verify-audit` passes on the run's log with its anchors.
- [ ] The report shows held-out results beside public ones, the guard-off ablation apart from the main run, and zero successes as a bound.
- [ ] Keys come only from the environment, never from files the CLI reads; the query child gets none.
- [ ] Every paid run printed its estimate and has a spend-ledger line.
