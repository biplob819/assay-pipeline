# S3.2 acceptance criteria

**AUD-01** A new log starts with a genesis line (seq 0, zero prev_hash, nothing else), and every line's hash is the SHA-256 of its canonical JSON without the hash field, including its prev_hash.
**AUD-02** One writer per log: a second writer is refused while the lock is held, appends from many threads keep one unbroken chain, every line is flushed and fsynced, and a broken log is never resumed or appended to.
**AUD-03** Question, query and answer lines carry their event's fields under the N1.1 rules, blocked queries are logged like allowed ones, and callers cannot set seq, prev_hash, hash, the time or a genesis line.
**AUD-04** Anchors (log, seq, hash, time) go to a separate file; closing the log anchors its final line, and a reopened log continues the chain after verifying it.
**AUD-05** `verify` fails on an edited, deleted, inserted or reordered line naming that line's seq, on a truncated tail or a rewritten log with a valid chain through the anchors, and on an incomplete last line; without anchors it reports how many lines are unanchored.
**AUD-06** `assay verify-audit` exits 0 for a good log and 1 for a broken one, printing the first broken seq (also as JSON), a `make verify-audit` target runs it, and the build smoke verifies the shipped fixture log.
**AUD-07** A property test shows that changing any single byte of a log makes verification fail.
