# Data card: EGFR IC50 snapshot 2026-11-15.1

Generated from the raw cache by `assay snapshot`; every number here is reproducible.

## Sources and releases

- ChEMBL (target CHEMBL203): release ChEMBL_37, fetched 2026-11-15T16:18:50Z
- PubChem (gene EGFR, UniProt P00533): release 2026-11-15
- Target: EGFR (IC50)
- Planted rows: none

## Yields

| Source | Rows offered to normalise | Rows in the snapshot |
| --- | --- | --- |
| chembl | 26600 | 26553 |
| pubchem | 14910 | 14910 |

Dropped rows by reason (ingest and normalise):

| Reason | Rows |
| --- | --- |
| chembl_deposited_aid | 4291 |
| chembl_imported_aid | 5 |
| no_structure | 47 |
| not_activity_type | 258 |
| other_target | 7271 |
| target_not_configured | 85 |

## Quality rules

Rules flag and never delete (D-6). Definitions: Tech Spec §5.3.

| Rule | Meaning | Rows flagged |
| --- | --- | --- |
| QC1 | nM value cannot be computed | 1470 |
| QC2 | conflict: '=' spread above 1.0 pIC50 on the same key | 4414 |
| QC3 | pIC50 outside [3, 12] or 0 nM | 174 |
| QC4 | no source record id | 0 |
| QC5 | censored or unknown relation | 21761 |

- Consensus keys: 26495, in conflict: 917
- ChEMBL rows kept with a data validity comment: 510
- PubChem rows whose source InChIKey differs from the RDKit parent: 3
- Compounds (InChIKey) present in both ChEMBL and PubChem, any depositor: 4414 (D-24)
- RDKit 2026.09.2

## Stated limits

- PubChem rows carry no mutation: BindingDB wild-type and mutant rows look alike (H2).
- PubChem record ids are positional within a release, so PubChem uids are stable within a release, not across releases.
- Assays naming the target only by a RefSeq accession are dropped (target_not_configured).
- The PubChem list of AIDs that ChEMBL imported is read live, not tied to the ChEMBL release.
- No QC2 key spans ChEMBL and PubChem (PubChem rows are assay type U); cross-source overlap is reported below by InChIKey.
- RDKit Cleanup disconnects metals and the largest fragment can be a large counterion (D-3).
- Stealth poison (plausible values wrong by 10x with no record to disagree) is not caught by any rule (H7).

## Licence

Data: CC BY-SA 3.0, see DATA_LICENSE.md. Code: MIT.
