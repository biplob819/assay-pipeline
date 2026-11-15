"""The `assay` command line. Subcommands are added by the nodes that build them."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from assay_pipeline import __version__


def default_fetcher(max_requests_per_second: float):  # replaced in tests
    from assay_pipeline.pipeline.ingest.http import Fetcher

    return Fetcher(max_requests_per_second=max_requests_per_second)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="assay",
        description="Build trusted assay snapshots and attack the agent that reads them.",
    )
    ap.add_argument("--version", action="version", version=f"assay {__version__}")
    sub = ap.add_subparsers(dest="command", metavar="command")
    ing = sub.add_parser("ingest", help="fetch source data into the raw cache")
    ing.add_argument("source", choices=["chembl"])
    ing.add_argument("--config", type=Path, help="build config YAML (default: EGFR IC50)")
    ing.add_argument("--cache", type=Path, default=Path("cache"), help="raw cache folder")
    ing.add_argument("--new-release", action="store_true", help="start a cache for a new release")
    ing.add_argument("--json", action="store_true", help="machine-readable output")
    ver = sub.add_parser("verify-audit", help="re-hash an audit log and check its anchors")
    ver.add_argument("log", type=Path)
    ver.add_argument("--anchors", type=Path, help="anchors file (default: <log>.anchors.jsonl)")
    ver.add_argument(
        "--no-anchors",
        action="store_true",
        help="check the chain only (a truncated tail is then undetectable)",
    )
    ver.add_argument("--json", action="store_true", help="machine-readable output")
    return ap


def _printable(text: str) -> str:
    """Escape control characters so a crafted file name cannot drive the terminal."""
    return "".join(ch if ch.isprintable() else ch.encode("unicode_escape").decode() for ch in text)


def _verify_fail(args: argparse.Namespace, msg: str) -> int:
    if args.json:
        print(json.dumps({"ok": False, "error": msg}))
    else:
        print(_printable(msg), file=sys.stderr)
    return 1


def _verify_audit(args: argparse.Namespace) -> int:
    from assay_pipeline.audit import verify

    try:
        if args.no_anchors:
            anchors = None
        elif args.anchors is not None:
            anchors = args.anchors
        elif args.log.name:
            anchors = args.log.with_suffix(".anchors.jsonl")
        else:
            return _verify_fail(args, f"{args.log} is not a log file path")
        if anchors is not None and not anchors.exists():
            return _verify_fail(
                args,
                f"anchors file {anchors} not found (pass --no-anchors to check the chain only)",
            )
        report = verify(args.log, anchors)
    except (OSError, ValueError) as exc:
        return _verify_fail(args, f"cannot verify {args.log}: {exc}")
    if args.json:
        print(json.dumps(asdict(report)))
    elif report.ok:
        tail = f", {report.unanchored_tail} after the last anchor" if report.unanchored_tail else ""
        print(f"audit log OK: {report.lines} lines, {report.anchors} anchors{tail}")
    else:
        print(
            _printable(f"audit log BROKEN at seq {report.first_broken_seq}: {report.reason}"),
            file=sys.stderr,
        )
    return 0 if report.ok else 1


def _ingest(args: argparse.Namespace) -> int:
    from assay_pipeline.config import ConfigError, load_build_config
    from assay_pipeline.pipeline.ingest.cache import CacheError, RawCache
    from assay_pipeline.pipeline.ingest.chembl import IngestError, ingest_chembl
    from assay_pipeline.pipeline.ingest.http import FetchError

    try:
        cfg = load_build_config(args.config)
        fetcher = default_fetcher(cfg.chembl.max_requests_per_second)
        report = ingest_chembl(cfg, RawCache(args.cache), fetcher, new_release=args.new_release)
    except (ConfigError, IngestError, FetchError, CacheError, ValueError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"assay ingest: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"ok": True, **asdict(report)}))
    else:
        print(
            f"{report.source} {report.release}: {report.records} records in {report.pages} pages "
            f"({report.requests_made} requests, {report.cached_hits} pages from cache)"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.command is None:
        ap.print_help()
        return 0
    if args.command == "ingest":
        return _ingest(args)
    if args.command == "verify-audit":
        return _verify_audit(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
