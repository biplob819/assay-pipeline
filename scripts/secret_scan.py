#!/usr/bin/env python3
"""Fail if anything that looks like an API key is in the repo.

Scans tracked and untracked (not ignored) files. Lines containing the marker
`secret-scan: allow` are skipped (for documented fake examples only).

Known limit: a key split across string literals ("sk-ant-" "abc...") is not detected.
Provider-side secret scanning on GitHub is the backstop for that case.

Usage:
    python scripts/secret_scan.py [paths...]
    python scripts/secret_scan.py --self-test
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from harness_lib import ROOT

PATTERNS: dict[str, re.Pattern[str]] = {
    "anthropic key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "openai key": re.compile(r"\bsk-(?!ant-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{32,}"),
    "aws access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "aws secret key": re.compile(
        r"(?i)aws_?secret_?(?:access_?)?key\W{0,4}[=:]\W{0,4}[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])"
    ),
    "github token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "github fine-grained token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}"),
    "pypi token": re.compile(r"\bpypi-[A-Za-z0-9_\-]{50,}"),
    "private key": re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----"
    ),
}
ALLOW = "secret-scan: allow"
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".whl", ".gz", ".zip", ".ico"}


def candidate_files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root,
            capture_output=True,
            check=True,
        )  # -z: paths with non-ASCII characters arrive unquoted
        return [root / p for p in out.stdout.decode("utf-8").split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        skip = {".git", ".venv", "node_modules", "__pycache__", "dist"}
        return [p for p in root.rglob("*") if p.is_file() and not (skip & set(p.parts))]


def scan(files: list[Path]) -> list[str]:
    hits: list[str] = []
    for f in files:
        if f.suffix.lower() in SKIP_SUFFIXES or not f.is_file():
            continue
        try:
            # never skip a file for its encoding: undecodable bytes become U+FFFD
            text = f.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if ALLOW in line:
                continue
            for name, pat in PATTERNS.items():
                if pat.search(line):
                    hits.append(f"{f}:{lineno}: possible {name}")
    return hits


def staged_files(root: Path, into: Path) -> list[Path]:
    """Copy each staged blob (what the next commit adds, not the working tree) under `into`."""
    names = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    out = []
    for rel in (n for n in names.split("\0") if n):
        blob = subprocess.run(
            ["git", "show", f":{rel}"], cwd=root, capture_output=True, check=True
        ).stdout
        dest = into / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        out.append(dest)
    return out


def self_test() -> int:
    fake_ant = "sk-ant-" + "a1B2" * 10
    fake_oai = "sk-proj-" + "Z9y8" * 10
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "leak.py"
        bad.write_text(f'KEY = "{fake_ant}"\nOTHER = "{fake_oai}"\n', encoding="utf-8")
        allowed = Path(tmp) / "doc.md"
        allowed.write_text(f"example: {fake_ant}  # {ALLOW}\n", encoding="utf-8")
        clean = Path(tmp) / "clean.py"
        clean.write_text('KEY = os.environ["ANTHROPIC_API_KEY"]\n', encoding="utf-8")
        checks = [
            ("detects leaked keys", len(scan([bad])) == 2),
            ("honours allow marker", scan([allowed]) == []),
            ("clean file passes", scan([clean]) == []),
        ]
    for name, ok in checks:
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'WRONG'}] {name}")
    print("self-test", "PASSED" if failures == 0 else f"FAILED ({failures})")
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("paths", nargs="*", type=Path)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--staged", action="store_true", help="scan what the next commit adds")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.staged:
        with tempfile.TemporaryDirectory() as tmp:
            files = staged_files(ROOT, Path(tmp))
            hits = scan(files)
    else:
        files = args.paths or candidate_files(ROOT)
        hits = scan(files)
    if hits:
        print("secret scan: FAIL")
        for h in hits:
            print(f"  - {h}")
        return 1
    print(f"secret scan: OK ({len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
