# B5.2 acceptance criteria

**THR-01** `docs/threat-model.md` names the assets (snapshot integrity, restricted data, system instructions, audit trail, compute and spend, provider keys), the trust boundaries and the data flow from sources to the agent's answer.
**THR-02** Every OWASP 2025 category has a row; every attack-suite category and LLM03, LLM07 and LLM09 carry a threat, controls and evidence, and every ATLAS technique the suite uses appears in the row of each category that uses it; every control named exists in the code today or is marked planned.
**THR-03** The stated limits (DuckDB has no GRANTs, anchor trust, stealth poison, canary matching, the boundary gate, the resource cap, the small held-out split) each come with a production step or are marked accepted or out of scope, and a review checklist closes the page.
**THR-04** `docs/threat-model.pdf` is a single page rendered from the current Markdown (its embedded hash matches), the render is reproducible, and a test checks that every suite category, ATLAS technique and control appears.
