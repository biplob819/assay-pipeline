# D6.1 acceptance criteria

**CON-01** `run_case()` runs the real agent, guard and locked connection on the console's copy and returns the reply, every guard decision with its reason, and exactly the audit lines the case added.
**CON-02** The packaged sample attacks replay through the real guard and database with no API call, and a recording that no longer matches the code is refused.
**CON-03** A live request is refused before any model call on a wrong passcode, a spent session rate limit, a per-request bound over the cap or a used-up daily budget; the daily counter is reserved atomically (no overspend under concurrency), settled to the actual cost, kept whole on failure, and a store outage is refused readably.
**CON-04** The console works on its own hash-checked copy of the agent database and an in-memory, hash-chained audit log; the original database is untouched and nothing of the log reaches disk.
**CON-05** The persistent log keeps aggregates only (timestamp, mode, model, query counts, stop reason, latency, cost, refusal) and never question, answer or SQL text.
