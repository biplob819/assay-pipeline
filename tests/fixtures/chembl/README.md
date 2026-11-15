# Recorded ChEMBL responses

Real responses from the ChEMBL web services (https://www.ebi.ac.uk/chembl/api/data), fetched on
15 Nov 2026 from release ChEMBL_37: `status.json` and three pages of five IC50 activities for
target CHEMBL203 (human EGFR), ordered by activity id.

Trimmed for tests: `page_meta.total_count` is set to 15 (the live value was 26,600) and the last
page's `next` link is null, so a test ingest ends after three pages. Records are unchanged.

ChEMBL data is licensed under CC BY-SA 3.0 (https://creativecommons.org/licenses/by-sa/3.0/).
Source: ChEMBL, EMBL-EBI. Mendez D. et al., ChEMBL: towards direct deposition of bioassay data,
Nucleic Acids Research 47 (2019) D930–D940.
