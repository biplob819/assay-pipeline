"""D6.3: the compound explorer (EXP-01, EXP-02)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from assay_pipeline.report.explorer import explorer_data, render_explorer, source_links
from tests.security.test_behaviour import env  # noqa: F401  (fixture)

LICENSE = "# Data licence\n\nCC BY-SA 3.0, ChEMBL and PubChem attribution."
REPO = Path(__file__).resolve().parents[2]
needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="needs node for the page script"
)


@pytest.fixture
def snap_dir(env):  # noqa: F811
    root = env["db"].path.parent / "snapshots"
    return root / env["db"].snapshot


def _payload(page: str) -> dict:
    m = re.search(r'<script type="application/json" id="data">(.*?)</script>', page, re.S)
    assert m and "<" not in m.group(1)  # nothing in the data block can end or unbalance it
    return json.loads(m.group(1))


def _row_tuple(data: dict, r: list) -> tuple:
    c = data["categories"]
    ik, smi = data["compounds"][r[0]]
    return (ik, smi, c["type"][r[1]], c["variant"][r[2]], c["relation"][r[3]], r[4], r[5],
            c["source"][r[6]], r[7], r[8], c["flags"][r[9]])  # fmt: skip


def test_every_row_round_trips_to_its_snapshot_row(snap_dir):
    from assay_pipeline.pipeline.snapshot import read_parquet_rows

    rnd = lambda x: None if x is None else round(x, 4)  # noqa: E731
    want = sorted(
        (r["inchikey"], r["smiles"], r["assay_type"], r["variant"], r["relation"],
         rnd(r["value_nm"]), rnd(r["pic50"]), r["source"], r["source_record_id"],
         r["assay_id"] or "", ",".join(r["flags"] or []))
        for r in read_parquet_rows(snap_dir / "activities.parquet")
    )  # fmt: skip
    data = explorer_data(snap_dir)
    assert sorted(_row_tuple(data, r) for r in data["rows"]) == want
    assert len({tuple(c) for c in data["compounds"]}) == len(data["compounds"])  # each once


def test_tautomers_keep_their_own_smiles(snap_dir, tmp_path):
    from assay_pipeline.pipeline.snapshot import read_parquet_rows, write_parquet

    rows = read_parquet_rows(snap_dir / "activities.parquet")
    twin = {**rows[0], "smiles": rows[0]["smiles"] + ".O", "source_record_id": "twin-1"}
    write_parquet(tmp_path / "activities.parquet", "activities", [*rows, twin])
    data = explorer_data(tmp_path)
    got = [_row_tuple(data, r)[:2] for r in data["rows"] if r[7] == "twin-1"]
    assert got == [(twin["inchikey"], twin["smiles"])]


def test_every_value_links_to_its_source_record():
    assert source_links("chembl", "26587589", "CHEMBL5729820") == [
        ("activity", "https://www.ebi.ac.uk/chembl/api/data/activity/26587589.json"),
        ("assay", "https://www.ebi.ac.uk/chembl/explore/assay/CHEMBL5729820"),
    ]
    assert source_links("pubchem", "1799141:405267419:1", "1799141") == [
        ("bioassay", "https://pubchem.ncbi.nlm.nih.gov/bioassay/1799141"),
        ("substance", "https://pubchem.ncbi.nlm.nih.gov/substance/405267419"),
    ]


def test_the_page_is_self_contained_and_carries_the_licence(snap_dir):
    data = explorer_data(snap_dir)
    page = render_explorer(data, LICENSE)
    assert not re.search(r'<(script|link|img)[^>]+(src|href)="https?://', page)
    assert "CC BY-SA 3.0" in page and "Back to the security report" in page
    assert _payload(page) == json.loads(json.dumps(data))
    for control in ('id="compound"', 'id="type"', 'id="variant"', 'id="source"', 'id="lo"',
                    'id="hi"', 'role="status"', "<caption"):  # fmt: skip
        assert control in page


def test_markup_in_the_data_cannot_break_out_of_the_data_block():
    hostile = "<!--<script>x</script><img src=x onerror=alert(1)>"
    data = {"compounds": [["AAAAAAAAAAAAAA-UHFFFAOYSA-N", hostile]],
            "categories": {"type": ["B"], "variant": [hostile], "relation": ["="],
                           "source": ["chembl"], "flags": [""]},
            "rows": [[0, 0, 0, 0, 1.0, 9.0, 0, "1", "", 0]], "snapshot": hostile}  # fmt: skip
    page = render_explorer(data, LICENSE)
    assert _payload(page)["compounds"][0][1] == hostile
    assert page.count("<script") == 2 and "<img" not in page  # the two of the page, no more


def _run_page(data: dict, settings: dict[str, dict], tmp_path: Path) -> dict[str, int]:
    """Run the page script under node with a stub DOM, apply each named filter setting and
    return the reported match count for each."""
    script = re.findall(r"<script>(.*?)</script>", render_explorer(data, LICENSE), re.S)[-1]
    js = [
        "const els = {};",
        "global.document = {getElementById: id => els[id] ||= (id === 'data' ? "
        f"{{textContent: {json.dumps(json.dumps(data))}}} : "
        "{value: '', checked: false, innerHTML: '', textContent: ''}),"
        " querySelectorAll: () => []};",
        script,
        "const out = {};",
        "const reset = () => { for (const k of ['compound','type','variant','source','lo','hi'])"
        " els[k].value = ''; els.noflag.checked = false; };",
    ]
    for name, s in settings.items():
        sets = "; ".join(
            f"els.{k}.checked = {json.dumps(v)}"
            if k == "noflag"
            else f"els.{k}.value = {json.dumps(v)}"
            for k, v in s.items()
        )
        js.append(f"reset(); {sets}; run(); out[{json.dumps(name)}] = "
                  "parseInt(els.count.textContent.replace(/[^0-9 ]/g, ''));")  # fmt: skip
    js.append("console.log(JSON.stringify(out));")
    harness = tmp_path / "run.js"
    harness.write_text("\n".join(js), encoding="utf-8")
    proc = subprocess.run(["node", str(harness)], capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


@needs_node
def test_each_filter_matches_a_reference_count(tmp_path):
    # built to tell the filters apart: three variants, two sources, two types, a tautomer
    # pair, mixed-case SMILES, flagged rows and a row with no pIC50
    cats = {
        "type": ["B", "F"],
        "variant": ["wild-type/unspecified", "T790M", "L858R"],
        "relation": ["=", ">"],
        "source": ["chembl", "pubchem"],
        "flags": ["", "QC2"],
    }
    comps = [["AAAAAAAAAAAAAA-UHFFFAOYSA-N", "c1ccncc1C"], ["AAAAAAAAAAAAAA-UHFFFAOYSA-N",
             "C1CCNCC1C"], ["BBBBBBBBBBBBBB-UHFFFAOYSA-N", "c1ccccc1O"]]  # fmt: skip
    rows = []
    for i in range(36):
        ci, ti, vi, si, fi = i % 3, i % 2, (i // 2) % 3, (i // 6) % 2, int(i % 5 == 0)
        p = None if i == 35 else 5 + (i % 9) * 0.5
        rows.append([ci, ti, vi, 0, None if p is None else 10 ** (9 - p), p, si, str(i), "", fi])
    data = {"compounds": comps, "categories": cats, "rows": rows, "snapshot": "t"}
    settings = {
        "all": {}, "type F": {"type": "1"}, "T790M": {"variant": "1"}, "L858R": {"variant": "2"},
        "pubchem": {"source": "1"}, "hi 6": {"hi": "6"}, "7 to 8": {"lo": "7", "hi": "8"},
        "lo 0": {"lo": "0"}, "unflagged": {"noflag": True}, "ik lower": {"compound": "aaaaaaaa"},
        "smiles aromatic": {"compound": "c1ccncc1"}, "smiles aliphatic": {"compound": "C1CCNCC1"},
        "combined": {"type": "0", "variant": "1", "source": "0", "noflag": True},
    }  # fmt: skip
    ref = {
        "all": lambda r: True, "type F": lambda r: r[1] == 1, "T790M": lambda r: r[2] == 1,
        "L858R": lambda r: r[2] == 2, "pubchem": lambda r: r[6] == 1,
        "hi 6": lambda r: r[5] is not None and r[5] <= 6,
        "7 to 8": lambda r: r[5] is not None and 7 <= r[5] <= 8,
        "lo 0": lambda r: r[5] is not None, "unflagged": lambda r: r[9] == 0,
        "ik lower": lambda r: r[0] in (0, 1), "smiles aromatic": lambda r: r[0] == 0,
        "smiles aliphatic": lambda r: r[0] == 1,
        "combined": lambda r: r[1] == 0 and r[2] == 1 and r[6] == 0 and r[9] == 0,
    }  # fmt: skip
    got = _run_page(data, settings, tmp_path)
    want = {k: sum(1 for r in rows if f(r)) for k, f in ref.items()}
    assert got == want
    assert all(n < want["all"] for k, n in want.items() if k != "all")  # each one filters


@needs_node
def test_the_filters_run_on_a_real_snapshot(snap_dir, tmp_path):
    data = explorer_data(snap_dir)
    first = data["compounds"][0][0]
    settings = {"all": {}, "compound": {"compound": first[:14]}, "none": {"lo": "99"},
                "unflagged": {"noflag": True}}  # fmt: skip
    got = _run_page(data, settings, tmp_path)
    assert got == {
        "all": len(data["rows"]),
        "compound": sum(1 for r in data["rows"] if data["compounds"][r[0]][0][:14] == first[:14]),
        "none": 0,
        "unflagged": sum(1 for r in data["rows"] if data["categories"]["flags"][r[9]] == ""),
    }


def test_the_published_explorer_holds_the_published_snapshot():
    """The committed page must be the real snapshot the published bundle came from, never a
    test fixture (the Pages job uploads docs/report as committed)."""
    latest = (REPO / "docs/results/LATEST").read_text(encoding="utf-8").strip()
    bundle = json.loads((REPO / "docs/results" / latest).read_text(encoding="utf-8"))
    data = _payload((REPO / "docs/report/explorer.html").read_text(encoding="utf-8"))
    assert data["snapshot"] == bundle["snapshot"]
    assert len(data["rows"]) == sum(bundle["quality"]["rows_by_source"].values())
