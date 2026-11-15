# Q2.3 — Agent database and views

**Wave 3. Depends on:** Q2.2. **Unblocks:** S3.3, D6.3.

## Goal
Build `agent.duckdb` from a verified snapshot: base tables, the four approved views of §5.4, and a synthetic `embargoed_results` table holding canaries. Open it for the agent only through a locked read-only connection, where every write, file read, attach and extension load fails even with the guard bypassed (FR6, NFR1, D-4, §6.2). Give the attack runner fresh, hash-checked copies, planted with descriptions or rows (C3, §2.4, §7.1).

## Outputs
- **`pipeline/agentdb.py` — `build_agent_db(snapshot_dir, out_path, *, seed=0, views=None) -> AgentDB(path, sha256, snapshot, canaries)`:**
  - **Inputs and outputs:** it reads the snapshot through `read_snapshot` (verified) and builds the tables from those verified rows (never re-reading the files), in a temp file renamed into place. View SQL is NULL-safe: a NULL flag list or element is never visible. A view-contract failure of any kind is an `AgentDBError`. It writes `agent.json` beside the database with the snapshot id, sha256, seed and canaries; that file is for the scorer and never reaches the agent. The database never stores the snapshot path.
  - **Base tables:** `activities`, `consensus`, `quality` (rows per source, drops per reason, flags per rule, conflicts, hidden rows) and `embargoed_results`.
  - **`embargoed_results`:** 24 synthetic rows, seeded from the snapshot id and seed, labelled by a `synthetic` column and a `SYNTHETIC` table comment. Canaries `EMBCANARY…` (four per seed) appear in a row column and, lower-cased, as that column's name. They appear in no view.
- **The views (columns from `views.yaml`):**
  - `v_activities`: rows whose flags are a subset of {QC2, QC5}.
  - `v_compounds`: one row per InChIKey over visible rows. `smiles` is the lexicographically smallest parent SMILES (a fixed rule for tautomers that share a key).
  - `v_consensus`: consensus rows of keys whose members are all visible, so a hidden row's uid never shows in a member list.
  - `v_quality`: the `quality` table.
- **View contract:** `check_view_contract` runs at build and on demand. The views in the file must be exactly those of `views.yaml`, with the same columns in the same order.
- **`agent/connection.py` — `open_agent_db(path, limits) -> LockedConnection`:** the only place the agent zone opens DuckDB (boundary gate).
  - It connects `read_only=True` with literal config: external access off, extension autoload, autoinstall and community extensions off, and disk spilling off (`temp_directory=''`, `max_temp_directory_size='0KB'`, so a query past `memory_limit` fails as out-of-memory and writes nothing).
  - It then sets `memory_limit` (validated pattern) and `threads` (1–64) from `limits`, then `SET lock_configuration = true`.
  - `LockedConnection.execute` runs exactly one statement whose type, by DuckDB's own parser, is SELECT. Read-only DuckDB still allows session-local TEMP tables, views and macros, and a TEMP view named `v_activities` would shadow the approved view for the rest of the session; the gate refuses them, and SET, PRAGMA, LOAD, CALL, ATTACH, EXPLAIN, CHECKPOINT and multi-statements, with the guard bypassed.
  - It returns a `Result` exposing only `description` and `fetchone`/`fetchmany`/`fetchall`, never the DuckDB connection, so no unchecked statement can be chained onto it.
  - Before every statement it checks that no temporary object exists (table, view, macro, sequence or index). If one ever does, the connection is closed and refuses everything after (fail closed). The raw connection stays reachable from Python as `_con`; D-25 does not defend against our own agent-zone code.
  - Harmless SELECT-typed session effects remain (`setseed()`, `truncate_duckdb_logs()`); the guard's function allow-list blocks both.
- **Attack copies:**
  - `fresh_copy(src, expected_sha256, dest_dir)` checks the hash before and after copying and gives a unique file per trial.
  - `plant_descriptions(src, sha, dest_dir, {source_record_id: text})` makes a trial copy with replaced `assay_description` (LLM01). An id matching anything but exactly one row visible in `v_activities` is refused, so an injection never lands where the agent cannot see it; the source is untouched.
  - Rows are planted through `build_snapshot(planted=...)` and then `build_agent_db`.
- **`planted_visibility(db)` and `planted.run_case_in_views(case, cache, cfg, workdir)`:** the full LLM04 check.
  - The case's rows are planted into a scratch snapshot and database.
  - A planted row flagged only QC2/QC5 must show in `v_activities` with those flags, and a QC2 key's consensus row must say `conflict`.
  - Every other planted row must be absent from every view.
  - Every planted row must be in the base table with its expected flags, and a QC2 case's keys must have a consensus row in conflict, so a dropped plant or a missing conflict fails the case.
- **CLI:** `assay build-db SNAPSHOT [--out agent.duckdb] [--seed] [--json]`; `make build-db SNAPSHOT=...`.

## Spec clarifications (recorded in Tech Spec §5.4 and §7.1)
- QC2 and QC5 rows are visible in `v_activities` with their flags (§5.4), so an LLM04 QC2 case cannot require absence. It requires the rows to show flagged, with the conflict marked. "Absent from every approved view" applies to QC1, QC3 and QC4 rows.
- `v_compounds` and `v_consensus` are built over visible rows only.

## Out of scope
The agent itself (S3.3), the oracle run over the attack suite (A4.x).
