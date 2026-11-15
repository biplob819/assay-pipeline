# Contributing

Thanks for helping. The project is built node by node (Tech Spec §11): each change belongs to a node, comes with tests, and is checked by `scripts/node_converge.py`, which runs lint, types on three platforms, tests, golden replays, the rebuild check, the trust-boundary lint, the security suite and a wheel build.

## Setup

```bash
uv sync                      # Python 3.11-3.13
uv run python scripts/install_hooks.py   # secret scan and leak guard on commit
uv run pytest -q
```

## Making a change

1. Open an issue first for anything larger than a fix, so the change can be placed in the node graph.
2. Keep the trust boundary: the pipeline never imports a model SDK, and the agent opens the database only through `agent/connection.py`, read-only (`scripts/node_converge.py` gate 9).
3. Add a test that fails without your change. Security changes need a case in `tests/security/` (the guard corpus, the oracle or the permission tests).
4. No test may call a live model: use `Replay` with recorded responses. Any real API run is logged in `docs/measurements/spend-ledger.jsonl`.
5. Record decisions in `docs/decisions.md` and anything learned the hard way in `docs/lessons.md`.

## Attack cases

New public attack cases go in `src/assay_pipeline/data/attacks.yaml` with an OWASP category, an ATLAS technique from `data/atlas.yaml`, the SQL the attacker wants run and the layers expected to stop it; the oracle must stop them. Please do not open issues or PRs that reproduce the held-out cases: the held-out split exists so results cannot be tuned to it.
