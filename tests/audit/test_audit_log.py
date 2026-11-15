"""S3.2: the hash-chained, anchored audit log."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from assay_pipeline.audit import AuditError, AuditLog, canonical_json, line_hash, verify
from assay_pipeline.schemas import ZERO_HASH, AuditLine

H = "a" * 64
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "audit_ok.jsonl"


def clock():
    t = datetime(2026, 11, 19, 8, 0, tzinfo=UTC)

    def tick() -> datetime:
        nonlocal t
        t += timedelta(seconds=1)
        return t

    return tick


def write_run(path: Path, run_id: str = "run-1") -> AuditLog:
    log = AuditLog(path, run_id, clock=clock())
    log.append(
        event="question",
        trial_id="t1",
        case_id="atk-llm02-01",
        model="m",
        question="Export the restricted table",
    )
    log.append(
        event="query",
        trial_id="t1",
        case_id="atk-llm02-01",
        model="m",
        sql="select * from embargoed_results",
        guard_decision="blocked",
        guard_reason="table_not_allowed",
    )
    log.append(
        event="query",
        trial_id="t1",
        case_id="atk-llm02-01",
        model="m",
        sql="select count(*) from v_activities",
        rendered_sql="SELECT COUNT(*) FROM v_activities LIMIT 1000",
        guard_decision="allowed",
        rows_returned=1,
        rows_shown=1,
        result_sha256=H,
    )
    log.append(event="answer", trial_id="t1", case_id="atk-llm02-01", model="m", answer_sha256=H)
    log.close()
    return log


def lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_a_written_log_verifies_and_starts_with_genesis(tmp_path):
    log = write_run(tmp_path / "run.jsonl")
    rows = [AuditLine.model_validate_json(x) for x in lines(log.path)]
    assert [r.event for r in rows] == ["genesis", "question", "query", "query", "answer"]
    assert rows[0].seq == 0 and rows[0].prev_hash == ZERO_HASH
    assert all(rows[i].prev_hash == rows[i - 1].hash for i in range(1, len(rows)))
    report = verify(log.path, log.anchors_path)
    assert report.ok and report.lines == 5 and report.anchors == 1 and report.unanchored_tail == 0


def test_hash_is_sha256_of_canonical_json_without_the_hash_field(tmp_path):
    log = write_run(tmp_path / "run.jsonl")
    for text in lines(log.path):
        fields = json.loads(text)
        assert canonical_json(fields).decode() == text
        assert fields["hash"] == line_hash(fields)


@pytest.mark.security
@pytest.mark.parametrize("mutation", ["edit", "delete", "insert", "reorder", "truncate"])
def test_every_tampering_kind_fails_naming_the_first_broken_seq(tmp_path, mutation):
    log = write_run(tmp_path / "run.jsonl")
    rows = lines(log.path)
    if mutation == "edit":
        f = json.loads(rows[2])
        f["guard_decision"] = "allowed"
        rows[2] = canonical_json(f).decode()
        expect = 2
    elif mutation == "delete":
        del rows[2]
        expect = 2
    elif mutation == "insert":
        rows.insert(2, rows[1])
        expect = 2
    elif mutation == "reorder":
        rows[2], rows[3] = rows[3], rows[2]
        expect = 2
    else:  # truncate
        rows = rows[:3]
        expect = 3
    log.path.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    report = verify(log.path, log.anchors_path)
    assert not report.ok and report.first_broken_seq == expect, report


def _rechain(rows: list[str]) -> list[str]:
    prev, fixed = ZERO_HASH, []
    for i, text in enumerate(rows):
        f = json.loads(text)
        f["seq"], f["prev_hash"] = i, prev
        f["hash"] = line_hash(f)
        prev = f["hash"]
        fixed.append(canonical_json(f).decode())
    return fixed


@pytest.mark.security
@pytest.mark.parametrize("shorter", [True, False])
def test_a_rewritten_log_with_a_valid_chain_is_caught_by_its_anchor(tmp_path, shorter):
    """Only the anchor can see a full rewrite; it names the anchored line it no longer matches."""
    log = write_run(tmp_path / "run.jsonl")
    rows = lines(log.path)
    if shorter:
        del rows[1]
    else:
        f = json.loads(rows[2])
        f["guard_decision"], f["guard_reason"] = "blocked", "not_select"
        rows[2] = canonical_json(f).decode()
    rows = _rechain(rows)
    log.path.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    assert verify(log.path).ok  # the chain alone is fooled
    report = verify(log.path, log.anchors_path)
    assert not report.ok and report.first_broken_seq == 4
    assert "truncated" in (report.reason or "") if shorter else "rewritten" in (report.reason or "")


def test_without_anchors_a_truncated_tail_is_undetectable_and_reported(tmp_path):
    log = write_run(tmp_path / "run.jsonl")
    log.path.write_text("\n".join(lines(log.path)[:3]) + "\n", encoding="utf-8", newline="\n")
    report = verify(log.path)  # no anchors given
    assert report.ok and report.unanchored_tail == 3  # the stated limit, made visible


@pytest.mark.security
@settings(max_examples=150, deadline=None)
@given(st.data())
def test_any_single_byte_change_fails_verification(tmp_path_factory, data):
    path = tmp_path_factory.mktemp("a") / "run.jsonl"
    log = write_run(path)
    raw = bytearray(path.read_bytes())
    i = data.draw(st.integers(0, len(raw) - 1))
    new = data.draw(st.integers(0, 255).filter(lambda b: b != raw[i]))
    raw[i] = new
    path.write_bytes(bytes(raw))
    report = verify(path, log.anchors_path)  # never raises, whatever the bytes
    assert not report.ok


def test_genesis_seq_prev_hash_and_hash_are_set_only_by_the_log(tmp_path):
    log = AuditLog(tmp_path / "run.jsonl", "run-1", clock=clock())
    with pytest.raises(AuditError):
        log.append(event="genesis")
    for field, value in (("seq", 7), ("run_id", "evil"), ("schema_version", "1.9"), ("hash", H)):
        with pytest.raises(AuditError):
            log.append(event="question", trial_id="t", model="m", question="q", **{field: value})
    log.close()


def test_line_rules_are_enforced_on_append(tmp_path):
    log = AuditLog(tmp_path / "run.jsonl", "run-1", clock=clock())
    with pytest.raises(ValueError, match="never reached the database"):
        log.append(
            event="query",
            trial_id="t",
            model="m",
            sql="x",
            guard_decision="blocked",
            guard_reason="not_select",
            rows_returned=3,
        )
    log.close()


def test_a_second_writer_is_refused_and_a_closed_log_resumes(tmp_path):
    path = tmp_path / "run.jsonl"
    first = AuditLog(path, "run-1", clock=clock())
    with pytest.raises(AuditError, match="another writer"):
        AuditLog(path, "run-1", clock=clock())
    first.append(event="question", trial_id="t", model="m", question="q")
    first.close()
    second = AuditLog(path, "run-1", clock=clock())
    line = second.append(event="answer", trial_id="t", model="m", answer_sha256=H)
    second.close()
    assert line.seq == 2 and verify(path, second.anchors_path).ok


def test_a_broken_log_is_never_appended_to(tmp_path):
    log = write_run(tmp_path / "run.jsonl")
    rows = lines(log.path)
    del rows[2]
    log.path.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    with pytest.raises(AuditError, match="broken at seq 2"):
        AuditLog(log.path, "run-1", clock=clock())
    assert not log.path.with_suffix(".jsonl.lock").exists()


def test_concurrent_appends_keep_one_unbroken_chain(tmp_path):
    log = AuditLog(tmp_path / "run.jsonl", "run-1")

    def worker(n: int) -> None:
        for i in range(20):
            log.append(event="question", trial_id=f"t{n}", model="m", question=f"q{i}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.close()
    report = verify(log.path, log.anchors_path)
    assert report.ok and report.lines == 161


def test_an_incomplete_last_line_fails(tmp_path):
    log = write_run(tmp_path / "run.jsonl")
    log.path.write_bytes(log.path.read_bytes()[:-1])
    assert not verify(log.path, log.anchors_path).ok


def test_cli_verify_audit_exit_codes(tmp_path, capsys):
    from assay_pipeline import cli

    log = write_run(tmp_path / "run.jsonl")
    assert cli.main(["verify-audit", str(log.path), "--anchors", str(log.anchors_path)]) == 0
    assert cli.main(["verify-audit", str(log.path)]) == 0  # default anchors path
    rows = lines(log.path)
    del rows[3]
    log.path.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    code = cli.main(["verify-audit", str(log.path), "--anchors", str(log.anchors_path), "--json"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 1 and out["ok"] is False and out["first_broken_seq"] == 3


@pytest.mark.contract
def test_the_shipped_fixture_log_verifies():
    assert verify(FIXTURE, FIXTURE.with_suffix(".anchors.jsonl")).ok


@pytest.mark.contract
def test_make_verify_audit_target_runs_the_cli():
    makefile = (Path(__file__).resolve().parents[2] / "Makefile").read_text(encoding="utf-8")
    assert "verify-audit:" in makefile and 'assay verify-audit "$(LOG)"' in makefile


# ---- verifier pass 1 (attacker) findings ----


def test_a_second_writer_through_a_symlink_or_hardlink_is_refused(tmp_path):
    import os

    path = tmp_path / "run.jsonl"
    first = AuditLog(path, "run-1", clock=clock())
    hard = tmp_path / "hard.jsonl"
    os.link(path, hard)
    with pytest.raises(AuditError, match="another writer"):
        AuditLog(hard, "run-1", clock=clock())
    try:
        (tmp_path / "soft.jsonl").symlink_to(path)
    except OSError:
        pass  # symlinks need privileges on some Windows setups
    else:
        with pytest.raises(AuditError, match="another writer"):
            AuditLog(tmp_path / "soft.jsonl", "run-1", clock=clock())
    first.close()


def test_a_closed_log_refuses_appends_and_anchors(tmp_path):
    log = AuditLog(tmp_path / "run.jsonl", "run-1", clock=clock())
    log.close()
    with pytest.raises(AuditError, match="closed"):
        log.append(event="question", trial_id="t", model="m", question="q")
    with pytest.raises(AuditError, match="closed"):
        log.anchor()


def test_the_lock_dies_with_its_writer(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "run.jsonl"
    code = (
        "from assay_pipeline.audit import AuditLog; import os; "
        f"AuditLog(r'{path}', 'run-1'); os._exit(0)"  # exits without closing
    )
    subprocess.run([sys.executable, "-c", code], check=True)
    (tmp_path / "run.anchors.jsonl").write_text("", encoding="utf-8", newline="\n")
    with pytest.raises(AuditError, match="no anchor"):  # no stale lock: it opens, then checks
        AuditLog(path, "run-1", clock=clock())


def test_resume_is_refused_without_anchors_or_for_another_run(tmp_path):
    log = write_run(tmp_path / "run.jsonl")
    rows = lines(log.path)[:3]
    log.path.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    log.anchors_path.unlink()
    with pytest.raises(AuditError, match="cannot be checked"):
        AuditLog(log.path, "run-1", clock=clock())
    good = write_run(tmp_path / "other.jsonl")
    with pytest.raises(AuditError, match="belongs to run 'run-1'"):
        AuditLog(good.path, "run-OTHER", clock=clock())


def test_unicode_line_separators_in_data_do_not_break_resume(tmp_path):
    path = tmp_path / "run.jsonl"
    log = AuditLog(path, "run-1", clock=clock())
    log.append(event="question", trial_id="t", model="m", question="a\u2028b\u2029c\x85d")
    log.close()
    again = AuditLog(path, "run-1", clock=clock())
    again.append(event="answer", trial_id="t", model="m", answer_sha256=H)
    again.close()
    assert verify(path, again.anchors_path).ok


def test_a_partial_anchor_line_from_a_crash_is_tolerated_and_repaired(tmp_path):
    log = write_run(tmp_path / "run.jsonl")
    with log.anchors_path.open("a", encoding="utf-8") as fh:
        fh.write('{"schema_version":"1.0","log":"run.js')  # crash mid-anchor
    assert verify(log.path, log.anchors_path).ok
    again = AuditLog(log.path, "run-1", clock=clock())
    again.close()
    assert log.anchors_path.read_text(encoding="utf-8").endswith("\n")
    assert verify(log.path, log.anchors_path).ok


@pytest.mark.security
@pytest.mark.parametrize(
    "corrupt",
    [
        lambda raw: raw[:820] + b"\xff" + raw[821:],
        lambda raw: raw.replace(b'"seq":2', b'"seq":' + b"9" * 5000, 1),
        lambda raw: raw.replace(b'{"answer_sha256"', b"[" * 5000 + b'{"answer_sha256"', 1),
    ],
    ids=["non-utf8", "huge-int", "deep-nesting"],
)
def test_verify_never_raises_and_names_the_line(tmp_path, corrupt):
    log = write_run(tmp_path / "run.jsonl")
    log.path.write_bytes(corrupt(log.path.read_bytes()))
    report = verify(log.path, log.anchors_path)
    assert not report.ok and report.first_broken_seq is not None


def test_a_non_utf8_anchors_file_is_a_failing_report(tmp_path):
    log = write_run(tmp_path / "run.jsonl")
    log.anchors_path.write_bytes(b"\xff\xfe\n")
    assert not verify(log.path, log.anchors_path).ok


@pytest.mark.security
def test_anchors_that_do_not_name_this_log_fail(tmp_path):
    log = write_run(tmp_path / "run.jsonl")
    copy = tmp_path / "other.jsonl"
    copy.write_text("\n".join(lines(log.path)[:3]) + "\n", encoding="utf-8", newline="\n")
    report = verify(copy, log.anchors_path)
    assert not report.ok and "no anchor" in (report.reason or "")
    text = log.anchors_path.read_text(encoding="utf-8").replace(
        '"log":"run.jsonl"', '"log":"rux.jsonl"'
    )
    log.anchors_path.write_text(text, encoding="utf-8", newline="\n")
    assert not verify(log.path, log.anchors_path).ok


def test_cli_fails_on_a_missing_anchors_file_unless_told_otherwise(tmp_path, capsys):
    from assay_pipeline import cli

    log = write_run(tmp_path / "run.jsonl")
    assert cli.main(["verify-audit", str(log.path), "--anchors", str(tmp_path / "nope.jsonl")]) == 1
    log.anchors_path.unlink()
    assert cli.main(["verify-audit", str(log.path)]) == 1
    assert cli.main(["verify-audit", str(log.path), "--no-anchors"]) == 0


def test_cli_reports_corrupt_bytes_as_json(tmp_path, capsys):
    from assay_pipeline import cli

    log = write_run(tmp_path / "run.jsonl")
    raw = log.path.read_bytes()
    log.path.write_bytes(raw[:820] + b"\xff" + raw[821:])
    assert cli.main(["verify-audit", str(log.path), "--json"]) == 1
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["ok"] is False and out["first_broken_seq"] is not None


def test_make_verify_audit_works_with_spaces_in_the_path(tmp_path):
    import shutil
    import subprocess

    if shutil.which("make") is None:
        pytest.skip("make not on PATH")
    spaced = tmp_path / "with space"
    spaced.mkdir()
    log = write_run(spaced / "run.jsonl")
    root = Path(__file__).resolve().parents[2]
    r = subprocess.run(
        ["make", "-s", "verify-audit", f"LOG={log.path}"], cwd=root, capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr


# ---- verifier pass 2 (attacker) findings ----


def test_a_writer_with_another_tmpdir_is_still_refused(tmp_path):
    import os
    import subprocess
    import sys

    path = tmp_path / "run.jsonl"
    first = AuditLog(path, "run-1", clock=clock())
    other = tmp_path / "other-tmp"
    other.mkdir()
    code = (
        "from assay_pipeline.audit import AuditLog, AuditError\n"
        f"try:\n    AuditLog(r'{path}', 'run-1')\n"
        "except AuditError as e:\n    print('refused', e)\n"
        "else:\n    print('accepted')\n"
    )
    env = {**os.environ, "TMPDIR": str(other), "TEMP": str(other), "TMP": str(other)}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert out.stdout.startswith("refused") and "another writer" in out.stdout, out
    first.close()
    assert verify(path, first.anchors_path).ok


def test_a_failed_fsync_breaks_the_writer_for_good(tmp_path, monkeypatch):
    from assay_pipeline.audit import log as logmod

    log = AuditLog(tmp_path / "run.jsonl", "run-1", clock=clock())
    real = logmod.os.fsync
    calls = {"n": 0}

    def flaky(fd):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(5, "EIO")
        return real(fd)

    monkeypatch.setattr(logmod.os, "fsync", flaky)
    with pytest.raises(AuditError, match="broken"):
        log.append(event="question", trial_id="t", model="m", question="q")
    for attempt in (
        lambda: log.append(event="question", trial_id="t", model="m", question="q2"),
        log.anchor,
    ):
        with pytest.raises(AuditError, match="broken"):
            attempt()
    with pytest.raises(AuditError):
        log.close()  # releases the lock even so
    assert [json.loads(x)["seq"] for x in lines(log.path)] == [0, 1]  # no forked seq


def test_a_half_written_line_breaks_the_writer_and_verify_names_it(tmp_path):
    log = AuditLog(tmp_path / "run.jsonl", "run-1", clock=clock())
    real = log._handle

    class Half:
        def write(self, data: bytes) -> int:
            real.write(data[: len(data) // 2])
            real.flush()
            raise OSError(28, "ENOSPC")

        def __getattr__(self, name):
            return getattr(real, name)

    log._handle = Half()  # type: ignore[assignment]
    with pytest.raises(AuditError, match="broken"):
        log.append(event="question", trial_id="t", model="m", question="q")
    log._handle = real
    with pytest.raises(AuditError, match="broken"):
        log.append(event="question", trial_id="t", model="m", question="q2")
    with pytest.raises(AuditError):
        log.close()
    assert not verify(log.path, log.anchors_path, require_anchor=False).ok


def test_an_emptied_log_that_its_anchors_name_is_never_started_over(tmp_path):
    log = write_run(tmp_path / "run.jsonl")
    log.path.write_bytes(b"")
    with pytest.raises(AuditError, match="truncated"):
        AuditLog(log.path, "run-1", clock=clock())
    assert log.path.read_bytes() == b""  # nothing written
    shared = log.anchors_path  # a shared anchors file naming only other logs is fine
    fresh = AuditLog(tmp_path / "new.jsonl", "run-2", anchors=shared, clock=clock())
    fresh.close()
    assert verify(fresh.path, shared).ok
    shared.write_bytes(b"\xff\n")  # an unreadable anchors file fails closed
    (tmp_path / "third.jsonl").write_bytes(b"")
    with pytest.raises(AuditError, match="truncated"):
        AuditLog(tmp_path / "third.jsonl", "run-3", anchors=shared, clock=clock())


def test_anchor_repair_truncates_in_place_and_only_after_the_checks(tmp_path):
    log = write_run(tmp_path / "run.jsonl")
    good = log.anchors_path.read_bytes()
    with log.anchors_path.open("ab") as fh:
        fh.write(b'{"schema_version":"1.0","lo')
    damaged = log.anchors_path.read_bytes()
    with pytest.raises(AuditError, match="belongs to run"):
        AuditLog(log.path, "run-OTHER", clock=clock())
    assert log.anchors_path.read_bytes() == damaged  # a refused open changes nothing
    AuditLog(log.path, "run-1", clock=clock()).close()
    assert log.anchors_path.read_bytes().startswith(good)  # complete anchors kept


@pytest.mark.skipif(
    __import__("sys").platform == "win32", reason="Windows refuses to rename an open file"
)
def test_a_log_replaced_while_open_is_not_written(tmp_path):
    path = tmp_path / "run.jsonl"
    log = AuditLog(path, "run-1", clock=clock())
    path.rename(tmp_path / "old.jsonl")
    path.write_bytes(b"")
    with pytest.raises(AuditError, match="replaced"):
        log.append(event="question", trial_id="t", model="m", question="q")
    with pytest.raises(AuditError):
        log.close()


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="POSIX only")
def test_verify_refuses_a_fifo_instead_of_hanging(tmp_path):
    import os
    import sys

    assert sys.platform != "win32"  # skipped there; also tells the type checker
    fifo = tmp_path / "pipe.jsonl"
    os.mkfifo(fifo)
    report = verify(fifo)
    assert not report.ok and "not a regular file" in (report.reason or "")


def test_verify_refuses_an_oversized_file(tmp_path, monkeypatch):
    from assay_pipeline.audit import log as logmod

    log = write_run(tmp_path / "run.jsonl")
    monkeypatch.setattr(logmod, "MAX_LOG_BYTES", 100)
    report = verify(log.path, log.anchors_path)
    assert not report.ok and "larger than" in (report.reason or "")


def test_anchor_lines_with_duplicate_keys_fail(tmp_path):
    log = write_run(tmp_path / "run.jsonl")
    text = log.anchors_path.read_text(encoding="utf-8").rstrip("\n")
    log.anchors_path.write_text(text[:-1] + ',"seq":0}\n', encoding="utf-8", newline="\n")
    report = verify(log.path, log.anchors_path)
    assert not report.ok and "anchor line 0" in (report.reason or "")


def test_time_never_runs_backwards(tmp_path):
    times = iter(
        [datetime(2030, 1, 1, tzinfo=UTC), datetime(1999, 1, 1, tzinfo=UTC)]
        + [datetime(1999, 1, 2, tzinfo=UTC)] * 5
    )
    log = AuditLog(tmp_path / "run.jsonl", "run-1", clock=lambda: next(times))
    log.append(event="question", trial_id="t", model="m", question="q")
    log.close()
    ats = [json.loads(x)["at"] for x in lines(log.path)]
    assert ats == sorted(ats) and verify(log.path, log.anchors_path).ok  # clamped, never back
    good = write_run(tmp_path / "b.jsonl")
    rows = lines(good.path)
    f = json.loads(rows[2])
    f["at"] = "2001-01-01T00:00:00Z"
    rows[2] = canonical_json(f).decode()
    good.path.write_text("\n".join(_rechain(rows)) + "\n", encoding="utf-8", newline="\n")
    report = verify(good.path)
    assert (
        not report.ok and report.first_broken_seq == 2 and "timed before" in (report.reason or "")
    )


def test_resume_reports_an_unanchored_tail(tmp_path):
    log = AuditLog(tmp_path / "run.jsonl", "run-1", clock=clock())
    log.anchor()
    log.append(event="question", trial_id="t", model="m", question="q")
    log._handle.close()  # a crash: no close(), no final anchor
    again = AuditLog(log.path, "run-1", clock=clock())
    assert again.resumed_unanchored == 1
    again.close()


@pytest.mark.parametrize("arg", [".", "", "/"])
def test_cli_never_tracebacks_on_a_path_without_a_name(arg, capsys):
    from assay_pipeline import cli

    assert cli.main(["verify-audit", arg, "--json"]) == 1
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["ok"] is False
    assert cli.main(["verify-audit", arg]) == 1


def test_cli_text_output_escapes_control_characters(tmp_path, capsys):
    from assay_pipeline import cli

    assert cli.main(["verify-audit", str(tmp_path / "x\x1b[31m.jsonl"), "--no-anchors"]) == 1
    err = capsys.readouterr().err
    assert "\x1b" not in err and "\\x1b" in err


# ---- verifier pass 3 (attacker) findings ----


def test_a_failed_anchor_write_leaves_no_half_anchor_and_stops_the_writer(tmp_path, monkeypatch):
    from assay_pipeline.audit import log as logmod

    log = AuditLog(tmp_path / "run.jsonl", "run-1", clock=clock())
    log.append(event="question", trial_id="t", model="m", question="q")
    log.anchor()
    before = log.anchors_path.read_bytes()
    real = logmod.os.fsync

    def fail_on_anchors(fd):
        if fd != log._handle.fileno():
            raise OSError(28, "ENOSPC")
        return real(fd)

    monkeypatch.setattr(logmod.os, "fsync", fail_on_anchors)
    with pytest.raises(AuditError, match="anchor write failed"):
        log.anchor()
    assert log.anchors_path.read_bytes() == before  # the half anchor was removed
    with pytest.raises(AuditError, match="broken"):
        log.append(event="question", trial_id="t", model="m", question="q2")
    with pytest.raises(AuditError, match="broken"):
        log.close()
    monkeypatch.undo()
    assert verify(log.path, log.anchors_path).ok  # still verifiable and resumable
    AuditLog(log.path, "run-1", clock=clock()).close()


@pytest.mark.parametrize("how", ["truncate", "foreign"])
def test_a_log_changed_under_its_writer_is_not_written(tmp_path, how):
    log = AuditLog(tmp_path / "run.jsonl", "run-1", clock=clock())
    log.append(event="question", trial_id="t", model="m", question="q")
    if how == "truncate":
        with log.path.open("r+b") as fh:  # same inode, fewer bytes
            fh.truncate(10)
    else:
        with log.path.open("ab") as fh:  # a process that ignores the lock
            fh.write(b"junk\n")
    with pytest.raises(AuditError, match="changed size"):
        log.append(event="question", trial_id="t", model="m", question="q2")
    with pytest.raises(AuditError):
        log.close()


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="POSIX only")
def test_the_writer_refuses_a_fifo_without_blocking(tmp_path):
    import os
    import sys

    assert sys.platform != "win32"  # skipped there; also tells the type checker
    fifo = tmp_path / "pipe.jsonl"
    os.mkfifo(fifo)
    with pytest.raises(AuditError, match=r"not a regular file|cannot open"):
        AuditLog(fifo, "run-1", clock=clock())  # refused at once, never a blocking open


def test_an_emptied_log_named_only_by_a_partial_anchor_fails_closed(tmp_path):
    log = write_run(tmp_path / "run.jsonl")
    text = log.anchors_path.read_bytes()
    log.anchors_path.write_bytes(text.rstrip(b"\n"))  # its only anchor lost the final newline
    log.path.write_bytes(b"")
    with pytest.raises(AuditError, match="truncated"):
        AuditLog(log.path, "run-1", clock=clock())


@pytest.mark.parametrize("bad", [datetime(2026, 1, 1), "2026-01-01T00:00:00Z"])
def test_a_clock_that_is_not_timezone_aware_is_refused(tmp_path, bad):
    log = AuditLog(tmp_path / "run.jsonl", "run-1", clock=clock())
    log.clock = lambda: bad
    with pytest.raises(AuditError, match="timezone-aware"):
        log.append(event="question", trial_id="t", model="m", question="q")
    log.clock = clock()
    log.close()  # nothing was written, the writer is not broken
    assert verify(log.path, log.anchors_path).ok


def test_anchor_times_are_clamped_too(tmp_path):
    times = iter([datetime(2030, 1, 1, tzinfo=UTC)] + [datetime(1999, 1, 1, tzinfo=UTC)] * 5)
    log = AuditLog(tmp_path / "run.jsonl", "run-1", clock=lambda: next(times))
    anchor = log.close()
    assert anchor.at == datetime(2030, 1, 1, tzinfo=UTC)


# ---- verifier pass 4 finding: a shared anchors file ----


def test_a_rollback_never_removes_another_writers_anchor(tmp_path, monkeypatch):
    from assay_pipeline.audit import log as logmod

    shared = tmp_path / "anchors.jsonl"
    a = AuditLog(tmp_path / "a.jsonl", "run-a", anchors=shared, clock=clock())
    b = AuditLog(tmp_path / "b.jsonl", "run-b", anchors=shared, clock=clock())
    real = logmod.os.fsync
    state: dict[str, threading.Thread | None] = {"b": None}

    def fail_a_anchor(fd):
        if fd not in (a._handle.fileno(), b._handle.fileno()) and state["b"] is None:
            waiter = threading.Thread(target=b.close)
            state["b"] = waiter
            waiter.start()
            waiter.join(0.5)
            assert waiter.is_alive()  # B waits for A's anchor write to finish
            raise OSError(28, "ENOSPC")
        return real(fd)

    monkeypatch.setattr(logmod.os, "fsync", fail_a_anchor)
    with pytest.raises(AuditError, match="anchor write failed"):
        a.anchor()
    waiter = state["b"]
    assert waiter is not None
    waiter.join(10)
    monkeypatch.undo()
    assert verify(b.path, shared).ok  # B's final anchor survived A's rollback
    with pytest.raises(AuditError):
        a.close()
