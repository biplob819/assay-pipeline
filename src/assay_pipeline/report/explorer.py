# ruff: noqa: E501  (an HTML template: its text lines are page content, not code)
"""The compound explorer (D6.3, EXP-01, EXP-02): one self-contained page, linked from the
dashboard, that filters the published snapshot by compound, assay type, variant, source and
pIC50 range, with every row linking to its source record.

The data is embedded as compact JSON (compounds listed once, rows referring to them by index)
and filtered by a small inline script; nothing is fetched. Flagged rows are kept and labelled,
as the snapshot keeps them; the licence and attribution of the snapshot travel with the page.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from assay_pipeline.pipeline.snapshot import read_parquet_rows

ASSAY_TYPES = {"B": "binding", "F": "functional", "A": "ADMET", "T": "toxicity",
               "U": "unspecified (PubChem)"}  # fmt: skip


def source_links(source: str, record: str, assay: str | None) -> list[tuple[str, str]]:
    """Links to the record a value came from: ChEMBL activity and assay, or the PubChem
    bioassay and substance (PubChem record ids are AID:SID:n)."""
    if source == "chembl":
        links = [("activity", f"https://www.ebi.ac.uk/chembl/api/data/activity/{record}.json")]
        if assay:
            links.append(("assay", f"https://www.ebi.ac.uk/chembl/explore/assay/{assay}"))
        return links
    if source == "pubchem":
        aid, sid, *_ = [*record.split(":"), "", ""]
        links = [("bioassay", f"https://pubchem.ncbi.nlm.nih.gov/bioassay/{aid}")]
        if sid:
            links.append(("substance", f"https://pubchem.ncbi.nlm.nih.gov/substance/{sid}"))
        return links
    return []


def explorer_data(snapshot_dir: Path) -> dict[str, Any]:
    rows = read_parquet_rows(snapshot_dir / "activities.parquet")
    compounds: dict[tuple[str, str], int] = {}  # tautomers share an InChIKey, not a SMILES
    smiles: list[list[str]] = []
    cats: dict[str, dict[str, int]] = {"type": {}, "variant": {}, "relation": {}, "source": {},
                                       "flags": {}}  # fmt: skip

    def idx(kind: str, value: str) -> int:
        table = cats[kind]
        return table.setdefault(value, len(table))

    out = []
    for r in sorted(rows, key=lambda r: (r["inchikey"], r["smiles"], r["source"],
                                         r["source_record_id"])):  # fmt: skip
        ci = compounds.setdefault((r["inchikey"], r["smiles"]), len(compounds))
        if ci == len(smiles):
            smiles.append([r["inchikey"], r["smiles"]])
        flags = ",".join(r["flags"] or [])
        out.append([
            ci, idx("type", r["assay_type"]), idx("variant", r["variant"]),
            idx("relation", r["relation"]),
            None if r["value_nm"] is None else round(r["value_nm"], 4),
            None if r["pic50"] is None else round(r["pic50"], 4),
            idx("source", r["source"]), r["source_record_id"], r["assay_id"] or "",
            idx("flags", flags),
        ])  # fmt: skip
    return {
        "compounds": smiles,
        "categories": {k: list(v) for k, v in cats.items()},
        "rows": out,
        "snapshot": rows[0]["snapshot"] if rows else "",
    }


SCRIPT = r"""
const D = JSON.parse(document.getElementById('data').textContent);
const C = D.categories, $ = id => document.getElementById(id);
const TYPES = %TYPES%;
function opt(sel, values, label) {
  sel.innerHTML = '<option value="">All</option>' +
    values.map((v, i) => `<option value="${i}">${esc(label ? label(v) : v)}</option>`).join('');
}
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function links(src, rec, assay) {
  if (src === 'chembl') {
    let l = `<a href="https://www.ebi.ac.uk/chembl/api/data/activity/${encodeURIComponent(rec)}.json">activity</a>`;
    if (assay) l += ` · <a href="https://www.ebi.ac.uk/chembl/explore/assay/${encodeURIComponent(assay)}">assay</a>`;
    return l;
  }
  const [aid, sid] = rec.split(':');
  let l = `<a href="https://pubchem.ncbi.nlm.nih.gov/bioassay/${encodeURIComponent(aid)}">bioassay</a>`;
  if (sid) l += ` · <a href="https://pubchem.ncbi.nlm.nih.gov/substance/${encodeURIComponent(sid)}">substance</a>`;
  return l;
}
opt($('type'), C.type, v => `${v} ${TYPES[v] || ''}`);
opt($('variant'), C.variant); opt($('source'), C.source);
const LIMIT = 200;
function run() {
  const raw = $('compound').value.trim(), q = raw.toUpperCase(), t = $('type').value, v = $('variant').value,
        s = $('source').value, lo = parseFloat($('lo').value), hi = parseFloat($('hi').value),
        noflag = $('noflag').checked;
  const hits = [];
  for (const r of D.rows) {
    const [ci, ti, vi, ri, nm, p, si, rec, assay, fi] = r;
    if (t !== '' && ti != t) continue;
    if (v !== '' && vi != v) continue;
    if (s !== '' && si != s) continue;
    if (!isNaN(lo) && !(p !== null && p >= lo)) continue;
    if (!isNaN(hi) && !(p !== null && p <= hi)) continue;
    if (noflag && C.flags[fi] !== '') continue;
    if (raw) { const [ik, smi] = D.compounds[ci]; if (!ik.startsWith(q) && !smi.includes(raw)) continue; }
    hits.push(r);
  }
  $('count').textContent = `${hits.length.toLocaleString()} matching rows` +
    (hits.length > LIMIT ? `; showing the first ${LIMIT}` : '');
  $('rows').innerHTML = hits.slice(0, LIMIT).map(([ci, ti, vi, ri, nm, p, si, rec, assay, fi]) => {
    const [ik, smi] = D.compounds[ci], rel = C.relation[ri], fl = C.flags[fi];
    return `<tr><td><code title="${esc(smi)}">${esc(ik)}</code></td><td>${esc(C.type[ti])}</td>` +
      `<td>${esc(C.variant[vi])}</td><td class="num">${esc(rel)} ${nm === null ? 'n/a' : nm.toLocaleString()}</td>` +
      `<td class="num">${p === null ? 'n/a' : p.toFixed(2)}</td><td>${fl ? esc(fl) : ''}</td>` +
      `<td>${esc(C.source[si])}: ${links(C.source[si], rec, assay)}</td></tr>`;
  }).join('');
}
document.querySelectorAll('#filters input, #filters select').forEach(e => e.addEventListener('input', run));
run();
"""


def render_explorer(data: dict[str, Any], license_md: str, dashboard: str = "index.html") -> str:
    from .html import CSS

    # no "<" at all inside the data block: "</script" and "<!--" can then never end or
    # unbalance it, whatever the data holds (SMILES case matters, so the query is not folded)
    payload = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    script = SCRIPT.replace("%TYPES%", json.dumps(ASSAY_TYPES))
    lic = "".join(f"<p class=\"small\">{html.escape(line.lstrip('#- ').strip())}</p>"
                  for line in license_md.splitlines() if line.strip())  # fmt: skip
    n_rows, n_cmp = len(data["rows"]), len(data["compounds"])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Assay Compound Explorer</title><style>{CSS}
#filters{{display:flex;flex-wrap:wrap;gap:12px;align-items:end}}#filters label{{display:flex;flex-direction:column;
font-size:13px;color:var(--ink2);gap:4px}}input,select{{font:inherit;padding:6px 8px;border:1px solid var(--line);
border-radius:6px;background:var(--surface);color:var(--ink);min-width:0}}#filters .check{{flex-direction:row;align-items:center}}
a{{color:var(--s1)}}</style></head><body><main>
<h1>Compound explorer</h1>
<p class="muted">Snapshot <code>{html.escape(data["snapshot"])}</code>: {n_rows:,} activity rows for {n_cmp:,}
compounds. Filter by compound (InChIKey prefix, any case, or SMILES fragment, case-sensitive), assay type, variant, source and pIC50 range.
Every row links to the record it came from. Flagged rows are kept and labelled, as the snapshot keeps them.
<a href="{html.escape(dashboard)}">Back to the security report</a>.</p>
<section aria-labelledby="h-f"><h2 id="h-f">Filters</h2>
<div id="filters">
<label>Compound<input id="compound" type="search" placeholder="InChIKey or SMILES" autocomplete="off"></label>
<label>Assay type<select id="type"></select></label>
<label>Variant<select id="variant"></select></label>
<label>Source<select id="source"></select></label>
<label>pIC50 from<input id="lo" type="number" step="0.1" inputmode="decimal"></label>
<label>pIC50 to<input id="hi" type="number" step="0.1" inputmode="decimal"></label>
<label class="check"><input id="noflag" type="checkbox"> Hide flagged rows</label>
</div>
<p id="count" class="small" role="status" aria-live="polite"></p>
<div class="wrap"><table><caption class="small">Matching activity rows</caption><thead><tr><th>Compound (InChIKey)</th><th>Assay type</th><th>Variant</th>
<th class="num">Value (nM)</th><th class="num">pIC50</th><th>Flags</th><th>Source record</th></tr></thead>
<tbody id="rows"></tbody></table></div></section>
<section aria-labelledby="h-l"><h2 id="h-l">Licence and attribution</h2>{lic}</section>
</main><script type="application/json" id="data">{payload}</script><script>{script}</script></body></html>
"""
