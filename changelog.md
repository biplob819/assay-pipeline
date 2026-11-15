# Changelog

Format: `[TYPE] [NODE] — summary`. Types: FEAT, FIX, DOCS, HARNESS, RELEASE.

## 2026-11-15
- [FEAT] [N1.1] — schemas and config: 11 frozen pydantic schemas with cross-field rules (Tech Spec §3, D-24), format v1 fixtures with byte-identical round trips, build and views configs shipped inside the package (any target, EGFR IC50 default)
- [HARNESS] [W0.1] — repo and harness: uv project (Python 3.11–3.13), CI on Linux, macOS and Windows with SHA-pinned actions, EvalKit harness ported with 13 gates (rebuild, boundary and security added; judge dropped), graph and catalog generated from the Tech Spec (21 nodes, 128 capabilities), decisions D-1..D-26; seven verifier passes hardened the boundary gate (static lint plus runtime import check, D-25), the records gate (git-time freshness) and the leak guard (staged blobs, paths and messages)
- [DOCS] [—] — PRD and Tech Spec published; job-search framing moved out of the repo (OWNER D-18)
