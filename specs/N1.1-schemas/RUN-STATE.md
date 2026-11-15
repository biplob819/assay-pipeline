# N1.1 run state

**Now:** green

## History
- 2026-11-15 — spec written; API probe: ChEMBL_37, 26,600 EGFR IC50 activities; PubChem 4,625 EGFR AIDs, about 95% deposited by ChEMBL.
- 2026-11-15 — started (base 3e19655); 11 schemas with cross-field rules, configs shipped in the package (ChEMBL rate limit added), 192 schema tests; build smoke loads the packaged configs.
- 2026-11-15 — spec and criteria updated for the per-source rates and the packaged-defaults proof.
- 2026-11-15 — verifier pass 1: SCH-02, SCH-04 confirmed; SCH-01 (pIC50 tolerance looser than the spec; deepcopy/pickle crashed on FrozenDict; floats took bools and strings; key order changed JSON), SCH-03 (empty or non-object LLM04 planted data; Unicode digits in ids; impossible case ids and stopping layers) and SCH-05 (Unicode-digit schema versions) disproved. Fixed all, plus should-fix: UTC timestamps, consensus count rules, real snapshot dates, trial ids required on audit lines, config knobs the schemas ignore removed, function allow-list limited to known names, a wheel-inspection test.
- 2026-11-15 — verifier pass 2: SCH-01..05 confirmed, no regressions. Should-fix applied (functions validated at their field; cross-record answer_correct rule assigned to A4.2; N1.4 must compute pIC50 from the stored value). Converge passed twice; rows flipped green.
