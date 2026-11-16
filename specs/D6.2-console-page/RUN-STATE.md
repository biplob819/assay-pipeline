# D6.2 run state

**Now:** building

## History
- 2026-11-16: started (base 1ae1fca). The page logic is in `console/page.py`, with a thin Streamlit script in `apps/console/`. Usage statistics are off. 9 tests: settings, the cap bound, live wiring, the budget line, decision-to-audit pairing (including a scripted blocked query), and three AppTest runs covering replay, a missing database and live mode with a wrong passcode. The console is cached keyed on its settings, which fixed a stale console found by the AppTest runs. Streamlit 1.67 was added to the `console` extra and the dev group.
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
- 2026-11-17: owner asked for live mode on visitors' own keys, still behind the passcode (OWNER D-37, DEM-05). Built: guard `admit_own_key` and optional spend store; engine `api_key` (key passed to the SDK client constructor, shape check, SDK errors not chained, rejected key named plainly, aggregate `key` field); page "Pay with" choice, provider-labelled password field, own-key budget-line text. 13 new tests in `test_own_key.py`, page tests rewritten for the new rules (live = passcode; hosted = passcode + store + key). DEM-05 building until the verifier pass.
- 2026-11-17: verifier pass on DEM-05 (two rounds, no real API calls; scratch probes captured outgoing requests through a fake transport). Round 1: must-fix pyright (Optional spend in older tests, httpx2 types); should-fix own-key clients still took the host's SDK environment (OpenAI org/project/base URL, Anthropic base URL and custom headers); notes: SDK error reachable via `__context__`, host-key request for an unkeyed agent held the full bound, wording. Round 2: must-fix `OPENAI_CUSTOM_HEADERS` (a host `Authorization` there replaced the visitor's key, billing the host with nothing held); should-fix omitting an auth-named custom header stripped the visitor's credential; note proxy variables applied. All fixed: `_client` pins the public endpoint, omits both providers' custom headers, clears org/project, sets `trust_env=False`; `own_key_unavailable` refuses own keys before the guard when the host's custom headers carry a credential; `ProviderError` raised outside the except block; `Console(host_agents=...)`. The spend table is now `assay_spend` so the console can share EvalKit's demo Postgres (OWNER D-38). 60 console tests; pyright clean.
- 2026-11-17: the eligibility test's catalog count is now 129 (DEM-05). Converged twice after the verifier fixes; DEM-05 green. DEM-04 stays building until the console is deployed (owner: EvalKit's demo Postgres for the spend counter, OWNER D-38).
- 2026-11-17: owner chose a separate Community Cloud app from this public repo, built at startup (OWNER D-39), hosted keys plus own keys, and to deploy it themselves from written steps. Built: `ASSAY_CONSOLE_SNAPSHOT` with a repo-relative base, startup build with a random seed (about 3 s), the published snapshot committed in `docs/data/2026-11-15.1/`, `tests/console/test_startup_build.py` (6 tests, including the page starting from the snapshot alone in a foreign working directory), and click-by-click deploy steps. DEM-04 stays building until the app is live.
- 2026-11-17: verifier pass on the startup build (no real API calls): 5 should-fix, all fixed. A database supplied by path got a seed-0 prompt canary (now `AgentDB.seed` from the build record feeds the console); a 31-bit seed could be brute-forced from one leaked prompt canary (now 128 bits); failed starts leaked a temp folder and rebuilt on every rerun (now cleaned up and cached); docs said the seed stays in memory (it is in the server-side build record; wording fixed); Cloud would install untested versions (requirements now pinned from uv.lock, with a drift test) and the ~700 MB build peak, SSL and table-creation permission are now in the deploy steps. Notes kept: the app does not pin the manifest hash (changing it needs repo write access, which is code access too).
