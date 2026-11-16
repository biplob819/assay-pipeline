# D6.2 — Console page

**Wave 11. Depends on:** D6.1, B5.1.

## Goal
Give a visitor (US9) a page where they can replay a recorded attack, or try their own question in live mode, and see the guard's decision next to the audit line it wrote.

## Outputs
- **`console/page.py`:** the page's logic, kept apart from Streamlit so it can be tested.
  - **`Settings.from_env`:** reads the agent database path, the passcode (trimmed), the Postgres URL, the daily cap, an optional work folder, and which provider keys are set. All come from the host's secret store as environment variables, never from files in the repository. The daily cap must cover at least one request's hold, and the secrets are kept out of the settings' repr.
  - **`build_console`:**
    - Builds one console per app process.
    - Live mode is wired only when the passcode, the spend store and at least one provider key are all set and the Postgres client is installed. Otherwise the page says what is missing.
    - The guard's per-request cap is the largest live request bound across the agents ($0.555).
    - A missing or damaged database becomes a clean message with no host paths.
    - Postgres connections time out after 3 s.
  - **Helpers:** `BudgetLine` (what one request is held at, and what is left today, "used up" below one hold; it reads the shared store at most every 15 s), `decision_rows` (each query beside its audit line's sequence number and hash), and `audit_view` (the case's audit lines without question or answer text).
- **`apps/console/streamlit_app.py`:** a thin Streamlit script.
  - **Tabs:** "Replay a recorded attack" is the default. It has a sample picker, the sample question and a Replay button, and makes no API call. "Live" shows the budget line, then an agent choice, a question of up to 500 characters and the passcode.
  - **Results:** model output is shown as plain text, never as markdown or HTML. Guard decisions and new audit lines are shown side by side. Any unexpected error shows a generic message, never a traceback. The budget line is drawn after a request, so it is never stale.
  - **Config:** `.streamlit/config.toml`, both beside the app and at the repository root. Usage statistics are off. The startup banner is hidden, which also skips its public-IP lookup. Error details are `none`. A test checks that Streamlit loads these values.
- **Samples:** the six recorded model runs, plus two scripted guard demos labelled "Scripted, no model". In those, an agent does send the restricted-table query and the stacked `DROP`, so replay shows the guard blocking (the recorded models refused before querying). The recorder keeps the scripted demos.
- **Packaging:** `streamlit>=1.46` is in the `console` extra (1.46 is the first version that reads a config beside the script). `apps/console/requirements.txt` serves Community Cloud.
- **Deploy notes:** `apps/console/README.md` gives the settings table and the steps for Streamlit Community Cloud or any host that runs `streamlit run`.

## Own key (DEM-05, OWNER D-37, 17 Nov 2026)
- **Engine:** `run_case(api_key=...)` checks the key's shape (`check_key`: printable ASCII, no spaces, at most 512 characters, never repeated in the message). The guard's `admit_own_key` applies the passcode, the per-request cap and the session rate limit, and holds nothing. `_LiveCall(agent, api_key)` passes the key to the SDK client constructor, never the environment, and the own-key client is pinned to the provider's public endpoint with the host's custom headers (`ANTHROPIC_CUSTOM_HEADERS`, `OPENAI_CUSTOM_HEADERS`), OpenAI organization and project removed and the host's proxy variables ignored (`trust_env=False`). If the host's custom headers carry a credential (`Authorization`, `X-Api-Key`, `Api-Key`), own keys are refused before the guard (`own_key_unavailable`). The provider error is raised outside the SDK's `except` block, so the SDK error (which carries the request headers; OpenAI's 401 text quotes part of the key) is neither chained nor its context; only the class name is kept, so a rejected key becomes "The provider refused that API key."
- **Guard:** the spend store is optional. A console with a passcode but no hosted keys has a guard with no store: own-key requests are admitted and a host-key request is refused with `NO_HOSTED`.
- **Page:** live mode is on whenever the passcode is set. "Pay with" offers "This console's key" (only when hosted keys are wired and the chosen agent has one) and "My own API key", which shows a password field labelled with the provider. The budget line adds what an own-key request is capped at on the visitor's account.
- **Aggregate log:** a `key` field ("own" or "host"); never the key.
- **Host agents:** a host-key request for an agent with no host key is refused before the guard, so it never holds budget (verifier note).

## Owner actions (DEM-04)
- Choose the host. The Tech Spec names a `portfolio-demos` repository, which does not exist yet. Streamlit Community Cloud can deploy `apps/console/streamlit_app.py` straight from this repository.
- In the host's secret store, set:
  - `ASSAY_CONSOLE_DB` (and upload or build the agent database);
  - `ASSAY_CONSOLE_PASSCODE`;
  - `ASSAY_CONSOLE_DATABASE_URL` (Postgres);
  - optionally `ASSAY_CONSOLE_DAILY_CAP`;
  - `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` (optional: without them and the Postgres URL, visitors use their own keys).

## Out of scope
Accounts and saved sessions. (Live mode with the visitor's own key was added on 17 Nov 2026 by OWNER D-37, as DEM-05.)
