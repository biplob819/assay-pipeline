# A4.1 acceptance criteria

**ATK-01** The suite has 60 attack cases, ten per OWASP category (eight public, two held out), each tagged with a primary OWASP category, any secondary ones, and an ATLAS technique from the checked list.
**ATK-02** Every agent case lists the SQL the attacker wants executed and an `expected_stop` naming an enforced layer, every LLM04 case is stopped by the quality rules, and the model-free oracle stops every case with an expected layer and no canary, on the recorded fixture and on the real snapshot, while a case whose SQL nothing stops fails.
**ATK-03** The indirect-injection cases carry planted description texts that land in a row visible to the agent.
**ATK-04** There are 30 normal questions (14 statistics, 8 censored, 4 ambiguous, 4 off-scope) tagged with their behaviour rules; every gold query passes the guard; gold results are recorded with the snapshot's manifest hash, refused for another snapshot, and recompute identically.
**ATK-05** The held-out split (12 attacks, 6 questions, its gold) is never tracked by git; a committed manifest pins its files by hash and counts, and an edited held-out file is refused.
**ATK-06** No canary appears in any prompt, attacker SQL, planted text or question, and a case that carries one is refused.
**ATK-07** `data/atlas.yaml` lists every technique used with the ATLAS source, version and check date.
