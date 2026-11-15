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
