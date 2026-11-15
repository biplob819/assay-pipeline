# R6.1 run state

**Now:** green

## History
- 2026-11-16 — started (base b15d5ef). Roadmap written from snapshot 2026-11-15.1 (counts from the A4.1 measurements and the QC tallies) and the planted stealth rows. Author check: the first draft promised a time split, but rows carry only the source release, so the page now names the date as the addition a time split needs. One page at 8 pt. 4 tests.
- 2026-11-16 — verifier pass: 2 must-fix, 4 should-fix, 2 notes, all handled:
  - **Must-fix, numbers:** several figures came from the agent's view, not the snapshot (compounds, conflicts, censored, unknown). The ChEMBL count was the pre-drop ingest figure (26,600, not 26,553), and "nothing deleted" ignored the 11,957 ingest drops. Every figure is now taken from the snapshot, recorded in `docs/measurements/snapshot-2026-11-15.1.json`.
  - **Must-fix, tests:** the tests could not catch wrong numbers. They now compare every figure on the page with that facts file, and a local test checks the file against the real manifest.
  - **Should-fix, duplicates:** BindingDB duplicates are counted on the data card, not flagged or merged. The page now says so, and D-24 got an update.
  - **Should-fix, consensus:** labels are recomputed after the QC1/QC3 filters, since the published consensus includes QC3 rows.
  - **Should-fix, consensus key:** it is named in full, and variants are already separate keys.
  - **Should-fix, time split:** rows carry a document id, so the year needs a lookup, not a new field.
  - **Notes:** the empty "QC2 through consensus" advice was removed. The scientist-review queue runs on the training pool before the test set is frozen.
- 2026-11-16 — green: converged twice after the verifier pass.
