# N1.4 — Normalise

**Wave 2. Depends on:** N1.2, N1.3. **Unblocks:** Q2.1.

## Goal
Map every cached ChEMBL and PubChem row of the configured target to one `Activity` row, never merged (D-6). Each row gets the RDKit parent structure (D-3), a value in nM rounded in one place (§5.5), pIC50, the relation, variant and assay type, and full provenance. Every row that cannot become an `Activity` goes in the drop ledger with a reason (FR2, FR3, US1, Tech Spec §2.2, §5.1, §5.2).

## Outputs (`src/assay_pipeline/pipeline/normalise.py`)
- `normalise(cfg, cache, snapshot, *, chembl_release=None, pubchem_release=None, terms=None) -> NormaliseResult`:
  - It is a pure function of the raw cache: no network, clock or randomness (D-2, §2.1).
  - `NormaliseResult` holds `snapshot`, `activities` (sorted by `(inchikey, source, source_record_id, activity_uid)`, §5.5), `drops` (PubChem ingest drops plus normalise drops, one ledger), `releases`, `rows_in`, `source_key_mismatches`, `rdkit_version` and `drops_by_reason()`.
  - Snapshot ids, file writing and stage manifests are Q2.2 (SNP-03, SNP-07).
- `read_chembl(cfg, cache, release=None)` (in `ingest/chembl.py`) returns every cached activity of a complete release once, in activity_id order:
  - Pages are re-checked as at ingest.
  - It refuses: a record cached twice, a count that disagrees with the cursor, an incomplete or abandoned release, another target, or several complete releases when none is named.
- **Structures (D-3):** `standardise(smiles)` runs RDKit `Cleanup`, `LargestFragmentChooser` and `Uncharger` (no tautomer canonicalisation), then gives the canonical SMILES and InChIKey. Each distinct input SMILES is standardised once. `source_inchikey` is PubChem's key; ChEMBL's activity endpoint has none. `source_key_mismatches` counts rows whose source key differs from the parent's.
- **Values:**
  - `parse_value` accepts finite numbers only: JSON numbers, or ASCII decimal strings (no `1_000`, no non-Latin digits, no thousands separators). A non-zero string that underflows (`1e-400`) or an integer too large for a float gives `None`.
  - `to_nm` converts over pM, nM, uM, µM, μM, mM and M. Mass units, missing or non-numeric values and negatives keep the row with `value_nm = None`, for QC1 (D-6, flag never delete).
  - `round_sig` rounds `value_nm` to `rounding.value_nm_significant_figures` once. Overflow, or a positive value that underflows to zero, gives `None`; `-0.0` is stored as `0.0`.
  - `pic50 = pic50_of(rounded value_nm)` when `value_nm > 0`.
- **Relations:** ChEMBL `=`, `<`, `>`, `<=` and `>=` are kept. Anything else (`~`, `<<`, missing) and every PubChem row is `unknown` (H4).
- **Variant:** ChEMBL's `assay_variant_mutation` (trimmed), or `wild-type/unspecified`. PubChem rows are always `wild-type/unspecified`.
- **Assay type:** ChEMBL B/F/A/T/P, anything else is dropped as `unknown_assay_type`. PubChem rows are `U`.
- **Provenance:**
  - every row: `source`, `source_record_id` (ChEMBL activity_id; PubChem `AID:SID:n`), `assay_id`, `assay_description`, `target` (the configured name) and `snapshot`;
  - ChEMBL rows also carry `document_id` (ChEMBL document id), `chembl_src_id` and `data_validity_comment`, which is kept and counted, not dropped;
  - PubChem rows also carry `document_id` (`PMID:n` when the PubMed id is ASCII digits) and `depositor`.
  - `validity_comments` counts the ChEMBL rows kept with a `data_validity_comment`.
- **Ids:** `activity_uid = sha256(source + "\0" + source_record_id)[:16]`. A collision is refused.
- **Drops at this stage:** `no_structure` (blank SMILES), `unparseable_structure` (RDKit cannot read or standardise it, or the SMILES has whitespace inside, which RDKit would half-read), `unknown_assay_type` (also for a non-string type), and `malformed_record` (a ChEMBL field of the wrong type: a missing or blank assay id, a non-string variant, units, relation, comment, description or document id, a src_id that is not a non-negative integer; or a value the `Activity` schema refuses on a source-derived field only, such as a newline in a variant or a malformed PubChem InChIKey). No record crashes the run, and no bug hides as data: a schema refusal on any other field (pIC50, nM value, snapshot, target, the model-level checks) is raised. The snapshot id and target name are validated before any row. A run where every offered row is refused as `malformed_record` raises: that is a mapping bug, not data. Every row offered to normalise is either an `Activity` or one of these drops.
- **Dependency:** `rdkit>=2026.9,<2026.10` (exact version in `uv.lock`, reported in the result).

## Stated limits
- PubChem rows carry no mutation, so BindingDB wild-type and mutant rows look alike (H2); the data card says so.
- PubChem record ids are positional within a release (N1.3), so a PubChem `activity_uid` is stable within a release, not across releases.
- The PubChem ChEMBL-import list is read live, not tied to the cached ChEMBL release (N1.3). The data card states both releases (Q2.2).

## Out of scope
QC flags (Q2.1), snapshot id, Parquet files and manifests (Q2.2), the agent database (Q2.3).
- D-3 as designed: RDKit `Cleanup` disconnects metals (cisplatin's parent is ammonia), and the largest fragment can be a large counterion (a pamoate or stearate salt). Rare for kinase inhibitors; the data card states it.
- Tautomer pairs share an InChIKey but keep different parent SMILES, and stereo-specified and unspecified forms get different keys: Q2.3's `v_compounds` must pick one SMILES per InChIKey by a fixed rule.
- ChEMBL variant strings are kept as given: `UNDEFINED MUTATION` (a mutant whose mutation is unknown) is not folded into `wild-type/unspecified`.
