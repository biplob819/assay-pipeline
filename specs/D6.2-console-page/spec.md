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

## Owner actions (DEM-04)
- Choose the host. The Tech Spec names a `portfolio-demos` repository, which does not exist yet. Streamlit Community Cloud can deploy `apps/console/streamlit_app.py` straight from this repository.
- In the host's secret store, set:
  - `ASSAY_CONSOLE_DB` (and upload or build the agent database);
  - `ASSAY_CONSOLE_PASSCODE`;
  - `ASSAY_CONSOLE_DATABASE_URL` (Postgres);
  - optionally `ASSAY_CONSOLE_DAILY_CAP`;
  - `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`.

## Out of scope
Accounts, saved sessions, and live mode with the visitor's own keys.
