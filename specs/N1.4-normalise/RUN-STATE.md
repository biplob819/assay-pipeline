# N1.4 run state

**Now:** green

## History
- 2026-11-15 — started (base d0c5ac8); RDKit 2026.09.2 added; `normalise` and a cache-only `read_chembl`; 51 tests over both recorded fixtures. Deferred the N1.3 import-list cross-check to a data-card statement (Q2.2): the ChEMBL activity records carry no PubChem AID to check against.
- 2026-11-15 — verifier pass 1 (attacker): 20,000 hypothesis cases found no wrong nM or pIC50, no silent loss, no merge, full determinism; it disproved NRM-01 (whitespace inside a SMILES was half-read: `CCO c1ccccc1` became ethanol) and NRM-05 (a null `assay_chembl_id` became the string 'None'), and found malformed fields crashing the run. Fixed: whitespace-split SMILES dropped, `malformed_record` drops for badly typed fields and schema refusals, ASCII-only numbers and PMIDs, underflow to None, `validity_comments` count, `IngestError` for unsafe release names. D-3 metal and counterion behaviour recorded as stated limits. 31 more tests.
- 2026-11-15 — verifier pass 2 confirmed every pass-1 fix and NRM-01/03/04, and disproved NRM-05/06: my `_build` wrapper caught every ValidationError, so a bad snapshot id, a target name with a space or a planted pIC50 bug gave an empty table of `malformed_record` drops and no error; and NRM-02 narrowly (`"1e-400"` stored as 0.0). Fixed: drops only for refusals on source-derived fields, everything else raised; snapshot and target validated up front; underflow and huge integers give no value; the whitespace guard moved into `standardise()`; assay ids with inner whitespace refused; cursor `complete` must be `true`. 10 more tests (92).
- 2026-11-15 — fixed a test typing slip caught by gate 5.
- 2026-11-15 — verifier pass 3 confirmed NRM-02/05/06 (no must-fix). Took two guards: a run where every row is refused as malformed raises (a mapping bug), and document ids with inner whitespace are malformed. 2 more tests (94).
- 2026-11-15 — green: converge passed twice; three attacker verifier passes; all six criteria confirmed.
