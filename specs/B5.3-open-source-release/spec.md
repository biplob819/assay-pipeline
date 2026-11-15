# B5.3 — Open-source release v0.1.0

**Wave 11. Depends on:** B5.1, B5.2.

## Goal
Make the project usable and checkable by someone who has never seen it: a README that shows the real results and how to rebuild them, docs for the four first tasks, community files, a readiness check, the published run with its audit anchors, a second target proved from config, and a release to PyPI by trusted publishing (OWNER D-20, D-21, D-22, D-26).

## Outputs
- **README.md:**
  - **Diagram:** one Mermaid diagram of the pipeline, the agent boundary, the audit log and scoring.
  - **Results:** the headline table is generated from the published bundle by `scripts/readme_results.py`, and a test fails if it is stale.
  - **Rebuild:** three lines re-render the dashboard from the committed bundle and compare it byte for byte.
  - **Also:** the privacy statement, licence and attribution, and links to every doc.
- **Docs:** `docs/quickstart.md`, `docs/configuring-a-target.md`, `docs/cost.md` and `docs/reading-the-dashboard.md`.
- **Community files:** `CONTRIBUTING.md`, `SECURITY.md` (private vulnerability reports, key handling, privacy), `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), two issue templates and a PR template.
- **`assay doctor`:** reports Python, the provider keys (only "set" or "not set" is shown), whether ChEMBL and PubChem can be reached, and the cache, snapshots and agent database. It takes `--offline` and `--json` and exits 1 on any failure. Every command takes `--json`.
- **Published run** (`docs/results/main-2026-11-16/`):
  - `bundle.json`;
  - both anchors files, which hold only sequence numbers, hashes and times;
  - `verify.json`, holding `assay verify-audit` results for the main and ablation logs, with log and anchor hashes.
- **Data card:** `docs/data/2026-11-15.1/DATA_CARD.md` and `DATA_LICENSE.md` for the published snapshot, checked against the bundle.
- **Hand-read:** `docs/measurements/hand-read-main-2026-11-16.md` covers 10 answers and 12 audit lines from public cases. It lists five findings, three of them follow-ups for the next run.
- **Second target:** ABL1 builds end to end (ingest, snapshot, agent database) from a config that differs from the default only in its target (and a ChEMBL page size of 5, so two recorded pages cover the build), using recorded real responses (`tests/fixtures/targets/abl1/`, `tests/pipeline/test_second_target.py`).
- **Release:** `.github/workflows/release.yml` runs on a `v*` tag.
  - **Build:** checks the tag against the package version, builds, and smoke-tests the wheel.
  - **Publish:** publishes to PyPI by trusted publishing. Only the publish job gets `id-token: write`, in the `pypi` environment, and no token is stored.
  - **Smoke test:** runs `uvx --from assay-pipeline==<v> assay doctor` on Linux, macOS and Windows.
- **Version:** 0.1.0, for Python 3.11 to 3.13 (`requires-python = ">=3.11,<3.14"`, matching what `assay doctor` accepts).
- **Private vulnerability reporting** enabled on the repository (2026-11-16); the code of conduct routes conduct reports through the same private channel.

## Owner actions
- Register a **pending** trusted publisher on PyPI, since the project does not exist there yet. Use pypi.org → Account → Publishing → "Add a new pending publisher" with project `assay-pipeline`, owner `biplob819`, repository `assay-pipeline`, workflow `release.yml` and environment `pypi`.
- Recommended: create the GitHub environment `pypi` with yourself as a required reviewer, so pushing a tag alone cannot publish.
- Then push tag `v0.1.0`. REL-06 and OSS-01 go green when the release workflow's smoke jobs pass.

## Out of scope
Signing artifacts beyond PyPI's own attestations, and a docs website.
