# N1.1 — Schemas and config

**Wave 1. Depends on:** W0.1. **Unblocks:** N1.2, N1.3, S3.1, S3.2.

## Goal
Every record that crosses a stage, the guard, the audit log or the evaluation has one frozen, validated pydantic model (Tech Spec §3), and the build and agent configuration loads from YAML with clear errors.

## Outputs (`src/assay_pipeline/schemas/`, `src/assay_pipeline/config.py`, `src/assay_pipeline/defaults/`)
- Base model: frozen, `extra="forbid"`, strict finite floats (no bools or strings), tuples for sequences and a frozen mapping for dicts with sorted keys (EvalKit L-8, L-9); frozen mappings survive copy, deepcopy and pickle; timestamps are aware and normalised to UTC; every pattern uses ASCII digits `[0-9]` (L-16); every top-level file model carries `schema_version: "1.0"`. `model_copy(update=...)` and `model_construct` skip validation by pydantic's design; pipeline code builds records with `model_validate`.
- Data schemas (SCH-01): `RawResponse`, `Activity`, `Drop`, `Consensus`, `Manifest`, with the cross-field rules below.
- Security schemas (SCH-02): `GuardDecision`, `AuditLine`, `Anchor`.
- Evaluation schemas (SCH-03): `AttackCase`, `NormalQuestion`, `TrialResult`.
- `BuildConfig` and `ViewsConfig` loaded from YAML; the defaults ship inside the package as `assay_pipeline/defaults/build.yaml` and `views.yaml` so a pip install needs no repo checkout (SCH-04): target (name, ChEMBL target id, UniProt accession, gene symbol; default EGFR, OWNER D-22), activity type (default IC50), the QC3 pIC50 range (the QC2 conflict spread of 1.0 and the 4-dp rounding are fixed in the schemas, not configurable), sources (each with its own request rate: `chembl.max_requests_per_second` and `pubchem.max_requests_per_second`, the latter at most 5 per PubChem's usage policy), snapshot rules, approved views with their columns, the guard's function allow-list (only names in `KNOWN_FUNCTIONS`; table functions never), row limits (1,000 executed, 50 shown, 6,000 characters), timeout (10 s), step budget (8).
- Format v1 fixtures under `tests/fixtures/schemas/` and round-trip contract tests (SCH-05).

## Cross-field rules
- `Activity`: `pic50` present iff `value_nm` present and > 0, and exactly `pic50_of(value_nm)` (9 − log10, rounded to 4 dp, from the stored value; float noise only); `relation="unknown"` when the source gives no relation or a non-standard one (either source); `assay_type="U"` for, and only for, PubChem; `chembl_src_id` and `data_validity_comment` ChEMBL-only; `depositor` set iff source is PubChem; `flags` sorted, unique, and keys of `flag_reasons` equal to `flags`; `inchikey` matches the InChIKey pattern; `snapshot` matches `YYYY-MM-DD.n`.
- `Consensus`: `conflict == (spread_pic50 > 1.0)`; `median_pic50` and `spread_pic50` both null or both set; `n_records == len(member_uids)`; member uids unique; `n_sources <= n_records`; a non-zero spread needs at least two records. Snapshot ids carry a real calendar date.
- `Manifest`: `planted` is a bool with no default (every writer states it); every file in `files` also in `content_sha256`; hashes are 64 lowercase hex.
- `GuardDecision`: `allowed` requires `rendered_sql` and no `reason`; `blocked` requires a `reason` from the closed set of §6.1 codes.
- `AuditLine`: field presence follows `event` (`question` text only on `question` events; `answer_sha256` only on `answer` events, so answers are evidenced without storing their text; SQL and guard fields only on `query` events, with a blocked query carrying no database fields and an allowed one either a result triple or a `db_error_class`; `genesis` has seq 0, a zero `prev_hash` and nothing else); hashes 64 hex.
- `Manifest` also names the `target` and `activity_type` it was built for (OWNER D-22).
- `AttackCase`: id pattern `atk-<owasp lower>-NN`; `attacker_sql` non-empty except for LLM04 cases, which need non-empty `planted_data` (a JSON object: string keys, JSON values) instead; `expected_stop` non-empty.
- `NormalQuestion`: `gold_sql` required for `stat` and `censored`, forbidden for `ambiguous` and `off_scope`.
- `TrialResult`: `case_id` names one of the six suite categories or a normal question; attacks carry `outcome` and `stopping_layer` (`quality_rule` for, and only for, LLM04 cases that were stopped), normal questions carry `answer_correct` (null only when the question has no gold — a cross-record rule the schema cannot see, enforced by the A4.2 scorer against the `NormalQuestion`); `status="void"` or `"transport"` carry neither.
- `AuditLine`: every non-genesis line names the model and the trial (the §6.3 coverage cross-check).

## Additions to Tech Spec §3 (D-24)
- `Activity.data_validity_comment` (ChEMBL's flag, counted in the data card, §5.1) and `Activity.chembl_src_id` (ChEMBL's own source, e.g. 7 = PubChem BioAssay, 37 = BindingDB) so cross-source duplicates can be traced (probe of 15 Nov 2026).

## Out of scope
Parsing source payloads (N1.2, N1.3), computing values (N1.4), guard logic (S3.1).

## Build notes
- The packaged defaults are proven to ship in the wheel: gate 11 loads both configs from the installed wheel in a clean venv.
- For N1.4: `pic50` must be computed from the stored (rounded) `value_nm`; computing it from the unrounded value disagrees on about 0.4% of values (verifier pass 2: 851 of 200,000) and fails validation.
