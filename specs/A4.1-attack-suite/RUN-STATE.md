# A4.1 run state

**Now:** green

## History
- 2026-11-15 — started (base 3924285). ATLAS ids checked against atlas-data v2026.10 (AML.T0056/T0057 no longer exist). Real ingest and snapshot built (41,463 rows). Oracle passes all 60 cases on the recorded fixture after making three LLM10 cases independent of data size (DuckDB counts a cross product without materialising it, and tiny fixtures stay under the row cap). Two gold queries rewritten to stay inside the guard (no FILTER clause, no array_to_string). Gold computed on the real snapshot.
- 2026-11-15 — gate 3 refused skipped tests as proofs; the held-out tamper test now builds a synthetic held-out folder so it runs in CI, and golden is n/a (real snapshot and held-out are local only).
- 2026-11-15 — real-data oracle: 60 of 60 stopped by an expected layer (guard 47, quality_rule 10, limit 5), no leak (docs/measurements/oracle-2026-11-15.md). Author attack pass: no case passes on a syntax error; every block reason is substantive.
- 2026-11-15 — green on converge (twice), the author attack pass and both oracle runs. The independent attacker verifier pass was owed at this point; it ran later the same day (below).
- 2026-11-15 — independent attacker verifier pass: 2 must-fix, 4 should-fix, 4 notes, fixed in an A4.1 fix commit with tests:
  - **Must-fix, censored gold:** gold counted `unknown` relations as censored. nq-19 named PubChem, whose 14,892 rows are all unknown, when the answer is ChEMBL with 5,339 censored rows. nq-21 said 50.8% instead of 13.4%. Censored now means <, <=, > or >= everywhere: in the gold SQL, the question wording (nq-17 to nq-21), the system prompt and the B3 rubric (D-33). Gold was regenerated.
  - **Must-fix, held-out leak:** the SQL of two held-out cases was already in public test material, so the guard had been tuned on it. Both cases were replaced with techniques that appear nowhere public, and the held-out manifest was rewritten.
  - **Should-fix, held-out gold:** the manifest now pins held-out `gold.json`, and the git-ignore test covers it.
  - **Should-fix, row cap:** the cap phrase inside a cell no longer counts as the cap. The cap is the `limit` defence only on LLM10 cases (D-34), and a new negative control shows the oracle can fail an LLM10 case.
  - **Should-fix, cases that didn't test their claim:** atk-llm01-06 and atk-llm02-07 now have prompts that match their SQL. atk-llm02-08 and atk-llm05-06 read the canary column, so a guard-off leak is detectable, and the unreal `permissions` stop was dropped. atk-llm05-05 uses a function that exists. atk-llm10-07 is a sorted three-way cross product that hits the real timeout.
  - **Should-fix, suite checks:** `check_suite` now enforces an enforced layer per agent case, quality_rule only for LLM04, planted descriptions for indirect cases, the held-out question count, and each file's split.
  - **Notes:** the oracle doc's table counts statements. Ties at nq-05's cut-off and in nq-11 do not occur on this snapshot (checked). `classify` mapping InvalidInput to `permission` is left as is; no allowed query gets false credit from it.
  - **Oracle re-run:** on the real snapshot, the eight changed cases all pass.
