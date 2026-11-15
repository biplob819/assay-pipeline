#!/usr/bin/env python3
"""Install the local git pre-commit hook: secret scan and leak guard on every commit.

python scripts/install_hooks.py
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / ".git" / "hooks"
BODIES = {
    "pre-commit": (
        "#!/bin/sh\n# installed by scripts/install_hooks.py\nset -e\n"
        "python3 scripts/secret_scan.py --staged\npython3 scripts/leak_guard.py --staged\n"
    ),
    "commit-msg": (
        "#!/bin/sh\n# installed by scripts/install_hooks.py\nset -e\n"
        'python3 scripts/leak_guard.py --message "$1"\n'
    ),
}


def install(hooks: Path = HOOKS) -> list[Path]:
    hooks.mkdir(parents=True, exist_ok=True)
    out = []
    for name, body in BODIES.items():
        hook = hooks / name
        hook.write_text(body, encoding="utf-8")
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        out.append(hook)
    return out


if __name__ == "__main__":
    for h in install():
        print(f"installed {h}")
    sys.exit(0)
