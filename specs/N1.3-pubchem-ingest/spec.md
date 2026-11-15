# N1.3 — PubChem ingest

**Wave 2. Depends on:** N1.1. **Unblocks:** N1.4.

## Goal
Fetch the configured target's PubChem BioAssay rows into the raw cache (FR1, FR2, Tech Spec §5.2, D-15, D-24, OWNER D-22), without counting a measurement twice (H3) and without letting rows from depositors with unknown terms into the published snapshot.

## Outputs (`src/assay_pipeline/pipeline/ingest/pubchem.py`)
- `ingest_pubchem(cfg, cache, fetcher, *, new_release=False, terms=None) -> PubchemIngestReport`. Every response is checked before it is cached. The steps:
  1. **AID list:** `assay/target/genesymbol/<gene>/aids/JSON`.
  2. **Summaries:** batches of 100.
  3. **ChEMBL imports:** ChEMBL's `assay.json?target_chembl_id=<id>&src_id=7` (paged); `src_assay_id` `493040_69` counts as AID 493040.
  4. **Concise tables:** `concise/JSON` for every kept AID.
  5. **Structures:** `compound/cid/<cids>/property/SMILES,InChIKey/JSON` in batches of 100, for the CIDs of kept rows only.
- **AID decisions, first reason wins:**
  - `no_summary` (a summary PubChem leaves out; recorded, not fatal)
  - `chembl_deposited_aid` (depositor ChEMBL, always, or one in `pubchem.exclude_depositors`; compared after trimming and without case)
  - `chembl_imported_aid`
  - `target_not_configured` (no target accession equals the configured UniProt accession; a RefSeq-only target such as `NP_005219` is dropped here, a stated limit)
  - `depositor_not_cleared` (not `keep: true` in the depositor ledger)
- **A gene symbol with no assays** (PubChem's `PUGREST.NotFound`) is a clear error, not a retry.
- **Row decisions in kept AIDs:** `other_target` (multi-target assays), `not_activity_type`, `no_sid`, `no_cid` (CIDs and SIDs must be positive integers), `no_structure` (PubChem leaves the CID out, sends it without SMILES or InChIKey, or answers with a `PUGREST.NotFound` 404, which is cached like any answer; a NotFound for a batch of several CIDs is re-asked one CID at a time, so a transient fault costs at most the CIDs it hits). Only a 404 carrying that fault counts as NotFound; any other fault body or status is an error and nothing is cached.
- **Record ids:** a row's id is `AID:SID:n`, where n numbers the rows sharing that AID and SID across the whole table in order (real AID 1794871 repeats a SID for separate measurements, and 1798470 repeats one per target). A row without a usable SID is `AID:?:i` (its table position).
- Concise cells must be strings or null; repeated column names are refused.
- Every drop is a `Drop(source="pubchem", stage="ingest", reason, source_record_id=AID or the row id)`.
- **The reader:** `read_pubchem(cfg, cache, release=None, terms=None) -> PubchemRead(release, decisions, rows, drops)` rebuilds everything from the cache alone. Rows are `PubchemRow(source_record_id, aid, sid, cid, depositor, outcome, value_um, activity_name, assay_name, pubmed_id, smiles, inchikey)` A read with another config or depositor ledger than the release was built with is refused and points to `--new-release`; it never returns different rows silently.
- **The depositor ledger:** `load_depositor_terms(path=None)` reads the packaged `defaults/depositor_terms.yaml`. It is strict: names unique without regard to case, unique keys, `keep` a real boolean (only `true`/`false`; `yes`, `on`, `1`, `"true"` are refused), https evidence, a checked date, a licence and an attribution. The ledger ships BindingDB as cleared (CC BY on curated data).
- **Release folders:** only folders named `YYYY-MM-DD` or `YYYY-MM-DD.N` whose cursor names the same release count; anything else in the cache (a `backup` copy) is ignored.
- **Releases:** the release is the UTC date of the AID listing's `Date` header, converted from any offset (`2026-11-15`; a second release that day is `2026-11-15.2`, ordered numerically so `.10` follows `.9`). The cursor records the config (gene, UniProt, ChEMBL id, activity type, excluded depositors) and the depositor ledger's sha256. A cache built for another config or ledger is refused, so a changed ledger can never make a resume or a read differ from a clean run.
  - An incomplete release resumes with no re-fetch.
  - A complete release makes a rerun a no-op (0 requests) until `--new-release`, which also abandons an incomplete release.
- **Rate limit:** `pubchem.max_requests_per_second` (default 4; the config requires fewer than 5) paces the shared `Fetcher`. `PUGREST.ServerBusy` (HTTP 503) and other 5xx/429 responses back off under the §2.5 rules (N1.2).
- **CLI:** `assay ingest pubchem [--config] [--cache] [--new-release] [--depositor-terms FILE] [--json]` (exit 0, or 2 with the reason).

## Rules
- The single-writer lock per source (N1.2's `RawCache.lock`) applies.
- No network in tests: recorded real responses (`tests/fixtures/pubchem/`, README lists provenance and trimming).
- The concise table has no qualifier column, so N1.4 sets `relation="unknown"` for every PubChem row (H4); N1.3 keeps the value string as sent.

## Out of scope
Unit conversion, standardisation and the unified table (N1.4); the data card yields (Q2.2).

## Stated limits
- RefSeq-only target accessions are dropped (`target_not_configured`).
- The concise table has no mutation column, so BindingDB wild-type and mutant rows look alike; N1.4 maps PubChem rows to `wild-type/unspecified` (H2 risk, data card).
- The ChEMBL import list is read live, not tied to the N1.2 ChEMBL release; the data card states both releases (Q2.2). ChEMBL activity records carry no PubChem AID, so no cross-check is possible from the cache.
- Record ids are positional within a release: if PubChem inserts a row for a SID in a later release, `AID:SID:n` may name a different measurement there. They are unique and stable within a release (and across a resume); N1.4 must not treat them as stable across releases.
- A concise table with no `Row` key is recorded as an empty table.
