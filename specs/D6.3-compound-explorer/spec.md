# D6.3 — Compound explorer

**Wave 10. Depends on:** Q2.3, B5.1.

## Goal
Let a chemist sanity-check the published data (US10): find a compound, narrow by assay type, variant, source and pIC50 range, and open the exact record each value came from (FR3).

## Outputs
- **`report/explorer.py`:**
  - **`explorer_data(snapshot_dir)`:** embeds every activity row of the snapshot as compact JSON. Compounds are listed once; rows refer to them and to category tables by index.
  - **`source_links(source, record, assay)`:** for ChEMBL, the activity record in ChEMBL's API and the assay page; for PubChem, the bioassay and substance pages, parsed from `AID:SID:n`.
  - **`render_explorer(data, licence)`:** one self-contained page with no external scripts, styles or images.
    - **Filters:** compound (InChIKey prefix or SMILES fragment), assay type, variant, source, a pIC50 range, and an option to hide flagged rows. Every filter is a labelled control.
    - **Results:** a live match count, the first 200 matching rows, flags shown, and source links on every row.
    - **Licence:** the snapshot's licence and attribution, carried on the page.
- **`assay report --run ...`:** also writes `docs/report/explorer.html`, linked from the dashboard's data-quality section.
- **Published:** `docs/report/explorer.html` for snapshot 2026-11-15.1, with 41,463 rows and 18,257 compounds (4.1 MB). The link patterns were checked live (HTTP 200) on 2026-11-16.

## Out of scope
Searching by substructure or similarity, which needs a chemistry engine in the browser.
