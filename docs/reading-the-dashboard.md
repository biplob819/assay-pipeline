# Reading the dashboard

The dashboard (`docs/report/index.html`, published at the project page) is rendered from one results bundle. Every number on it comes from that bundle.

- **Headline tiles.** Attack trials that got through, and each agent's exact 95% upper bound on the success rate per trial. Zero successes is a bound, not 0%: with n trials and none succeeding, the true rate is below 1 - 0.05^(1/n).
- **Did anything get through?** One row per attack category, agent and split. Held-out rows have fewer trials, so their bounds are wider; a held-out result outside the public interval is flagged.
- **What stopped it?** The layer that stopped each trial: the main run, the same public attacks with the SQL guard switched off (permissions and limits stay on), and the model-free oracle, which runs each attack's SQL straight at the controls. The oracle shows what the controls stop if the model complies; the ablation shows how much the model stops on its own.
- **How did the agents behave?** Behaviour rules B1-B10 per agent. B3, B4 and B10 are judged by two blinded judges from different model families; a rule is kept or broken only when both agree. Normal-question accuracy is shown two ways: whether a query returned exactly the gold rows, and whether the answer states the gold value.
- **Access events.** The audit log's verify status and anchors, every query by guard decision (split by attack cases and normal questions: blocks in normal questions are the guard's cost to honest use), and recent queries from public cases.
- **Data quality.** Flags per rule, rows by source, drops by reason and the snapshot history. The compound explorer links every value to its source record.
