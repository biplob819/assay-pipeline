# S3.1 — SQL guard

**Wave 3. Depends on:** N1.1. **Unblocks:** S3.3.

## Goal
The only path from the agent to any row (Tech Spec §2.3, §6.1, D-5): a default-deny check over a sqlglot AST that allows one plain SELECT over the approved views, runs only SQL re-rendered from its own parse, and executes it under a timeout with sanitised errors and a capped view of the result for the model.

## Outputs (`src/assay_pipeline/guard/`; new dependencies `sqlglot`, `duckdb`)
- `SqlGuard(views_config).check(sql) -> GuardDecision`, never raising (any internal failure is a block). Checks, first failure wins, reason codes from §6.1:
  1. parses (DuckDB dialect) to exactly one statement (`parse_error`, `multiple_statements`);
  2. the root is a SELECT or a UNION / INTERSECT / EXCEPT of SELECTs (`not_select`); unparseable statements (EXPORT, IMPORT) are `parse_error`;
  3. no recursive CTE (`recursive_cte`); no CTE named like an approved view, wherever defined (`cte_shadows_view`);
  4. every table reference is a bare identifier: table functions (`read_*`, `query`, `query_table`, `glob`, `range`, `duckdb_*()`) are `function_not_allowed`; `information_schema`, `pg_catalog`, `duckdb_*` and `pg_*` names are `catalog_access`; a quoted name that came from a string literal (`FROM 'x.parquet'`) is `file_reference`; a schema- or catalog-qualified name is `table_not_allowed`; and, resolved per scope with sqlglot's scope analysis (so a CTE is visible only where SQL makes it visible), every table is an approved view or an in-scope CTE (`table_not_allowed`);
  5. every function maps to the allow-list (`count, sum, avg, median, min, max, quantile_cont, stddev_samp, abs, round, log10, ln, exp, power, sqrt, lower, upper, length, trim, contains, starts_with, coalesce, nullif, cast`; `CASE`, `IF`, `EXISTS`, `AND`, `OR` are grammar, not calls) (`function_not_allowed`);
  6. every syntax node is plain SELECT grammar (no PIVOT, QUALIFY, SAMPLE, arrays, structs, lambdas, parameters) (`not_select`); a non-positive literal LIMIT is `parse_error`;
  7. the outer LIMIT becomes `min(existing literal, 1000)` (a non-literal limit becomes 1000);
  8. the SQL that runs is the AST re-rendered without comments, and it must pass all checks again unchanged.
- `Guard.version`: a hash of the allow-lists, node set and sqlglot version.
- `execute_guarded(con, decision, limits) -> QueryResult`: runs only `rendered_sql`; a `threading.Timer` calls `con.interrupt()` at the timeout; every database error becomes one of the generic messages (never the database's text, so "Did you mean …" cannot leak a table name); the model sees at most 50 rows and 6,000 characters plus the total count; the full result (at most 1,000 rows) is hashed (`result_sha256`).
- `tests/security/guard_corpus.yaml`: at least 80 blocked statements (each with its exact reason, every reason covered) and 40 allowed ones whose rendering runs on a DuckDB stand-in for the views.

## Rules
- The guard is pure: no I/O, no clock. `execute_guarded` takes a connection; opening the agent's locked connection is Q2.3's job.
- `SqlGuard` refuses a views config whose functions it cannot map.

## Out of scope
The locked read-only connection and permission tests (Q2.3), the audit log (S3.2), the agent loop (S3.3).

## Build notes
- `sqlglot` is pinned below 31 (`>=30.31,<31`): the guard's security rests on its parser, and a major release may change how statements parse; its version is also hashed into `Guard.version`. A sqlglot upgrade is a guard change and reruns this node's verifier.
- Corpus: 109 blocked statements over every reason code and 42 allowed ones; property tests (compositions with denied fragments, token soup, arbitrary text).
- Verifier pass 1 (attacker): no restricted row could be read, but a guard-allowed CTE chain defeated the interrupt and memory_limit in DuckDB's planner. Added (D-28): complexity bounds (2,000 nodes, depth 64, 16 tables after CTE expansion, refused as `not_select`); keyword functions as bare columns refused (`function_not_allowed`); LIMIT/OFFSET must be integer literals (LIMIT ≥ 1, OFFSET ≥ 0) else `parse_error`; casts only to scalar types; `tables` lists only the approved views read, never CTE names; execution caps each cell at 200 characters with line breaks and pipes escaped, keeps the 6,000-character cap absolute (headers included), and stops fetching at about 8 MB.
- S3.3 must run queries in a child process killed at the timeout plus grace (D-28); the interrupt is a first line, not the guarantee.
- Verifier pass 2 (attacker): still no leak; two more blow-up classes found (lateral column aliases binding exponentially; string doubling through nested derived tables, 17.8 GB at memory_limit 1 GB), plus a fetch budget checked only per 100-row chunk and a single-column footer forgery. Added: lateral-alias, concat-duplication and derived-depth refusals; row-by-row fetch; `[result]` footer with `[` escaped and every line-boundary character escaped; footer reserve sized from the real footer. D-28 now states that the availability guarantee is S3.3's isolated child process (deadline + memory cap), not the guard (L-19).
- Verifier pass 3 (attacker): GRD-09 confirmed, no leak; both new rules were evaded by renaming (alias chains named after view columns; string growth with distinct names per CTE level). Made structural: lateral aliases are refused by cross-reference whatever the name; a growth factor per output column replaces the duplicate-name rule; FROM-less column references are refused; cell text is NFKC-normalised before escaping (full-width look-alikes of the footer marker). False positives fixed: one-step aliases in GROUP BY and HAVING, qualified `a.x || c.x`.

## Update 2026-11-17 (OWNER D-40)
Hand-read follow-ups of run main-2026-11-16. The D-28 resource bounds now block as `too_complex` instead of `not_select`, so the audit shows a depth or fan-out cap apart from a write attempt. The new reason is carried by audit format 1.1 (`AUDIT_SCHEMA_VERSION`); snapshots and run records keep `SCHEMA_VERSION` 1.0, so no published snapshot or manifest changes. The capped-result footer now reads `N shown of at least M rows (row or size limit reached)`, because both agents read `18 of 1000 rows shown` as the size of the view; `capped_text` also reads the old footer so finished runs rescore the same. Decisions and allow-lists are unchanged: only reason labels and footer words differ.
