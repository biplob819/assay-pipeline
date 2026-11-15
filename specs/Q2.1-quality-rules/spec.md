# Q2.1 — Quality rules

**Wave 3. Depends on:** N1.4. **Unblocks:** Q2.2.

## Goal
Flag every normalised row under QC1–QC5 exactly as Tech Spec §5.3 defines them, without deleting any (D-6, FR4). Build the consensus table: one row per QC2 key, with the median over `=` rows and every member's uid (D-6, H1). Ship the LLM04 planted-row suite and a scratch-build runner that proves each rule catches what it should (US2, US7, H7).

## Outputs
- `pipeline/quality.py` — `apply_quality(activities, cfg) -> QualityResult(activities, consensus)`:
  - **Rows:** every input row is returned in input order, re-validated as an `Activity`, with sorted `flags` and one reason per flag. Re-applying the rules gives the same result.
  - **QC1:** `value_nm` is null; the reason says "no numeric value", "no units", or the value and units that can't be converted.
  - **QC2:** the key `(inchikey, target, variant, assay_type)` has at least two `=` rows with a pIC50 spread above 1.0. Every row of the key is flagged, censored members included. Spreads are compared at pIC50 precision (4 dp), so exactly 1.0 (and 7.3 − 6.3) is not a conflict.
  - **QC3:** `pic50` is present and outside the configured bounds (default [3, 12], inclusive), censored rows included; a value of exactly 0 nM (pIC50 +inf) is out of range too (D-29).
  - **QC4:** `source_record_id` is missing or blank.
  - **QC5:** the relation is not `=` (censored or `unknown`).
  - **Consensus:** one row per key, sorted. The median and spread are over `=` rows with a pIC50 (QC5 rows are excluded). `conflict` is true exactly when QC2 flags the key. `n_records` and `member_uids` cover every row of the key, and `n_sources` counts the sources. A table whose rows mix snapshots is refused. QC3-flagged rows still count in a key's median and spread (literal §5.3: only QC5 rows are excluded).
  - `flags_by_rule()` (all five keys) and `conflicts`.
- `pipeline/planted.py` and `defaults/planted_rows.yaml` (LLM04, QCR-07):
  - **The suite:** 10 cases over QC1 (mass units, no units, no value), QC2 (a conflict pair, a conflict with a censored member, a unit-mix conflict), QC3 (too potent, too weak, censored out of range) and QC4 (no record id). There are 3 stealth rows (plausible values wrong by 10×, with no record to disagree).
  - **Loading:** `load_planted(path=None)` validates the suite strictly: unique ids, QC1–QC4 rules only, every row expecting its case's rule (so a case cannot pass for another reason), SMILES that standardise, YAML numbers only (no booleans or strings), no repeated flags, no duplicate YAML keys; an unreadable file is a `ConfigError`.
  - **`run_case(case, base, cfg, snapshot=None)`:** plants the rows next to real normalised rows in a scratch table, runs the rules, and passes when every planted row has every expected flag. A planted structure that collides with a real compound is refused.
  - **`run_stealth`:** returns the flags a stealth row gets, expected none (a known miss, H7); a stealth structure colliding with a real compound is refused.
  - The view-absence half of an LLM04 case comes with Q2.3's views.

## Stated consequence (recorded in Tech Spec §5.3)
PubChem rows are assay type `U` and ChEMBL rows never are, so no QC2 key spans both sources and `n_sources` is 1 in practice. Cross-source overlap is reported in the data card by InChIKey (D-24, Q2.2), not through QC2.

## Out of scope
Snapshot files and the manifest's `flags_by_rule` (Q2.2), views and the agent database (Q2.3), LLM04 cases as attack-suite entries (A4.1).
