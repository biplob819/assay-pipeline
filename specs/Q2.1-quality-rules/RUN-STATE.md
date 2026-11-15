# Q2.1 run state

**Now:** green

## History
- 2026-11-15 — started (base 047d7d4); `apply_quality`, consensus, LLM04 suite and scratch-build runner; 37 tests incl. a hypothesis property that QC2 flags exactly the conflicted keys. Found while planting: no QC2 key can span ChEMBL and PubChem (PubChem is type U), so the "cross-source conflict" case became a unit-mix conflict and the consequence is recorded in Tech Spec §5.3.
- 2026-11-15 — verifier pass 1 (attacker) confirmed all seven criteria (exhaustive 4-dp spread pairs near 1.0, 3000 Decimal-checked random pairs, 20 shuffles, 30k rows in 0.32 s, every QC1–QC4 switch-off alone and in pairs). Took its nice-to-haves: a 0 nM row escaped every rule, now QC3 (D-29, Tech Spec §1 and §5.3); planted rows must expect their case's rule, standardise, and use YAML numbers; stealth collision check; a whole table must share one snapshot; switch-off test over QC1–QC4. Left for the owner: whether QC3 rows should leave the median (literal §5.3 keeps them). 11 more tests (48).
- 2026-11-15 — green: converge passed twice; one attacker verifier pass; all seven criteria confirmed.
