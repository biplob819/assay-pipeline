"""B5.3: the release surface — README, published run, data card, docs and community files."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _bundle() -> tuple[str, dict]:
    rel = _read("docs/results/LATEST").strip()
    return rel, json.loads(_read(f"docs/results/{rel}"))


def test_the_readme_results_are_the_published_bundles():
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/readme_results.py"), "--check"],
                          capture_output=True, text=True, cwd=ROOT)  # fmt: skip
    assert proc.returncode == 0, "README results are stale: run scripts/readme_results.py"
    readme = _read("README.md")
    assert "```mermaid" in readme  # one diagram
    rebuild = readme.split("## Rebuild", 1)[1].split("```bash", 1)[1].split("```", 1)[0]
    assert len([ln for ln in rebuild.splitlines() if ln.strip()]) == 3


PUBLISHED_MD = ("*.md", "docs/**/*.md", ".github/**/*.md", "tests/fixtures/**/README.md")


def _published_md() -> list[str]:
    found = {p.relative_to(ROOT) for g in PUBLISHED_MD for p in ROOT.glob(g)}
    ignored = subprocess.run(["git", "check-ignore", "--stdin"], input="\n".join(map(str, found)),
                             capture_output=True, text=True, cwd=ROOT).stdout.split()  # fmt: skip
    return sorted(str(p) for p in found if str(p) not in set(ignored))


def test_every_relative_link_in_the_readme_and_docs_resolves():
    docs = _published_md()
    assert {"README.md", "CODE_OF_CONDUCT.md", "docs/data/2026-11-15.1/DATA_CARD.md"} <= set(docs)
    for doc in docs:
        text = _read(doc)
        base = (ROOT / doc).parent
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
            if re.match(r"[a-z]+:", target):
                continue
            path = (base / target.replace("%20", " ")).resolve()
            assert path.exists(), f"{doc}: broken link {target}"


def test_the_published_run_carries_anchors_and_a_passing_verify_result():
    rel, bundle = _bundle()
    run_dir = ROOT / "docs/results" / Path(rel).parent
    verify = json.loads((run_dir / "verify.json").read_text(encoding="utf-8"))
    main = verify["logs"]["audit.jsonl"]
    assert main["ok"] and main["unanchored_tail"] == 0
    assert (
        main["lines"] == bundle["audit"]["lines"] and main["anchors"] == bundle["audit"]["anchors"]
    )
    assert main["last_anchor"]["hash"] == bundle["audit"]["latest_anchor"]["hash"]
    from hashlib import sha256

    for log, rec in verify["logs"].items():
        raw = (run_dir / rec["anchors_file"]).read_bytes()
        assert sha256(raw).hexdigest() == rec["anchors_sha256"]  # the file verify.json checked
        anchors = raw.decode("utf-8").splitlines()
        assert len(anchors) == rec["anchors"]
        rows = [json.loads(a) for a in anchors]
        for r in rows:  # hashes only, on every line
            assert set(r) == {"schema_version", "log", "seq", "hash", "at"} and r["log"] == log
            assert re.fullmatch(r"[0-9a-f]{64}", r["hash"])
        assert [r["seq"] for r in rows] == sorted(r["seq"] for r in rows)
        last = rows[-1]
        assert (last["seq"], last["hash"]) == (
            rec["last_anchor"]["seq"],
            rec["last_anchor"]["hash"],
        )
        assert last["log"] == log


def test_the_published_data_card_matches_the_bundle_and_carries_attribution():
    _, bundle = _bundle()
    folder = ROOT / "docs/data" / bundle["snapshot"]
    card = (folder / "DATA_CARD.md").read_text(encoding="utf-8")
    lic = (folder / "DATA_LICENSE.md").read_text(encoding="utf-8")
    for source, n in bundle["quality"]["rows_by_source"].items():
        assert re.search(rf"\| {source} \| \d+ \| {n} \|", card), source
    for reason, n in bundle["quality"]["drops_by_reason"].items():
        assert re.search(rf"\| {reason} \| {n} \|", card), reason
    for rule, n in bundle["quality"]["flags_by_rule"].items():
        assert re.search(rf"\| {rule} \| .* \| {n} \|", card), rule
    assert "CC BY-SA 3.0" in lic and "ChEMBL" in lic and "PubChem" in lic and "BindingDB" in lic


def test_the_privacy_statement_says_no_telemetry_and_names_every_destination():
    for doc in ("README.md", "SECURITY.md"):
        text = _read(doc)
        assert "No telemetry" in text
        assert all(x in text for x in ("ChEMBL", "PubChem", "model providers"))


def test_community_files_and_templates_exist():
    assert "private" in _read("SECURITY.md").lower() and "never prints" in _read("SECURITY.md")
    assert "Contributor Covenant" in _read("CODE_OF_CONDUCT.md")
    assert "node_converge.py" in _read("CONTRIBUTING.md")
    for t in (".github/ISSUE_TEMPLATE/bug_report.md", ".github/ISSUE_TEMPLATE/feature_request.md",
              ".github/PULL_REQUEST_TEMPLATE.md"):  # fmt: skip
        assert (ROOT / t).is_file()
    assert "doctor --offline --json" in _read(".github/ISSUE_TEMPLATE/bug_report.md")


def test_every_assay_command_and_flag_in_the_docs_exists():
    from assay_pipeline.cli import build_parser

    sub = next(a for a in build_parser()._actions if a.__class__.__name__ == "_SubParsersAction")
    seen = 0
    for doc in _published_md():
        for m in re.finditer(r"\bassay ([a-z][a-z-]+)((?: +[^\s`|#;&)]+)*)", _read(doc)):
            cmd, rest = m.group(1), m.group(2)
            if cmd not in sub.choices:  # type: ignore[operator]
                assert cmd in {"pipeline", "doctor"} or not rest.strip().startswith("-"), (doc, cmd)
                continue
            opts = {o for a in sub.choices[cmd]._actions for o in a.option_strings}  # type: ignore[index]
            for flag in re.findall(r"(?<!\S)(--[a-z][a-z-]*)", rest):
                assert flag in opts, f"{doc}: assay {cmd} has no {flag}"
            seen += 1
    assert seen >= 20


def test_the_user_docs_cover_the_four_topics():
    assert "no API keys" in _read("docs/quickstart.md")
    assert "chembl_target_id" in _read("docs/configuring-a-target.md")
    assert "$" in _read("docs/cost.md")
    assert "upper bound" in _read("docs/reading-the-dashboard.md")


def test_every_command_takes_json():
    from assay_pipeline.cli import build_parser

    parser = build_parser()
    sub = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    for name, p in sub.choices.items():  # type: ignore[union-attr]
        assert any("--json" in a.option_strings for a in p._actions), name


def test_the_release_workflow_publishes_by_trusted_publishing_only():
    import yaml

    wf = yaml.safe_load(_read(".github/workflows/release.yml"))
    on = wf.get("on", wf.get(True))
    assert on == {"push": {"tags": ["v*"]}} and wf["permissions"] == {}
    jobs = wf["jobs"]
    assert jobs["publish"]["permissions"] == {"id-token": "write"}
    assert jobs["publish"]["environment"]["name"] == "pypi"
    for name, job in jobs.items():  # OIDC only where it publishes; no stored PyPI token anywhere
        assert name == "publish" or "id-token" not in job.get("permissions", {})
    text = _read(".github/workflows/release.yml")
    assert "password" not in text and "PYPI_API_TOKEN" not in text
    assert all(re.search(r"@[0-9a-f]{40} #", ln) for ln in text.splitlines() if "uses:" in ln)
    assert set(jobs["smoke"]["strategy"]["matrix"]["os"]) == {"ubuntu-latest", "macos-latest",
                                                               "windows-latest"}  # fmt: skip
    assert "uvx" in text and "assay doctor" in text and 'test "v$v" = "$GITHUB_REF_NAME"' in text


def test_the_hand_read_names_public_cases_only():
    from assay_pipeline.attack.suite import load_suite

    suite = load_suite()  # the packaged, public suite
    public = {c.id for c in [*suite.attacks, *suite.questions] if c.split == "public"}
    text = _read("docs/measurements/hand-read-main-2026-11-16.md")
    named = set(re.findall(r"\b(?:atk-llm\d\d-\d\d|nq-\d\d)\b", text))
    assert named and named <= public, named - public
