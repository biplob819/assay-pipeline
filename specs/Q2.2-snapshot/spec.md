# Q2.2 — Snapshot and manifest

**Wave 3. Depends on:** Q2.1. **Unblocks:** Q2.3, R6.1.

## Goal
Write the flagged table, the consensus table and the drop ledger as a snapshot that rebuilds byte for byte from the same raw cache and code (US3, NFR2, §5.5). It comes with a manifest that is enough to compare two builds (FR5), a data card and a licence (NFR5, D-15), stage manifests with a skip rule (D-1), and a publish path that refuses planted rows (H6).

## Outputs (`src/assay_pipeline/pipeline/snapshot.py`)
- **`build_snapshot(cfg, cache, out_root, *, chembl_release=None, pubchem_release=None, terms=None, planted=(), code_sha=None) -> BuildResult(manifest, directory, skipped)`**:
  - It reads the cache only, then runs normalise (N1.4) and quality (Q2.1).
  - It writes `<out_root>/<id>/` with `activities.parquet`, `consensus.parquet`, `drops.parquet`, `DATA_CARD.md`, `DATA_LICENSE.md`, `manifest.json` (canonical JSON) and `_stages/{normalise,quality,snapshot}.json`.
  - The folder is assembled in a temp folder and renamed into place, so a failed build leaves nothing.
- **Deterministic Parquet (SNP-01):**
  - explicit Arrow schemas;
  - rows sorted by `(inchikey, source, source_record_id, activity_uid)`;
  - `store_schema=False` (no pandas or Arrow schema blob), row groups of 65,536, zstd level 9, dictionary encoding on;
  - `flags` and `member_uids` are list<string>, and `flag_reasons` is canonical JSON text.
- **Manifest (SNP-02):**
  - `snapshot`, `target`, `activity_type`;
  - `created_at`: the ChEMBL status fetch time in the cache (no clock);
  - `source_releases`, and `raw_cache_sha256` over the index (request key and body hash) of every release read;
  - `rows_by_source`, `drops_by_reason`, `flags_by_rule`;
  - `files` (sha256 of bytes) and `content_sha256` (sha256 of canonical rows, or of the text for the two documents);
  - `tool_versions` (Python major.minor, assay-pipeline, pyarrow, rdkit, pydantic);
  - `code_sha`: a fingerprint of the package's own `.py` and `.yaml` files, exact for a pip install too;
  - `planted`.
- **Snapshot id (SNP-03):** `assign_snapshot_id` gives `YYYY-MM-DD.n`.
  - The date is the ChEMBL fetch date.
  - The id is keyed on the build's identity: the sha256 of raw-cache hash, ChEMBL fetch time, the content-affecting config (target, quality, rounding, excluded depositors; not base URLs or rate limits), depositor-ledger digest, every tool version (Python included, since the manifest records it), all of `CODE_VERSIONS`, planted rows and code fingerprint.
  - The same identity reuses its id, on any day. Anything else takes the next n, so different content never reuses or replaces an id. An existing snapshot whose identity cannot be read is never overwritten.
  - Only folders named exactly after their manifest's id count; hidden temp folders are ignored.
  - Scratch (planted) builds belong in their own root. In a shared root they take an n like any other identity.
- **Rebuild gate (SNP-04):** `pytest -m rebuild` builds twice in two clean folders, each from its own freshly ingested cache, with the real code fingerprint, and compares manifest bytes. Gate 8 runs it.
- **The golden test** compares the recorded-fixture manifest with `tests/fixtures/snapshot/expected_manifest.json`, leaving out `code_sha` and `tool_versions`, which change with every commit and Python version.
- **`DATA_CARD.md` (SNP-05):**
  - sources and releases;
  - yields per source (rows offered, rows kept) and drops by reason;
  - each rule's meaning and count, consensus keys and conflicts;
  - validity comments, source-key mismatches, and compounds present in both sources (D-24);
  - the RDKit version, the stated limits, and the licence.
- **`DATA_LICENSE.md` (SNP-05):** CC BY-SA 3.0 with ChEMBL attribution, and every cleared PubChem depositor's licence and attribution from the ledger (D-15). The code stays MIT.
- **`read_snapshot(folder)`:** verifies the snapshot, then re-validates every row. It refuses:
  - a manifest that doesn't list exactly the five files (no path tricks, no missing file);
  - a folder named other than its id;
  - a file or content hash that doesn't match;
  - counts that don't match the rows: `rows_by_source`, `drops_by_reason` and `flags_by_rule` are all recomputed, so a manifest cannot certify itself;
  - planted rows, or a planted stage input, under `planted=false`.

  - integrity breaks that need no config: a repeated `activity_uid`, consensus members that don't partition the activities or cross keys, medians or spreads that don't match their rows, QC2 flags that disagree with conflicts;
  - a data card whose "Planted rows" line disagrees with the manifest;
  - an unreadable snapshot stage manifest.

  Malformed Parquet is a `SnapshotError`, never a traceback. A full forgery (rows, hashes and counts rewritten together) is outside what an unsigned manifest can stop (the stated D-7-style limit).
- **`publish(folder, dest_root)` (SNP-06):** reads with `read_snapshot`, so every refusal above applies, and also refuses `planted=true` and a different snapshot already published under the same id. It copies only the five fixed files plus the manifest, sets mode 0755 and is idempotent.
- **Stage manifests (SNP-07):** each stage records `{stage, code_version, inputs, outputs, rows_in, rows_out, drops_by_reason, tool_versions}`. An existing snapshot is skipped only when its identity matches, it reads back fully verified, and all three stage manifests are present, parse, carry the current code version, and the snapshot stage's outputs equal the manifest's files. Otherwise it is rebuilt under the same id.
- **Atomic swap:** a rebuild moves the old folder aside, renames the new one in, then deletes the old one. A failed swap puts the old one back, and an old folder orphaned by a crash between the renames is restored at the next build.
- **Planted rows:** `build_snapshot` refuses planted rows without the `planted:` / `PLANTED-` markers that `planted_activity` gives them.
- **CLI:** `assay snapshot [--config] [--cache] [--out] [--chembl-release] [--pubchem-release] [--depositor-terms] [--json]` and `assay publish SNAPSHOT [--dest] [--json]`. Exit 0, or 2 with the reason. Make targets: `make ingest`, `make snapshot [CONFIG=...]`, `make publish SNAPSHOT=...`.
- **Dependency:** `pyarrow>=18` (exact version in `uv.lock`, recorded in `tool_versions`).

## Out of scope
The agent database and views (Q2.3), the published benchmark run (B5.x, REL).
