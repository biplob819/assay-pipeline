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
- 2026-11-17: reopened for DSH-08 (OWNER D-41): `report/metrics.py`, `assay report --metrics`, `gates.yaml`, `docs/results/main-2026-11-16/metrics.json` (55 metrics; regenerates byte for byte). 6 tests in `tests/report/test_metrics.py` (an independent check of the frozen metrics-1 rules, the README's numbers, no ids or text, every gate matching, the one-success case) and 1 in `test_report.py` (a fresh run's file). DSH-08 building until the verifier pass.
- 2026-11-17: verifier pass (fresh context; its own metrics-1 checker and gate evaluator from EvalKit's frozen spec; no API calls). All 55 numbers match the bundle and README; no ids or text; no gate exits 2. Must-fix: a row with no complete case (pass_k None) crashed the export with a TypeError (now exported without pass^k; tested). Should-fix: the "never" rules B6 to B10 were not gated (now fail gates at 100%); unscored trials and a missing agent passed silently (now `attack_trials_unscored` per agent gated at 0, and the bound gate names each agent). Notes kept: no warn fires today, though GPT B1 (0.80) and its public stated rate (0.73) sit near their warn lines; a run without judges or the oracle exits 2 on the judge or oracle gates (by EvalKit's spec). Also: EvalKit's standard cost names `system_cost` and `eval_overhead_cost` (its D-67); per-model tokens, latency and retries wait for a bundle that carries them. DSH-08 green.
