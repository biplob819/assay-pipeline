# Security

## Reporting a vulnerability

Please report a way past the SQL guard, the read-only connection, the audit chain or the console's live-mode guard privately, through GitHub's "Report a vulnerability" (private security advisory) on this repository, not in a public issue. Include the statement or steps and what you expected the controls to do. You will get an acknowledgement within a week; fixes land as a new guard-corpus or oracle case so the bypass stays tested.

## Keys

- Provider keys are read from your environment only (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`). The CLI never reads `.env` files or config files for keys, never prints them (`assay doctor` shows only "set" or "not set"), and never writes them to logs or recordings.
- The query process that touches the database runs isolated, with an allowlisted environment and no keys.
- Recorded responses used in tests are scrubbed of the system prompt and canaries.

## Scope and limits

The threat model (`docs/threat-model.md`, one page) lists the assets, trust boundaries, threats mapped to OWASP and MITRE ATLAS, the controls and their evidence, and the stated limits. The restricted table `embargoed_results` is synthetic.

## Privacy

No telemetry. The tools send data only to ChEMBL and PubChem (to build snapshots, and when `assay doctor` checks they can be reached) and to the model providers whose keys you set (to run agents and judges). A hosted console's live mode also keeps a daily spend counter in the Postgres database its operator configures. Streamlit, which serves the console, has its usage statistics turned off, but it can look up the server's public IP when it checks a request's origin. The public console keeps visitor text in memory only; its persistent log holds aggregates (time, model, guard counts, latency, cost).
