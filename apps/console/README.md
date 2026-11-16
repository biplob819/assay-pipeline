# Assay console

A Streamlit page over the console backend (`assay_pipeline.console`). A visitor can replay a recorded attack through the real guard (no API call), or ask their own question in live mode behind a passcode, and see each guard decision next to the audit line it wrote.

## Run locally

```bash
uv sync --extra console
ASSAY_CONSOLE_DB=agent.duckdb uv run streamlit run apps/console/streamlit_app.py
```

Replay works with only `ASSAY_CONSOLE_DB` set. Live mode needs the passcode. With only the passcode set, visitors bring their own Anthropic or OpenAI key. With the spend store and a provider key set too, the console also offers its own key, for the agents whose key is set, against a daily budget. The page says which settings are missing.

## Your own API key

In live mode a visitor can choose **My own API key** and paste a key for the chosen agent's provider. The passcode is still required, so the console never becomes an open relay for anyone's key. Requests on your own key:

- are billed to your provider account and use none of the console's daily budget;
- still pass the per-request cap ($0.555, shown on the page rounded up as $0.56: the most one request can cost) and the session rate limit;
- send the key only to that request's provider client, which talks to the provider's public endpoint: the server's own SDK settings (base URL, custom headers, OpenAI organization and project, proxy variables) are not applied to your key. If the server's custom headers (`ANTHROPIC_CUSTOM_HEADERS`, `OPENAI_CUSTOM_HEADERS`) carry a credential, own keys are turned off rather than risk sending the server's credential with your request. It is never put in the server's environment (other visitors share the process), never written to disk or logs, and never shown. It stays in your browser tab's session memory on the server until you close the tab.

If the provider rejects the key, the page says so without repeating it; that request still counts toward the session rate limit, as any request that reached a provider does. The aggregate log records only whether a request used the console's key or the visitor's.

## Settings

All settings are environment variables, which come from the host's secret store when deployed. Nothing is read from files in this repository.

| Variable | Meaning |
| --- | --- |
| `ASSAY_CONSOLE_DB` | the agent database built by `assay build-db` (its `.json` must sit beside it) |
| `ASSAY_CONSOLE_PASSCODE` | live mode's passcode; live mode is off when unset |
| `ASSAY_CONSOLE_DATABASE_URL` | Postgres for the shared daily spend counter (table `assay_spend`; EvalKit's demo database works, OWNER D-38); the console's hosted keys are off when unset |
| `ASSAY_CONSOLE_DAILY_CAP` | daily live budget in USD (default 3.00, at least one request's hold) |
| `ASSAY_CONSOLE_WORKDIR` | where the database copy and the aggregate log go (default: a new temp folder) |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | the console's own provider keys (optional: visitors can bring theirs) |

Each live request is held at its upper bound ($0.555, shown rounded up as $0.56) and settled to its actual cost. The guard limits each browser session to 5 live requests and 5 wrong passcodes an hour. A reload starts a new session, so the daily cap is what really bounds spend. It also allows 50 wrong passcodes an hour across all sessions; past that, live mode refuses everyone, right passcode included, until the hour passes.

## Deploy (Streamlit Community Cloud)

1. New app → this repository, branch `main`, main file `apps/console/streamlit_app.py`, Python 3.12. `apps/console/requirements.txt` installs this package with the `console` extra.
2. Under Secrets, set the variables above. For an own-key-only console, `ASSAY_CONSOLE_DB` and `ASSAY_CONSOLE_PASSCODE` are enough: no Postgres and no provider keys.
3. Provide the agent database. It is not in the repository (it is built from a snapshot and holds the synthetic restricted table's canaries), and Community Cloud has no shell to run `assay build-db`. Use a host where you can place the file, such as a VM or a container host with a volume. On Community Cloud you would need a private copy of the repository that carries `agent.duckdb` and its `.json`.

Usage statistics are off, the startup banner is hidden (which skips its public-IP lookup) and visitors never see tracebacks (`.streamlit/config.toml`, both beside the app and at the repository root). Streamlit can still look up the server's public IP (checkip.amazonaws.com) when it checks a request's origin. The page keeps visitor text in memory only; the aggregate log keeps time, mode, model, whose key (console or visitor), query counts, stop reason, latency and cost. The table the attacks aim at, `embargoed_results`, is synthetic.
