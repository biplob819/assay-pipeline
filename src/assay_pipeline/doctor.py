"""`assay doctor`: is this machine ready to build and evaluate? (OSS-02, D-26)

Checks, each ok / warn / fail with a one-line detail:
- Python is a supported version (3.11 to 3.13);
- provider keys are set (only "set" or "not set" is ever shown, never a value);
- ChEMBL and PubChem answer (skipped with --offline);
- the raw cache, snapshots and agent database that the next command would use.
Nothing is sent anywhere but the two source APIs, and only when the network check runs.
"""

from __future__ import annotations

import os
import platform
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

SUPPORTED = ((3, 11), (3, 12), (3, 13))
KEYS = (("ANTHROPIC_API_KEY", "Claude agent and judge"), ("OPENAI_API_KEY", "GPT agent and judge"))
SOURCES = (
    ("ChEMBL", "https://www.ebi.ac.uk/chembl/api/data/status.json"),
    (
        "PubChem",
        "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/2244/property/InChIKey/JSON",
    ),
)


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # ok | warn | fail
    detail: str


def _reach(url: str, timeout: float = 8.0) -> str | None:
    """None when the URL answers 2xx, else a short reason."""
    import urllib.request

    from assay_pipeline import __version__

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"assay-pipeline/{__version__} (+https://github.com/biplob819/assay-pipeline)"
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return None if 200 <= resp.status < 300 else f"HTTP {resp.status}"
    except Exception as exc:  # any failure is a reason to report, never a crash
        return type(exc).__name__


def run_checks(
    *,
    cache: Path,
    snapshots: Path,
    db: Path,
    offline: bool,
    env: dict[str, str] | None = None,
    reach: Callable[[str], str | None] = _reach,
    version: tuple[int, int] | None = None,
) -> list[Check]:
    env = dict(os.environ) if env is None else env
    v = version or sys.version_info[:2]
    from assay_pipeline import __version__

    checks = [
        Check("assay", "ok", f"{__version__} on {platform.system()} {platform.machine()}"),
        Check(
            "python",
            "ok" if v in SUPPORTED else "fail",
            f"{v[0]}.{v[1]} ({'supported' if v in SUPPORTED else 'use 3.11 to 3.13'})",
        )
    ]
    for key, use in KEYS:
        is_set = bool(env.get(key, "").strip())
        checks.append(
            Check(
                key,
                "ok" if is_set else "warn",
                f"{'set' if is_set else 'not set'} ({use}; building snapshots needs no key)",
            )
        )
    for name, url in SOURCES:
        if offline:
            checks.append(Check(f"network {name}", "warn", "skipped (--offline)"))
            continue
        why = reach(url)
        checks.append(
            Check(
                f"network {name}",
                "ok" if why is None else "fail",
                "reachable" if why is None else f"not reachable: {why}",
            )
        )
    if cache.is_dir():
        sources = sorted(p.name for p in cache.iterdir() if p.is_dir())
        checks.append(
            Check("cache", "ok" if sources else "warn", f"{cache}: {', '.join(sources) or 'empty'}")
        )
    else:
        checks.append(Check("cache", "warn", f"{cache}: none yet (run `assay ingest chembl`)"))
    snaps = (
        sorted(p.parent.name for p in snapshots.glob("*/manifest.json"))
        if snapshots.is_dir()
        else []
    )
    checks.append(
        Check(
            "snapshots",
            "ok" if snaps else "warn",
            f"{snapshots}: {', '.join(snaps[-3:]) if snaps else 'none yet (run `assay snapshot`)'}",
        )
    )
    has_db = db.is_file() and db.with_suffix(".json").is_file()
    checks.append(
        Check(
            "agent database",
            "ok" if has_db else "warn",
            f"{db}: {'present' if has_db else 'none yet (run `assay build-db`)'}",
        )
    )
    return checks


def as_json(checks: list[Check]) -> dict[str, object]:
    return {
        "ok": not any(c.status == "fail" for c in checks),
        "checks": [asdict(c) for c in checks],
    }
