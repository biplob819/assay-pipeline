#!/usr/bin/env python3
"""Render a one-page PDF from a small Markdown file (B5.2 threat model, R6.1 data roadmap).

Supports the subset those pages use: `#`/`##` headings, paragraphs, `-` bullets (and `- [ ]`
checkboxes), `**bold**`, and pipe tables. The font shrinks until the page fits on one landscape
A4 sheet; if even the smallest size spills over, rendering fails rather than produce two pages.
The PDF carries the source's sha256 in its keywords, so a test can prove it is current.

Usage: uv run python scripts/render_onepager.py docs/threat-model.md docs/threat-model.pdf
"""

from __future__ import annotations

import hashlib
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from fpdf import FPDF
from fpdf.fonts import FontFace

SIZES = (8.0, 7.5, 7.0, 6.6, 6.2, 5.8, 5.5)
FIXED_DATE = datetime(2026, 1, 1, tzinfo=UTC)  # reproducible bytes for the same source


def source_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _blocks(text: str) -> list[tuple[str, object]]:
    blocks: list[tuple[str, object]] = []
    if "--" in text.replace("---", ""):
        raise SystemExit("'--' is underline in fpdf2's markdown; rephrase it")
    if "__" in text or re.search(r"\]\(", text):
        raise SystemExit("italic (__) and link markup do not render on this page; rephrase it")
    lines = text.replace("`", "").splitlines()  # code spans print as plain text
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
        elif line.startswith("# "):
            blocks.append(("title", line[2:].strip()))
            i += 1
        elif line.startswith(("## ", "### ")):
            blocks.append(("h2", line.lstrip("#").strip()))
            i += 1
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            blocks.append(("table", rows))
        elif re.match(r"^\s*- ", line):
            items = []
            while i < len(lines) and re.match(r"^\s*- ", lines[i]):
                items.append(
                    re.sub(r"^\s*- (\[ \] )?", lambda m: "[ ] " if m.group(1) else "", lines[i])
                )
                i += 1
            blocks.append(("bullets", items))
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not re.match(r"^(#|\||\s*- )", lines[i]):
                para.append(lines[i].strip())
                i += 1
            blocks.append(("para", " ".join(para)))
    return blocks


def _render(blocks: list[tuple[str, object]], size: float, sha: str) -> FPDF:
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_creation_date(FIXED_DATE)
    pdf.set_compression(False)  # zlib output differs by build (zlib-ng on Windows 3.14+): L-41
    pdf.set_keywords(f"source-sha256:{sha}")
    pdf.set_margins(9, 8, 9)
    pdf.set_auto_page_break(True, margin=8)
    pdf.add_page()
    lh = size * 0.47
    for kind, value in blocks:
        if kind == "title":
            pdf.set_font("Helvetica", "B", size + 5)
            pdf.multi_cell(0, lh + 2.5, str(value), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(0.8)
        elif kind == "h2":
            pdf.ln(0.8)
            pdf.set_font("Helvetica", "B", size + 1.5)
            pdf.multi_cell(0, lh + 1, str(value), new_x="LMARGIN", new_y="NEXT")
        elif kind == "para":
            pdf.set_font("Helvetica", "", size)
            pdf.multi_cell(0, lh, str(value), markdown=True, align="L", new_x="LMARGIN",
                           new_y="NEXT")  # fmt: skip
        elif kind == "bullets":
            pdf.set_font("Helvetica", "", size)
            for item in value:  # type: ignore[attr-defined]
                pdf.set_x(pdf.l_margin + 2)
                pdf.multi_cell(0, lh, f"- {item}", markdown=True, align="L", new_x="LMARGIN",
                               new_y="NEXT")  # fmt: skip
        elif kind == "table":
            rows = value  # type: ignore[assignment]
            pdf.set_font("Helvetica", "", size - 0.6)
            widths = (14, 16, 22, 30, 18)[: len(rows[0])]  # type: ignore[index]
            bold = FontFace(emphasis="BOLD")
            opts = {"line_height": lh - 0.3, "markdown": True, "text_align": "LEFT",
                    "headings_style": bold, "padding": 0.8}  # fmt: skip
            with pdf.table(col_widths=widths, **opts) as table:
                for r in rows:  # type: ignore[attr-defined]
                    row = table.row()
                    for c in r:
                        row.cell(c)
    return pdf


def render(source: Path, out: Path) -> float:
    text = source.read_text(encoding="utf-8")
    try:
        text.encode("latin-1")
    except UnicodeEncodeError as exc:
        msg = f"{source}: use plain ASCII/Latin-1 text for the PDF core font ({exc})"
        raise SystemExit(msg) from exc
    blocks, sha = _blocks(text), source_sha256(text)
    for size in SIZES:
        pdf = _render(blocks, size, sha)
        if pdf.pages_count == 1:
            out.write_bytes(bytes(pdf.output()))
            return size
    raise SystemExit(f"{source}: does not fit on one page even at {SIZES[-1]} pt; shorten it")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    size = render(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"{sys.argv[2]}: one page at {size} pt")
