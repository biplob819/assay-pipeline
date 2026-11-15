"""B5.3 OSS-02: `assay doctor` reports readiness and never shows a key's value."""

from __future__ import annotations

import json

from assay_pipeline.doctor import SOURCES, as_json, run_checks

SECRET = "fake-key-value-not-a-real-key-0123456789"


def _checks(tmp_path, **kw):
    base = {"cache": tmp_path / "cache", "snapshots": tmp_path / "snapshots",
            "db": tmp_path / "agent.duckdb", "offline": True, "env": {}}  # fmt: skip
    return run_checks(**{**base, **kw})


def test_keys_are_reported_as_set_or_not_set_never_by_value(tmp_path):
    checks = _checks(tmp_path, env={"ANTHROPIC_API_KEY": SECRET, "OPENAI_API_KEY": "  "})
    by = {c.name: c for c in checks}
    assert by["ANTHROPIC_API_KEY"].status == "ok" and by["ANTHROPIC_API_KEY"].detail.startswith(
        "set"
    )
    assert by["OPENAI_API_KEY"].status == "warn" and "not set" in by["OPENAI_API_KEY"].detail
    assert SECRET not in json.dumps(as_json(checks))


def test_offline_skips_the_network_and_a_fresh_machine_only_warns(tmp_path):
    calls = []
    checks = _checks(tmp_path, reach=lambda u: calls.append(u))
    assert calls == []
    report = as_json(checks)
    assert report["ok"] is True  # nothing built yet and no keys: warnings, not failures
    assert {c.status for c in checks} <= {"ok", "warn"}


def test_network_failures_and_unsupported_python_fail(tmp_path):
    checks = _checks(tmp_path, offline=False, version=(3, 10),
                     reach=lambda u: None if "ebi.ac.uk" in u else "ConnectError")  # fmt: skip
    by = {c.name: c for c in checks}
    assert by["python"].status == "fail" and "3.11 to 3.13" in by["python"].detail
    assert by["network ChEMBL"].status == "ok"
    assert by["network PubChem"].status == "fail" and "ConnectError" in by["network PubChem"].detail
    assert as_json(checks)["ok"] is False
    assert len(SOURCES) == 2


def test_local_state_is_found(tmp_path):
    (tmp_path / "cache" / "chembl").mkdir(parents=True)
    (tmp_path / "snapshots" / "2026-11-15.1").mkdir(parents=True)
    (tmp_path / "snapshots" / "2026-11-15.1" / "manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "agent.duckdb").write_bytes(b"")
    (tmp_path / "agent.json").write_text("{}", encoding="utf-8")
    by = {c.name: c for c in _checks(tmp_path)}
    assert by["cache"].status == "ok" and "chembl" in by["cache"].detail
    assert by["snapshots"].status == "ok" and "2026-11-15.1" in by["snapshots"].detail
    assert by["agent database"].status == "ok"


def test_cli_doctor_json_and_exit_code(tmp_path, capsys, monkeypatch):
    from assay_pipeline.cli import main

    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    rc = main(["doctor", "--offline", "--json", "--cache", str(tmp_path / "c"),
               "--snapshots", str(tmp_path / "s"), "--db", str(tmp_path / "a.duckdb")])  # fmt: skip
    out = capsys.readouterr().out
    assert rc == 0 and json.loads(out)["ok"] is True and SECRET not in out
    rc = main(["doctor", "--offline", "--cache", str(tmp_path / "c")])
    text = capsys.readouterr().out
    assert rc == 0 and "ANTHROPIC_API_KEY" in text and SECRET not in text


def test_cli_doctor_exits_1_on_any_failure(tmp_path, capsys, monkeypatch):
    import assay_pipeline.doctor as doctor
    from assay_pipeline.cli import main

    monkeypatch.setattr(doctor, "SUPPORTED", ())  # this Python now counts as unsupported
    rc = main(["doctor", "--offline", "--json", "--cache", str(tmp_path / "c")])
    report = json.loads(capsys.readouterr().out)
    assert rc == 1 and report["ok"] is False
    assert any(c["name"] == "python" and c["status"] == "fail" for c in report["checks"])


def test_the_report_names_the_version_and_platform(tmp_path):
    from assay_pipeline import __version__

    first = _checks(tmp_path)[0]
    assert first.name == "assay" and __version__ in first.detail
