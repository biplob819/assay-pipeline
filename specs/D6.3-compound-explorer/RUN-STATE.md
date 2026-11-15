# D6.3 run state

**Now:** green

## History
- 2026-11-16 — started (base 1480584). Explorer built: the full snapshot is embedded compactly and filtered by an inline script. Source links for both sources were checked live (HTTP 200 for a ChEMBL activity, a ChEMBL assay, a PubChem bioassay and a PubChem substance). The page script is tested under node with a minimal DOM. 4 tests.
- 2026-11-16 — verifier pass (light). Must-fix: the report CLI's `--explorer` default was a repo path, so the report tests overwrote the published page with fixture data (L-36). Fixed: the default is `explorer.html` beside `--html` with a relative back link; the page was regenerated from snapshot 2026-11-15.1 (manifest hash checked against the bundle: 41,463 rows, 18,400 compound entries); a test pins the committed page to the published bundle's snapshot and row count, and it runs in the Pages job too. Should-fixes: compounds are keyed on (InChIKey, SMILES), so tautomers keep their own SMILES; SMILES matching is case-sensitive (InChIKey prefix stays case-free); every filter is tested under node against a reference count on data built to tell them apart, plus a full-tuple round trip on the snapshot. Note: the data block now has no `<` at all (escaped `<`), so `<!--<script` cannot unbalance it; the table has a caption. 9 explorer tests.
- 2026-11-16 — converged twice (13/13); green.
