# D6.1 run state

**Now:** green

## History
- 2026-11-16 — started (base f694072).
  - **Built:** engine, in-memory audit and guard. The guard is ported from EvalKit and its daily spend counter keeps EvalKit's atomic reserve.
  - **Live bound:** the first estimate assumed one tool result per turn. It is now bounded by the whole possible history on every turn, since one turn can carry several calls and dense results run at about a token per two characters. That gives $0.54 a request.
  - **Samples:** six recorded live for $0.052. Most show the model refusing without a query; one shows a stacked statement reduced to an allowed SELECT.
  - **Tests:** 15.
- 2026-11-16 — attacker verifier pass 1: 3 must-fix, 2 should-fix, 2 notes, all handled with repros ported:
  - **Must-fix, cost bound:** the bound assumed a token per two characters, but a guard-allowed literal of private-use characters makes a 5,830-character result of 23,143 bytes. Results are now escaped to ASCII and cut to 3,000 characters, and the bound assumes tokens ≤ UTF-8 bytes. It is now $0.555 a request.
  - **Must-fix, concurrency:** concurrent cases took each other's audit lines. Cases now filter by their own trial id, taken under a lock.
  - **Must-fix, full log:** a full in-memory log stopped the console, and a live request reserved before failing. The log now rotates before it fills.
  - **Should-fix, passcode guessing:** guesses were unlimited. Each session now gets 5 wrong passcodes per window, and the aggregate log is size-capped.
  - **Should-fix, unseen usage:** live clients now make no SDK retries, and a transport stop keeps the whole hold.
  - **Notes:** a replay mismatch is now a ConsoleError, and it is documented that holds can briefly block live use under load.
- 2026-11-16 — attacker verifier pass 2: no must-fix. Every pass-1 fix held against variants; the worst modelled request cost was $0.394 against the $0.555 bound. Should-fixes handled:
  - **Global guess limit:** a limit of 50 wrong passcodes per window across all sessions now sits on top of the per-session one, since a new tab is a new session.
  - **Unbilled status errors:** a provider HTTP status error (429, 529) is refused unbilled, so it now settles at the turns already completed. Only connection errors and timeouts keep the whole hold.
  - **Notes:** the replay error no longer swallows audit errors, and aggregate-log rotation is locked.
  - **For D6.2:** the page must set `per_request_cap` to at least $0.555. Holds limit concurrent live requests to about five.
- 2026-11-16 — green: converged twice after verifier pass 2 (no must-fix).
