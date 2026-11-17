# B5.1 — CISO dashboard

**Wave 9. Depends on:** A4.2, A4.3, Q2.2. **Unblocks:** B5.3, D6.2, D6.3.

## Goal
One page a security lead can read in five minutes: did any attack get through, what stopped each one, how the agents behaved, what the audit trail shows, and how clean the data is (US2, US6, FR11). The page must not overstate what was measured.

## Outputs
- **`report/collect.py`:** `collect(run_dir, snapshots_root, snapshot, oracle)` builds the public results bundle.
  - **Refusals:** an unfinished run (trials hash mismatch) or one not yet assessed by `assay behaviour` is refused.
  - **Contents:**
    - the run summary with behaviour, judges and answer text;
    - the ablation summary;
    - the oracle results;
    - the audit verify status, line and anchor counts, the latest anchor, and the 30 most recent audit lines from public cases only;
    - data quality from the manifest (flags per rule, drops per reason, rows by source, source releases, target) and the snapshot history from every manifest.
  - **Held out:** no held-out text and no held-out case id ever enters the bundle (D-12).
- **`report/html.py`:** `render(bundle)` returns one self-contained HTML page, with no scripts and no external resources.
  - **Headline tiles:** attack trials that got through, each agent's exact one-sided 95% upper bound on the success rate per trial on this suite, and total spend (main run, ablation and judges).
  - **"Did anything get through?":** one row per category, agent and split (never pooled). Each row shows the observed success rate and its exact (Clopper-Pearson) 95% upper bound, on an axis that grows to fit. Zero successes are a bound, not 0%. A table by split adds pass^k and the held-out flag.
  - **"What stopped it?":** 100% stacked bars of the stopping layer per category, for the main run per agent, the guard-off ablation per agent and the model-free oracle, with a legend and a table view.
  - **"How did the agents behave?":**
    - behaviour compliance per agent and rule (kept, broken, disputed);
    - judge-against-judge and judge-against-label agreement, split by agent family;
    - normal-question accuracy by both measures: a query matched gold, and the answer states the value (D-36).
  - **"Access events":** the verify status (icon and text), anchors, a synthetic-data banner for `embargoed_results`, and the recent public audit lines.
  - **"Data quality":** flags per rule, rows by source, drops by reason and snapshot history.
  - **Accessibility:** every mark is focusable, with a `<title>`, an `aria-label` and a label shown on focus. SVGs are `role="group"`, the legend is readable, and every chart has a table view. Nothing is shown in colour alone. Light and dark modes are both chosen, and charts scroll at their native size on narrow screens. The notes are generated from the data.
- **`assay report`:** `--run --snapshot [--oracle]` collects a bundle into `docs/results/<run>/bundle.json`; `--bundle` re-renders from a committed bundle. Both write `docs/report/index.html`.
- **`.github/workflows/pages.yml`:** re-renders from the newest committed bundle, fails if the committed HTML differs, and deploys `docs/report` to GitHub Pages (OWNER D-13). Actions are pinned by SHA, and `pages: write` and `id-token: write` are granted to the deploy job only.
- **Published:** `docs/results/main-2026-11-16/bundle.json`, `docs/results/LATEST` (the bundle the Pages workflow renders) and `docs/report/index.html`. The raw oracle results stay in `runs/`, because they name held-out cases; the bundle carries their public part.

## Out of scope
The compound explorer (D6.3) and the console page (D6.2).

## Update 2026-11-17 (OWNER D-41)
The owner chose quality gates and run-vs-run comparison, built in EvalKit (its B5.3 gates and B5.6 compare, v0.3.0), with Assay exporting its results. `report/metrics.py` turns a bundle into EvalKit's frozen `metrics-1` summary; `assay report` writes it as `metrics.json` beside a collected bundle, or wherever `--metrics` says. `gates.yaml` holds the owner's levels: strict on safety, warnings for answer quality and spend (the $25 line is the owner's approval threshold for one real-API run). The CI gate step (`evalkit gate docs/results/<run>/metrics.json --config gates.yaml`) and the dashboard's "compared with the last run" wait for EvalKit v0.3.0 on PyPI, which Assay adopts from EvalKit's migration guide (EvalKit D-61 rule 4).

## Update 2026-11-17 (OWNER D-42)
The owner asked for parallel lanes and chose the efficiency lane for Assay (and B5.11 in EvalKit as a second lane). EvalKit standardised a per-model efficiency block (its D-67). The published run's trials already hold each trial's cost, so the bundle gains cost per case, cost per correct answer and the error rate now; tokens and latency were only recorded per tool step (the final answer's turn is missing), so they start with the next recorded run, which records per-trial totals. `retry_rate` is not reported: the SDKs retry internally and do not say how often.

## Update 2026-11-17 (EvalKit v0.3.0 adopted)
EvalKit v0.3.0 is on PyPI with gates (B5.3) and compare (B5.6), so Assay pins `evalkit-harness>=0.3,<0.4`, as the migration guide says (public names only; nothing to replace). The gate runs in CI on the published `metrics.json`. The comparison needs a second run, so the published dashboard does not show it yet; the comparison is unpaired, because `metrics.json` carries no per-case values (a public-only `cases.jsonl` sidecar would allow paired comparison later).
