# S3.1 acceptance criteria

**GRD-01** Empty input, unparseable text and non-positive LIMITs are `parse_error`, several statements (including ones hidden after comments) are `multiple_statements`, every non-SELECT statement in the corpus is `not_select`, and recursive CTEs are `recursive_cte`.
**GRD-02** Every table reference is resolved per scope: restricted, qualified and misspelled tables are `table_not_allowed` wherever they appear (joins, unions, subqueries, EXISTS, nested CTEs out of scope), a CTE named like a view is `cte_shadows_view`, and a string-literal table is `file_reference`.
**GRD-03** Table functions and any function outside the allow-list are `function_not_allowed`, and catalog schemas and catalog-named tables are `catalog_access`; allowed functions and SELECT grammar (CASE, EXISTS, AND/OR, arithmetic, IN, BETWEEN, LIKE) pass.
**GRD-04** The outer LIMIT is rewritten to the smaller of the existing literal and 1,000 (1,000 for a non-literal or absent limit, set operations included), and inner limits cannot raise it.
**GRD-05** Only the comment-free re-rendered SQL is ever executed, it re-checks to the identical decision, a blocked decision never reaches the connection, and the guard never raises on any input (fuzzed).
**GRD-06** A query running past the timeout is interrupted and reported as `timeout` well before it could finish, with the generic timeout message.
**GRD-07** Every database error reaches the model only as one of the generic messages, so no table name, column suggestion or database text leaks, and a restricted canary never appears in any text the model is given.
**GRD-08** The corpus holds at least 80 blocked statements covering every reason code with its exact expected reason and at least 40 allowed statements whose renderings run on DuckDB, and a property test shows any composition containing a denied fragment is blocked.
**GRD-09** The model sees at most 50 rows and 6,000 characters plus the total row count, while the full result of up to 1,000 rows is hashed deterministically into `result_sha256`.
