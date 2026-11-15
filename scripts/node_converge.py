#!/usr/bin/env python3
"""Run every gate for a build node and report all of them, even after a failure.

Usage:
    python scripts/node_converge.py W0.1          # run all 13 gates
    python scripts/node_converge.py --self-test   # prove every gate can fail

Exit 0 only when no gate FAILs. SKIP means the gate had nothing to check yet
(for example no golden tests exist before node N1.2); SKIP never hides a failure.

Gates:
   1 claims-work     at least one capability row of the node is building or green
   2 eligibility     graph has no cycles and nothing builds ahead of its dependencies
   3 proofs          every row has a proof for all five dimensions, and each proof resolves
   4 ruff            uv run ruff check .
   5 pyright         uv run pyright
   6 pytest          unit, property and contract tests
   7 golden          pytest -m golden (recorded responses, no network)
   8 rebuild         pytest -m rebuild (two clean snapshot builds, identical manifests)
   9 boundary        trust boundary: pipeline never imports a model SDK or the agent;
                     the agent never imports the pipeline or opens DuckDB writable
  10 security        pytest -m security, plus the secret scan
  11 build           uv build, install the wheel in a clean venv, run the smoke commands
  12 changed-tests   if source changed since node start, tests changed too
  13 records         changelog entry, a criterion line per row, record files present,
                     spec and criteria changed since the base, RUN-STATE changed at or
                     after the newest source/test/config change (git times)
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import secret_scan
from harness_lib import (
    PROOF_DIMENSIONS,
    ROOT,
    SPECS_DIR,
    Cap,
    HarnessError,
    Node,
    criteria_missing,
    eligibility_problems,
    load_catalog,
    load_graph,
    parse_catalog,
    parse_graph,
    spec_dir,
)

# Commands run inside the clean venv by gate 11. Node S3.2 adds `verify-audit` on a fixture.
SMOKE_COMMANDS: list[list[str]] = [
    ["assay", "--help"],
    [  # N1.1: the default configs ship inside the wheel (SCH-04)
        "python",
        "-c",
        "from assay_pipeline.config import load_build_config, load_views_config; "
        "assert load_build_config().target.name == 'EGFR'; load_views_config()",
    ],
    [  # S3.2: the installed CLI verifies the shipped fixture log (AUD-06)
        "assay",
        "verify-audit",
        str(ROOT / "tests" / "fixtures" / "audit_ok.jsonl"),
    ],
]
PYTEST_UNIT_MARKERS = "not golden and not rebuild and not security"
NA_MIN_REASON = 10
NA_FORBIDDEN = {"logical"}  # every capability needs at least a real unit test


@dataclass
class Result:
    status: str  # PASS | FAIL | SKIP
    detail: str


@dataclass
class Ctx:
    node: str
    root: Path
    nodes: dict[str, Node]
    caps: list[Cap]
    specs: Path


def _run(cmd: list[str], cwd: Path, timeout: int = 900) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return 127, str(exc)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    return p.returncode, (p.stdout + p.stderr).strip()


def _tail(text: str, n: int = 12) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:])


# ---------- gates ----------


def gate_claims_work(ctx: Ctx) -> Result:
    rows = [c for c in ctx.caps if c.node == ctx.node]
    active = [c for c in rows if c.status in ("building", "green")]
    if not rows:
        return Result("FAIL", f"{ctx.node} has no catalog rows")
    if not active:
        return Result(
            "FAIL", f"no row of {ctx.node} is building or green (run node_start --mark-building)"
        )
    return Result("PASS", f"{len(active)}/{len(rows)} rows active")


def gate_eligibility(ctx: Ctx) -> Result:
    problems = eligibility_problems(ctx.nodes, ctx.caps)
    return Result("FAIL", "; ".join(problems)) if problems else Result("PASS", "eligible")


PROOF_RE = re.compile(r'^(?P<path>[^›"]+)›"(?P<test>[^"]+)"$')


def _test_functions(path: Path) -> dict[str, bool]:
    """Map each real test function name to whether it is skipped.

    Only `def test_*` at module level or inside a `Test*` class count, so names that
    appear in comments, strings or as prefixes of other names do not.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict[str, bool] = {}

    def record(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if fn.name.startswith("test"):
            found[fn.name] = any("skip" in ast.unparse(d) for d in fn.decorator_list)

    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            record(node)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    record(item)
    return found


def proof_problems(cap: Cap, root: Path) -> list[str]:
    if not cap.proof:
        return [f"{cap.cap_id}: no proof"]
    entries: dict[str, str] = {}
    for part in cap.proof.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            return [f"{cap.cap_id}: malformed proof entry {part!r}"]
        key, value = (s.strip() for s in part.split("=", 1))
        if key in entries:
            return [f"{cap.cap_id}: duplicate proof key {key!r}"]
        entries[key] = value
    problems: list[str] = []
    tests_root = (root / "tests").resolve()
    for dim in PROOF_DIMENSIONS:
        value = entries.get(dim)
        if value is None:
            problems.append(f"{cap.cap_id}: missing {dim} proof")
            continue
        if value.startswith("n/a:"):
            if dim in NA_FORBIDDEN:
                problems.append(f"{cap.cap_id}: {dim} proof cannot be n/a")
            elif len(value[4:].strip()) < NA_MIN_REASON:
                problems.append(f"{cap.cap_id}: {dim} n/a needs a reason of {NA_MIN_REASON}+ chars")
            continue
        m = PROOF_RE.match(value)
        if not m:
            problems.append(f'{cap.cap_id}: {dim} proof must be path›"test" or n/a:<reason>')
            continue
        rel, test = m["path"].strip(), m["test"].strip()
        path = (root / rel).resolve()
        if not path.is_relative_to(tests_root) or path.suffix != ".py":
            problems.append(f"{cap.cap_id}: {dim} proof file {rel} must be a .py file under tests/")
            continue
        if not path.is_file():
            problems.append(f"{cap.cap_id}: {dim} proof file {rel} not found")
            continue
        try:
            tests = _test_functions(path)
        except SyntaxError as exc:
            problems.append(f"{cap.cap_id}: {dim} proof file {rel} does not parse: {exc.msg}")
            continue
        if test not in tests:
            problems.append(f"{cap.cap_id}: {dim} test {test!r} is not a test function in {rel}")
        elif tests[test]:
            problems.append(f"{cap.cap_id}: {dim} test {test!r} is skipped")
    extra = set(entries) - set(PROOF_DIMENSIONS)
    if extra:
        problems.append(f"{cap.cap_id}: unknown proof keys {sorted(extra)}")
    return problems


def gate_proofs(ctx: Ctx) -> Result:
    rows = [c for c in ctx.caps if c.node == ctx.node]
    problems = [p for c in rows for p in proof_problems(c, ctx.root)]
    if problems:
        shown = "; ".join(problems[:6]) + (
            f"; … {len(problems) - 6} more" if len(problems) > 6 else ""
        )
        return Result("FAIL", shown)
    return Result("PASS", f"{len(rows)} rows × {len(PROOF_DIMENSIONS)} dimensions resolve")


def _tool_gate(cmd: list[str], root: Path, label: str) -> Result:
    code, out = _run(cmd, root)
    if code == 0:
        return Result("PASS", label)
    return Result("FAIL", _tail(out))


def gate_ruff(ctx: Ctx) -> Result:
    return _tool_gate([sys.executable, "-m", "ruff", "check", "."], ctx.root, "ruff clean")


PYRIGHT_PLATFORMS = ("Linux", "Darwin", "Windows")  # CI runs all three (L-23)


def gate_pyright(ctx: Ctx) -> Result:
    for platform in PYRIGHT_PLATFORMS:
        cmd = [sys.executable, "-m", "pyright", "--pythonplatform", platform]
        result = _tool_gate(cmd, ctx.root, "pyright clean")
        if result.status != "PASS":
            return Result(result.status, f"as {platform}: {result.detail}")
    return Result("PASS", f"pyright clean on {', '.join(PYRIGHT_PLATFORMS)}")


def _pytest(root: Path, marker: str, label: str) -> Result:
    code, out = _run([sys.executable, "-m", "pytest", "-q", "-m", marker], root)
    if code == 0:
        last = out.splitlines()[-1] if out else ""
        return Result("PASS", f"{label}: {last}")
    if code == 5:
        return Result("SKIP", f"no {label} tests collected yet")
    return Result("FAIL", _tail(out))


def gate_pytest(ctx: Ctx) -> Result:
    return _pytest(ctx.root, PYTEST_UNIT_MARKERS, "unit")


def gate_golden(ctx: Ctx) -> Result:
    return _pytest(ctx.root, "golden", "golden")


def gate_rebuild(ctx: Ctx) -> Result:
    return _pytest(ctx.root, "rebuild", "rebuild")


# Trust boundary (Tech Spec §6.2, D-25). A lint against accidental coupling in our own code: in
# a zone, dynamic import machinery is banned outright, so every import is a static statement
# the gate can resolve. Runtime tests (Q2.3 VWS-03, the §7.3 oracle) prove the permissions
# themselves; this gate keeps the code from reaching around them.
PACKAGE = "assay_pipeline"
BOUNDARY_RULES: dict[str, tuple[str, ...]] = {
    "pipeline": (f"{PACKAGE}.agent", f"{PACKAGE}.attack"),
    "agent": (f"{PACKAGE}.pipeline",),
}
MODEL_SDK_PREFIXES = ("anthropic", "openai")  # any top-level module starting with these
SDK_FREE_ZONES = ("pipeline",)
DYNAMIC_NAMES = frozenset(
    {
        "exec",
        "eval",
        "compile",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "vars",
        "globals",
        "locals",
        "import_module",
        "__builtins__",
    }
)
DYNAMIC_MODULES = (
    "importlib", "runpy", "builtins", "imp", "pkgutil", "ctypes", "sys.modules", "pydoc",
    "zipimport", "pickle", "marshal", "code", "codeop", "operator.attrgetter",
    "operator.methodcaller",
)  # fmt: skip
ALLOWED_MODULES = ("importlib.metadata", "importlib.resources")  # static lookups only
# Top-level names of the banned modules: importing one from anywhere (a re-export such as
# `from inspect import builtins`) is still importing the banned module.
BANNED_MODULE_NAMES = frozenset(m.split(".")[0] for m in DYNAMIC_MODULES if "." not in m)
DYNAMIC_ATTRS = frozenset(
    {"__dict__", "__builtins__", "__import__", "import_module", "modules", "sys",
     "__getattribute__", "__globals__", "__subclasses__", "__code__", "attrgetter",
     "methodcaller", "f_builtins", "f_globals", "_getframe"}
)  # fmt: skip
IMPORTLIB_OK_ATTRS = frozenset({"metadata", "resources"})
DUCKDB_FILE = "agent/connection.py"  # the one agent module allowed to open DuckDB


def _module_of(path: Path, src: Path) -> str:
    """The package used to resolve relative imports in a file under src/."""
    parts = list(path.relative_to(src).with_suffix("").parts)
    return ".".join(parts[:-1])  # x.py and __init__.py both resolve against their folder


def _resolve(module: str | None, level: int, package: str) -> str:
    if level == 0:
        return module or ""
    base = package.split(".") if package else []
    if level - 1 > len(base):
        return "<relative import beyond top-level package>"
    base = base[: len(base) - (level - 1)]
    return ".".join([*base, *([module] if module else [])])


def _literal_true(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _connect_problem(call: ast.Call) -> str | None:
    kw = {k.arg: k.value for k in call.keywords}
    if None in kw:
        return "passes **kwargs to duckdb.connect"
    if not (len(call.args) <= 1 and "read_only" in kw and _literal_true(kw["read_only"])):
        return "opens DuckDB without a literal read_only=True keyword"
    cfg = kw.get("config")
    if cfg is not None:
        if not isinstance(cfg, ast.Dict) or not all(
            isinstance(k, ast.Constant) and isinstance(k.value, str) for k in cfg.keys
        ):
            return "passes a non-literal config to duckdb.connect"
        if not all(isinstance(v, ast.Constant) for v in cfg.values):
            return "passes a non-literal config value to duckdb.connect"
        if any(k.value == "access_mode" for k in cfg.keys if isinstance(k, ast.Constant)):
            return "sets access_mode in duckdb.connect config"
    return None


def zone_problems(tree: ast.AST, zone: str, package: str, rel_in_pkg: str) -> list[str]:
    problems: list[str] = []
    banned = BOUNDARY_RULES[zone]
    duckdb_allowed = zone == "agent" and rel_in_pkg == DUCKDB_FILE
    connect_calls: set[int] = set()

    def imported(name: str, line: int) -> None:
        top = name.split(".")[0]
        if name.startswith("<"):
            problems.append(f"line {line}: {name}")
        elif any(name == b or name.startswith(b + ".") for b in banned):
            problems.append(f"line {line}: imports {name}")
        elif zone in SDK_FREE_ZONES and top.startswith(MODEL_SDK_PREFIXES):
            problems.append(f"line {line}: imports {name} (model SDK)")
        elif any(name == m or name.startswith(m + ".") for m in ALLOWED_MODULES):
            pass
        elif any(name == m or name.startswith(m + ".") for m in DYNAMIC_MODULES):
            problems.append(f"line {line}: imports {name} (dynamic import machinery)")
        elif name == PACKAGE:
            problems.append(f"line {line}: imports the bare {PACKAGE} package")

    def attr_name(name: str, line: int) -> None:
        if name in DYNAMIC_ATTRS:
            problems.append(f"line {line}: uses .{name} (banned in zones)")
        if name == "duckdb" and zone == "agent":
            problems.append(f"line {line}: reaches duckdb outside {DUCKDB_FILE}")

    for node in ast.walk(tree):
        if isinstance(node, ast.MatchClass):
            for kw in node.kwd_attrs:  # `case object(__globals__=g)` reads an attribute
                attr_name(kw, node.lineno)
        if isinstance(node, ast.Import):
            for a in node.names:
                imported(a.name, node.lineno)
                if a.name.startswith(PACKAGE + ".") and a.asname is None:
                    problems.append(
                        f"line {node.lineno}: `import {a.name}` binds the bare {PACKAGE} "
                        "package (use `from ... import` or `as`)"
                    )
                if a.name in ("sys", "os") and a.asname is not None:
                    problems.append(f"line {node.lineno}: aliases {a.name} (banned in zones)")
                if a.asname in ("sys", "os") and a.name != a.asname:
                    problems.append(f"line {node.lineno}: binds {a.asname} to {a.name} (banned)")
                is_duckdb = a.name == "duckdb" or a.name.startswith("duckdb.")
                plain = duckdb_allowed and a.name == "duckdb" and a.asname is None
                if zone == "agent" and is_duckdb and not plain:
                    problems.append(
                        f"line {node.lineno}: imports duckdb outside {DUCKDB_FILE} "
                        "or not as a plain `import duckdb`"
                    )
        elif isinstance(node, ast.ImportFrom):
            base = _resolve(node.module, node.level, package)
            for a in node.names:
                if a.name == "*":
                    problems.append(f"line {node.lineno}: star import from {base}")
                    imported(base, node.lineno)
                else:
                    imported(f"{base}.{a.name}" if base else a.name, node.lineno)
            if zone == "agent" and (base == "duckdb" or base.startswith("duckdb.")):
                problems.append(f"line {node.lineno}: from-import of duckdb (use `import duckdb`)")
            if base == "sys":
                problems.append(f"line {node.lineno}: from-import of sys (banned in zones)")
            for a in node.names:
                bound = a.asname or a.name
                if a.name in DYNAMIC_NAMES or bound in DYNAMIC_NAMES:
                    problems.append(f"line {node.lineno}: imports the banned name {a.name}")
                own = base == PACKAGE or base.startswith(PACKAGE + ".")
                if a.name in BANNED_MODULE_NAMES and base not in ALLOWED_MODULES and not own:
                    problems.append(
                        f"line {node.lineno}: imports the banned module {a.name} from {base} "
                        "(a re-export is still the banned module)"
                    )
                if (bound == "os") != (a.name == "os") or (a.name == "os" and base != "os"):
                    problems.append(f"line {node.lineno}: binds os to {base}.{a.name} (banned)")
                if bound == "sys" or a.name == "sys":
                    problems.append(f"line {node.lineno}: binds sys by from-import (banned)")
            if zone == "agent" and not duckdb_allowed:
                for a in node.names:
                    if a.name == "duckdb" or a.asname == "duckdb":
                        problems.append(f"line {node.lineno}: reaches duckdb outside {DUCKDB_FILE}")
        elif isinstance(node, ast.Name) and node.id in DYNAMIC_NAMES:
            problems.append(f"line {node.lineno}: uses {node.id} (dynamic code is banned in zones)")
        elif isinstance(node, ast.Name) and node.id == "duckdb" and zone == "agent":
            if not duckdb_allowed:
                problems.append(f"line {node.lineno}: reaches duckdb outside {DUCKDB_FILE}")
        elif isinstance(node, ast.Attribute):
            attr_name(node.attr, node.lineno)
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "importlib"
                and node.attr not in IMPORTLIB_OK_ATTRS
            ):
                problems.append(f"line {node.lineno}: uses importlib.{node.attr} (banned)")
        elif isinstance(node, ast.Call) and duckdb_allowed:
            f = node.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr == "connect"
                and isinstance(f.value, ast.Name)
                and f.value.id == "duckdb"
            ):
                connect_calls.add(id(f.value))
                p = _connect_problem(node)
                if p:
                    problems.append(f"line {node.lineno}: {p}")
    if duckdb_allowed:
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "duckdb" and id(node) not in connect_calls:
                if isinstance(node.ctx, ast.Load):
                    problems.append(
                        f"line {node.lineno}: uses duckdb other than in duckdb.connect(...)"
                    )
                else:
                    problems.append(f"line {node.lineno}: rebinds the name duckdb")
    return problems


def _zone_files(src: Path, zone: str) -> list[Path]:
    pkg = src / PACKAGE
    files = sorted((pkg / zone).rglob("*.py")) if (pkg / zone).is_dir() else []
    single = pkg / f"{zone}.py"
    return [*files, single] if single.is_file() else files


def boundary_problems(src: Path) -> tuple[list[str], int]:
    problems: list[str] = []
    checked = 0
    for zone in BOUNDARY_RULES:
        for f in _zone_files(src, zone):
            checked += 1
            rel = f.relative_to(src).as_posix()
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            rel_in_pkg = f.relative_to(src / PACKAGE).as_posix()
            for p in dict.fromkeys(zone_problems(tree, zone, _module_of(f, src), rel_in_pkg)):
                problems.append(f"{rel}: {p}")
    return problems, checked


# Runtime half of the boundary gate: import each zone module in a fresh interpreter with a
# finder that records and blocks every banned import, so a helper module outside the zones
# that pulls in a model SDK or the other zone is caught. Imports made only at call time are
# outside what any import-time check can see (D-25 states this limit).
_RUNTIME_PROBE = r"""
import importlib, json, sys
zone_banned = json.loads(sys.argv[1])
modules = sys.argv[2:]
hits = []
class Block:
    def find_spec(self, name, path=None, target=None):
        top = name.split(".")[0]
        sdk = zone_banned["sdk"] and top.startswith(("anthropic", "openai"))
        pkg = any(name == b or name.startswith(b + ".") for b in zone_banned["prefixes"])
        if sdk or pkg:
            hits.append((current[0], name))
            raise ImportError(f"blocked by the boundary gate: {name}")
        return None
sys.meta_path.insert(0, Block())
current = [None]
errors = []
for m in modules:
    current[0] = m
    try:
        mod = importlib.import_module(m)
    except BaseException as exc:  # SystemExit too: a zone module must import cleanly
        if not any(h[0] == m for h in hits):
            errors.append((m, f"{type(exc).__name__}: {exc}"))
        continue
    # a banned module bound under any name (a re-export such as `from enum import bltns`)
    for name, value in list(vars(mod).items()):
        if type(value).__name__ != "module":
            continue
        real = value.__name__
        if real in zone_banned["allowed"]:
            continue
        if real == "importlib" and name == "importlib":
            continue  # bound by `import importlib.metadata`; the static half bans other uses
        if real.split(".")[0] in zone_banned["modules"] or real in zone_banned["modules"]:
            hits.append((m, f"{real} (bound as {name})"))
print(json.dumps({"hits": hits, "errors": errors}))
"""


def runtime_boundary_problems(root: Path) -> list[str]:
    import json

    src = root / "src"
    problems: list[str] = []
    for zone, banned in BOUNDARY_RULES.items():
        files = _zone_files(src, zone)
        if not files:
            continue
        mods = [
            ".".join(f.relative_to(src).with_suffix("").parts).removesuffix(".__init__")
            for f in files
            if f.name != "__main__.py"  # an entry point runs its program on import
        ]
        if not mods:
            continue
        spec = {
            "sdk": zone in SDK_FREE_ZONES,
            "prefixes": list(banned),
            "modules": sorted(BANNED_MODULE_NAMES | {m for m in DYNAMIC_MODULES if "." in m}),
            "allowed": list(ALLOWED_MODULES),
        }
        env = {**os.environ, "PYTHONPATH": str(src), "PYTHONIOENCODING": "utf-8"}
        p = subprocess.run(
            [sys.executable, "-c", _RUNTIME_PROBE, json.dumps(spec), *mods],
            cwd=root,
            capture_output=True,
            env=env,
            timeout=300,
        )
        out = p.stdout.decode("utf-8", "replace").strip().splitlines()
        if p.returncode != 0 or not out:
            err = p.stderr.decode("utf-8", "replace").strip()[-300:] or f"exit code {p.returncode}"
            problems.append(f"{zone}: runtime import check crashed: {err}")
            continue
        result = json.loads(out[-1])
        problems += [f"{m} imports {name} at import time (runtime)" for m, name in result["hits"]]
        problems += [f"{m} cannot be imported: {e}" for m, e in result["errors"]]
    return problems


def gate_boundary(ctx: Ctx) -> Result:
    try:
        problems, checked = boundary_problems(ctx.root / "src")
    except SyntaxError as exc:
        return Result("FAIL", f"cannot parse {exc.filename}: {exc.msg}")
    if checked == 0:
        return Result("SKIP", "no pipeline or agent modules yet")
    problems += runtime_boundary_problems(ctx.root)
    if problems:
        return Result("FAIL", "; ".join(problems[:6]))
    return Result("PASS", f"{checked} pipeline/agent modules respect the trust boundary")


def gate_security(ctx: Ctx) -> Result:
    hits = secret_scan.scan(secret_scan.candidate_files(ctx.root))
    tests = _pytest(ctx.root, "security", "security")
    if hits:
        return Result("FAIL", "secret scan: " + "; ".join(hits[:5]))
    if tests.status == "FAIL":
        return tests
    return Result("PASS", f"secret scan clean; {tests.detail}")


def _venv_bin(venv: Path, exe: str) -> Path:
    sub = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return venv / sub / f"{exe}{suffix}"


def gate_build(ctx: Ctx, smoke: list[list[str]] | None = None) -> Result:
    smoke = SMOKE_COMMANDS if smoke is None else smoke
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "dist"
        code, log = _run(["uv", "build", "--wheel", "--out-dir", str(out)], ctx.root)
        if code != 0:
            return Result("FAIL", "uv build: " + _tail(log))
        wheels = sorted(out.glob("*.whl"))
        if not wheels:
            return Result("FAIL", "uv build produced no wheel")
        venv = Path(tmp) / "venv"
        code, log = _run(["uv", "venv", "-q", str(venv)], ctx.root)
        if code != 0:
            return Result("FAIL", "uv venv: " + _tail(log))
        py = _venv_bin(venv, "python")
        code, log = _run(
            ["uv", "pip", "install", "-q", "--python", str(py), str(wheels[0])], ctx.root
        )
        if code != 0:
            return Result("FAIL", "install wheel: " + _tail(log))
        for cmd in smoke:
            exe = _venv_bin(venv, cmd[0])
            code, log = _run([str(exe), *cmd[1:]], Path(tmp))
            if code != 0:
                return Result("FAIL", f"{' '.join(cmd)}: {_tail(log)}")
        return Result(
            "PASS", f"{wheels[0].name} installs; smoke: {', '.join(' '.join(c) for c in smoke)}"
        )


def changed_files(root: Path, base: str) -> list[str] | None:
    """Files changed since base: committed, staged, unstaged and untracked.

    Returns None when git is unusable.
    """
    code, _ = _run(["git", "rev-parse", "--git-dir"], root)
    if code != 0:
        return None
    files: set[str] = set()
    code, _ = _run(["git", "rev-parse", "HEAD"], root)
    if code == 0 and base not in ("", "none"):
        code, out = _run(["git", "diff", "--name-only", base], root)
        if code != 0:
            raise HarnessError(f"git diff against base {base!r} failed: {_tail(out, 2)}")
        files.update(out.splitlines())
    else:
        code, out = _run(["git", "ls-files"], root)
        files.update(out.splitlines())
    code, out = _run(["git", "ls-files", "--others", "--exclude-standard"], root)
    files.update(out.splitlines())
    return sorted(f for f in files if f)


def changed_tests_problem(files: list[str]) -> str | None:
    src = [
        f for f in files if f.startswith("src/") or (f.startswith("scripts/") and f.endswith(".py"))
    ]
    tests = [f for f in files if f.startswith("tests/") and not f.endswith("__init__.py")]
    if src and not tests:
        return f"{len(src)} source files changed but no tests changed (e.g. {src[0]})"
    return None


def gate_changed_tests(ctx: Ctx) -> Result:
    d = spec_dir(ctx.node, ctx.specs)
    base_file = d / ".base" if d else None
    base = (
        base_file.read_text(encoding="utf-8").strip()
        if base_file and base_file.is_file()
        else "none"
    )
    try:
        files = changed_files(ctx.root, base)
    except HarnessError as exc:
        return Result("FAIL", str(exc))
    if files is None:
        return Result("SKIP", "not a git repository")
    problem = changed_tests_problem(files)
    if problem:
        return Result("FAIL", problem)
    return Result(
        "PASS", f"{len(files)} files changed since {base[:10]}; tests changed with source"
    )


RECORD_FILES = ("spec.md", "criteria.md", "RUN-STATE.md")


FRESHNESS_SCOPE = ("src/", "scripts/", "tests/", "pyproject.toml", "uv.lock", ".github/")


def _dirty_paths(root: Path) -> set[str]:
    """Paths whose content differs from HEAD (modified, added, deleted or untracked)."""
    p = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "-uall"], cwd=root, capture_output=True
    )
    if p.returncode != 0:
        raise HarnessError(f"git status failed: {p.stderr.decode('utf-8', 'replace')[-200:]}")
    paths: set[str] = set()
    entries = iter(p.stdout.decode("utf-8").split("\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        paths.add(entry[3:])
        if entry[0] in "RC":  # a rename's source path follows as its own entry
            paths.add(next(entries, ""))
    return paths


def _changed_at(root: Path, rel: str, dirty: set[str]) -> float:
    """When a file's content last changed: its mtime if it differs from HEAD, else the time
    of the last commit that touched it. A bare `touch` or a checkout changes neither."""
    path = root / rel
    if rel in dirty:
        return path.stat().st_mtime if path.exists() else dt.datetime.now().timestamp()
    code, out = _run(["git", "log", "-1", "--format=%ct", "--", rel], root)
    if code != 0 or not out.strip():
        raise HarnessError(f"cannot read the commit time of {rel}")
    return float(out.strip().splitlines()[-1])


def _base_problem(root: Path, base: str) -> str | None:
    if base in ("", "none"):
        return None
    code, _ = _run(["git", "cat-file", "-e", f"{base}^{{commit}}"], root)
    if code != 0:
        return f"unknown base {base[:12]} (not a commit here; a shallow clone lacks history)"
    return None


def freshness_problem(root: Path, d: Path, base: str) -> str | None:
    """Spec and criteria must have changed since the node's base. RUN-STATE must follow the
    newest source, test or config change: while any such file differs from HEAD, RUN-STATE
    must differ too and be at least as new as every existing changed file (so a backdated
    edit still demands a RUN-STATE update); once committed, commit times decide."""
    if _run(["git", "rev-parse", "--git-dir"], root)[0] != 0:
        return "records freshness needs a git repository"
    bad = _base_problem(root, base)
    if bad:
        return bad
    files = changed_files(root, base) or []
    scope = [f for f in files if f.startswith(FRESHNESS_SCOPE)]
    if not scope:
        return None
    rel = {name: (d / name).relative_to(root).as_posix() for name in RECORD_FILES}
    unwritten = [n for n in ("spec.md", "criteria.md") if rel[n] not in files]
    if unwritten:
        return f"{', '.join(unwritten)} not changed since the node's base ({base[:10]})"
    dirty = _dirty_paths(root)
    run_state = rel["RUN-STATE.md"]
    dirty_scope = [f for f in scope if f in dirty]
    if dirty_scope:
        if run_state not in dirty:
            return f"RUN-STATE.md not updated since the last source change ({dirty_scope[0]})"
        stamps = [((root / f).stat().st_mtime, f) for f in dirty_scope if (root / f).exists()]
        if stamps and (root / run_state).stat().st_mtime < max(stamps)[0]:
            return f"RUN-STATE.md older than the last source change ({max(stamps)[1]})"
        return None
    newest, newest_file = max((_changed_at(root, f, dirty), f) for f in scope)
    if _changed_at(root, run_state, dirty) < newest:
        return f"RUN-STATE.md older than the last source change ({newest_file})"
    return None


def records_problems(node: str, caps: list[Cap], root: Path, specs: Path) -> list[str]:
    problems: list[str] = []
    changelog = root / "changelog.md"
    if not changelog.is_file() or f"[{node}]" not in changelog.read_text(encoding="utf-8"):
        problems.append(f"changelog.md has no [{node}] entry")
    try:
        d = spec_dir(node, specs)
    except HarnessError as exc:
        return [*problems, str(exc)]
    if d is None:
        return [*problems, f"no spec folder for {node}"]
    criteria = d / "criteria.md"
    text = criteria.read_text(encoding="utf-8") if criteria.is_file() else ""
    missing = criteria_missing(text, [c.cap_id for c in caps if c.node == node])
    if missing:
        problems.append(f"criteria.md has no criterion line for {', '.join(missing)}")
    absent = [f for f in RECORD_FILES if not (d / f).is_file()]
    if absent:
        problems.append(f"{', '.join(absent)} missing")
    base_file = d / ".base"
    base = base_file.read_text(encoding="utf-8").strip() if base_file.is_file() else "none"
    if not absent and not all(c.status == "green" for c in caps if c.node == node):
        # freshness applies while a node is building; a green node's records are frozen
        try:
            stale = freshness_problem(root, d, base)
        except HarnessError as exc:
            stale = str(exc)
        if stale:
            problems.append(stale)
    return problems


def gate_records(ctx: Ctx) -> Result:
    problems = records_problems(ctx.node, ctx.caps, ctx.root, ctx.specs)
    return Result("FAIL", "; ".join(problems)) if problems else Result("PASS", "records complete")


GATES: list[tuple[str, Callable[[Ctx], Result]]] = [
    ("claims-work", gate_claims_work),
    ("eligibility", gate_eligibility),
    ("proofs", gate_proofs),
    ("ruff", gate_ruff),
    ("pyright", gate_pyright),
    ("pytest", gate_pytest),
    ("golden", gate_golden),
    ("rebuild", gate_rebuild),
    ("boundary", gate_boundary),
    ("security", gate_security),
    ("build", gate_build),
    ("changed-tests", gate_changed_tests),
    ("records", gate_records),
]


# ---------- self-test: every gate must be shown to fail on bad input ----------

BOUNDARY_PLANTS: dict[str, tuple[str, str]] = {
    # zone file: (code, text the FAIL detail must contain). Every bypass the W0.1 verifier
    # found (passes 1 and 2) is here; the clean modules below must still pass.
    "pipeline/a.py": ("import anthropic\n", "imports anthropic"),
    "pipeline/a2.py": ("import anthropic_bedrock\n", "model SDK"),
    "pipeline/b.py": ("from openai import OpenAI\n", "imports openai.OpenAI"),
    "pipeline/c.py": ("from assay_pipeline.agent.loop import x\n", "imports assay_pipeline.agent"),
    "pipeline/d.py": ("from assay_pipeline import agent\n", "imports assay_pipeline.agent"),
    "pipeline/e.py": ("from ..agent import loop\n", "imports assay_pipeline.agent"),
    "pipeline/sub/f.py": ("from ...agent.loop import x\n", "imports assay_pipeline.agent"),
    "pipeline/g.py": ("from .. import attack\n", "imports assay_pipeline.attack"),
    "pipeline/h.py": ("import importlib\n", "dynamic import machinery"),
    "pipeline/i.py": ("from importlib import import_module as im\n", "dynamic import machinery"),
    "pipeline/j.py": ("__import__('anthropic')\n", "uses __import__"),
    "pipeline/k.py": ("im = loader.import_module\n", "uses .import_module"),
    "pipeline/l.py": ("exec('import anthropic')\n", "uses exec"),
    "pipeline/m.py": ("eval(\"__import__('anthropic')\")\n", "uses eval"),
    "pipeline/n.py": ("import runpy\n", "dynamic import machinery"),
    "pipeline/o.py": ("import importlib.util\n", "dynamic import machinery"),
    "pipeline/p.py": ("import sys\nm = sys.modules['assay_pipeline.agent']\n", ".modules"),
    "pipeline/q.py": ("from sys import modules\n", "dynamic import machinery"),
    "pipeline/r.py": ("import builtins\n", "dynamic import machinery"),
    "pipeline/s.py": ("x = getattr(mod, 'agent')\n", "uses getattr"),
    "pipeline/t.py": ("from assay_pipeline import *\n", "star import"),
    "pipeline/u.py": ("import assay_pipeline\n", "bare assay_pipeline"),
    "pipeline/v.py": ("x = vars(mod)['agent']\n", "uses vars"),
    "pipeline/w.py": ("x = mod.__dict__['agent']\n", "uses .__dict__"),
    "pipeline.py": ("import anthropic\n", "imports anthropic"),
    "agent/a.py": ("from assay_pipeline import pipeline\n", "imports assay_pipeline.pipeline"),
    "agent/b.py": ("from ..pipeline import snapshot\n", "imports assay_pipeline.pipeline"),
    "agent/c.py": ("import duckdb\n", "outside agent/connection.py"),
    "agent/d.py": ("import duckdb.typing\n", "outside agent/connection.py"),
    "agent/e.py": ("from duckdb import connect\n", "from-import of duckdb"),
    "agent/f.py": ("d = __import__('duckdb')\n", "uses __import__"),
    "agent/connection.py": ("import duckdb as d\n", "not as a plain `import duckdb`"),
    "agent.py": ("import assay_pipeline.pipeline.snapshot\n", "imports assay_pipeline.pipeline"),
    # verifier pass 3
    "pipeline/x.py": ("import sys as s\nm = s.modules['x']\n", "aliases sys"),
    "pipeline/y.py": ("import os\nm = os.sys.modules['anthropic']\n", "uses .sys"),
    "pipeline/z.py": ("import sys\nm = sys.modules\n", "uses .modules"),
    "pipeline/aa.py": ("import assay_pipeline.schemas\n", "binds the bare assay_pipeline"),
    "pipeline/ab.py": ("import pydoc\n", "dynamic import machinery"),
    "pipeline/ac.py": ("from operator import attrgetter\n", "dynamic import machinery"),
    "pipeline/ad.py": ("x = object.__getattribute__(pkg, 'agent')\n", "__getattribute__"),
    "pipeline/ae.py": ("import zipimport\n", "dynamic import machinery"),
    "agent/g.py": ("from .connection import duckdb\n", "reaches duckdb outside"),
    "agent/h.py": ("from . import connection\nconnection.duckdb.connect('x')\n", "reaches duckdb"),
    "agent/i.py": (
        "from assay_pipeline.agent.connection import duckdb as d\n",
        "reaches duckdb outside",
    ),
    "agent/j.py": ("import sys as s\ns.modules['duckdb'].connect('x')\n", "aliases sys"),
    # verifier pass 4: new forms, and a plant for every rule a mutant could drop unnoticed
    "pipeline/af.py": (
        "from importlib.metadata import import_module as im\n",
        "imports the banned name import_module",
    ),
    "pipeline/ag.py": (
        "def f():\n    pass\nmatch f:\n    case object(__globals__=g):\n        pass\n",
        "uses .__globals__",
    ),
    "pipeline/ah.py": (
        "import sys\nmatch sys:\n    case object(modules=m):\n        pass\n",
        "uses .modules",
    ),
    "pipeline/ai.py": (
        "import importlib.metadata\nimportlib.util.find_spec('x')\n",
        "importlib.util",
    ),
    "pipeline/aj.py": ("import operator\nf = operator.attrgetter('agent')\n", "uses .attrgetter"),
    "pipeline/ak.py": ("import operator\nf = operator.methodcaller('x')\n", "uses .methodcaller"),
    "pipeline/al.py": ("from os import sys\n", "binds sys by from-import"),
    "pipeline/am.py": ("from sys import argv\n", "from-import of sys"),
    "pipeline/an.py": ("import os as o\n", "aliases os"),
    "pipeline/ao.py": ("import sys\nf = sys._getframe()\n", "uses ._getframe"),
    "pipeline/ap.py": ("setattr(mod, 'x', 1)\n", "uses setattr"),
    "pipeline/aq.py": ("delattr(mod, 'x')\n", "uses delattr"),
    "pipeline/ar.py": ("g = globals()\n", "uses globals"),
    "pipeline/as_.py": ("v = locals()\n", "uses locals"),
    "pipeline/at.py": ("import pickle\n", "dynamic import machinery"),
    "pipeline/au.py": ("import marshal\n", "dynamic import machinery"),
    "pipeline/av.py": ("import ctypes\n", "dynamic import machinery"),
    "pipeline/aw.py": ("from operator import methodcaller\n", "dynamic import machinery"),
    "pipeline/ax.py": ("c = compile('1', 'x', 'eval')\n", "uses compile"),
    "agent/k.py": ("x = duckdb\n", "reaches duckdb outside"),
    "pipeline/ay.py": ("from inspect import builtins as b\n", "banned module builtins"),
    "pipeline/az.py": ("from inspect import importlib as il\n", "banned module importlib"),
    "pipeline/ba.py": ("from unittest.mock import pkgutil as pu\n", "banned module pkgutil"),
    "pipeline/bb.py": ("import json as sys\n", "binds sys to json"),
    "pipeline/bc.py": ("from importlib.metadata import os as o\n", "binds os"),
    "agent/l2.py": (
        "from . import connection\nmatch connection:\n    case object(duckdb=d):\n        pass\n",
        "reaches duckdb outside",
    ),
}
CONNECTION_PLANTS: dict[str, str] = {
    # agent/connection.py bodies (after `import duckdb`) and the problem each must raise
    "duckdb.connect('x')\n": "without a literal read_only=True",
    "duckdb.connect('x', read_only=False)\n": "without a literal read_only=True",
    "duckdb.connect('x', read_only=ro)\n": "without a literal read_only=True",
    "duckdb.connect('x', **kw)\n": "**kwargs",
    "duckdb.connect('x', read_only=True, config={'access_mode': 'READ_WRITE'})\n": "access_mode",
    "duckdb.connect('x', read_only=True, config=cfg)\n": "non-literal config",
    "db = duckdb\n": "other than in duckdb.connect",
    "f = duckdb.connect\n": "other than in duckdb.connect",
    "duckdb.sql(\"ATTACH 'x'\")\n": "other than in duckdb.connect",
    "x = duckdb.__dict__['connect']\n": "uses .__dict__",
    "duckdb = 1\n": "rebinds the name duckdb",
    "duckdb.connect('x', read_only=True, config={'enable_external_access': flag})\n": (
        "non-literal config value"
    ),
}
CLEAN_CONNECTION = (
    "import duckdb\n\n\ndef open_agent_db(path):\n"
    "    return duckdb.connect(path, read_only=True, config={'enable_external_access': False})\n"
)
CLEAN_TOOL = (
    "import importlib.metadata\nimport importlib.resources\n"
    "V = importlib.metadata.version\n"
    "from importlib import resources\nfrom importlib.metadata import version\n"
    "from . import connection\nfrom ..guard import check\n\n\ndef run(con, sql):\n"
    "    return con.execute(\"SELECT * FROM v_activities WHERE note = 'load set'\")\n"
)


def _git(repo: Path, *args: str) -> str:
    cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args]
    p = subprocess.run(
        cmd, cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    p.check_returncode()
    return p.stdout


def self_test() -> int:
    """Feed each real gate a planted defect and require a FAIL that names the defect."""
    failures = 0

    def check(name: str, ok: bool) -> None:
        nonlocal failures
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'WRONG'}] {name}")

    graph = parse_graph("| **W0.1** | Harness | — |\n| **N1.1** | Schemas | W0.1 |\n")
    hdr = "cap_id\tnode\tstatus\tproof\tnote\n"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        specs = root / "specs"
        (specs / "W0.1-harness").mkdir(parents=True)

        def ctx(cat: str, node: str = "W0.1", at: Path = root) -> Ctx:
            return Ctx(node, at, graph, parse_catalog(hdr + cat), specs)

        # 1 claims-work
        r = gate_claims_work(ctx("H-1\tW0.1\tspec\t\t\n"))
        check("1 claims-work fails naming the idle node", r.status == "FAIL" and "W0.1" in r.detail)
        r = gate_claims_work(ctx("H-1\tN1.1\tspec\t\t\n"))
        check("1 claims-work fails on a node with no rows", "no catalog rows" in r.detail)
        check(
            "1 claims-work passes when building",
            gate_claims_work(ctx("H-1\tW0.1\tbuilding\t\t\n")).status == "PASS",
        )

        # 2 eligibility
        r = gate_eligibility(ctx("H-1\tW0.1\tbuilding\t\t\nS-1\tN1.1\tbuilding\t\t\n", "N1.1"))
        check(
            "2 eligibility fails naming the dependency", r.status == "FAIL" and "W0.1" in r.detail
        )

        # 3 proofs
        tests_dir = root / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_x.py").write_text(
            "import pytest\n\n"
            "# test_in_comment\n"
            "NAME = 'test_in_string'\n\n"
            "def test_real():\n    pass\n\n"
            "def test_realistic():\n    pass\n\n"
            "@pytest.mark.skip\ndef test_skipped():\n    pass\n",
            encoding="utf-8",
        )
        (root / "outside.py").write_text("def test_real():\n    pass\n", encoding="utf-8")
        ok = "; ".join(
            [f'{d}=tests/test_x.py›"test_real"' for d in PROOF_DIMENSIONS[:3]]
            + ["repro=n/a:no state to reproduce here", "safety=n/a:no secrets or spend involved"]
        )
        r = gate_proofs(ctx(f"H-1\tW0.1\tgreen\t{ok}\t\n"))
        check("3 proofs pass on resolvable proof", r.status == "PASS")
        bad = {
            "empty proof": ("", "no proof"),
            "missing dimension": ('logical=tests/test_x.py›"test_real"', "missing contract"),
            "unknown test": (ok.replace('"test_real"', '"test_ghost"', 1), "test_ghost"),
            "name only in a comment": (
                ok.replace('"test_real"', '"test_in_comment"', 1),
                "test_in_comment",
            ),
            "name only in a string": (
                ok.replace('"test_real"', '"test_in_string"', 1),
                "test_in_string",
            ),
            "prefix of another test": (ok.replace('"test_real"', '"test_rea"', 1), "test_rea"),
            "skipped test": (ok.replace('"test_real"', '"test_skipped"', 1), "skipped"),
            "path outside tests/": (ok.replace("tests/test_x.py", "outside.py", 1), "outside.py"),
            "path escaping repo": (
                ok.replace("tests/test_x.py", "tests/../../x.py", 1),
                "must be a .py file under tests/",
            ),
            "short n/a reason": (
                ok.replace("n/a:no state to reproduce here", "n/a:x"),
                "n/a needs",
            ),
            "logical n/a": (
                ok.replace(
                    'logical=tests/test_x.py›"test_real"', "logical=n/a:nothing to unit test here"
                ),
                "cannot be n/a",
            ),
            "duplicate key": (ok + '; logical=tests/test_x.py›"test_real"', "duplicate proof key"),
        }
        for label, (proof, needle) in bad.items():
            r = gate_proofs(ctx(f"H-1\tW0.1\tgreen\t{proof}\t\n"))
            check(
                f"3 proofs fail on {label}",
                r.status == "FAIL" and "H-1:" in r.detail and needle in r.detail,
            )

        # 4 ruff — must name the planted rule, so a missing tool cannot pass for a caught defect
        lint = root / "lint"
        lint.mkdir()
        (lint / "bad.py").write_text("import os\n", encoding="utf-8")
        r = gate_ruff(Ctx("W0.1", lint, graph, [], specs))
        check("4 ruff fails naming F401", r.status == "FAIL" and "F401" in r.detail)

        # 5 pyright
        types = root / "types"
        types.mkdir()
        (types / "bad.py").write_text('x: int = "a"\n', encoding="utf-8")
        (types / "pyrightconfig.json").write_text("{}", encoding="utf-8")
        r = gate_pyright(Ctx("W0.1", types, graph, [], specs))
        check("5 pyright fails naming bad.py", r.status == "FAIL" and "bad.py" in r.detail)

        # 6, 7, 8, 10 pytest-backed gates, each on its own failing fixture
        pt = root / "pt"
        pt.mkdir()
        (pt / "test_fail.py").write_text(
            "import pytest\n\n"
            "def test_unit_fails():\n    assert False\n\n"
            "@pytest.mark.golden\ndef test_golden_fails():\n    assert False\n\n"
            "@pytest.mark.rebuild\ndef test_rebuild_fails():\n    assert False\n\n"
            "@pytest.mark.security\ndef test_security_fails():\n    assert False\n",
            encoding="utf-8",
        )
        (pt / "pytest.ini").write_text(
            "[pytest]\nmarkers =\n    golden: g\n    rebuild: r\n    security: s\n",
            encoding="utf-8",
        )
        pctx = Ctx("W0.1", pt, graph, [], specs)
        for num, gate, test in (
            ("6 pytest", gate_pytest, "test_unit_fails"),
            ("7 golden", gate_golden, "test_golden_fails"),
            ("8 rebuild", gate_rebuild, "test_rebuild_fails"),
            ("10 security", gate_security, "test_security_fails"),
        ):
            r = gate(pctx)
            check(f"{num} fails naming {test}", r.status == "FAIL" and test in r.detail)
        empty = root / "empty"
        empty.mkdir()
        (empty / "test_none.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
        check(
            "7 golden reports SKIP, not PASS, with no golden tests",
            gate_golden(Ctx("W0.1", empty, graph, [], specs)).status == "SKIP",
        )
        leak = root / "leak"
        leak.mkdir()
        (leak / "k.py").write_text('K = "sk-ant-' + "Q" * 30 + '"\n', encoding="utf-8")
        r = gate_security(Ctx("W0.1", leak, graph, [], specs))
        check(
            "10 security fails on a leaked key", r.status == "FAIL" and "anthropic key" in r.detail
        )

        # 9 boundary — each planted violation must be named with its file
        bnd = root / "bnd"
        zone_pkg = bnd / "src" / PACKAGE
        (zone_pkg / "pipeline" / "sub").mkdir(parents=True)
        (zone_pkg / "agent").mkdir(parents=True)
        bctx = Ctx("W0.1", bnd, graph, [], specs)
        check("9 boundary SKIPs with no modules", gate_boundary(bctx).status == "SKIP")
        # an importable package: stub duckdb and guard so the runtime half can import zones
        (bnd / "src" / "duckdb.py").write_text(
            "def connect(*a, **k):\n    pass\n", encoding="utf-8"
        )
        for init in ("", "agent", "pipeline", "pipeline/sub"):
            (zone_pkg / init / "__init__.py").write_text("", encoding="utf-8")
        (zone_pkg / "guard.py").write_text("def check():\n    pass\n", encoding="utf-8")
        (zone_pkg / "agent" / "connection.py").write_text(CLEAN_CONNECTION, encoding="utf-8")
        (zone_pkg / "agent" / "tool.py").write_text(CLEAN_TOOL, encoding="utf-8")
        r = gate_boundary(bctx)
        check(f"9 boundary passes on clean modules ({r.detail[:60]})", r.status == "PASS")
        for rel, (code, needle) in BOUNDARY_PLANTS.items():
            f = zone_pkg / rel
            saved = f.read_text(encoding="utf-8") if f.is_file() else None
            f.write_text(code, encoding="utf-8")
            r = gate_boundary(bctx)
            check(
                f"9 boundary fails naming {rel}",
                r.status == "FAIL" and f"{PACKAGE}/{rel}" in r.detail and needle in r.detail,
            )
            if saved is None:
                f.unlink()
            else:
                f.write_text(saved, encoding="utf-8")
        # runtime half: a helper outside the zones pulls in a model SDK at import time
        (zone_pkg / "helpers.py").write_text("import anthropic\n", encoding="utf-8")
        user = zone_pkg / "pipeline" / "uses_helper.py"
        user.write_text("from assay_pipeline import helpers\n", encoding="utf-8")
        r = gate_boundary(bctx)
        check(
            "9 boundary runtime check catches an SDK pulled in through a helper",
            r.status == "FAIL"
            and "assay_pipeline.pipeline.uses_helper imports anthropic at import time" in r.detail,
        )
        user.unlink()
        (zone_pkg / "helpers.py").write_text(
            "from assay_pipeline.pipeline import thing\n", encoding="utf-8"
        )
        (zone_pkg / "pipeline" / "thing.py").write_text("X = 1\n", encoding="utf-8")
        user = zone_pkg / "agent" / "uses_helper.py"
        user.write_text("from assay_pipeline import helpers\n", encoding="utf-8")
        r = gate_boundary(bctx)
        check(
            "9 boundary runtime check catches the agent reaching the pipeline through a helper",
            r.status == "FAIL"
            and "assay_pipeline.agent.uses_helper imports assay_pipeline.pipeline" in r.detail,
        )
        user.unlink()
        (zone_pkg / "pipeline" / "thing.py").unlink()
        (zone_pkg / "helpers.py").unlink()
        (zone_pkg / "reexport.py").write_text("import builtins as hidden\n", encoding="utf-8")
        renamed = zone_pkg / "pipeline" / "renamed.py"
        renamed.write_text("from assay_pipeline.reexport import hidden\n", encoding="utf-8")
        r = gate_boundary(bctx)
        check(
            "9 boundary runtime check catches a banned module re-exported under another name",
            r.status == "FAIL" and "imports builtins (bound as hidden)" in r.detail,
        )
        renamed.unlink()
        (zone_pkg / "reexport.py").unlink()
        own_code = zone_pkg / "pipeline" / "code.py"
        own_code.write_text("X = 1\n", encoding="utf-8")
        uses_code = zone_pkg / "pipeline" / "uses_code.py"
        uses_code.write_text("from . import code\n", encoding="utf-8")
        r = gate_boundary(bctx)
        check(f"9 boundary allows our own module named code ({r.detail[:50]})", r.status == "PASS")
        uses_code.unlink()
        own_code.unlink()
        main_mod = zone_pkg / "pipeline" / "__main__.py"
        main_mod.write_text("raise SystemExit(3)\n", encoding="utf-8")
        boom = zone_pkg / "pipeline" / "boom.py"
        boom.write_text("raise SystemExit(4)\n", encoding="utf-8")
        r = gate_boundary(bctx)
        check(
            "9 boundary skips __main__.py and reports a zone module that exits on import",
            r.status == "FAIL"
            and "assay_pipeline.pipeline.boom cannot be imported: SystemExit" in r.detail
            and "__main__" not in r.detail,
        )
        main_mod.unlink()
        boom.unlink()
        conn = zone_pkg / "agent" / "connection.py"
        for body, needle in CONNECTION_PLANTS.items():
            conn.write_text("import duckdb\n" + body, encoding="utf-8")
            r = gate_boundary(bctx)
            check(
                f"9 boundary fails on connection.py: {body.strip()[:50]}",
                r.status == "FAIL" and needle in r.detail,
            )
        conn.write_text(CLEAN_CONNECTION, encoding="utf-8")

        # 11 build — the tool must exist, so a missing uv cannot pass for a caught defect
        has_uv = shutil.which("uv") is not None
        check("11 build: uv is on PATH (a missing tool is not a planted defect)", has_uv)
        broken = root / "broken"
        broken.mkdir()
        (broken / "pyproject.toml").write_text("[project\nname=", encoding="utf-8")
        r = gate_build(Ctx("W0.1", broken, graph, [], specs), smoke=[])
        check(
            "11 build fails on a broken project",
            has_uv and r.status == "FAIL" and "uv build" in r.detail and "Errno" not in r.detail,
        )
        r = gate_build(
            Ctx("W0.1", ROOT, graph, [], specs),
            smoke=[["python", "-c", "raise SystemExit('planted smoke failure')"]],
        )
        check(
            "11 build fails naming a failing smoke command",
            r.status == "FAIL" and "planted smoke failure" in r.detail,
        )

        # 12 changed-tests, on a real repository with a recorded base
        repo = root / "repo"
        (repo / "specs" / "W0.1-x").mkdir(parents=True)
        _git(repo, "init", "-q")
        _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
        base = _git(repo, "rev-parse", "HEAD").strip()
        (repo / "specs" / "W0.1-x" / ".base").write_text(base + "\n", encoding="utf-8")
        (repo / "src" / PACKAGE).mkdir(parents=True)
        (repo / "src" / PACKAGE / "x.py").write_text("X = 1\n", encoding="utf-8")
        rctx = Ctx("W0.1", repo, graph, [], repo / "specs")
        r = gate_changed_tests(rctx)
        check(
            "12 changed-tests fails naming the untested source",
            r.status == "FAIL" and f"src/{PACKAGE}/x.py" in r.detail,
        )
        (repo / "tests").mkdir()
        (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
        check(
            "12 an __init__.py does not count as a test change",
            gate_changed_tests(rctx).status == "FAIL",
        )
        (repo / "tests" / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
        check("12 passes when tests changed", gate_changed_tests(rctx).status == "PASS")
        (repo / "specs" / "W0.1-x" / ".base").write_text("deadbeef\n", encoding="utf-8")
        r = gate_changed_tests(rctx)
        check("12 an unknown base commit fails", r.status == "FAIL" and "deadbeef" in r.detail)

        # 13 records — each planted defect must be named; freshness uses git, not raw mtimes
        rec = root / "rec"
        rspecs = rec / "specs"
        d = rspecs / "W0.1-harness"
        d.mkdir(parents=True)
        cat = parse_catalog(hdr + "H-1\tW0.1\tbuilding\t\t\n")  # freshness applies while building
        rc = Ctx("W0.1", rec, graph, cat, rspecs)
        _git(rec, "init", "-q")
        _git(rec, "commit", "-q", "--allow-empty", "-m", "base")
        (d / ".base").write_text(_git(rec, "rev-parse", "HEAD").strip() + "\n", encoding="utf-8")

        def records_fail(needle: str) -> bool:
            r = gate_records(rc)
            return r.status == "FAIL" and needle in r.detail

        def later(path: Path, seconds: int) -> None:
            stamp = dt.datetime.now().timestamp() + seconds
            os.utime(path, (stamp, stamp))

        check("13 records fails naming the missing changelog entry", records_fail("[W0.1]"))
        (rec / "changelog.md").write_text("[FEAT] [W0.1] — harness\n", encoding="utf-8")
        (d / "spec.md").write_text("spec\n", encoding="utf-8")
        (d / "criteria.md").write_text("H-1 is mentioned but has no criterion\n", encoding="utf-8")
        check("13 records fails naming a capability with no criterion", records_fail("H-1"))
        (d / "criteria.md").write_text(
            "**H-1** the harness is ported with every gate proven able to fail\n",
            encoding="utf-8",
        )
        check("13 records fails naming a missing RUN-STATE.md", records_fail("RUN-STATE.md"))
        (d / "RUN-STATE.md").write_text("ok\n", encoding="utf-8")
        check("13 records passes when complete", gate_records(rc).status == "PASS")
        _git(rec, "add", "-A")
        _git(rec, "commit", "-q", "-m", "records")
        (rec / "src").mkdir()
        late = rec / "src" / "late.py"
        late.write_text("X = 1\n", encoding="utf-8")
        later(late, 60)
        check("13 records fails naming stale records", records_fail("src/late.py"))
        for name in RECORD_FILES:
            later(d / name, 120)
        check("13 a bare touch of the records does not make them fresh", records_fail("late.py"))
        (d / "RUN-STATE.md").write_text("built late.py\n", encoding="utf-8")
        later(d / "RUN-STATE.md", 120)
        check("13 records pass once RUN-STATE's content changes", gate_records(rc).status == "PASS")
        _git(rec, "add", "-A")
        _git(rec, "commit", "-q", "-m", "node green")
        later(late, 600)
        check(
            "13 a content-free touch of committed source is not a change",
            gate_records(rc).status == "PASS",
        )
        (d / "RUN-STATE.md").write_text("notes written before the edit\n", encoding="utf-8")
        os.utime(d / "RUN-STATE.md", (946684800, 946684800))
        late.write_text("X = 4\n", encoding="utf-8")
        check(
            "13 RUN-STATE edited before the latest source edit is stale",
            records_fail("RUN-STATE.md older than the last source change"),
        )
        _git(rec, "checkout", "-q", "--", ".")
        _git(rec, "mv", "src/late.py", "src/renamed.py")
        check("13 a rename needs a RUN-STATE update", records_fail("renamed.py"))
        (d / "RUN-STATE.md").write_text("renamed late.py\n", encoding="utf-8")
        later(d / "RUN-STATE.md", 120)
        check(
            "13 ... and passes once RUN-STATE changes (rename)", gate_records(rc).status == "PASS"
        )
        _git(rec, "mv", "src/renamed.py", "src/late.py")
        _git(rec, "checkout", "-q", "--", ".")
        late.write_text("X = 3\n", encoding="utf-8")
        os.utime(late, (946684800, 946684800))  # backdated to 2000
        check("13 a backdated edit still needs a RUN-STATE update", records_fail("late.py"))
        (d / "RUN-STATE.md").write_text("changed late.py again\n", encoding="utf-8")
        check("13 ... and passes once RUN-STATE changes", gate_records(rc).status == "PASS")
        _git(rec, "add", "-A")
        _git(rec, "commit", "-q", "-m", "late v3")
        # next node: base moves to HEAD, so late.py existed at base and deleting it is a change
        (d / ".base").write_text(_git(rec, "rev-parse", "HEAD").strip() + "\n", encoding="utf-8")
        (d / "spec.md").write_text("spec for the removal\n", encoding="utf-8")
        (d / "criteria.md").write_text(
            "**H-1** the harness is ported with every gate proven able to fail, minus late.py\n",
            encoding="utf-8",
        )
        late.unlink()
        check("13 a deletion needs a RUN-STATE update", records_fail("late.py"))
        (d / "RUN-STATE.md").write_text("removed late.py\n", encoding="utf-8")
        check(
            "13 ... and passes once RUN-STATE changes (deletion)", gate_records(rc).status == "PASS"
        )
        _git(rec, "add", "-A")
        _git(rec, "commit", "-q", "-m", "removed late")
        clone = root / "clone"
        _git(root, "clone", "-q", str(rec), str(clone))
        r = gate_records(Ctx("W0.1", clone, graph, cat, clone / "specs"))
        check(f"13 a fresh clone passes ({r.detail[:60]})", r.status == "PASS")
        for rel_cfg in ("uv.lock", ".github/workflows/ci.yml"):
            cfg_file = rec / rel_cfg
            cfg_file.parent.mkdir(parents=True, exist_ok=True)
            cfg_file.write_text("changed\n", encoding="utf-8")
            check(f"13 a {rel_cfg} change needs a RUN-STATE update", records_fail(rel_cfg))
            cfg_file.unlink()
        (d / ".base").write_text(_git(rec, "rev-parse", "HEAD").strip() + "\n", encoding="utf-8")
        (rec / "src" / "next.py").write_text("Y = 2\n", encoding="utf-8")
        (d / "RUN-STATE.md").write_text("next node\n", encoding="utf-8")
        later(d / "RUN-STATE.md", 120)
        check(
            "13 spec and criteria not written since the base fail",
            records_fail("spec.md, criteria.md not changed since"),
        )
        (d / ".base").write_text("deadbeef\n", encoding="utf-8")
        check("13 an unknown base fails instead of skipping", records_fail("unknown base deadbeef"))
        nogit = root / "nogit"
        (nogit / "specs" / "W0.1-x").mkdir(parents=True)
        (nogit / "changelog.md").write_text("[W0.1]\n", encoding="utf-8")
        for name in RECORD_FILES:
            (nogit / "specs" / "W0.1-x" / name).write_text(
                "**H-1** the harness is ported with every gate proven able to fail\n",
                encoding="utf-8",
            )
        r = gate_records(Ctx("W0.1", nogit, graph, cat, nogit / "specs"))
        check("13 outside git the records gate fails", r.status == "FAIL" and "git" in r.detail)
        (rspecs / "W0.1-dup").mkdir()
        check("13 records fails on two spec folders", records_fail("several spec folders"))
    print("self-test", "PASSED" if failures == 0 else f"FAILED ({failures})")
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("node", nargs="?")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--no-log", action="store_true", help="do not write a log under .harness/")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.node:
        ap.error("node is required")
    try:
        ctx = Ctx(args.node, ROOT, load_graph(), load_catalog(), SPECS_DIR)
    except (HarnessError, OSError) as exc:
        print(f"node-converge: cannot read inputs: {exc}")
        return 1
    if args.node not in ctx.nodes:
        print(f"node-converge: unknown node {args.node}")
        return 1
    lines = [f"node-converge {args.node} — {dt.datetime.now().isoformat(timespec='seconds')}"]
    failed = 0
    for i, (name, fn) in enumerate(GATES, start=1):
        try:
            r = fn(ctx)
        except Exception as exc:  # a crashing gate is a failing gate
            r = Result("FAIL", f"gate crashed: {exc!r}")
        failed += r.status == "FAIL"
        first, *rest = r.detail.splitlines() or [""]
        lines.append(f"{i:>2} {name:<14} {r.status:<4}  {first}")
        lines.extend(f"{'':>22}{ln}" for ln in rest)
    lines.append(
        f"RESULT: {'FAIL' if failed else 'PASS'} "
        f"({failed} failing gate{'s' if failed != 1 else ''})"
    )
    report = "\n".join(lines)
    print(report)
    if args.no_log:
        return 1 if failed else 0
    log_dir = ROOT / ".harness" / args.node
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    (log_dir / f"converge-{stamp}.txt").write_text(report + "\n", encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
