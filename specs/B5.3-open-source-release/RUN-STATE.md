# B5.3 run state

**Now:** building

## History
- 2026-11-16: started (base d450e6c). Built `assay doctor` (the live network check passed against both sources), the four docs, the community files, and a README whose results are generated from the bundle. The run's anchors and verify result were published: both logs verify and the last anchor matches the bundle. The data card and licence were published, and a public-only hand-read found five findings. The release workflow is written, with action SHAs resolved from their release tags. Version is 0.1.0. OSS-06 was delegated to a subagent.
- 2026-11-16: OSS-06 done by a subagent. ABL1 (CHEMBL1862, P00519) builds ingest → snapshot → agent database from a config that differs from the default only in `target` and `chembl.page_size`. It runs on recorded real responses fetched 2026-11-15: 2 ChEMBL pages of 5, and 7 PubChem AIDs covering every AID decision. The fixture is about 82 KB. `replay.py` refuses any request it has no recording for. The ABL1 snapshot has 10 ChEMBL rows and 20 PubChem rows, all ABL1. 7 tests, run in CI (CI runs every marker). No production change was needed.
- 2026-11-16: verifier pass. Two must-fixes.
  - `requires-python` had no upper bound, so 3.14 installs worked but `assay doctor` failed on them. It is now bounded to below 3.14, and the post-release smoke test no longer pins a Python or passes `--offline`.
  - SECURITY's private reporting channel was off. It is now enabled. The code of conduct named a profile contact that doesn't exist, so it now routes conduct reports through the private reporting channel.

  Should-fixes, all done:
  - Docs: the quickstart no longer assumes `assay` is on PATH after `uvx`; cost.md no longer claims `ask` has a cap; the target guide shows where the default config lives and gives a separate cache and output folder.
  - Privacy statement: now names doctor's reachability check and the hosted console's Postgres spend counter.
  - Doctor: reports the version and platform, and a test drives exit 1.
  - Release tests: they now hash the anchors files against verify.json and check every line, check every `assay` command and flag in the published markdown against the parser (this caught Tech Spec drift, `--guard off` versus the built `--ablation`), cover every published markdown file in the link check, and check drop reasons in the data card.
  - README generator: takes the target from the bundle, states "refused by the model" only when the numbers show it, and survives a bundle with no oracle.

  Spec owner actions now say "pending publisher" and recommend a required reviewer on the `pypi` environment.
- 2026-11-16: converged twice (13/13). Ten rows green. REL-06 and OSS-01 stay building until the owner registers the pending PyPI publisher and pushes v0.1.0.
