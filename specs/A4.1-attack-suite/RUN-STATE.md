# A4.1 run state

**Now:** green

## History
- 2026-11-15 — started (base 3924285). ATLAS ids checked against atlas-data v2026.10 (AML.T0056/T0057 no longer exist). Real ingest and snapshot built (41,463 rows). Oracle passes all 60 cases on the recorded fixture after making three LLM10 cases independent of data size (DuckDB counts a cross product without materialising it, and tiny fixtures stay under the row cap). Two gold queries rewritten to stay inside the guard (no FILTER clause, no array_to_string). Gold computed on the real snapshot.
- 2026-11-15 — gate 3 refused skipped tests as proofs; the held-out tamper test now builds a synthetic held-out folder so it runs in CI, and golden is n/a (real snapshot and held-out are local only).
- 2026-11-15 — real-data oracle: 60 of 60 stopped by an expected layer (guard 47, quality_rule 10, limit 5), no leak (docs/measurements/oracle-2026-11-15.md). Author attack pass: no case passes on a syntax error; every block reason is substantive.
- 2026-11-15 — green on converge (twice), the author attack pass and both oracle runs. **Owed:** the independent attacker verifier pass (the subagent account is rate-limited until 18 Nov, 1:30 pm IST).
