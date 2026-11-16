# D6.2 run state

**Now:** building

## History
- 2026-11-16: started (base 3d5d745). The page logic is in `console/page.py`, with a thin Streamlit script in `apps/console/`. Usage statistics are off. 9 tests: settings, the cap bound, live wiring, the budget line, decision-to-audit pairing (including a scripted blocked query), and three AppTest runs covering replay, a missing database and live mode with a wrong passcode. The console is cached keyed on its settings, which fixed a stale console found by the AppTest runs. Streamlit 1.67 was added to the `console` extra and the dev group.
- 2026-11-16: verifier pass. One must-fix: with no provider key, live mode crashed after the passcode check, showed a traceback and kept the $0.555 hold, so about five clicks used up the day.

  Fixes:
  - Live needs a key, and only agents with keys are offered.
  - Unexpected errors show a generic message.
  - Error details are set to `none`.
  - A damaged database or a missing Postgres client gives a clean message, or live switches off.
  - The budget read is cached for 15 s and Postgres connects time out after 3 s.
  - Streamlit is pinned to 1.46 or later, and a root config exists for Community Cloud. A test checks that Streamlit loads the config.
  - The startup banner, and with it the public-IP lookup, is off. The remaining origin-check lookup is disclosed in README and SECURITY.
  - Tests now kill the surviving mutants: plain-text replies with hostile markup, the passcode, agent and question reaching `run_case`, the picked sample's reply, a passcode-only build, and the bound with distinct prices.
  - Two scripted guard demos are added, so replay shows a block.
  - Deploy notes: requirements, supplying the database, and the 50-per-hour lockout wording.
  - Money is rounded up, "used up" shows below one hold, the cap must cover a hold, the passcode is trimmed, secrets are kept out of the repr, audit cells have no NaN, `width="stretch"` is used, and the work folder and cache entries are bounded.

  13 page tests.
- 2026-11-16: converged twice (13/13). DEM-01..03 green. DEM-04 stays building until the owner chooses a host and sets the secrets.
