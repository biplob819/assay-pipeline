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
    ing.add_argument("source", choices=["chembl", "pubchem"])
    ing.add_argument("--config", type=Path, help="build config YAML (default: EGFR IC50)")
    ing.add_argument("--cache", type=Path, default=Path("cache"), help="raw cache folder")
    ing.add_argument("--new-release", action="store_true", help="start a cache for a new release")
    ing.add_argument(
        "--depositor-terms",
        type=Path,
        help="PubChem depositor ledger YAML (default: the shipped depositor_terms.yaml)",
    )
    ing.add_argument("--json", action="store_true", help="machine-readable output")
    snap = sub.add_parser("snapshot", help="normalise, flag and write a snapshot from the cache")
    snap.add_argument("--config", type=Path, help="build config YAML (default: EGFR IC50)")
    snap.add_argument("--cache", type=Path, default=Path("cache"), help="raw cache folder")
    snap.add_argument("--out", type=Path, default=Path("snapshots"), help="snapshot root folder")
    snap.add_argument("--chembl-release", help="ChEMBL release to read (default: the complete one)")
    snap.add_argument("--pubchem-release", help="PubChem release to read (default: the latest)")
    snap.add_argument("--depositor-terms", type=Path, help="PubChem depositor ledger YAML")
    snap.add_argument("--json", action="store_true", help="machine-readable output")
    pub = sub.add_parser("publish", help="copy a verified, unplanted snapshot to the publish root")
    pub.add_argument("snapshot", type=Path, help="a snapshot folder")
    pub.add_argument("--dest", type=Path, default=Path("published"), help="publish root folder")
    pub.add_argument("--json", action="store_true", help="machine-readable output")
    bdb = sub.add_parser("build-db", help="build agent.duckdb from a verified snapshot")
    bdb.add_argument("snapshot", type=Path, help="a snapshot folder")
    bdb.add_argument("--out", type=Path, default=Path("agent.duckdb"), help="database file")
    bdb.add_argument("--seed", type=int, default=0, help="seed for the synthetic embargoed table")
    bdb.add_argument("--json", action="store_true", help="machine-readable output")
    askp = sub.add_parser("ask", help="ask one agent one question about the snapshot")
    askp.add_argument("question")
    askp.add_argument("--db", type=Path, default=Path("agent.duckdb"), help="agent database")
    askp.add_argument("--model", choices=["claude", "sol"], default="claude", help="which agent")
    askp.add_argument("--audit", type=Path, default=Path("audit/ask.jsonl"), help="audit log")
    askp.add_argument("--replay", type=Path, help="replay recorded responses (no API calls)")
    askp.add_argument("--record", type=Path, help="save the live responses to this file")
    askp.add_argument("--json", action="store_true", help="machine-readable output")
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
    from assay_pipeline.pipeline.ingest.pubchem import (
        PubchemIngestReport,
        ingest_pubchem,
        load_depositor_terms,
    )

    try:
        cfg = load_build_config(args.config)
        cache = RawCache(args.cache)
        if args.source == "pubchem":
            terms = load_depositor_terms(args.depositor_terms)
            fetcher = default_fetcher(cfg.pubchem.max_requests_per_second)
            report = ingest_pubchem(cfg, cache, fetcher, new_release=args.new_release, terms=terms)
        else:
            fetcher = default_fetcher(cfg.chembl.max_requests_per_second)
            report = ingest_chembl(cfg, cache, fetcher, new_release=args.new_release)
    except (ConfigError, IngestError, FetchError, CacheError, ValueError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"assay ingest: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"ok": True, **asdict(report)}))
    elif isinstance(report, PubchemIngestReport):
        drops = ", ".join(f"{k} {v}" for k, v in report.drops_by_reason.items()) or "none"
        print(
            f"pubchem {report.release}: {report.rows} rows from {report.aids_kept} of "
            f"{report.aids_listed} AIDs ({report.requests_made} requests); dropped: {drops}"
        )
    else:
        print(
            f"{report.source} {report.release}: {report.records} records in {report.pages} pages "
            f"({report.requests_made} requests, {report.cached_hits} pages from cache)"
        )
    return 0


def _fail(args: argparse.Namespace, command: str, exc: BaseException) -> int:
    if args.json:
        print(json.dumps({"ok": False, "error": str(exc)}))
    else:
        print(_printable(f"assay {command}: {exc}"), file=sys.stderr)
    return 2


def _snapshot(args: argparse.Namespace) -> int:
    from assay_pipeline.config import ConfigError, load_build_config
    from assay_pipeline.pipeline.ingest.cache import CacheError, RawCache
    from assay_pipeline.pipeline.ingest.chembl import IngestError
    from assay_pipeline.pipeline.ingest.pubchem import load_depositor_terms
    from assay_pipeline.pipeline.snapshot import SnapshotError, build_snapshot

    try:
        result = build_snapshot(
            load_build_config(args.config),
            RawCache(args.cache),
            args.out,
            chembl_release=args.chembl_release,
            pubchem_release=args.pubchem_release,
            terms=load_depositor_terms(args.depositor_terms),
        )
    except (ConfigError, CacheError, IngestError, SnapshotError, ValueError, OSError) as exc:
        return _fail(args, "snapshot", exc)
    m = result.manifest
    if args.json:
        print(json.dumps({"ok": True, "folder": str(result.directory), "skipped": result.skipped,
                          "manifest": m.model_dump(mode="json")}))  # fmt: skip
    else:
        state = "up to date (skipped)" if result.skipped else "built"
        rows = ", ".join(f"{k} {v}" for k, v in m.rows_by_source.items())
        print(f"snapshot {m.snapshot} {state} at {result.directory}: {rows}")
    return 0


def _build_db(args: argparse.Namespace) -> int:
    from assay_pipeline.pipeline.agentdb import AgentDBError, build_agent_db
    from assay_pipeline.pipeline.snapshot import SnapshotError

    try:
        db = build_agent_db(args.snapshot, args.out, seed=args.seed)
    except (AgentDBError, SnapshotError, OSError, ValueError) as exc:
        return _fail(args, "build-db", exc)
    if args.json:
        print(json.dumps({"ok": True, "path": str(db.path), "sha256": db.sha256,
                          "snapshot": db.snapshot}))  # fmt: skip
    else:
        print(f"agent database for snapshot {db.snapshot} at {db.path} (sha256 {db.sha256[:12]})")
    return 0


def _ask(args: argparse.Namespace) -> int:
    from assay_pipeline.agent.ask import AskError, ask

    try:
        replay = None
        if args.replay is not None:
            replay = json.loads(args.replay.read_text(encoding="utf-8"))
            if not isinstance(replay, list):
                raise AskError(f"{args.replay}: a recording is a JSON list of responses")
        result = ask(
            args.question, args.db, args.model, args.audit, replay=replay,
            record=args.record is not None,
        )  # fmt: skip
        if args.record is not None and result.recording is not None:
            args.record.write_text(json.dumps(result.recording, indent=1), encoding="utf-8")
    except (AskError, OSError, ValueError, RuntimeError) as exc:
        return _fail(args, "ask", exc)
    t = result.trial.trajectory
    if args.json:
        print(json.dumps({"ok": True, "answer": result.trial.answer, "model": result.model,
                          "snapshot": result.snapshot, "stop_reason": t.stop_reason,
                          "steps": len(t.steps), "cost_usd": t.cost_usd,
                          "audit": str(result.audit_path)}))  # fmt: skip
    else:
        answer = result.trial.answer
        lines = answer.split("\n")  # keep the answer's lines; escape control characters in each
        print("\n".join(_printable(line) for line in lines) if answer else "(no answer)")
        print(f"\n[{result.model}, {len(t.steps)} queries, {t.stop_reason}, ${t.cost_usd:.4f}; "
              f"audit {result.audit_path}]")  # fmt: skip
    return 0


def _publish(args: argparse.Namespace) -> int:
    from assay_pipeline.pipeline.snapshot import SnapshotError, publish

    try:
        dest = publish(args.snapshot, args.dest)
    except (SnapshotError, OSError, ValueError) as exc:
        return _fail(args, "publish", exc)
    print(json.dumps({"ok": True, "published": str(dest)}) if args.json else f"published {dest}")
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
    if args.command == "snapshot":
        return _snapshot(args)
    if args.command == "publish":
        return _publish(args)
    if args.command == "build-db":
        return _build_db(args)
    if args.command == "ask":
        return _ask(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
