# Data roadmap: from a trusted snapshot to model training

What this pipeline gives a modelling team today, how that becomes a training set without leaking the answer into the test, and what a scientist would add before trusting a model built on it. Numbers are from the published EGFR IC50 snapshot 2026-11-15.1 (docs/measurements/snapshot-2026-11-15.1.json).

## What the snapshot holds

- **41,463 activity rows:** ChEMBL_37 (26,553) and PubChem (14,910, all BindingDB depositions, after dropping assays ChEMBL already holds). 11,957 records were dropped at ingest with a reason in the drop ledger; no kept row is ever deleted.
- **Every row keeps its provenance:** source record id, assay, relation, raw value and unit, document id (all ChEMBL rows, 1,232 PubChem rows), the standardised parent structure (InChIKey) and the rounded nM and pIC50.
- **QC flags instead of deletions:** QC1 unit not convertible (1,470), QC2 duplicate disagreement (4,414), QC3 out of range (174), QC5 censored or unknown relation (21,761). 18,257 compounds; 26,495 consensus keys (compound, target, variant, assay type), 917 in conflict.
- **A hashed manifest:** the same inputs and code give the same snapshot id and bytes, so a model can name exactly the data it was trained on.

## From snapshot to training set

- **Target:** pIC50 from exact (=) rows only. Censored rows (<, <=, >, >=: 5,469) are bounds, not values: use them as censored targets (a survival-style loss or an "active beyond threshold" classifier), never as point values. Unknown relations (16,292: all PubChem rows and 1,382 ChEMBL rows) are held out until a scientist confirms their meaning.
- **Filters first, then labels:** drop QC1 and QC3 rows, then recompute one label per consensus key from what remains (the published consensus includes QC3 rows). Keys whose spread exceeds 1 log unit are excluded and kept for error analysis; record every filter with its count next to the model.
- **Duplicates:** 7,261 ChEMBL rows come from BindingDB and 4,414 compounds appear in both sources. The data card reports that overlap, but rows are not flagged or merged, so the same measurement can appear twice: deduplicate before weighting or averaging.
- **Features:** from the standardised parent structure only (fingerprints, descriptors), never from assay text, source or row ids, which encode who measured it, not what the molecule does.

## Leakage rules for the split

- **Split by compound and scaffold, not by row:** the same InChIKey never sits on both sides (this also keeps cross-source duplicates together), and Bemis-Murcko scaffolds are grouped so near-duplicates stay on one side.
- **A time split as a second test:** train before a cut-off, test after. Rows carry a document id, so the year comes from a document lookup (ChEMBL documents; PubChem deposition dates for rows without one).
- **The test set is frozen with the snapshot id;** a new snapshot means a new, separately reported test.

## What a scientist would add

- **Assay context:** cell-based versus biochemical and ATP concentration. Variants are already separate keys; PubChem rows carry no mutation, so mutant data from them needs assay-text review.
- **Unknown relations resolved:** read a sample of assays to decide which "unknown" rows are exact.
- **Known actives and activity cliffs** checked by hand (approved EGFR inhibitors must rank high) before any model is trusted.

## What would catch stealth poison

The three planted stealth rows (plausible values, wrong by 10x, no second measurement to disagree with) pass every QC rule today. What would catch them:

- **Cross-assay consistency:** the same compound in a related assay (another source, cell versus enzyme) should agree within a log unit; a lone value far from its structural neighbours is flagged for review.
- **Literature checks:** values traced to their primary publication or deposition, and a row with no traceable source quarantined.
- **Scientist review:** a ranked queue of rows far from what a model trained on the other rows predicts (cross-validated on the training pool), reviewed by hand before the snapshot and its test set are frozen.
