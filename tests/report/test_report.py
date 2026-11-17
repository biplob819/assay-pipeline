"""B5.1: the results bundle and the CISO dashboard (DSH-01..06). No model is called."""

from __future__ import annotations

import json
import re

import pytest

from assay_pipeline.attack.assess import Assessment, assess
from assay_pipeline.attack.run import run
from assay_pipeline.cli import main
from assay_pipeline.report.collect import ReportError, collect
from assay_pipeline.report.html import render
from tests.security.test_behaviour import env  # noqa: F401  (fixture)
from tests.security.test_run import COUNT, LLM02, SUITE, VIEWS, plan, scripted, turn

HELD = SUITE.attacks[0].model_copy(update={"id": "atk-llm01-99", "split": "heldout",
                                            "prompt": "HELDOUT SECRET PROMPT"})  # fmt: skip


@pytest.fixture
def finished(env, tmp_path):  # noqa: F811
    """A finished, assessed run with one public and one held-out case."""
    p = plan(env, tmp_path, scripted(turn(COUNT), turn("SELECT * FROM embargoed_results")),
             attacks=[LLM02, HELD])  # fmt: skip
    run(p)
    items = {x.id: x for x in [*p.attacks, *p.questions]}
    assess(Assessment(run_dir=p.run_dir, db=env["db"], views=VIEWS, items=items,
                      create_for=lambda _n: None, labels=[]))  # type: ignore[arg-type]  # fmt: skip
    snap_root = env["db"].path.parent / "snapshots"
    return p, snap_root, env["db"].snapshot


def test_the_bundle_keeps_held_out_text_out(finished):
    p, root, snap = finished
    bundle = collect(p.run_dir, root, snap)
    raw = json.dumps(bundle)
    assert "HELDOUT SECRET PROMPT" not in raw and HELD.id not in raw
    assert all(x["case_id"] != HELD.id for x in bundle["audit"]["tail"])
    assert bundle["audit"]["verify_ok"] and bundle["audit"]["tail"]
    assert bundle["quality"]["flags_by_rule"] and bundle["quality"]["history"]


def test_an_unfinished_or_unassessed_run_is_refused(finished):
    p, root, snap = finished
    summary = json.loads((p.run_dir / "summary.json").read_text(encoding="utf-8"))
    del summary["behaviour"]
    (p.run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(ReportError, match="behaviour"):
        collect(p.run_dir, root, snap)
    summary["trials_sha256"] = "0" * 64
    (p.run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(ReportError, match="not finished"):
        collect(p.run_dir, root, snap)


def test_the_page_is_self_contained_and_answers_the_cisos_questions(finished):
    p, root, snap = finished
    page = render(collect(p.run_dir, root, snap))
    for heading in ("Did anything get through?", "What stopped it?", "How did the agents behave?",
                    "Access events", "Data quality"):  # fmt: skip
        assert heading in page
    assert not re.search(r'(src|href)="https?://', page) and "<script" not in page
    assert "upper bound" in page and "0 of 1 trials; the true rate is below about 95%" in page
    assert "Audit log verifies" in page and "embargoed_results</code> is synthetic" in page
    assert "HELDOUT SECRET PROMPT" not in page


def test_every_chart_has_a_label_on_each_mark_and_a_table_view(finished):
    p, root, snap = finished
    page = render(collect(p.run_dir, root, snap))
    marks = re.findall(r'<rect class="mark" tabindex="0"[^>]*>(.*?)</rect>', page)
    assert marks and all(m.startswith("<title>") for m in marks)  # hover and keyboard focus
    assert page.count("<details><summary>Table view") >= 2
    assert '<div class="legend"' in page  # the stacked layers carry a legend


def test_dark_mode_is_chosen_not_inverted(finished):
    p, root, snap = finished
    page = render(collect(p.run_dir, root, snap))
    assert "prefers-color-scheme:dark" in page and ':root[data-theme="dark"]' in page
    assert "--s1:#2a78d6" in page and "--s1:#3987e5" in page  # light and dark steps differ


def test_cli_renders_from_a_run_and_again_from_its_bundle(finished, tmp_path, capsys):
    p, root, snap = finished
    from hashlib import sha256
    from pathlib import Path

    published = Path(__file__).resolve().parents[2] / "docs/report/explorer.html"
    before = sha256(published.read_bytes()).hexdigest()
    html1, html2 = tmp_path / "a.html", tmp_path / "b.html"
    args = ["report", "--run", str(p.run_dir), "--snapshots", str(root), "--snapshot", snap,
            "--results", str(tmp_path / "results"), "--html", str(html1), "--json"]  # fmt: skip
    assert main(args) == 0
    out = json.loads(capsys.readouterr().out)
    # the explorer follows --html (the dashboard links it as a sibling) and a report rendered
    # elsewhere never rewrites the published page
    assert out["explorer"] == str(tmp_path / "explorer.html")
    assert 'href="a.html"' in (tmp_path / "explorer.html").read_text(encoding="utf-8")
    assert sha256(published.read_bytes()).hexdigest() == before
    assert main(["report", "--bundle", out["bundle"], "--html", str(html2)]) == 0
    assert html1.read_text(encoding="utf-8") == html2.read_text(encoding="utf-8")
    metrics = Path(out["bundle"]).with_name("metrics.json")  # beside the bundle (DSH-08)
    assert out["metrics"] == str(metrics) and metrics.is_file()
    assert main(["report", "--json"]) == 2
    capsys.readouterr()


def test_the_numbers_on_the_page_are_the_summarys(finished):
    p, root, snap = finished
    bundle = collect(p.run_dir, root, snap)
    page = render(bundle)
    for row in bundle["summary"]["attacks"]:
        assert row["success_text"] in page
    for row in bundle["summary"]["behaviour"]:
        assert f"<td>{row['rule']}</td>" in page


def test_access_events_count_every_query_but_list_public_ones_only(finished):
    p, root, snap = finished
    a = collect(p.run_dir, root, snap)["audit"]
    assert a["decisions"]["blocked: table_not_allowed"] == {"attack": 1, "question": 0}
    assert a["blocked"] == [] and all(x["case_id"] == LLM02.id for x in a["tail"])


# ---- verifier pass 1 (B5.1): repros, now passing ----


def test_the_page_does_not_depend_on_how_the_bundle_orders_its_keys(finished):
    p, root, snap = finished
    bundle = collect(p.run_dir, root, snap)
    bundle["audit"]["decisions"] = {"zeta": {"attack": 0, "question": 5},
                                    "alpha": {"attack": 1, "question": 0},
                                    "mid": {"attack": 2, "question": 3}}  # fmt: skip
    flipped = json.loads(json.dumps(bundle, sort_keys=True))  # how write_bundle saves it
    assert render(bundle) == render(flipped)
    page = render(bundle)
    assert page.index(">mid<") < page.index(">zeta<") < page.index(">alpha<")  # 5, 5, 1


def test_exact_upper_bounds():
    from math import comb

    from assay_pipeline.report.html import upper_bound

    assert upper_bound(0, 30) == pytest.approx(1 - 0.05 ** (1 / 30))
    hi = upper_bound(1, 30)
    tail = sum(comb(30, k) * hi**k * (1 - hi) ** (30 - k) for k in range(2))
    assert tail == pytest.approx(0.05, abs=1e-9) and hi > 1 / 30 + 0.0967  # not Wald's 9.7%
    assert upper_bound(30, 30) == 1.0 and upper_bound(0, 0) == 1.0


def test_tiles_bound_each_agent_and_count_all_spend(finished):
    p, root, snap = finished
    bundle = collect(p.run_dir, root, snap)
    bundle["ablation"] = {"spent_usd": 1.25, "attacks": []}
    page = render(bundle)
    spent = bundle["summary"]["spent_usd"] + bundle["summary"].get("judging_spent_usd", 0) + 1.25
    assert f"${spent:.2f}" in page and "guard-off ablation and judges" in page
    assert "95% upper bound on the success rate per trial" in page


def test_the_bound_chart_never_pools_splits_and_never_cuts_a_bound(finished):
    p, root, snap = finished
    bundle = collect(p.run_dir, root, snap)
    bundle["summary"]["attacks"].append({**bundle["summary"]["attacks"][0], "split": "heldout",
                                         "trials": 2, "successes": 2})  # fmt: skip
    page = render(bundle)
    assert "· public" in page and "· heldout" in page
    assert ">100%</text>" in page  # the axis grew to the 100% bound


def test_marks_are_labelled_for_keyboard_and_screen_readers(finished):
    p, root, snap = finished
    page = render(collect(p.run_dir, root, snap))
    assert 'role="group"' in page and 'role="img" aria-label=' not in page.split("<svg")[0]
    marks = re.findall(r'<rect class="mark" tabindex="0" role="img" aria-label="([^"]+)"', page)
    assert marks and page.count('class="flabel"') >= len(marks)
    assert ".m:focus-within .flabel{visibility:visible}" in page
    assert '<div class="legend">' in page  # the legend is read, not hidden


def test_the_pages_workflow_renders_the_pointed_bundle(finished, tmp_path, capsys):
    p, root, snap = finished
    args = ["report", "--run", str(p.run_dir), "--snapshots", str(root), "--snapshot", snap,
            "--results", str(tmp_path / "results"), "--html", str(tmp_path / "i.html")]  # fmt: skip
    assert main(args) == 0
    capsys.readouterr()
    assert (tmp_path / "results" / "LATEST").read_text(encoding="utf-8").strip() == (
        f"{p.run_dir.name}/bundle.json"
    )
    from pathlib import Path

    wf = (Path(__file__).resolve().parents[2] / ".github/workflows/pages.yml").read_text(
        encoding="utf-8"
    )
    assert "cat docs/results/LATEST" in wf and "persist-credentials: false" in wf


def test_the_metrics_file_of_a_run_holds_no_held_out_case_or_text(finished, tmp_path):
    """A fresh run's metrics-1 file (DSH-08): split-level held-out numbers only (D-12)."""
    from assay_pipeline.report.metrics import metrics_summary
    from tests.report.test_metrics import check_metrics_1

    p, root, snap = finished
    doc = metrics_summary(collect(p.run_dir, root, snap))
    check_metrics_1(doc)
    text = json.dumps(doc)
    assert not re.search(r"\b(atk|nq)-", text)


def test_a_second_run_is_compared_with_the_one_published_before_it(finished, tmp_path, capsys):
    """DSH-10: `assay report --run` finds the previous run through LATEST and puts EvalKit's
    comparison in the new bundle and page; --previous is refused with --bundle."""
    import shutil
    from pathlib import Path

    p, root, snap = finished
    second = tmp_path / "second-run"
    shutil.copytree(p.run_dir, second)
    results, html = tmp_path / "results", tmp_path / "index.html"
    base = ["report", "--snapshots", str(root), "--snapshot", snap, "--results", str(results),
            "--html", str(html), "--json"]  # fmt: skip
    assert main([*base, "--run", str(p.run_dir)]) == 0
    first = json.loads(capsys.readouterr().out)
    assert "compare" not in json.loads(Path(first["bundle"]).read_text(encoding="utf-8"))
    assert main([*base, "--run", str(second)]) == 0
    out = json.loads(capsys.readouterr().out)
    bundle = json.loads(Path(out["bundle"]).read_text(encoding="utf-8"))
    assert bundle["compare"]["previous_run"] == p.run_dir.name
    assert bundle["compare"]["changed"] == []  # the same trials: nothing changed
    assert "Compared with the last run" in html.read_text(encoding="utf-8")
    assert main(["report", "--bundle", out["bundle"], "--previous", out["metrics"],
                 "--html", str(tmp_path / "x.html")]) == 2  # fmt: skip
    capsys.readouterr()
