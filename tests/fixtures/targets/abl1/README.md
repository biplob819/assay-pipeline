# Recorded ABL1 responses (second target, OSS-06)

`build.yaml` is the shipped `defaults/build.yaml` with only the `target` block changed to ABL1
(ChEMBL target CHEMBL1862, UniProt P00519, gene symbol ABL1, IC50, as in
`docs/configuring-a-target.md`) and `chembl.page_size` set to 5. `responses/` holds real
responses from the ChEMBL web services (https://www.ebi.ac.uk/chembl/api/data, release
ChEMBL_37) and PubChem PUG-REST (https://pubchem.ncbi.nlm.nih.gov/rest/pug), fetched on
15 Nov 2026 (21:14 UTC) by running both ingests on this config through the project's own
`Fetcher` and the recording transport in `../replay.py`. `responses/index.json` maps each
request (method, host, path, sorted query) to its body file, status and the server's `Date`.

Seven AIDs chosen to cover every AID decision, with BindingDB AIDs small enough to keep whole:

| AID | Depositor | Decision |
| --- | --- | --- |
| 1622 | Thermo Scientific Dharmacon RNAi Technologies | `target_not_configured` (an RNAi screen, no target accession) |
| 32559 | ChEMBL | `chembl_deposited_aid` |
| 493040 | Abbott Labs | `chembl_imported_aid` (ChEMBL lists `src_assay_id` `493040_170`) |
| 1963317 | NCGC | `depositor_not_cleared` (names P00519; NCGC is not in the ledger) |
| 1796923 | BindingDB | kept; its ABL1 rows are Ki (`not_activity_type`, 19), the rest SRC (`other_target`, 10) |
| 1799577 | BindingDB | kept; 11 ABL1 IC50 rows over 6 compounds (repeated SIDs) |
| 1806059 | BindingDB | kept; 9 ABL1 IC50 rows |

The ingest yields 10 ChEMBL and 20 PubChem rows, all ABL1, and the snapshot keeps all 30.

Trimmed for tests, as the responses passed through the recorder: the two ChEMBL activity pages
(activity ids 129828 to 159483, the first ten ABL1 IC50 records by id) have
`page_meta.total_count` set to 10 (the live value was 5,295) and the second page's `next` set to
null, so the ingest stops after two pages; the PubChem AID list keeps only the seven AIDs above
(the live list for ABL1 had 2,612). Every other response (`status.json`, ChEMBL's list of
imported PubChem assays for CHEMBL1862, the summaries of the seven AIDs, the three concise
tables, the structures of the 15 CIDs in kept rows) is the full live answer. Records are
unchanged; bodies are stored as indented JSON, like the EGFR fixtures. The recording used the
pinned BindingDB-only ledger of the golden tests (`GOLDEN_TERMS`); the test uses it too, so an
edit of the shipped ledger cannot change the requests. Re-record with
`uv run python -m tests.fixtures.targets.replay abl1` (network; the totals and dates will move).

ChEMBL data are licensed under CC BY-SA 3.0 (https://creativecommons.org/licenses/by-sa/3.0/).
Source: ChEMBL, EMBL-EBI. Mendez D. et al., ChEMBL: towards direct deposition of bioassay data,
Nucleic Acids Research 47 (2019) D930–D940. PubChem data are public; BindingDB-curated data are
licensed CC BY (attribution: BindingDB, https://www.bindingdb.org). Kim S. et al., PubChem 2025
update, Nucleic Acids Research 53 (2025).
