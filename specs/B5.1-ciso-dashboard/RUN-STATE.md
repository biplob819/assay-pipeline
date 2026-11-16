# B5.1 run state

**Now:** green

## History
- 2026-11-16 — started (base a64acf0).
  - **Built:** the bundle collector, the renderer, `assay report` and the Pages workflow. The bundle and page come from `main-2026-11-16` and a fresh oracle run on the current suite (60 of 60 stopped: guard 45, limits 5, quality rules 10).
  - **Author review of the rendered page:**
    - SVGs scaled with the column and labels collided, so charts now render at their native width with a wider label column;
    - an undefined kappa showed as a blank cell and now reads "n/a", with a note on why kappa is degenerate here;
    - the access table first showed only question and answer lines, so it now shows guard decisions split by attack and normal question, recent blocked queries and recent queries;
    - the embedded oracle carried held-out case ids, so the bundle now holds public per-case results and held-out counts only, and the raw oracle file stays in `runs/`.
  - **Finding the page surfaced:** in the live run the guard blocked 3 queries in attack cases and 18 in normal questions (window functions and ordered-set aggregates the allow-list does not cover). The models refused the attacks themselves. The guard's cost to honest use is now shown.
  - **Tests:** 8.
- 2026-11-16 — attacker verifier pass 1: 1 must-fix, 5 should-fix, 4 notes, handled with tests:
  - **Must-fix, re-render:** the page re-rendered from its bundle differed from the page rendered from the run, because decision order changed when the bundle was saved. That would have failed the Pages `cmp`. Decisions are now sorted in `render()`, and a test renders both orders.
  - **Should-fix, headline bound:** one pooled bound read as "the agent's" rate. There is now one exact bound per agent, per trial, on this suite.
  - **Should-fix, bound chart:** rows pooled public and held-out, used a Wald limit and cut the axis at 50%. There is now one row per split, Clopper-Pearson limits, and an axis that grows to fit.
  - **Should-fix, mark labels:** marks were labelled only by `<title>`. They now carry an `aria-label`, an SVG `role="group"`, a visible focus label and a readable legend.
  - **Should-fix, spend:** the ablation's cost was missing from the spend tile.
  - **Should-fix, tests:** new tests cover tiles, bounds, pooling, order independence and the Pages pointer.
  - **Notes:**
    - the Pages workflow renders an explicit `docs/results/LATEST`, not a name sort, and checks out without persisted credentials;
    - the fixed prose is now generated from the data;
    - bar labels name their populations (all cases, k=3; public, k=1; no model);
    - the spec drift on the oracle file is fixed;
    - the layout fits a 390px viewport with charts scrolling in place.
- 2026-11-16 — green: converged twice after the verifier pass; the page re-renders byte for byte from the committed bundle.
