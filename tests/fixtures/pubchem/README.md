# Recorded PubChem responses

Real responses from PubChem PUG-REST (https://pubchem.ncbi.nlm.nih.gov/rest/pug) and the ChEMBL
web services, fetched on 15 Nov 2026 for the EGFR default target (gene symbol EGFR, UniProt
P00533, CHEMBL203).

Six AIDs chosen to cover every decision:

| AID | Depositor | Decision |
| --- | --- | --- |
| 1433 | Ambit Biosciences | `target_not_configured` (a kinase panel without P00533) |
| 1729 | NCGC | `chembl_imported_aid` (ChEMBL imported it; its target is RefSeq NP_005219) |
| 8621 | ChEMBL | `chembl_deposited_aid` |
| 1794871 | BindingDB | kept; its EGFR rows are Kd, so `not_activity_type` |
| 1798470 | BindingDB | kept; one EGFR IC50 row, the rest `other_target` |
| 1798578 | BindingDB | kept; two EGFR IC50 rows |

Trimmed for tests: `aids.json` lists only these six AIDs (the live list for EGFR had 4,625).
`summary.json` is the real summary response for exactly these six. `chembl_src7_assays.json` is
ChEMBL's full list of PubChem assays it imported for CHEMBL203 (5 assays, one `src_assay_id` of
the form `493040_69`). `properties.json` holds the SMILES and InChIKey of five CIDs; the mock
server returns the ones asked for. Everything else is unchanged.

PubChem data are public; BindingDB-curated data are licensed CC BY (attribution: BindingDB,
https://www.bindingdb.org). ChEMBL data are licensed CC BY-SA 3.0. Kim S. et al., PubChem 2025
update, Nucleic Acids Research 53 (2025).
