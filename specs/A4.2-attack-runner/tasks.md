# A4.2 tasks

- [x] `attack/score.py`: outcome, change and consumption checks, stopping layer, audit coverage, normal-question match
- [x] `attack/stats.py` and `attack/summary.py`: two-level bootstrap, pass^k, zero-success bound, held-out flag
- [x] `attack/passthrough.py` and the `QueryTool(guard=...)` override for the ablation; `AuditLog.last_seq`
- [x] `attack/run.py`: plan, estimate, trials on fresh copies, LLM04 scratch builds, gold check, resume, cost cap, ablation files, summary
- [x] `assay attack` CLI; `/runs/` git-ignored
- [x] `agent.json` records the manifest hash; `load_agent_db`
- [x] tests on scripted providers (`tests/security/test_run.py`) and the recorded live pilot as golden
- [x] pilot on the real snapshot, spend logged
