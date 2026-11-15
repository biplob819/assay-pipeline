# assay-pipeline

Public EGFR IC50 bioactivity data from ChEMBL and PubChem, normalised into one versioned, quality-checked table with provenance on every row. An AI agent reads that table through a SQL guard and read-only access, every query goes into a tamper-evident audit log, and the agent is attacked with cases mapped to the OWASP Top 10 for LLM Applications (2025).

**Status:** specification. Nothing is built yet. The build runs node by node; see Tech Spec §11.

- [PRD](Trusted%20Assay%20Pipeline%20PRD.md): what it does and why
- [Tech Spec](Trusted%20Assay%20Pipeline%20Tech%20Spec.md): how it's built, including the build node graph and the loophole review of the PRD

Scoring reuses [EvalKit](https://github.com/biplob819/evalkit). ChEMBL data is CC BY-SA 3.0; snapshots published from this repo keep that licence and attribution.
