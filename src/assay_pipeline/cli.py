"""The `assay` command line. Subcommands are added by the nodes that build them."""

from __future__ import annotations

import argparse
import sys

from assay_pipeline import __version__


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="assay",
        description="Build trusted assay snapshots and attack the agent that reads them.",
    )
    ap.add_argument("--version", action="version", version=f"assay {__version__}")
    ap.add_subparsers(dest="command", metavar="command")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.command is None:
        ap.print_help()
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
