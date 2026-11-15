# N1.2 — ChEMBL ingest

**Wave 1. Depends on:** N1.1. **Unblocks:** N1.4.

## Goal
Fetch every activity record of the configured target and activity type (default CHEMBL203, IC50; ChEMBL paced by `chembl.max_requests_per_second` in the build config: 26,600 records in ChEMBL_37 on 15 Nov 2026) into a content-addressed raw cache, resumably, recording the ChEMBL release, so every later stage and every rebuild reads frozen bytes and never the network (D-2).

## Outputs (`src/assay_pipeline/pipeline/ingest/`)
- `http.py` (new dependency `httpx`): one `Fetcher` over httpx with an injectable transport (tests use `httpx.MockTransport`), a per-source rate limit, the §2.5 retry rules (backoff with jitter on transport errors, 429 and 5xx, up to 6 attempts; never on other 4xx or an unparseable body) and a user agent naming the project, never the user.
- `cache.py`: `RawCache(root)`. Bodies are stored gzip-compressed with mtime 0 at `<root>/<source>/<release>/<key[:2]>/<key>.json.gz`, keyed `request_key = sha256(method, url, sorted params)`; one `RawResponse` line per stored body in `<root>/<source>/<release>/index.jsonl`, written by one writer, atomically; reading a body checks it against `body_sha256`.
- `chembl.py`: `ingest_chembl(cfg, cache, fetcher)`:
  1. reads `status.json` and takes `chembl_db_version` as the release (CHM-03);
  2. pages `activity.json` with `target_chembl_id`, `standard_type`, `order_by=activity_id`, `limit=page_size` and an `offset`, until `page_meta.total_count` records are cached (CHM-01);
  3. keeps a cursor in `<root>/chembl/<release>/cursor.json` (`offset`, `total_count`, `complete`); a rerun resumes at the cursor and re-fetches nothing already cached (CHM-04);
  4. refuses to continue when an older release's cache is incomplete and the live release differs, unless `--new-release` starts a separate cache; a complete cache makes the rerun a no-op; a cache built for another target, type or page size is refused (CHM-04);
  5. returns an `IngestReport(release, pages, records, requests_made, cached_hits)`.
- The fields each later stage needs are present in the cached records: activity id, molecule, canonical SMILES, standard value, units, relation and type, pChEMBL value, assay id, type and description, variant mutation, ChEMBL source id, data validity comment, document id, target organism (CHM-02). A test checks them on the recorded fixture.
- `assay ingest chembl [--config FILE] [--cache DIR] [--new-release] [--json]` (exit 0 done, 2 error).
- `tests/fixtures/chembl/`: a recorded run of real ChEMBL responses (status, and three pages of 5 activities for CHEMBL203 IC50), with the attribution required by ChEMBL's CC BY-SA 3.0 licence (CHM-06). Tests never touch the network.

## Rules
- `ingest` is the only stage that uses the network; it lives in the pipeline zone (no model SDKs, boundary gate).
- A page whose records don't match the requested target or type, or whose `total_count` changes mid-run, stops the ingest with an error naming the offset.
- Nothing about the user (name, email, machine) goes into requests.

## Out of scope
Parsing records into `Activity` (N1.4), PubChem (N1.3).

## Build notes
- `tests/fixtures/chembl/expected_cache.sha256` holds the digest of the cache the recorded fixture ingests to; the golden test fails on any change to cache layout, decompressed body content, index or cursor format (gzip's mtime-0 header is checked separately, in the CHM-03 test).
- Pipeline tests run with sockets blocked (`tests/pipeline/conftest.py`), so a test that reaches the network fails loudly.
- Verifier pass 1 changes: a page is validated (shape, target and type, integer and strictly increasing `activity_id`, `total_count`) before it is cached, so a bad page never poisons the cache; an older release folder with no cursor counts as incomplete; `--new-release` marks superseded incomplete releases `abandoned`, so later plain runs need no flag; a newer live release with a complete older cache simply starts a new cache folder; every 5xx and 429 is retried, `Retry-After` is honoured up to the backoff cap, other `httpx` errors fail at once, and fetch errors name the request's parameters (offset); corrupt cursors, bodies and index lines are `CacheError`s (CLI exit 2); release names are allow-listed (`[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}`, no Windows device names) and stray folders are ignored; a `.lock` file enforces the single writer; `fetched_at` must come from the server's Date header (`-0000` read as UTC); the golden digest hashes decompressed bodies so zlib builds cannot change it.
- User agent (D-27): `assay-pipeline/<version> (+<project homepage>)`. It names the tool and its public homepage, never the person or machine running it.
- Verifier pass 2 changes: `total_count` is checked against the cursor inside `_check_page`, before caching; `--new-release` abandons older releases only after the new release's config check passes; a returning abandoned release resumes with its abandoned flag cleared; any non-2xx response is an error; release names with a trailing dot or a device-name stem (`NUL.x`) are refused.
