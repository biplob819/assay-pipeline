# Quickstart

Build a trusted snapshot of public EGFR IC50 data with no API keys, then (optionally) put an agent in front of it and attack it.

## 1. Install

```bash
uv tool install assay-pipeline   # or: pip install assay-pipeline (Python 3.11 to 3.15)
assay doctor                     # Python, keys (set or not), network, local state
```

To try it without installing, prefix each command with `uvx --from assay-pipeline`, for example `uvx --from assay-pipeline assay doctor`. From a clone instead: `uv sync` and prefix commands with `uv run`.

## 2. Build a snapshot (no keys)

```bash
assay ingest chembl            # ChEMBL activities for the target, into ./cache
assay ingest pubchem           # PubChem bioassays, ChEMBL depositions dropped
assay snapshot                 # normalise, flag (QC1-QC5), write snapshots/<date>.<n>/
```

The snapshot folder holds `activities.parquet`, `consensus.parquet`, `drops.parquet`, a hashed `manifest.json`, `DATA_CARD.md` and `DATA_LICENSE.md`. Run `assay snapshot` again: the same inputs give the same id and the same bytes. A full EGFR build takes about 10 minutes, almost all of it rate-limited downloads.

## 3. Put an agent in front of it (keys)

```bash
assay build-db snapshots/<id>                       # agent.duckdb: four approved views + a synthetic restricted table
export ANTHROPIC_API_KEY=...                        # or OPENAI_API_KEY for --model sol
assay ask "How many compounds have an exact IC50?" --db agent.duckdb
```

Every question, query and answer is written to a hash-chained audit log; `assay verify-audit audit/ask.jsonl` re-checks it.

## 4. Attack it (keys, a few dollars)

```bash
assay attack --db agent.duckdb --k 3 --dry-run       # prints the estimate, makes no call
assay attack --db agent.duckdb --k 3 --max-cost 10   # the run; resumable with the same --run-id
assay attack --db agent.duckdb --ablation            # the same attacks with the SQL guard off
assay behaviour --run-id <id> --db agent.duckdb      # behaviour rules and the two judges
assay report --run runs/<id> --snapshot <snapshot-id>  # the dashboard, docs/report/index.html
```

See [controlling cost](cost.md) before a full run and [reading the dashboard](reading-the-dashboard.md) after.
