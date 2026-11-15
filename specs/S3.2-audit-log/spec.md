# S3.2 — Audit log

**Wave 3. Depends on:** N1.1. **Unblocks:** S3.3.

## Goal
Every agent call leaves a line in a log nobody can quietly edit (US5, FR9, Tech Spec §6.3, D-7): a hash chain over canonical JSON from a fixed genesis line, written by one writer, with anchors kept outside the log so a truncated tail or a fully rewritten log is caught; `assay verify-audit` and `make verify-audit` re-check it and name the first broken line.

## Outputs (`src/assay_pipeline/audit/`)
- `AuditLog(path, run_id, anchors=None, clock=...)`: writes a genesis line (seq 0, prev_hash 64 zeros) to a new log, or resumes an existing log only after it verifies; holds a `<log>.lock` file (exclusive create) for its lifetime, so a second writer is refused; serialises appends with a thread lock; each line is canonical JSON (sorted keys, UTF-8, no whitespace), validated against `AuditLine` (N1.1 event rules), flushed and fsynced.
- `append(**fields)`: callers give event fields only; `seq`, `prev_hash`, `hash`, `at` and genesis are set by the log.
- `anchor()` appends `{log, seq, hash, at}` to `<log>.anchors.jsonl`; `close()` anchors the final line and releases the lock.
- `line_hash(fields) = sha256(canonical_json(fields without "hash"))`.
- `verify(log, anchors=None) -> VerifyReport(ok, lines, anchors, first_broken_seq, reason, unanchored_tail)`: every line parses, is canonical, has seq equal to its position, only line 0 is genesis, links to the previous hash and re-hashes to its own; an incomplete last line fails; every anchor for this log must point inside the log at a line with its hash. Lines after the last anchor are reported (`unanchored_tail`), because their truncation is undetectable — the stated limit.
- CLI `assay verify-audit LOG [--anchors FILE] [--json]` (exit 0 ok, 1 broken) and a `make verify-audit` target; gate 11's smoke runs it on `tests/fixtures/audit_ok.jsonl`.

## Rules
- Stated limit (threat model): someone able to rewrite both the log and the anchor file can forge history; production adds WORM storage or external timestamping.
- The log never stores answer text: answers are evidenced by `answer_sha256` (N1.1).

## Out of scope
Deciding what to log for each agent call (S3.3), the coverage cross-check between trajectories and lines (A4.2).
