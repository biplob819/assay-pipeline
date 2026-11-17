import re
from pathlib import Path

import pytest

import status
from harness_lib import ROOT

WORKFLOWS = ROOT / ".github" / "workflows"


def test_decisions_seeded_d1_to_d26():
    text = (ROOT / "docs" / "decisions.md").read_text(encoding="utf-8")
    ids = {int(n) for n in re.findall(r"^\| (?:OWNER )?D-(\d+) \|", text, re.M)}
    assert set(range(1, 27)) <= ids
    for owner in (11, 12, 13, 18, 20, 21, 22, 23):  # owner rulings of 15 Nov 2026
        assert re.search(rf"^\| OWNER D-{owner} \|", text, re.M)


def test_lessons_and_changelog_exist():
    assert "L-1" in (ROOT / "docs" / "lessons.md").read_text(encoding="utf-8")
    assert "[W0.1]" in (ROOT / "changelog.md").read_text(encoding="utf-8")


def test_status_prints_graph_and_node(capsys):
    assert status.main([]) == 0
    out = capsys.readouterr().out
    assert "W0.1" in out and "D6.3" in out and "capabilities green" in out
    assert status.main(["W0.1"]) == 0
    assert "HAR-07" in capsys.readouterr().out
    assert status.main(["Z9.9"]) == 1


def test_graph_matches_tech_spec_node_table():
    """graph.md was generated from Tech Spec §11.2; the two must name the same edges."""
    from harness_lib import load_graph, parse_graph

    spec = (ROOT / "Trusted Assay Pipeline Tech Spec.md").read_text(encoding="utf-8")
    table = spec.split("### 11.2")[1].split("### 11.3")[0]
    want = {n.id: n.depends_on for n in parse_graph(table).values()}
    got = {n.id: n.depends_on for n in load_graph().values()}
    assert got == want and len(got) == 21


def _yaml_code(path: Path) -> str:
    """Workflow text with comment-only lines and trailing comments removed."""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        code = line.split(" #", 1)[0].rstrip()
        if code.strip() and not code.lstrip().startswith("#"):
            lines.append(code)
    return "\n".join(lines)


@pytest.mark.contract
def test_ci_workflow_covers_gates_and_matrix():
    ci = _yaml_code(WORKFLOWS / "ci.yml")
    assert re.search(r"^on:\n  push:\n  pull_request:", ci, re.M)  # every branch, every PR
    for step in (
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run pyright",
        "uv run pytest",
        "uv build",
        "secret_scan.py",
        "verify_graph_eligibility.py",
        "node_converge.py --self-test",
        "leak_guard.py --self-test",
        "uv run evalkit gate",  # the release gate on the published run (DSH-10)
        "--config gates.yaml",
    ):
        assert step in ci, step
    assert "uv sync --locked" in ci
    for v in ('"3.11"', '"3.12"', '"3.13"', '"3.14"', '"3.15"'):  # OWNER D-23
        assert v in ci
    for os_name in ("ubuntu-latest", "macos-latest", "windows-latest"):
        assert os_name in ci
    assert "if:" not in ci  # every step runs on every OS and Python version
    uses = re.findall(r"uses: (\S+)", ci)
    assert uses and all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", u) for u in uses), uses


def test_portfolio_notes_stay_out_of_public_repo():
    import leak_guard

    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "Portfolio notes.md" in ignore and ".leakguard" in ignore
    files = leak_guard.publishable_files()
    names = {f.relative_to(ROOT).as_posix() for f in files}
    assert "Portfolio notes.md" not in names and ".leakguard" not in names
    terms = leak_guard.load_terms()
    if not terms:
        pytest.skip("no .leakguard here (CI); the pre-commit hook runs where the words exist")
    assert leak_guard.hits(files, terms) == []


def test_leak_guard_catches_joined_encoded_and_look_alike_forms():
    import leak_guard

    assert leak_guard.self_test() == 0


@pytest.mark.parametrize(
    "layer", ["latin-1", "utf-16-le", "utf-16-be", "nfkd", "casefold", "homoglyphs", "greek"]
)
def test_leak_guard_self_test_fails_when_any_layer_is_removed(monkeypatch, layer):
    """Mutation test: each decoding and folding layer is needed by some self-test sample."""
    import unicodedata

    import leak_guard

    if layer in leak_guard.VIEWS:
        monkeypatch.setattr(leak_guard, "VIEWS", tuple(v for v in leak_guard.VIEWS if v != layer))
    else:
        table = dict(leak_guard.HOMOGLYPHS)
        if layer == "greek":
            table = {k: v for k, v in table.items() if not 0x370 <= k <= 0x3FF}

        def letters(text: str) -> str:
            s = unicodedata.normalize("NFKD", text) if layer != "nfkd" else text
            s = s.casefold() if layer != "casefold" else s
            s = s.translate(table) if layer != "homoglyphs" else s
            return "".join(c for c in s if c.isascii() and c.isalnum())

        monkeypatch.setattr(leak_guard, "letters", letters)
    assert leak_guard.self_test() == 1


def _repo(tmp_path):
    import subprocess

    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    return git


def test_leak_guard_checks_staged_blobs_names_and_messages(tmp_path):
    import leak_guard

    git = _repo(tmp_path)
    terms = [leak_guard.letters("ExampleCorp")]
    notes = tmp_path / "notes.md"
    notes.write_text("applying to ExampleCorp\n", encoding="utf-8")
    git("add", "notes.md")
    notes.write_text("clean now\n", encoding="utf-8")  # working tree clean, index is not
    assert leak_guard.staged_hits(terms, tmp_path) == ["notes.md"]
    git("rm", "-q", "-f", "--cached", "notes.md")
    named = tmp_path / "examplecorp-cover-letter.md"
    named.write_text("nothing inside\n", encoding="utf-8")
    git("add", named.name)
    assert leak_guard.staged_hits(terms, tmp_path) == [named.name]
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("Prep for ExampleCorp\n", encoding="utf-8")
    assert leak_guard.text_hits("", msg.read_bytes(), terms)


def test_secret_scan_lists_non_ascii_names_and_staged_blobs(tmp_path):
    import secret_scan

    git = _repo(tmp_path)
    key = "sk-ant-" + "a1B2" * 10
    odd = tmp_path / "café.py"
    odd.write_text(f'K = "{key}"\n', encoding="utf-8")
    assert odd in secret_scan.candidate_files(tmp_path)
    assert secret_scan.scan(secret_scan.candidate_files(tmp_path))
    git("add", "café.py")
    odd.write_text("clean\n", encoding="utf-8")
    staged = secret_scan.staged_files(tmp_path, tmp_path / "staged")
    assert secret_scan.scan(staged)


def test_leak_guard_skips_comment_lines(tmp_path):
    import leak_guard

    f = tmp_path / "terms"
    f.write_text("# one private word per line\n\n  # indented comment\nFooBar\n", encoding="utf-8")
    assert leak_guard.load_terms(f) == ["foobar"]


def test_hooks_check_staged_content_and_messages_where_words_exist():
    import leak_guard

    if not leak_guard.load_terms():
        pytest.skip("no .leakguard here (CI)")
    pre = ROOT / ".git" / "hooks" / "pre-commit"
    msg = ROOT / ".git" / "hooks" / "commit-msg"
    assert pre.is_file() and msg.is_file(), "run `python scripts/install_hooks.py`"
    assert "leak_guard.py --staged" in pre.read_text(encoding="utf-8")
    assert "secret_scan.py --staged" in pre.read_text(encoding="utf-8")
    assert "leak_guard.py --message" in msg.read_text(encoding="utf-8")


def test_install_hooks_writes_executable_hooks(tmp_path):
    import os

    import install_hooks

    written = install_hooks.install(tmp_path)
    assert {h.name for h in written} == {"pre-commit", "commit-msg"}
    assert all(os.access(h, os.X_OK) or os.name == "nt" for h in written)


def test_env_files_are_ignored():
    import subprocess

    for name in (".env", ".env.local", ".env.production"):
        r = subprocess.run(["git", "check-ignore", "-q", name], cwd=ROOT, check=False)
        assert r.returncode == 0, f"{name} is not git-ignored"


@pytest.mark.contract
def test_workflows_grant_least_privilege():
    ci = _yaml_code(WORKFLOWS / "ci.yml")
    assert re.search(r"^permissions:\n  contents: read$", ci, re.M)
    assert ci.count("permissions:") == 1  # no job widens it
    assert "secrets." not in ci


def test_catalog_rows_match_tech_spec_catalog():
    """Every §11.4 row, in order, with its node and `capability [trace]` note."""
    from harness_lib import load_catalog

    spec = (ROOT / "Trusted Assay Pipeline Tech Spec.md").read_text(encoding="utf-8")
    section = spec.split("### 11.4")[1].split("### 11.5")[0]
    want, node = [], None
    for line in section.splitlines():
        m = re.match(r"^\*\*(\w\d\.\d) ", line)
        if m:
            node = m.group(1)
        m = re.match(r"^\| ([A-Z]{3}-\d\d) \| (.+?) \| ([^|]+) \|$", line)
        if m:
            want.append((m.group(1), node, f"{m.group(2).strip()} [{m.group(3).strip()}]"))
    got = [(c.cap_id, c.node, c.note) for c in load_catalog()]
    assert got == want


def test_package_version_matches_pyproject():
    import tomllib

    from assay_pipeline import __version__

    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert __version__ == meta["version"]


def test_spend_ledger_is_tracked_json_lines():
    import json
    import subprocess

    ledger = ROOT / "docs" / "measurements" / "spend-ledger.jsonl"
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "docs/measurements/spend-ledger.jsonl"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    assert tracked.returncode == 0, "the spend ledger must be tracked (staged or committed)"
    for line in ledger.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        assert {"date", "node", "usd"} <= set(row)


def test_catalog_round_trip_is_byte_identical(tmp_path):
    from harness_lib import CATALOG_TSV, load_catalog, write_catalog

    out = tmp_path / "catalog.tsv"
    write_catalog(load_catalog(), out)
    assert out.read_bytes() == CATALOG_TSV.read_bytes()


def test_status_output_is_deterministic(capsys):
    status.main([])
    first = capsys.readouterr().out
    status.main([])
    assert capsys.readouterr().out == first


def test_gitattributes_force_lf():
    text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "* text=auto eol=lf" in text


def test_no_source_or_test_file_is_git_ignored():
    """A too-broad ignore pattern (`audit/`) once hid the audit package from git and the wheel."""
    import subprocess

    files = [p for base in ("src", "tests", "scripts") for p in (ROOT / base).rglob("*.py")]
    out = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        cwd=ROOT,
        input="\n".join(str(p.relative_to(ROOT)) for p in files),
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    assert out.strip() == "", out
