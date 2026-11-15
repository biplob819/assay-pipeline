# W0.1 — Repo and harness

**Wave 0. Depends on:** —. **Unblocks:** N1.1.

## Goal
A repo where every later node can be started, built, proven and verified the same way EvalKit was (Tech Spec §11): a uv project, CI, the ported harness scripts with every gate shown to fail, and the records seeded.

## Outputs
- `pyproject.toml` (Python ≥3.11, hatchling, src layout, distribution `assay-pipeline`, `assay` CLI entry point), `src/assay_pipeline/` skeleton with `assay --help` / `--version` (version read from package metadata), MIT `LICENSE`, ruff pre-commit, `.gitattributes` forcing LF (HAR-01).
- `.github/workflows/ci.yml`: lint, format, types, tests, secret scan, graph eligibility, gate self-test and build on every push and PR, Python 3.11–3.13 on Linux, macOS and Windows, actions pinned by commit SHA, least-privilege permissions (HAR-02, OWNER D-23).
- `scripts/harness_lib.py` with node ids `^[WNQSABRD]\d+\.\d+$`; `verify_graph_eligibility.py` (HAR-03) and `node_start.py` (HAR-04) ported unchanged in behaviour.
- `scripts/node_converge.py` with the 13 gates of Tech Spec §11.6: EvalKit's judge gate and its min-Python golden rerun dropped; `rebuild`, `boundary` (AST trust-boundary check of §6.2) and `security` added; the self-test calls the real gate functions and every gate must fail naming its planted defect, including every boundary bypass the verifier found (HAR-05, D-25).
- Records: `docs/graph/graph.md` and `docs/graph/catalog.tsv` generated from Tech Spec §11.2/§11.4 (21 nodes, 128 rows), `docs/decisions.md` D-1..D-26 with the eight owner rulings, `docs/lessons.md`, `changelog.md`, `docs/measurements/`, `scripts/status.py` (HAR-06).
- `scripts/secret_scan.py` gate; `.env` and `.env.*` git-ignored; the job-search notes git-ignored, and no file git would publish contains a forbidden word (checked by hash, so the test does not publish it) (HAR-07, OWNER D-18).

## Rules
- Harness behaviour matches EvalKit's except where §11.6 says otherwise; EvalKit's harness tests are ported with it.
- `graph.md` must stay identical in edges to Tech Spec §11.2 (test).

## Out of scope
Any pipeline, guard, agent or dependency beyond pydantic and pyyaml (added by the nodes that need them).
