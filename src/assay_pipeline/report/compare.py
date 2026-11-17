"""Compare a run with the one published before it (OWNER D-41, DSH-10), through EvalKit's
public `evalkit.compare` (its B5.6, v0.3.0). Assay keeps only the rows that changed, rounded,
so the bundle stays deterministic; the full comparison is EvalKit's to compute.

Both sides are `metrics.json` files (metrics-1, no case ids), so the comparison is unpaired
(each run's own 95% interval, combined by MOVER) and held-out rows stay split-level.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evalkit.compare import compare_runs


def _r(x: Any) -> Any:
    return round(x, 6) if isinstance(x, float) else x


def _previous_run(old: Path) -> str | None:
    try:
        run = json.loads(old.read_text(encoding="utf-8")).get("run_id")
    except (OSError, ValueError, AttributeError):
        return None
    return run if isinstance(run, str) else None


def _clean(errors: list[str], *paths: Path) -> list[str]:
    """EvalKit's reasons, at most five, with no host paths (the bundle is published)."""
    out = []
    for e in errors[:5]:
        for p in paths:
            e = e.replace(str(p), p.name)
        out.append(e)
    return out


def compare_metrics(old: Path, new: Path, gates: Path | None = None) -> dict[str, Any]:
    """Never raises for a bad previous file: the reason is kept and shown instead (DSH-10)."""
    use = gates if gates is not None and gates.is_file() else None
    c = compare_runs(old, new, gates=use)
    gates_note = "applied" if use else "not applied (no gates file)"
    if c.exit_code == 2 and use is not None:  # a baseline from before a gated metric existed
        plain = compare_runs(old, new)
        if plain.exit_code != 2:
            gates_note = "not applied: " + "; ".join(_clean(c.errors, old, new, use)[:2])
            c = plain
    changed = [
        {"metric": x.metric, "labels": dict(x.labels), "unit": x.unit, "old": _r(x.old),
         "new": _r(x.new), "diff": _r(x.diff), "ci_low": _r(x.ci_low), "ci_high": _r(x.ci_high),
         "pairing": x.pairing, "flagged": bool(x.flagged), "gate": x.gate,
         "regression": bool(x.regression)}
        for x in c.changes if x.diff not in (0, None) or x.flagged or x.regression
    ]  # fmt: skip
    return {"previous_run": _previous_run(old) or old.parent.name, "verdict": c.verdict,
            "exit_code": c.exit_code, "settings": c.settings_status, "rows": len(c.changes),
            "changed": changed, "missing": len(c.missing), "gates": gates_note,
            "errors": _clean(list(c.errors), old, new, *([use] if use else []))}  # fmt: skip
