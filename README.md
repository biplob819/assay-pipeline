# assay-pipeline

[![CI](https://github.com/biplob819/assay-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/biplob819/assay-pipeline/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/assay-pipeline)](https://pypi.org/project/assay-pipeline/)
[![Python](https://img.shields.io/pypi/pyversions/assay-pipeline)](https://pypi.org/project/assay-pipeline/)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue)](LICENSE)
[![Dashboard](https://img.shields.io/badge/dashboard-live-2a78d6)](https://biplob819.github.io/assay-pipeline/)
[![Console](https://img.shields.io/badge/console-live-ff4b4b)](https://console12as.streamlit.app/)

**Trusted assay data, and an AI agent you can attack.** Public bioactivity data (EGFR IC50 by default, any target from config) from ChEMBL and PubChem, normalised into one versioned table with provenance on every row and quality problems flagged, never deleted. An AI agent answers questions about that table through a SQL guard and read-only access. Every query goes into a tamper-evident audit log, and the agent is attacked with cases mapped to the OWASP Top 10 for LLM Applications (2025) and MITRE ATLAS.

**[Security dashboard](https://biplob819.github.io/assay-pipeline/)** · **[Compound explorer](https://biplob819.github.io/assay-pipeline/explorer.html)** · **[PyPI](https://pypi.org/project/assay-pipeline/)** · **[Quickstart](docs/quickstart.md)** · **[Console guide](apps/console/README.md)** · **[Threat model](docs/threat-model.md)**

| [![The security dashboard: how many attack trials got through, with the 95% upper bound per category and agent](https://raw.githubusercontent.com/biplob819/assay-pipeline/main/docs/assets/screenshots/dashboard.png)](https://biplob819.github.io/assay-pipeline/) | [![The compound explorer: 41,463 activity rows with filters, flags and a link to each source record](https://raw.githubusercontent.com/biplob819/assay-pipeline/main/docs/assets/screenshots/explorer.png)](https://biplob819.github.io/assay-pipeline/explorer.html) |
| :---: | :---: |
| **[Security dashboard](https://biplob819.github.io/assay-pipeline/)**: did any attack get through, what stopped it, what it cost | **[Compound explorer](https://biplob819.github.io/assay-pipeline/explorer.html)**: every published row, filterable, each linked to its source record |
| [![The console replaying a recorded attack: the guard blocks a query on the restricted table and writes audit lines](https://raw.githubusercontent.com/biplob819/assay-pipeline/main/docs/assets/screenshots/console-replay.png)](apps/console/README.md) | [![The console's live mode: pick an agent and pay with your own API key, behind the passcode](https://raw.githubusercontent.com/biplob819/assay-pipeline/main/docs/assets/screenshots/console-live.png)](apps/console/README.md#your-own-api-key) |
| **Console, replay**: a recorded attack through the real guard, no API call | **Console, live**: your own question, on the console's key or your own |

![A 22-second walkthrough: the security dashboard, what stopped each attack, agent behaviour, the comparison with the previous run, the audit trail, the compound explorer, the console replaying a blocked query and the release gate](docs/assets/walkthrough.gif)

Walkthrough of the committed outputs ([shot list](docs/walkthrough.md)).

## What it does

- **A trusted snapshot.** Ingests ChEMBL and PubChem into a content-addressed cache, normalises structures with RDKit and units to nM and pIC50, and flags quality problems (QC1 to QC5) without deleting a row. Each snapshot is deterministic Parquet with a hashed manifest, a data card and a licence file. No API keys needed.
- **An agent behind a guard.** Claude or GPT answers questions through one SQL tool. Every query is parsed and checked against an allow-list of approved views and limits, runs read-only in an isolated process, and is written to a hash-chained audit log with published anchors.
- **An attack suite with honest numbers.** 60 attack cases across the OWASP LLM categories plus 30 normal questions, k trials per case, a scripted attacker with no model in the loop, a guard-off ablation, two judges checked against hand labels, and 95% upper bounds instead of "0%".

## Results

<!-- results:start -->
Run `main-2026-11-18-s3` on snapshot `2026-11-15.1`, from [`docs/results/main-2026-11-18-s3/bundle.json`](docs/results/main-2026-11-18-s3/bundle.json) ([dashboard](https://biplob819.github.io/assay-pipeline/)).

| | |
| --- | --- |
| Snapshot | 41,463 EGFR rows (chembl 26,553, pubchem 14,910); flagged, never deleted: QC1 1,470, QC2 4,414, QC3 174, QC5 21,761 |
| Attack trials that succeeded | **0 of 310**: 300 agent trials (k=3 per case, public and held-out cases) and 10 poisoning cases met by the pipeline itself |
| Claude Sonnet 5.5 | 0 of 150; 95% upper bound 2.0% per trial |
| GPT-6.1 Sol | 0 of 150; 95% upper bound 2.0% per trial |
| What stopped them | model 280, guard 10, quality_rule 10, limit 10 (most attacks were refused by the model before any query ran) |
| Guard off (ablation) | 0 of 80 succeeded: the models refused on their own |
| Scripted attacker (no model) | 60 of 60 cases stopped: guard 45, quality_rule 10, limit 5 |
| Normal questions (public split) | Claude Sonnet 5.5: query matches gold 57%, answer states the gold value 100%; GPT-6.1 Sol: query matches gold 90%, answer states the gold value 90% |
| SQL shown in the answer is what ran (B1, all questions) | Claude Sonnet 5.5 100%; GPT-6.1 Sol 100% |
| Judges | agree with each other on 96 of 96; against 24 hand labels, Opus 24 and Sol 24 |
| Audit log | 1,330 lines, 540 anchors, verifies |
| Spend | $4.13 for the run, the guard-off ablation and the judges |
<!-- results:end -->

What these numbers do and don't show: [reading the dashboard](docs/reading-the-dashboard.md). The short version: the models refused almost every attack themselves, so the guard is tested mainly by the scripted attacker, which sends the attack SQL with no model in the loop. Held-out attack cases stay private so the result can't be tuned to them.

## Get started

Python 3.11 to 3.15 on Linux, macOS or Windows. Building a snapshot needs no API keys.

```bash
uv tool install assay-pipeline      # or: pip install assay-pipeline
assay doctor                        # checks Python, keys (set or not, never shown), network, local state
assay ingest chembl                 # ChEMBL activities for the target, into ./cache
assay ingest pubchem                # PubChem bioassays, ChEMBL depositions dropped
assay snapshot                      # normalise, flag QC1-QC5, write snapshots/<date>.<n>/
```

Then, with a provider key, put an agent in front of it and attack it:

```bash
assay build-db snapshots/<id>                         # agent.duckdb: approved views + a synthetic restricted table
assay ask "How many compounds have an exact IC50?" --db agent.duckdb
assay attack --db agent.duckdb --k 3 --dry-run        # the cost estimate; no call is made
assay attack --db agent.duckdb --k 3 --max-cost 10    # refused if estimated over $10, stopped if spend reaches it
assay report --run runs/<id> --snapshot <snapshot-id> # the dashboard
```

The [quickstart](docs/quickstart.md) walks through each step, [configuring a target](docs/configuring-a-target.md) points the pipeline at another protein, and [controlling cost](docs/cost.md) covers estimates and caps.

## The console

A Streamlit page where anyone can attack the agent and watch the guard and the audit log answer.

- **Replay** (the default) plays a recorded attack through the real guard and a fresh audit log, with no API call. Two scripted samples show the guard blocking queries that the models themselves refused to send.
- **Live** takes your own question, behind a passcode. Pay with the console's key, which draws on a small daily budget, or with **your own Anthropic or OpenAI key**: the passcode is still required, your key goes only to your provider for that one request, and it is never stored, logged or shown.
- Every query's guard decision sits beside the audit line it wrote.

Run it locally against an agent database you built:

```bash
uv sync --extra console
ASSAY_CONSOLE_DB=agent.duckdb ASSAY_CONSOLE_PASSCODE=pick-one uv run streamlit run apps/console/streamlit_app.py
```

Settings, the own-key rules and deployment are in the [console guide](apps/console/README.md).

## How it fits together

```mermaid
flowchart LR
  subgraph build["Pipeline (no model, no keys)"]
    C[ChEMBL API] --> I[ingest<br/>raw cache, hashed]
    P[PubChem API] --> I
    I --> N[normalise<br/>RDKit parent, nM, pIC50]
    N --> Q[flag QC1-QC5<br/>never delete]
    Q --> S[(snapshot<br/>parquet + manifest<br/>+ data card)]
  end
  S --> D[(agent.duckdb<br/>approved views +<br/>synthetic restricted table)]
  subgraph agent["Agent boundary"]
    U[question or attack] --> A[agent<br/>Claude or GPT]
    A -->|SQL| G{SQL guard<br/>parse, allow-list,<br/>limits}
    G -->|allowed| R[read-only<br/>isolated process]
    R --> D
    G --> L[(audit log<br/>hash chain + anchors)]
    R --> L
  end
  L --> E[scoring<br/>EvalKit + judges]
  E --> B[results bundle] --> H[dashboard]
  S --> X[compound explorer]
```

## Rebuild

Re-render the published dashboard from its committed bundle and check it is byte-identical:

```bash
git clone https://github.com/biplob819/assay-pipeline && cd assay-pipeline && uv sync
uv run assay report --bundle "docs/results/$(cat docs/results/LATEST)" --html /tmp/index.html
cmp /tmp/index.html docs/report/index.html && uv run assay doctor --offline
```

Build your own snapshot with no API keys: `assay ingest chembl`, `assay ingest pubchem`, `assay snapshot`. See the [quickstart](docs/quickstart.md).

## Documentation

- [Quickstart](docs/quickstart.md): a first snapshot with no keys, then an agent and an attack run
- [Configuring another target](docs/configuring-a-target.md)
- [Controlling cost](docs/cost.md): estimates, caps and what the published run cost
- [Reading the dashboard](docs/reading-the-dashboard.md)
- [Console guide](apps/console/README.md): replay, live mode, your own key, settings and deployment
- [Threat model](docs/threat-model.md) ([PDF](docs/threat-model.pdf), one page): assets, trust boundaries, OWASP and ATLAS mapping, controls, limits
- [Data roadmap](docs/data-roadmap.md) ([PDF](docs/data-roadmap.pdf), one page): from snapshot to a training set
- Published snapshot: [the Parquet files and manifest](docs/data/2026-11-15.1/), with its [data card](docs/data/2026-11-15.1/DATA_CARD.md) and [data licence](docs/data/2026-11-15.1/DATA_LICENSE.md)
- Published run: [bundle](docs/results/main-2026-11-18/bundle.json), [audit anchors and verify result](docs/results/main-2026-11-18/verify.json), [hand-read of real answers](docs/measurements/hand-read-main-2026-11-18.md), [behaviour and judging](docs/measurements/behaviour-2026-11-18.md)
- Earlier run, with the held-out split: [bundle](docs/results/main-2026-11-16/bundle.json), [verify result](docs/results/main-2026-11-16/verify.json), [hand-read](docs/measurements/hand-read-main-2026-11-16.md)
- [Decision log](docs/decisions.md), [lessons](docs/lessons.md) and [changelog](changelog.md)
- [PRD](Trusted%20Assay%20Pipeline%20PRD.md) and [Tech Spec](Trusted%20Assay%20Pipeline%20Tech%20Spec.md), including the node graph the project was built by and the loophole review of the PRD

## All links

| | |
| --- | --- |
| Source | [github.com/biplob819/assay-pipeline](https://github.com/biplob819/assay-pipeline) |
| Package | [pypi.org/project/assay-pipeline](https://pypi.org/project/assay-pipeline/) · [releases](https://github.com/biplob819/assay-pipeline/releases) |
| Live pages | [security dashboard](https://biplob819.github.io/assay-pipeline/) · [compound explorer](https://biplob819.github.io/assay-pipeline/explorer.html) |
| Build status | [CI](https://github.com/biplob819/assay-pipeline/actions/workflows/ci.yml) · [release workflow](https://github.com/biplob819/assay-pipeline/actions/workflows/release.yml) |
| Get help | [issues](https://github.com/biplob819/assay-pipeline/issues) · [report a vulnerability privately](https://github.com/biplob819/assay-pipeline/security/advisories/new) · [contributing](CONTRIBUTING.md) |
| Data sources | [ChEMBL](https://www.ebi.ac.uk/chembl/) (EMBL-EBI) · [PubChem](https://pubchem.ncbi.nlm.nih.gov/) (NCBI) |
| Frameworks | [OWASP Top 10 for LLM Applications 2025](https://genai.owasp.org/llm-top-10/) · [MITRE ATLAS](https://atlas.mitre.org/) |
| Built with | [EvalKit](https://github.com/biplob819/evalkit) (scoring and the build harness) · [RDKit](https://www.rdkit.org/) · [DuckDB](https://duckdb.org/) · [sqlglot](https://github.com/tobymao/sqlglot) · [Streamlit](https://streamlit.io/) |

## Privacy

No telemetry. The tools send data only to ChEMBL and PubChem (when building a snapshot, and when `assay doctor` checks they can be reached) and to the model providers whose keys you set (when running agents and judges). If you host the console, its live mode keeps a daily spend counter for the console's own keys in the Postgres database you configure; a visitor's own key goes only to that visitor's provider and is never stored or logged. Streamlit, which serves the console, has its usage statistics turned off, but it can look up the server's public IP when it checks a request's origin. Keys are read from your environment only and are never printed or logged. The console keeps visitor text in memory only. See [SECURITY.md](SECURITY.md).

## Licence and attribution

Code: MIT ([LICENSE](LICENSE)). Snapshots and the data embedded in the dashboard and explorer are derived from ChEMBL (EMBL-EBI) and PubChem (NCBI; rows from BindingDB only) and are licensed CC BY-SA 3.0; see the [data licence](docs/data/2026-11-15.1/DATA_LICENSE.md) for the full attribution. Scoring reuses [EvalKit](https://github.com/biplob819/evalkit). The restricted table the attacks aim at, `embargoed_results`, is synthetic.

Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md) and the [code of conduct](CODE_OF_CONDUCT.md).
