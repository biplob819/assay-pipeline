# R6.1 — Data roadmap

**Wave 10. Depends on:** Q2.2. **Unblocks:** nothing in the graph (a PRD deliverable, linked from the project page).

## Goal
One page that shows a modelling team how the trusted snapshot becomes a training set without leakage, what a scientist would add, and what would catch the stealth poison the QC rules miss (PRD deliverable 6, H7).

## Outputs
- **`docs/data-roadmap.md`**, in five parts:
  - **What the snapshot holds:** real counts from snapshot 2026-11-15.1.
  - **From snapshot to training set:**
    - target from exact rows only, with censored rows as censored targets and unknown relations held out;
    - one label per key from consensus, with conflicts excluded;
    - QC-based filters;
    - structure-only features.
  - **Leakage rules for the split:**
    - scaffold and InChIKey grouping;
    - a time split once rows carry a date (an addition the snapshot needs);
    - cross-source duplicates counted once;
    - a test set frozen with the snapshot id.
  - **What a scientist would add:** assay context and mutant tasks, resolving unknown relations, and a known-actives check.
  - **What would catch stealth poison:** cross-assay consistency, literature checks, and scientist review of model-flagged outliers.
- **`docs/data-roadmap.pdf`:** one page, rendered by `scripts/render_onepager.py` (B5.2), with the source hash embedded.

## Out of scope
Building features, splits or models.
