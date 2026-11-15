# B5.2 — Threat model

**Wave 9. Depends on:** A4.2. **Unblocks:** B5.1 (the control list decides what the dashboard must evidence).

## Goal
A reviewer can judge the project's security thinking from one page (US8): what is protected, where the trust boundaries are, which threats apply, which control answers each, what evidence shows it works, and what the design does not cover.

## Outputs
- **`docs/threat-model.md`:**
  - assets;
  - trust boundaries and data flow;
  - a table mapping all ten OWASP Top 10 for LLM Applications 2025 categories to MITRE ATLAS 2026.10 techniques, each with its threat, controls and evidence. LLM03, LLM07 and LLM09 are covered by controls rather than suite categories; LLM08 is marked not applicable, since there is no retrieval store;
  - stated limits with their production steps (DuckDB has no GRANTs, anchor trust, stealth poison, canary matching);
  - a review checklist.
- **`docs/threat-model.pdf`:** a one-page landscape render made by `scripts/render_onepager.py`.
  - The renderer handles headings, paragraphs, bullets, bold and pipe tables, and shrinks the font until the page fits.
  - It refuses a page that cannot fit, text outside Latin-1 (the core font), and `--` (fpdf2's markdown underline).
  - It embeds the source's sha256, so a stale PDF fails a test. Its output is byte-reproducible.
- **Dependency:** `fpdf2` (dev group) for rendering. R6.1 reuses the renderer.

## Out of scope
The dashboard's evidence views (B5.1) and the data roadmap page (R6.1).
