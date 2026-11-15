# A4.3 run state

**Now:** building

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
