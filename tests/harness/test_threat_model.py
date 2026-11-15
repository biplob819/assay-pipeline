"""B5.2: the one-page threat model covers every suite category, ATLAS technique and control,
states its limits, and its PDF is current and one page (THR-01..04)."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

from assay_pipeline.attack.suite import CATEGORIES, load_suite

ROOT = Path(__file__).resolve().parents[2]
MD = ROOT / "docs" / "threat-model.md"
PDF = ROOT / "docs" / "threat-model.pdf"
TEXT = MD.read_text(encoding="utf-8")


def _renderer():
    spec = importlib.util.spec_from_file_location(
        "render_onepager", ROOT / "scripts" / "render_onepager.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rows() -> dict[str, str]:
    """OWASP id -> the whole table row that names it."""
    return {
        m.group(1): line for line in TEXT.splitlines() if (m := re.match(r"\| (LLM\d\d) ", line))
    }


def test_assets_boundaries_and_data_flow_are_stated():
    for heading in ("## Assets", "## Trust boundaries and data flow", "## Review checklist"):
        assert heading in TEXT
    for asset in ("Snapshot integrity", "Restricted data", "System instructions", "Audit trail",
                  "Compute and spend", "Provider keys"):  # fmt: skip
        assert asset in TEXT
    assert "model boundary" in TEXT and "child process" in TEXT


def test_every_owasp_category_has_a_row_with_controls_and_evidence():
    rows = _rows()
    assert sorted(rows) == [f"LLM{i:02d}" for i in range(1, 11)]  # all ten, incl. LLM03/07/09
    for cat in CATEGORIES:  # every suite category is a threat with a control and evidence
        cells = [c.strip() for c in rows[cat].strip("|").split("|")]
        assert len(cells) == 5 and all(c not in ("", "-") for c in cells[2:]), cat
    for covered in ("LLM03", "LLM07", "LLM09"):
        cells = [c.strip() for c in rows[covered].strip("|").split("|")]
        assert cells[3] not in ("", "-"), covered


def test_each_technique_sits_in_the_row_of_every_category_that_uses_it():
    rows = _rows()
    pairs = {(c.owasp, c.atlas) for c in load_suite().attacks}
    assert [(o, t) for o, t in sorted(pairs) if t not in rows[o]] == []


def test_every_atlas_technique_the_suite_uses_appears():
    atlas = yaml.safe_load(
        (ROOT / "src/assay_pipeline/data/atlas.yaml").read_text(encoding="utf-8")
    )
    used = {c.atlas for c in load_suite().attacks}
    assert used <= set(atlas["techniques"])
    missing = [t for t in atlas["techniques"] if t not in TEXT]
    assert missing == []


def test_every_control_appears():
    controls = (
        "SQL guard", "default deny", "allow-lists", "re-rendered SQL", "locked", "Read-only",
        "external access off", "no temp objects", "child process", "address-space cap",
        "step budget", "max-cost", "change check", "canary", "QC1-QC5", "audit log", "anchors",
        "pinned by SHA", "secret scan",
    )  # fmt: skip
    assert [c for c in controls if c not in TEXT] == []


def test_stated_limits_and_production_steps():
    for limit in ("no GRANTs", "Anchor trust", "Stealth poison", "Canary matching"):
        assert limit in TEXT
    section = TEXT.split("## Stated limits and production steps")[1].split("## ")[0]
    bullets = [b for b in section.splitlines() if b.startswith("- ")]
    assert len(bullets) >= 4
    for b in bullets:  # every limit has a production step, is accepted, or is out of scope
        assert any(w in b for w in ("Production", "accepted", "Out of scope")), b
    for step in ("database roles", "WORM", "replicate checks"):
        assert step in TEXT


def test_the_pdf_is_one_page_and_built_from_the_current_markdown():
    data = PDF.read_bytes()
    assert len(re.findall(rb"/Type\s*/Page\b", data)) == 1
    sha = _renderer().source_sha256(TEXT)
    assert f"source-sha256:{sha}".encode() in data, "re-render: scripts/render_onepager.py"


def test_the_renderer_refuses_what_it_cannot_print_on_one_page(tmp_path):
    r = _renderer()
    out = tmp_path / "x.pdf"
    (tmp_path / "dash.md").write_text("# T\n\nuse --flag\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="underline"):
        r.render(tmp_path / "dash.md", out)
    for bad in ("__italic__", "[link](https://x)"):
        (tmp_path / "md.md").write_text(f"# T\n\n{bad}\n", encoding="utf-8")
        with pytest.raises(SystemExit, match="markup"):
            r.render(tmp_path / "md.md", out)
    (tmp_path / "uni.md").write_text("# T\n\narrow → here\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="Latin-1"):
        r.render(tmp_path / "uni.md", out)
    (tmp_path / "long.md").write_text("# T\n\n" + "\n".join(f"- item {i}" for i in range(400)),
                                      encoding="utf-8")  # fmt: skip
    with pytest.raises(SystemExit, match="one page"):
        r.render(tmp_path / "long.md", out)
    assert r.render(MD, out) <= 8.0 and out.read_bytes() == PDF.read_bytes()  # reproducible
