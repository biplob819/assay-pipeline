# N1.3 run state

**Now:** green

## History
- 2026-11-15 — surveyed PubChem for EGFR: 4,625 AIDs, ChEMBL deposited 4,270 of the 4,514 that list P00533; BindingDB 244. Recorded six real AIDs that cover every decision; found RefSeq-only targets (NP_005219), multi-target concise tables, `src_assay_id` like `493040_69`, and no qualifier column. Drafted module and 40 tests in the scratchpad while S3.2 was verified.
- 2026-11-15 — started (base bfb4c94); module, depositor ledger, recorded fixture (6 AIDs) and golden digest, `assay ingest pubchem`; Tech Spec §5.2 updated with the imported-AID rule, RefSeq limit, row filters and missing qualifier.
- 2026-11-15 — two S3.2 Windows fixes landed on main while N1.3 was building (eb4f0fb, CI 9/9 green); N1.3 tree unaffected.
- 2026-11-15 — verifier pass 1 (attacker) confirmed PUB-01/02/05/06 and disproved PUB-03 (AID:SID is not unique: real AID 1794871 repeats a SID for two Kd values, so two measurements would share an activity_uid; empty and non-numeric SIDs were kept; non-string cells were stringified) and PUB-04 (one CID without an InChIKey, or a properties NotFound 404, stopped the ingest for good). Fixed: `AID:SID:n` record ids, `no_sid`, string-only cells, per-CID `no_structure`, cached NotFound answers (Fetcher `ok_statuses`). Also: ledger digest in the cursor (resume and read refused on a changed ledger), ChEMBL always excluded case-insensitively, UTC release date, numeric release order, YAML 1.2 booleans and case-insensitive names in the ledger, repeated concise columns and CID 0 refused, clear error for a gene with no assays, pacing strictly under 5/s, BindingDB info page as primary licence evidence, golden test on a pinned ledger. 24 new tests.
- 2026-11-15 — verifier pass 2 confirmed all six criteria (no must-fix). Took its top nice-to-haves: NotFound only on a 404 with the fault (odd fault bodies fail cleanly, nothing cached), a NotFound batch re-asked per CID, release folders must be date-named with a matching cursor; positional record ids and empty tables recorded as stated limits. 7 more tests.
- 2026-11-15 — green: converge passed twice; two attacker verifier passes; all six criteria confirmed.
