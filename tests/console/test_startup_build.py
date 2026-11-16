"""DEM-04 (OWNER D-39): a host with no file storage builds the console's database at startup
from the published snapshot committed in docs/data, with a random seed, so the deployed
console's canaries cannot be derived from the public code and snapshot. No test calls a model."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from assay_pipeline.agent.prompt import prompt_canary
from assay_pipeline.console.page import Settings, SettingsError, build_console
from assay_pipeline.pipeline.agentdb import canaries_for
from assay_pipeline.pipeline.snapshot import read_snapshot

ROOT = Path(__file__).resolve().parents[2]
# LATEST names the bundle file itself
BUNDLE = ROOT / "docs/results" / (ROOT / "docs/results/LATEST").read_text(encoding="utf-8").strip()
PUBLISHED = ROOT / "docs/data/2026-11-15.1"


def _manifest_sha(folder: Path) -> str:
    return hashlib.sha256((folder / "manifest.json").read_bytes()).hexdigest()


def test_the_committed_snapshot_is_the_published_benchmarks_and_verifies():
    snap = read_snapshot(PUBLISHED)  # hashes, counts and integrity
    bundle = json.loads(BUNDLE.read_text(encoding="utf-8"))
    assert snap.manifest.snapshot == "2026-11-15.1" and len(snap.activities) == 41_463
    assert _manifest_sha(PUBLISHED) == bundle["manifest_sha256"]


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"ASSAY_CONSOLE_DB": str(tmp_path / "none.duckdb"),
            "ASSAY_CONSOLE_SNAPSHOT": str(PUBLISHED), **extra}  # fmt: skip


def test_the_console_builds_its_database_with_a_private_seed(tmp_path):
    a = build_console(Settings.from_env(_env(tmp_path)), tmp_path / "a")
    b = build_console(Settings.from_env(_env(tmp_path)), tmp_path / "b")
    public = canaries_for("2026-11-15.1", 0)
    assert a.db.snapshot == "2026-11-15.1" and a.db.manifest_sha256 == _manifest_sha(PUBLISHED)
    assert not set(a.db.canaries) & set(public)  # not the seed-0 canaries anyone can compute
    assert a.db.canaries != b.db.canaries  # a fresh seed per start
    assert a.db.seed > 2**64 and a.db.seed != b.db.seed  # 128 bits: not found by trying seeds
    assert prompt_canary("2026-11-15.1", a.db.seed) in a.system  # the prompt uses the DB's seed
    assert prompt_canary("2026-11-15.1", 0) not in a.system
    r = a.run_case(sample_id="guard-restricted")  # and replay works on the built database
    assert [d["decision"] for d in r.decisions] == ["blocked"]
    assert a.audit.verify_chain()


def test_a_relative_snapshot_path_is_the_repositorys_whatever_the_working_directory(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)  # a host may start the app anywhere
    env = {"ASSAY_CONSOLE_SNAPSHOT": "docs/data/2026-11-15.1"}
    assert Settings.from_env(env, base=ROOT).snapshot == PUBLISHED
    assert Settings.from_env(
        {"ASSAY_CONSOLE_SNAPSHOT": str(PUBLISHED)}, base=tmp_path
    ).snapshot == (PUBLISHED)  # an absolute path is kept
    assert Settings.from_env(env).snapshot == Path("docs/data/2026-11-15.1")  # no base: as given


def test_a_configured_database_wins_over_the_snapshot(tmp_path):
    built = build_console(Settings.from_env(_env(tmp_path)), tmp_path / "a")
    env = _env(tmp_path, ASSAY_CONSOLE_DB=str(built.db.path))
    again = build_console(Settings.from_env(env), tmp_path / "b")
    assert again.db.path == built.db.path and again.db.canaries == built.db.canaries
    assert again.db.seed == built.db.seed != 0  # a supplied database keeps its own seed
    assert prompt_canary("2026-11-15.1", built.db.seed) in again.system


def test_a_damaged_snapshot_or_none_at_all_is_a_clean_settings_error(tmp_path):
    bad = tmp_path / "snap"
    shutil.copytree(PUBLISHED, bad)
    data = bytearray((bad / "drops.parquet").read_bytes())
    data[len(data) // 2] ^= 0xFF
    (bad / "drops.parquet").write_bytes(bytes(data))
    with pytest.raises(SettingsError, match="integrity check") as err:
        build_console(
            Settings.from_env(_env(tmp_path, ASSAY_CONSOLE_SNAPSHOT=str(bad))), tmp_path / "w"
        )
    assert str(tmp_path) not in str(err.value)
    with pytest.raises(SettingsError, match="ASSAY_CONSOLE_SNAPSHOT"):
        build_console(
            Settings.from_env({"ASSAY_CONSOLE_DB": str(tmp_path / "none")}), tmp_path / "x"
        )


def test_the_page_starts_from_the_snapshot_alone(tmp_path, monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    st.cache_resource.clear()
    for k in ("ASSAY_CONSOLE_PASSCODE", "ASSAY_CONSOLE_DATABASE_URL", "ASSAY_CONSOLE_DAILY_CAP",
              "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):  # fmt: skip
        monkeypatch.delenv(k, raising=False)
    env = _env(tmp_path, ASSAY_CONSOLE_WORKDIR=str(tmp_path / "w"))
    env["ASSAY_CONSOLE_SNAPSHOT"] = "docs/data/2026-11-15.1"  # relative, as on Community Cloud
    monkeypatch.chdir(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    at = AppTest.from_file(str(ROOT / "apps/console/streamlit_app.py"), default_timeout=120)
    at.run()
    assert not at.exception and not at.error
    at.button(key="replay").click().run()
    assert not at.exception and not at.error and at.dataframe


def test_a_failed_start_leaves_no_temp_folder(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    bad = tmp_path / "snap"
    shutil.copytree(PUBLISHED, bad)
    (bad / "drops.parquet").write_bytes(b"not parquet")
    env = {"ASSAY_CONSOLE_DB": str(tmp_path / "none"), "ASSAY_CONSOLE_SNAPSHOT": str(bad)}
    for _ in range(3):
        with pytest.raises(SettingsError):
            build_console(Settings.from_env(env))  # no workdir: it makes, then removes, its own
    assert list((tmp_path / "tmp").iterdir()) == []


def test_the_page_caches_a_failed_start_instead_of_retrying_every_rerun(tmp_path, monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    import assay_pipeline.console.page as page

    calls = []

    def failing(settings, workdir=None):
        calls.append(1)
        raise SettingsError("The published snapshot failed its integrity check.")

    monkeypatch.setattr(page, "build_console", failing)
    st.cache_resource.clear()
    for k in ("ASSAY_CONSOLE_PASSCODE", "ASSAY_CONSOLE_DATABASE_URL", "ASSAY_CONSOLE_SNAPSHOT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ASSAY_CONSOLE_DB", str(tmp_path / "none.duckdb"))
    at = AppTest.from_file(str(ROOT / "apps/console/streamlit_app.py"), default_timeout=60)
    at.run()
    at.run()
    assert not at.exception and "integrity check" in at.error[0].value
    assert calls == [1]


def test_the_deploy_requirements_pin_the_versions_ci_tested():
    """Community Cloud installs from requirements.txt and ignores uv.lock, so the file pins the
    locked versions; regenerate it with the command in its header when uv.lock changes."""
    import subprocess

    if shutil.which("uv") is None:
        pytest.skip("uv is not installed")
    out = subprocess.run(
        ["uv", "export", "--frozen", "--extra", "console", "--no-dev", "--no-hashes",
         "--no-emit-project", "--no-header"],
        capture_output=True, text=True, check=True, cwd=ROOT,
    ).stdout  # fmt: skip
    want = {ln.strip() for ln in out.splitlines() if ln.strip() and not ln.lstrip().startswith("#")}
    reqs = (ROOT / "apps/console/requirements.txt").read_text(encoding="utf-8").splitlines()
    have = {ln.strip() for ln in reqs if ln.strip() and not ln.startswith("#")}
    assert have == want | {".[console]"}
