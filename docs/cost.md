# Controlling cost

Building snapshots costs nothing: ChEMBL and PubChem are free, and ingest is rate-limited to their published limits. Only the agent stages call paid model APIs. `assay attack` and `assay behaviour` print their estimate first and take a cap; `assay ask` makes one trial and reports its cost afterwards.

| Command | What it calls | Measured (snapshot 2026-11-15.1) |
| --- | --- | --- |
| `assay ask` | one agent trial | about $0.003-0.02 a question |
| `assay attack --k 3 --split all` | 2 agents x (50 attacks + 30 questions) x 3 | $3.50 for 480 trials |
| `assay attack --ablation` | 2 agents x 40 public attacks x 1 | $0.38 |
| `assay behaviour` | 2 judges x 96 judged answers | $0.72 |

- `--dry-run` prints the estimate (from a planning figure per trial, `--trial-usd` to override) and makes no call.
- `--max-cost` refuses a run whose estimate exceeds it and stops a run whose spend reaches it; on resume, spend so far plus the remaining estimate is checked. `assay behaviour --max-cost` works the same way for the judges.
- Spend is counted from the providers' own usage fields, cached input included.
- Keys come only from your environment; the CLI never reads a `.env` file.
