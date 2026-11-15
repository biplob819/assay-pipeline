# B5.2 run state

**Now:** green

## History
- 2026-11-16 — started (base e0fd9ee). Threat model written from the Tech Spec §6–§7, the attack suite, ATLAS 2026.10 and the measured controls. Renderer built on fpdf2 2.9.0; first render showed fpdf2's markdown turning `--` into underline and justified table cells, so the renderer now refuses `--`, drops code backticks and left-aligns. One page at 7.5 pt. 7 tests.
- 2026-11-16 — converge 1: a proof note held a semicolon (fixed); the A4.2 run-record test (case content) rides in this commit.
- 2026-11-16 — attacker verifier pass: 2 must-fix, 5 should-fix, 3 notes, all handled:
  - **Must-fix, production step:** "Canary matching" and three other limits had no production step. Each limit now has its own bullet with a step, or is marked accepted or out of scope, and a test enforces it.
  - **Must-fix, trusted publishing:** "PyPI trusted publishing" was named as a control but does not exist yet. It is now marked planned (B5.3) and dropped from the required controls.
  - **Should-fix, wording:**
    - the corpus count is now 194 (138 blocked, 56 allowed);
    - LLM06 now lists AML.T0053, and a test checks every (OWASP, ATLAS) pair against its row;
    - "max-cost cap on every paid run" is now worded as an estimate plus an option;
    - the missing address-space cap now names macOS too;
    - the LLM07 echo scrub is described as hygiene for the leak scan.
  - **Notes:** the leak guard was removed from LLM03, and canary scoring moved to evidence. The public console is marked out of scope until D6. The renderer refuses `__` and links and reads `###`. The page is now 6.6 pt.
- 2026-11-16 — green: converged twice after the verifier pass.
