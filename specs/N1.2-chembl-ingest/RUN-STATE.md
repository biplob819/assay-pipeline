# N1.2 run state

**Now:** green

## History
- 2026-11-15 — spec written from the API probe (ChEMBL_37; activity records carry assay description, variant mutation and src_id).
- 2026-11-15 — started (base 3c19c72); `httpx` added; fetcher, content-addressed cache (gzip mtime 0, index of RawResponse lines, cursor), resumable ChEMBL ingest with release lock, `assay ingest chembl`; 22 tests on the recorded fixture incl. a golden cache digest.
- 2026-11-15 — verifier pass 1: CHM-01, 02, 03, 06 confirmed; CHM-04 (--new-release locked later runs out; a cursor-less older release went undetected; bad pages were cached before validation; no single-writer lock) and CHM-05 (501/505/520/524 not retried; redirect/decoding errors escaped; Retry-After ignored) disproved; should-fix: corrupt cache files gave tracebacks, release names blocklisted not allow-listed, null release accepted, overlapping pages undetected, wall-clock fetched_at fallback, `-0000` dates crashed, golden digest tied to zlib bytes. All fixed with tests; user agent kept as tool + homepage (D-27).
- 2026-11-15 — verifier pass 2: CHM-01, 02, 03, 05, 06 confirmed; CHM-04 disproved on one clause (a page with a changed total_count was cached before the check). Fixed, with the should-fix items (abandon after the config check, abandoned releases resume cleanly, non-2xx is an error, trailing-dot and device-stem release names refused, golden note corrected).
- 2026-11-15 — verifier pass 3: CHM-04 confirmed, no regressions. Leftovers fixed (a config-less cursor over cached pages is refused; build note corrected). Converge passed twice; rows flipped green.
- 2026-11-15 — CI on 9ffb7f0 failed on Windows 3.11/3.12 (retry test confused a pacing sleep with a backoff under Windows' coarse clock); test fixed (L-18).
