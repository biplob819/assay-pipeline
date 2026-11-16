# Trusted Assay Pipeline build graph

Source of truth for build-node dependencies. Parsed by `scripts/harness_lib.py`.
A node may start only when every node in its `Depends on` cell is green (all its rows in `catalog.tsv` are green).
Edge rules: no cycles; a node inherits the constraints of what it depends on; `Verified by` must pass before a node flips green.
Generated from Tech Spec §11.2 on 2026-11-15; edit here from now on and keep the Tech Spec in sync (`test_graph_matches_tech_spec_node_table`).


## Wave 0 — harness

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **W0.1** | Repo and harness | — | node_converge + verifier | Repo `assay-pipeline`, uv project (Python 3.11–3.15), CI on three OSes, ported harness scripts with `--self-test` on every gate, records seeded | — |

## Wave 1 — ingest and normalise

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **N1.1** | Schemas and config | W0.1 | node_converge + verifier | `assay.schemas` (§3), `BuildConfig`, view and allow-list config, format v1 fixtures | US1, US3 |
| **N1.2** | ChEMBL ingest | N1.1 | node_converge + verifier | Resumable activity ingest for the configured target (default CHEMBL203 IC50), raw cache, release lock, recorded fixture | US1 |
| **N1.3** | PubChem ingest | N1.1 | node_converge + verifier | AID discovery, ChEMBL-deposited AIDs dropped, concise data and structures, rate limit, depositor ledger | US1 |
| **N1.4** | Normalise | N1.2, N1.3 | node_converge + verifier | RDKit parent standardisation, nM, pIC50, variant, provenance, drop ledger | US1 |

## Wave 2 — data trust

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **Q2.1** | Quality rules | N1.4 | node_converge + verifier | QC1–QC5 with boundary tests, consensus table, planted-row fixtures for LLM04 | US2, US7 |
| **Q2.2** | Snapshot and manifest | Q2.1 | node_converge + verifier | Deterministic Parquet, manifest, rebuild gate live, `DATA_CARD.md`, `DATA_LICENSE.md` | US3 |
| **Q2.3** | Agent database and views | Q2.2 | node_converge + verifier | `agent.duckdb` with four approved views, synthetic embargoed table with canaries, locked connection, planting for attack copies | US4 |

## Wave 3 — security controls and agent

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **S3.1** | SQL guard | N1.1 | node_converge + verifier | Guard (§6.1), bypass corpus, property tests, error sanitising, timeout | US4 |
| **S3.2** | Audit log | N1.1 | node_converge + verifier | Hash chain, single writer, anchors, `make verify-audit`, tamper tests | US5 |
| **S3.3** | Agent | Q2.3, S3.1, S3.2 | node_converge + verifier | `QueryTool`, agent on Sonnet 5.5 and GPT-6.1 Sol with native tool calling, system prompt, step budget, `assay ask` | US4, US6 |

## Wave 4 — attack and behaviour evaluation

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **A4.1** | Attack suite and normal questions | Q2.1, Q2.3 | node_converge + verifier | 60 attacks and 30 normal questions with OWASP and ATLAS tags, canaries, `attacker_sql`, gold SQL, held-out split, oracle passing | US6, US7 |
| **A4.2** | Attack runner and scoring | A4.1, S3.3 | node_converge + verifier | `assay attack`: trials, fresh copies, EvalKit scorers, stopping layer, ablation, cost estimate and cap, audit-coverage cross-check | US5, US6 |
| **A4.3** | Behaviour scoring | A4.2 | node_converge + verifier | B1–B10 checks, dual judges for B3, B4 and B10, 24 hand labels, agreement report | US6 |

## Wave 5 — reporting and release

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **B5.1** | CISO dashboard | A4.2, A4.3, Q2.2 | node_converge + verifier | `assay report`: attack outcomes by category, model and layer; access events; data-quality tab; snapshot history; deployed | US2, US6 |
| **B5.2** | Threat model | A4.2 | node_converge + verifier | One-page PDF from `docs/threat-model.md`: assets, trust boundaries, threats mapped to OWASP and ATLAS, controls, stated limits, review checklist | US8 |
| **B5.3** | Open-source release v0.1.0 | B5.1, B5.2 | node_converge + verifier | README with diagram and real results, docs, community files, `assay doctor`, PyPI trusted publishing, tag v0.1.0 | US1–US8 |

## Wave 6 — roadmap, console and explorer

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **R6.1** | Data roadmap | Q2.2 | node_converge + verifier | One-page PDF: clean assay data to model training, what a scientist would add, what would catch stealth poison | — |
| **D6.1** | Console backend | S3.3, A4.1 | node_converge + verifier | `run_case()`, replay cache, live guard (passcode, rate limit, Postgres spend counter), ephemeral audit | US9 |
| **D6.2** | Console page | D6.1, B5.1 | node_converge + verifier | Streamlit page in `portfolio-demos`, sample attacks, live audit view, deployed | US9 |
| **D6.3** | Compound explorer | Q2.3, B5.1 | node_converge + verifier | Filter by compound, assay type and value range in the dashboard's data tab | US10 |
