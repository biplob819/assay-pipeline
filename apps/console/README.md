# Assay console

A Streamlit page over the console backend (`assay_pipeline.console`). A visitor can replay a recorded attack through the real guard (no API call), or ask their own question in live mode behind a passcode, and see each guard decision next to the audit line it wrote.

## Run locally

```bash
uv sync --extra console
ASSAY_CONSOLE_DB=agent.duckdb uv run streamlit run apps/console/streamlit_app.py
```

Replay works with only `ASSAY_CONSOLE_DB` set. Live mode is off until the passcode, the spend store and at least one provider key are set, and the page says which is missing. Live mode offers only the agents whose key is set.

## Settings

All settings are environment variables, which come from the host's secret store when deployed. Nothing is read from files in this repository.

| Variable | Meaning |
| --- | --- |
| `ASSAY_CONSOLE_DB` | the agent database built by `assay build-db` (its `.json` must sit beside it) |
| `ASSAY_CONSOLE_PASSCODE` | live mode's passcode; live mode is off when unset |
| `ASSAY_CONSOLE_DATABASE_URL` | Postgres for the shared daily spend counter; live mode is off when unset |
| `ASSAY_CONSOLE_DAILY_CAP` | daily live budget in USD (default 3.00, at least one request's hold) |
| `ASSAY_CONSOLE_WORKDIR` | where the database copy and the aggregate log go (default: a new temp folder) |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | provider keys; live mode offers only agents whose key is set |

Each live request is held at its upper bound ($0.555, shown rounded up as $0.56) and settled to its actual cost. The guard limits each browser session to 5 live requests and 5 wrong passcodes an hour. A reload starts a new session, so the daily cap is what really bounds spend. It also allows 50 wrong passcodes an hour across all sessions; past that, live mode refuses everyone, right passcode included, until the hour passes.

## Deploy (Streamlit Community Cloud)

1. New app → this repository, branch `main`, main file `apps/console/streamlit_app.py`, Python 3.12. `apps/console/requirements.txt` installs this package with the `console` extra.
2. Under Secrets, set the variables above.
3. Provide the agent database. It is not in the repository (it is built from a snapshot and holds the synthetic restricted table's canaries), and Community Cloud has no shell to run `assay build-db`. Use a host where you can place the file, such as a VM or a container host with a volume. On Community Cloud you would need a private copy of the repository that carries `agent.duckdb` and its `.json`.

Usage statistics are off, the startup banner is hidden (which skips its public-IP lookup) and visitors never see tracebacks (`.streamlit/config.toml`, both beside the app and at the repository root). Streamlit can still look up the server's public IP (checkip.amazonaws.com) when it checks a request's origin. The page keeps visitor text in memory only; the aggregate log keeps time, mode, model, query counts, stop reason, latency and cost. The table the attacks aim at, `embargoed_results`, is synthetic.
