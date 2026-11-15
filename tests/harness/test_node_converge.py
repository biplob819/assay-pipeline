import subprocess
from pathlib import Path

import node_converge as nc
from harness_lib import PROOF_DIMENSIONS, parse_catalog, parse_graph

HDR = "cap_id\tnode\tstatus\tproof\tnote\n"
GRAPH = parse_graph("| **W0.1** | Harness | — |\n| **N1.1** | Schemas | W0.1 |\n")


def ctx(tmp: Path, rows: str, node: str = "W0.1") -> nc.Ctx:
    return nc.Ctx(node, tmp, GRAPH, parse_catalog(HDR + rows), tmp / "specs")


def test_self_test_shows_every_gate_can_fail():
    assert nc.self_test() == 0


def test_thirteen_gates_in_documented_order():
    assert [name for name, _ in nc.GATES] == [
        "claims-work",
        "eligibility",
        "proofs",
        "ruff",
        "pyright",
        "pytest",
        "golden",
        "rebuild",
        "boundary",
        "security",
        "build",
        "changed-tests",
        "records",
    ]


def test_boundary_names_each_violation(tmp_path):
    pipe = tmp_path / "src" / "assay_pipeline" / "pipeline"
    agent = tmp_path / "src" / "assay_pipeline" / "agent"
    pipe.mkdir(parents=True)
    agent.mkdir(parents=True)
    (pipe / "a.py").write_text("from anthropic import Anthropic\n", encoding="utf-8")
    (pipe / "b.py").write_text("from ..agent import loop\n", encoding="utf-8")
    (agent / "connection.py").write_text(
        "import duckdb\ncon = duckdb.connect(path, read_only=flag)\n", encoding="utf-8"
    )
    problems, checked = nc.boundary_problems(tmp_path / "src")
    assert checked == 3
    assert (
        "assay_pipeline/pipeline/a.py: line 1: imports anthropic.Anthropic (model SDK)" in problems
    )
    assert "assay_pipeline/pipeline/b.py: line 1: imports assay_pipeline.agent.loop" in problems
    assert (
        "assay_pipeline/agent/connection.py: line 2: opens DuckDB without a literal "
        "read_only=True keyword" in problems
    )


def test_every_verifier_bypass_is_planted_in_the_self_test():
    """Both W0.1 verifier passes' bypass forms stay covered by the self-test plants."""
    plants = "".join(code for code, _ in nc.BOUNDARY_PLANTS.values())
    plants += "".join(nc.CONNECTION_PLANTS)
    for form in (
        "from assay_pipeline import agent",
        "from ..agent import",
        "from ...agent.loop import",
        "from .. import attack",
        "import importlib",
        "import_module as im",
        "import anthropic_bedrock",
        "import duckdb as d",
        "import duckdb.typing",
        "from duckdb import connect",
        "f = duckdb.connect",
        "db = duckdb",
        "duckdb.__dict__",
        "__import__('duckdb')",
        "exec(",
        "eval(",
        "import runpy",
        "import importlib.util",
        "sys.modules",
        "getattr(",
        "vars(",
        "from assay_pipeline import *",
        "import assay_pipeline\n",
        "access_mode",
        "**kw",
        "import_module as im",
        "case object(__globals__=g)",
        "importlib.util.find_spec",
        "operator.attrgetter",
        "from os import sys",
        "import os as o",
        "sys._getframe",
        "setattr(",
        "import pickle",
        "case object(duckdb=d)",
        "from inspect import builtins",
        "from unittest.mock import pkgutil",
        "import json as sys",
        "from importlib.metadata import os as o",
    ):
        assert form in plants, form
    assert "pipeline.py" in nc.BOUNDARY_PLANTS and "agent.py" in nc.BOUNDARY_PLANTS


def test_boundary_policy_is_closed_by_prohibition():
    """Every dynamic-import route is banned by name, not tracked (D-25)."""
    for name in ("exec", "eval", "compile", "__import__", "getattr", "vars", "import_module"):
        assert name in nc.DYNAMIC_NAMES
    for mod in ("importlib", "runpy", "builtins", "sys.modules"):
        assert mod in nc.DYNAMIC_MODULES


def test_boundary_allows_clean_agent_code_and_ordinary_sql(tmp_path):
    agent = tmp_path / "src" / "assay_pipeline" / "agent"
    agent.mkdir(parents=True)
    (agent / "connection.py").write_text(nc.CLEAN_CONNECTION, encoding="utf-8")
    (agent / "tool.py").write_text(nc.CLEAN_TOOL, encoding="utf-8")
    assert nc.boundary_problems(tmp_path / "src") == ([], 2)


def test_claims_work(tmp_path):
    assert nc.gate_claims_work(ctx(tmp_path, "H\tW0.1\tnot-started\t\t\n")).status == "FAIL"
    assert nc.gate_claims_work(ctx(tmp_path, "H\tW0.1\tgreen\t\t\n")).status == "PASS"


def test_proof_must_cover_all_five_dimensions(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.py").write_text("def test_a(): pass\n", encoding="utf-8")
    full = "; ".join(f'{d}=tests/t.py›"test_a"' for d in PROOF_DIMENSIONS)
    assert nc.gate_proofs(ctx(tmp_path, f"H\tW0.1\tgreen\t{full}\t\n")).status == "PASS"
    partial = full.rsplit(";", 1)[0]
    r = nc.gate_proofs(ctx(tmp_path, f"H\tW0.1\tgreen\t{partial}\t\n"))
    assert r.status == "FAIL" and "missing safety proof" in r.detail


def test_proof_rejects_unknown_keys_and_bad_format(tmp_path):
    (tmp_path / "t.py").write_text("def test_a(): pass\n", encoding="utf-8")
    full = "; ".join(f'{d}=t.py›"test_a"' for d in PROOF_DIMENSIONS)
    r = nc.gate_proofs(ctx(tmp_path, f"H\tW0.1\tgreen\t{full}; vibes=n/a:looks fine to me\t\n"))
    assert r.status == "FAIL" and "unknown proof keys" in r.detail
    r = nc.gate_proofs(ctx(tmp_path, "H\tW0.1\tgreen\tlogical=t.py test_a\t\n"))
    assert r.status == "FAIL"


def test_pytest_no_tests_is_skip_not_pass(tmp_path):
    (tmp_path / "test_ok.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    assert nc._pytest(tmp_path, "golden", "golden").status == "SKIP"
    assert nc._pytest(tmp_path, "not golden", "unit").status == "PASS"


def test_changed_tests_rule():
    assert nc.changed_tests_problem(["scripts/node_start.py"]) is not None
    assert nc.changed_tests_problem(["docs/decisions.md"]) is None
    assert nc.changed_tests_problem(["src/assay/a.py", "tests/test_a.py"]) is None


def test_records_require_changelog_criteria_and_run_state(tmp_path):
    caps = parse_catalog(HDR + "H-1\tW0.1\tgreen\t\t\n")
    specs = tmp_path / "specs"
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert nc.records_problems("W0.1", caps, tmp_path, specs) == [
        "changelog.md has no [W0.1] entry",
        "no spec folder for W0.1",
    ]
    (tmp_path / "changelog.md").write_text("- [FEAT] [W0.1] — x\n", encoding="utf-8")
    d = specs / "W0.1-harness"
    d.mkdir(parents=True)
    (d / "spec.md").write_text("x\n", encoding="utf-8")
    (d / "criteria.md").write_text("H-1 only mentioned\n", encoding="utf-8")
    assert nc.records_problems("W0.1", caps, tmp_path, specs) == [
        "criteria.md has no criterion line for H-1",
        "RUN-STATE.md missing",
    ]
    (d / "criteria.md").write_text(
        "**H-1** the capability works as the spec describes\n", encoding="utf-8"
    )
    (d / "RUN-STATE.md").write_text("x\n", encoding="utf-8")
    assert nc.records_problems("W0.1", caps, tmp_path, specs) == []


def test_a_crashing_gate_counts_as_failure(monkeypatch, capsys):
    def boom(_ctx):
        raise RuntimeError("kaput")

    monkeypatch.setattr(nc, "GATES", [("boom", boom)])
    assert nc.main(["W0.1", "--no-log"]) == 1
    assert "gate crashed" in capsys.readouterr().out


def test_later_gates_still_report_after_an_early_failure(monkeypatch, capsys):
    gates = [
        ("first", lambda _c: nc.Result("FAIL", "planted")),
        ("second", lambda _c: nc.Result("PASS", "fine")),
        ("third", lambda _c: nc.Result("SKIP", "nothing")),
    ]
    monkeypatch.setattr(nc, "GATES", gates)
    assert nc.main(["W0.1", "--no-log"]) == 1
    out = capsys.readouterr().out
    assert "1 first" in out and "2 second" in out and "3 third" in out
    assert "RESULT: FAIL (1 failing gate)" in out


def _proof(test: str, path: str = "tests/test_p.py") -> str:
    return "; ".join(f'{d}={path}›"{test}"' for d in PROOF_DIMENSIONS)


def test_proofs_require_a_real_unskipped_test_function(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_p.py").write_text(
        "import pytest\n# test_comment\nX = 'test_string'\n"
        "def test_abcdef():\n    pass\n"
        "@pytest.mark.skipif(True, reason='x')\ndef test_skipped():\n    pass\n"
        "class TestGroup:\n    def test_method(self):\n        pass\n",
        encoding="utf-8",
    )
    passes = ["test_abcdef", "test_method"]
    fails = ["test_comment", "test_string", "test_abc", "test_skipped"]
    for name in passes:
        assert nc.gate_proofs(ctx(tmp_path, f"H\tW0.1\tgreen\t{_proof(name)}\t\n")).status == "PASS"
    for name in fails:
        r = nc.gate_proofs(ctx(tmp_path, f"H\tW0.1\tgreen\t{_proof(name)}\t\n"))
        assert r.status == "FAIL", name


def test_proofs_must_live_under_tests(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "README.md").write_text("def test_a(): pass\n", encoding="utf-8")
    (tmp_path / "tests" / "test_p.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    for path in ("README.md", "tests/../README.md", "/etc/hosts"):
        r = nc.gate_proofs(ctx(tmp_path, f"H\tW0.1\tgreen\t{_proof('test_a', path)}\t\n"))
        assert r.status == "FAIL", path


def test_logical_proof_cannot_be_na_and_keys_cannot_repeat(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_p.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    na = _proof("test_a").replace(
        'logical=tests/test_p.py›"test_a"', "logical=n/a:no logic here at all"
    )
    assert nc.gate_proofs(ctx(tmp_path, f"H\tW0.1\tgreen\t{na}\t\n")).status == "FAIL"
    dup = _proof("test_a") + '; logical=tests/test_p.py›"test_a"'
    assert nc.gate_proofs(ctx(tmp_path, f"H\tW0.1\tgreen\t{dup}\t\n")).status == "FAIL"


def test_changed_tests_fails_on_unknown_base(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "a",
        ],
        cwd=tmp_path,
        check=True,
    )
    d = tmp_path / "specs" / "W0.1-x"
    d.mkdir(parents=True)
    (d / ".base").write_text("deadbeef\n", encoding="utf-8")
    r = nc.gate_changed_tests(ctx(tmp_path, "H\tW0.1\tbuilding\t\t\n"))
    assert r.status == "FAIL" and "deadbeef" in r.detail


def test_runtime_boundary_check_catches_imports_through_helpers(tmp_path):
    pkg = tmp_path / "src" / "assay_pipeline"
    (pkg / "pipeline").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "pipeline" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "helpers.py").write_text("import openai_extras\n", encoding="utf-8")
    (pkg / "pipeline" / "x.py").write_text("from assay_pipeline import helpers\n", encoding="utf-8")
    problems = nc.runtime_boundary_problems(tmp_path)
    assert "assay_pipeline.pipeline.x imports openai_extras at import time (runtime)" in problems
    assert nc.boundary_problems(tmp_path / "src")[0] == []  # the static half cannot see it


def test_green_nodes_skip_records_freshness(tmp_path):
    caps = parse_catalog(HDR + "H-1\tW0.1\tgreen\t\t\n")
    specs = tmp_path / "specs"
    d = specs / "W0.1-harness"
    d.mkdir(parents=True)
    (tmp_path / "changelog.md").write_text("- [FEAT] [W0.1] — x\n", encoding="utf-8")
    for name in ("spec.md", "criteria.md", "RUN-STATE.md"):
        (d / name).write_text(
            "**H-1** the capability works as the spec describes\n", encoding="utf-8"
        )
    # no git repository at all: freshness would fail, but a green node's records are frozen
    assert nc.records_problems("W0.1", caps, tmp_path, specs) == []
    building = parse_catalog(HDR + "H-1\tW0.1\tbuilding\t\t\n")
    assert nc.records_problems("W0.1", building, tmp_path, specs) == [
        "records freshness needs a git repository"
    ]


def test_dirty_paths_report_both_sides_of_a_rename(tmp_path):
    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    (tmp_path / "old.py").write_text("X = 1\n", encoding="utf-8")
    git("add", "old.py")
    git("commit", "-q", "-m", "base")
    git("mv", "old.py", "new name.py")
    assert {"old.py", "new name.py"} <= nc._dirty_paths(tmp_path)


def test_banned_modules_reached_through_re_exports_are_flagged(tmp_path):
    pipe = tmp_path / "src" / "assay_pipeline" / "pipeline"
    pipe.mkdir(parents=True)
    (pipe / "a.py").write_text(
        "from inspect import builtins\nfrom unittest.mock import pkgutil\n", encoding="utf-8"
    )
    problems, _ = nc.boundary_problems(tmp_path / "src")
    assert any("banned module builtins from inspect" in p for p in problems)
    assert any("banned module pkgutil from unittest.mock" in p for p in problems)


def test_build_smoke_loads_the_packaged_default_configs():
    """The wheel must carry assay_pipeline/defaults/*.yaml (SCH-04); gate 11 proves it."""
    joined = " ".join(" ".join(c) for c in nc.SMOKE_COMMANDS)
    assert "load_build_config()" in joined and "load_views_config()" in joined
