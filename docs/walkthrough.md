# Walkthrough

[`assets/walkthrough.gif`](assets/walkthrough.gif) is a looping, 22.5-second tour of the committed outputs of run `main-2026-11-18`. It has 9 screens, each held 2.5 s, captured at 1280×800 with headless Chrome and scaled to 960 px. The published run is now `main-2026-11-18-s3` (see the README results block); the tour still shows the earlier run's pages, so its numbers are that run's. No API key was set and nothing was spent. The console ran locally in replay mode, and no frame shows a secret, a canary or a held-out case.

| # | Screen | What it shows | Source |
| --- | --- | --- | --- |
| 1 | Security dashboard, top | 0 of 248 attack trials got through; the 95% upper bound per agent (below 2.5%) and per category; $3.49 spent | [`docs/report/index.html`](report/index.html) |
| 2 | What stopped it? | The layer that stopped each attack: main run, guard-off ablation and the model-free scripted attacker | same page, `#h-stop` |
| 3 | How did the agents behave? | Rules B1 to B10 per agent; B3, B4 and B10 judged by two blinded judges | same page, `#h-beh` |
| 4 | Compared with the last run | EvalKit's `compare` against `main-2026-11-16`: changed metrics, 95% intervals, Holm p, no flagged regression | same page, `#h-cmp` |
| 5 | Access events | The audit log verifies (997 lines, 394 anchors); every query by guard decision; recent blocked queries | same page, `#h-audit` |
| 6 | Compound explorer | The published snapshot's 41,463 activity rows, filterable, each linked to its source record | [`docs/report/explorer.html`](report/explorer.html) |
| 7 | Explorer, filtered | pIC50 9 or more with flagged rows hidden: 3,892 rows | same page |
| 8 | Console, replay | A scripted agent with no model queries `embargoed_results`; the guard blocks it (`table_not_allowed`) and the audit lines appear | [`apps/console`](../apps/console/README.md), `ASSAY_CONSOLE_DB=agent.duckdb`, no keys |
| 9 | Release gate | `evalkit gate` on the published `metrics.json` with `gates.yaml`: 27 rows, all pass, exit 0 | `uv run evalkit gate docs/results/main-2026-11-18/metrics.json --config gates.yaml` |

To rebuild it, capture the screens and then run
`ffmpeg -framerate 1/2.5 -i seq%02d.png -vf "fps=10,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=5" -loop 0 walkthrough.gif`.
