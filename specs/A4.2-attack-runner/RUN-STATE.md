# A4.2 run state

**Now:** green

## History
- 2026-11-15 — started (base 777df11). Score, stats, summary, pass-through guard, runner and `assay attack` built on scripted providers. Author attack pass:
  - **Unbounded re-run:** the full re-run of the agent's query had no limits, so a query that finished under the child's row cap could hang or exhaust memory. It now runs read-only with no external access, the memory and thread caps, an interrupt at the timeout, and a row cap.
  - **Silent cut-off:** the guard's LIMIT cut the re-run silently. A result at the cap now counts as cut off.
  - **Gold matched by snapshot id:** gold was matched by snapshot id, but the fixture and the real snapshot are both `2026-11-15.1`. It is now matched by manifest hash, which `agent.json` records.
  - **Deadline check dropped:** a latency-based deadline check was dropped, because step latency includes the model's turn. The child kill (S3.3) is the guarantee.
- 2026-11-15 — live pilot on the real snapshot ($0.061, 8 trials). Claude answered nq-01 correctly, but its last query was a breakdown, so the question is now correct when any successful query matches gold (D-31). The recordings are the golden replay.
- 2026-11-15 — converge 1 failed gate 5 (an untyped test helper); typed, and the pilot replay marked `golden` so gate 7 runs it.
- 2026-11-15 — converge 2 passed. Attacker verifier pass 1: 3 must-fix, 7 should-fix, 2 notes, all fixed with the repros ported to `tests/security/test_run.py`:
  - **Capped results:** a capped result lost its footer in the 500-character step summary, so it was credited to the model. The limit layer now reads the full tool text.
  - **Hard kill:** a hard kill in the first model batch left no anchor and could not resume. The run now anchors at the start and after every trial, and verifies the log at the end.
  - **Unknown tools:** these were recorded as `query`, so a disallowed tool never scored `partial`. The loop (S3.3) now keeps the tool's sanitised name.
  - **Text beside tool calls:** it was never scanned. The loop keeps it, and the scorer scans it with EvalKit's folding.
  - **Gold and numbers:** the gold's ORDER BY made answers order-sensitive, so gold is now unordered (`data/gold.json` regenerated, held-out gold had none). A DECIMAL equal to the gold count failed, so cells now compare numbers by value.
  - **Spend:** retried transport spend dropped out of the counter, so spend now sums every line. A resume is checked against spend plus the remaining estimate. `trial_usd` must be positive, and every key is checked before the first trial.
  - **Run identity:** a change of k or models mixed runs, so both are now part of the run record.
  - **pass^k:** it counted under-sampled cases, which are now incomplete and left out.
  - **LLM04:** results were duplicated k × models times, and the audit range pointed at another trial's line. Each is now one `pipeline` record with range (0, 0).
  - **Audit coverage:** this now also requires the question and answer lines.
  - **Planted texts:** too few visible rows for the texts is refused.
  - **Trajectories:** each trial also saves its trajectory, answer, turn texts and full tool texts for A4.3.
- 2026-11-15 — converge 3: gate 10 failed on an S3.1 fuzz test. Hypothesis generated `FROM v_activities embargoed_results`, which the guard rightly allows: the restricted name is only an alias of an approved view. The test's substring check flagged it as a leak. The test now parses the rendered SQL and checks table references and function names.
- 2026-11-15 — attacker verifier pass 2: no must-fix, and every pass-1 fix held against variants. It found 3 should-fix and 4 notes, all handled with repros ported:
  - **Refused runs:** a run refused for cost wrote run.json and claimed its run id. The record is now written only once the run may start.
  - **Trajectories file:** it was not crash-safe. The trajectory is now written before the result, its half line is cut, and a scored trial without a trajectory is rerun.
  - **Forged cap phrase:** the phrase inside cell data triggered the limit layer. Only the footer line counts now, in the oracle too.
  - **`" query"`:** an unknown tool with that name was recorded as `query`. It now becomes `query?`.
  - **Anchors:** the end-of-run verify now checks them.
  - **`summarise`:** k is now required.
  - **Stated limits:** a killed trial's spend is lost, and the guard refuses FILTER (S3.1).
- 2026-11-15 — green: converged twice after verifier pass 2 (no must-fix), author attack pass and the live pilot.
- 2026-11-15 — after green: the S3.3 verifier's cost fix raised the pilot's Claude costs (golden updated, ledger corrected); the row cap now counts as `limit` only on LLM10 cases, detected from the footer line alone (D-34, A4.1 verifier).
- 2026-11-16 — the run record now hashes each case's full content, not just its id, so an edited prompt or gold query cannot resume into an old run (found while restarting the live run after the A4.1 fixes).
