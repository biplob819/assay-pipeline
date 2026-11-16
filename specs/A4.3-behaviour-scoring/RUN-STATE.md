# A4.3 run state

**Now:** green

## History
- 2026-11-16 — started (base e0fd9ee). Deterministic checks, judges and the `assay behaviour` runner built and tested on scripted agents and judges (16 tests). Waiting on the main live run (`runs/main-2026-11-16`) for hand labels and live judging.
- 2026-11-16 — attacker verifier pass 1: 2 must-fix, 5 should-fix, 6 notes, all handled with repros ported:
  - **Must-fix, B1 parsing:** B1 misread inline backticks, fenced SQL with a lone `*` line, and prose inside the block. The block reader now takes, for each statement, the longest stretch of lines that parses as one query.
  - **Must-fix, judging spend:** judging spend and `--max-cost` counted only the current invocation. Spend is now cumulative from `judges.jsonl`, and the cap is checked against spend plus estimate.
  - **Should-fix, suite match:** a suite that is not the run's was skipped silently. It is now refused through the run's `cases` digest (`run.cases_digest`).
  - **Should-fix, B8:** B8 was derived, so it missed a trial that both changed the copy and leaked. Runs now record the change check itself; the derivation stays only for older records.
  - **Should-fix, transcript:** redaction swallowed answer text and data words ("Sol-gel"), and the answer could forge transcript tags. Redaction now targets names only, and the material is escaped.
  - **Should-fix, family split:** label agreement is now split by agent family, with agree counts.
  - **Should-fix, unfinished runs:** `assess` could truncate a live run's files, and a finished attack run wiped the behaviour sections. `assess` now reads without repairing and refuses an unfinished run, and a no-op resume keeps the behaviour sections.
  - **Notes:**
    - the judge output budget is now 8,000 tokens and the planning cost $0.05 per Opus call;
    - B2 needs the exact id;
    - kappa is undefined (None) for constant raters;
    - labels are checked for duplicates, trial existence and rule applicability.
- 2026-11-16 — CI on Windows: the unfinished-run check compared file times, which Windows updates too coarsely; `assay attack` now records the trials file's hash in summary.json and `assess` compares content. A run started before this change needs one no-op `assay attack` resume to write the hash.
- 2026-11-16 — main run finished (490 trials, $3.50; ablation 80 trials, $0.38), then a no-op resume to record its trials hash.
  - **Hand labels:** 24 answers drawn at random, 8 per judged rule and 4 per agent. All 24 keep their rule, with reasons recorded in `docs/measurements/hand-labels-main-2026-11-16.jsonl`.
  - **Live judging:** 192 calls for $0.72. Both judges agree with all 24 labels and with each other on 94 of 96 verdicts. Kappa is undefined or 0, because almost every answer passes (`docs/measurements/behaviour-2026-11-16.md`).
  - **Two accuracy views:** reading answers showed the strict query match misses right answers given from breakdown queries. Claude states the gold value in 100% of single-number trials but matches by query 60% of the time; Sol 73% and 70%. The second view is now reported beside the strict one (D-36), with a test.
- 2026-11-16 — green: converged twice; verifier pass done; live judging and hand labels recorded.
- 2026-11-16 — after green: the hand labels first drew two held-out questions (nq-26, nq-30) whose reasons were pushed in bfe3cb8; labels redrawn from public questions (judges still agree with all 24), a test added, D-12 update recorded.
