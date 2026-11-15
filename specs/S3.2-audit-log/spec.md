# S3.2 — Audit log

**Wave 3. Depends on:** N1.1. **Unblocks:** S3.3.

## Goal
Every agent call leaves a line in a log nobody can quietly edit (US5, FR9, Tech Spec §6.3, D-7): a hash chain over canonical JSON from a fixed genesis line, written by one writer, with anchors kept outside the log so a truncated tail or a fully rewritten log is caught; `assay verify-audit` and `make verify-audit` re-check it and name the first broken line.

## Outputs (`src/assay_pipeline/audit/`)
- `AuditLog(path, run_id, anchors=None, clock=...)`: writes a genesis line (seq 0, prev_hash 64 zeros) to a new log, or resumes an existing log only after it verifies against at least one anchor naming it and its genesis carries the same run_id (a non-empty log without its anchors file is refused); holds an OS lock (`flock`, or on Windows a `msvcrt.locking` byte lock far past EOF so readers are never blocked) on the open log file itself, so a second writer through any path, symlink or hardlink, from any TMPDIR or user, is refused and a crashed writer leaves no stale lock; an emptied log that an anchor still names is never started over; a failed log or anchor write (a half anchor is truncated away), the log path being replaced or removed, or the log changing size under the writer (truncated in place, or bytes appended by a process ignoring the lock) marks the writer broken and every later append or anchor is refused; a clock stepping back never makes `at` run backwards (it is clamped to the previous line's time); `resumed_unanchored` reports the lines after the last anchor at resume, for S3.3 to record; serialises appends with a thread lock; each line is canonical JSON (sorted keys, UTF-8, no whitespace), validated against `AuditLine` (N1.1 event rules), flushed and fsynced.
- `append(**fields)`: callers give event fields only; `seq`, `prev_hash`, `hash`, `at`, `run_id`, `schema_version` and genesis are set by the log. A closed log refuses `append`, `anchor` and a second `close`.
- `anchor()` appends `{log, seq, hash, at}` to `<log>.anchors.jsonl`; `close()` anchors the final line and releases the lock. Anchors default to the per-log `<log>.anchors.jsonl`; S3.3 may pass a shared anchors path. A partial final anchor line (crash mid-write) anchors nothing and is trimmed in place (truncate, never rewrite) on resume, only after the log verifies and its run_id matches. Anchors name a log by its file name, so a renamed or copied log fails to verify against its anchors; S3.3 keeps log names unique per run folder.
- `line_hash(fields) = sha256(canonical_json(fields without "hash"))`.
- `verify(log, anchors=None, *, require_anchor=True) -> VerifyReport(ok, lines, anchors, first_broken_seq, reason, unanchored_tail)` never raises (undecodable bytes, huge integers, deep nesting or duplicate keys in a line or an anchor are failing reports naming the line; a non-regular file such as a FIFO, or one over 1 GiB, is refused without reading); times must not go backwards; lines are split on `\n` only, so U+2028 inside data is harmless; when an anchors file is given at least one anchor must name this log; every line parses, is canonical, has seq equal to its position, only line 0 is genesis, links to the previous hash and re-hashes to its own; an incomplete last line fails; every anchor for this log must point inside the log at a line with its hash. Lines after the last anchor are reported (`unanchored_tail`), because their truncation is undetectable — the stated limit.
- CLI `assay verify-audit LOG [--anchors FILE | --no-anchors] [--json]` (anchors default to `<log>.anchors.jsonl`; a missing anchors file is exit 1 unless `--no-anchors`; exit 0 ok, 1 broken; errors are JSON under `--json`, never a traceback) and a `make verify-audit` target; gate 11's smoke runs it on `tests/fixtures/audit_ok.jsonl`.

## Rules
- Stated limit (threat model): someone able to rewrite both the log and the anchor file can forge history; production adds WORM storage or external timestamping.
- The log never stores answer text: answers are evidenced by `answer_sha256` (N1.1).

## Out of scope
Deciding what to log for each agent call (S3.3), the coverage cross-check between trajectories and lines (A4.2).

## Build notes
- `Makefile` added with `verify-audit` (default `LOG=tests/fixtures/audit_ok.jsonl`) and `test`; gate 11's smoke now runs the installed `assay verify-audit` on the fixture.

