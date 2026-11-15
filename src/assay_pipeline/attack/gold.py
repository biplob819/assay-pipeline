"""Gold result sets for the normal questions, tied to one snapshot (Tech Spec §7.1, ATK-04).

A gold file records the snapshot id and the sha256 of its `manifest.json`; every result is
the guard-rendered gold SQL run on that snapshot's agent database. A changed snapshot without
recomputed gold fails the check, so a score is never computed against stale answers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import duckdb

from assay_pipeline.config import ViewsConfig
from assay_pipeline.guard import SqlGuard
from assay_pipeline.schemas import NormalQuestion


class GoldError(RuntimeError):
    """The gold file does not match the snapshot it claims, or a gold query is not allowed."""


def manifest_sha256(snapshot_dir: Path | str) -> str:
    return hashlib.sha256((Path(snapshot_dir) / "manifest.json").read_bytes()).hexdigest()


def _cell(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, int | str | bool) or v is None:
        return v
    return str(v)


def compute_gold(
    questions: Sequence[NormalQuestion], db_path: Path | str, views: ViewsConfig
) -> dict[str, dict[str, Any]]:
    guard = SqlGuard(views)
    con = duckdb.connect(str(db_path), read_only=True)
    out: dict[str, dict[str, Any]] = {}
    try:
        for q in questions:
            if q.gold_sql is None:
                continue
            d = guard.check(q.gold_sql)
            if d.decision != "allowed" or d.rendered_sql is None:
                raise GoldError(f"{q.id}: gold SQL is not allowed by the guard ({d.reason})")
            cur = con.execute(d.rendered_sql)
            columns = [c[0] for c in cur.description or []]
            rows = [[_cell(v) for v in r] for r in cur.fetchall()]
            out[q.id] = {"columns": columns, "rows": rows, "ordered": "ORDER BY" in q.gold_sql}
    finally:
        con.close()
    return out


def write_gold(
    path: Path | str,
    questions: Sequence[NormalQuestion],
    snapshot_dir: Path | str,
    db_path: Path | str,
    views: ViewsConfig,
) -> dict[str, Any]:
    gold = {
        "snapshot": Path(snapshot_dir).name,
        "manifest_sha256": manifest_sha256(snapshot_dir),
        "results": compute_gold(questions, db_path, views),
    }
    Path(path).write_text(json.dumps(gold, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return gold


def check_gold(gold: dict[str, Any], snapshot_dir: Path | str) -> None:
    if gold.get("manifest_sha256") != manifest_sha256(snapshot_dir):
        raise GoldError(
            f"gold was computed for another snapshot ({gold.get('snapshot')}); recompute it"
        )
