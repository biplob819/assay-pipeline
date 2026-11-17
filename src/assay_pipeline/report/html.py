# ruff: noqa: E501  (an HTML template: its text lines are page content, not code)
"""Render the CISO dashboard from a results bundle (B5.1, DSH-01..06).

One self-contained HTML file: inline CSS and SVG, no scripts from anywhere, no network. Every
chart has a legend or a title naming its one series, a hover and keyboard-focus label on each
mark, and a table view, so nothing depends on colour alone. Light and dark are both chosen
(the dark steps are their own colours, not an inversion).
"""

from __future__ import annotations

import html
import math
from collections.abc import Sequence
from typing import Any

LAYERS = ("guard", "limit", "model", "quality_rule", "permissions", "leak")  # fixed slot order
LAYER_LABEL = {"guard": "SQL guard", "limit": "Resource limits", "model": "Model refused",
               "quality_rule": "Quality rules", "permissions": "Database permissions",
               "leak": "Got through"}  # fmt: skip
CATEGORY = {"LLM01": "Prompt injection", "LLM02": "Sensitive data", "LLM04": "Data poisoning",
            "LLM05": "Output as SQL", "LLM06": "Excessive agency",
            "LLM10": "Unbounded consumption"}  # fmt: skip
SHORT = {"claude-sonnet-5-5": "Claude Sonnet 5.5", "gpt-6.1-sol": "GPT-6.1 Sol",
         "pipeline": "Pipeline"}  # fmt: skip

CSS = """
:root{color-scheme:light;--surface:#fcfcfb;--page:#f4f4f2;--ink:#0b0b0b;--ink2:#52514e;
--ink3:#77766f;--line:#deddd8;--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;
--crit:#d03b3b;--good:#0ca30c;--warn-bg:#fff6e0}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;
--surface:#1a1a19;--page:#111110;--ink:#f0efec;--ink2:#c3c2b7;--ink3:#9a998f;--line:#3a3935;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--crit:#d03b3b;--good:#0ca30c;
--warn-bg:#2e2716}}
:root[data-theme="dark"]{color-scheme:dark;--surface:#1a1a19;--page:#111110;--ink:#f0efec;
--ink2:#c3c2b7;--ink3:#9a998f;--line:#3a3935;--s1:#3987e5;--s2:#d95926;--s3:#199e70;
--s4:#c98500;--s5:#d55181;--crit:#d03b3b;--good:#0ca30c;--warn-bg:#2e2716}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1080px;margin:0 auto;padding:24px 16px 64px;overflow-wrap:anywhere}
h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:0 0 4px}
h3{font-size:15px;margin:16px 0 6px}p{margin:6px 0}.muted{color:var(--ink2)}
.small{font-size:13px;color:var(--ink3)}
section{min-width:0;overflow-wrap:anywhere;background:var(--surface);border:1px solid var(--line);border-radius:10px;
padding:18px 20px;margin:18px 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(220px,100%),1fr));gap:12px;margin:16px 0}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.tile b{display:block;font-size:24px;line-height:1.2}.tile span{color:var(--ink2);font-size:13px}
table{border-collapse:collapse;width:100%;font-size:13.5px;margin:8px 0}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--ink2);font-weight:600}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.wrap{overflow-x:auto}code{font:12.5px ui-monospace,SFMono-Regular,Menlo,monospace;
word-break:break-word}
details{margin:8px 0}summary{cursor:pointer;color:var(--ink2)}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:13px;color:var(--ink2);margin:6px 0 10px}
.legend i{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;
vertical-align:-1px}
.chart{overflow-x:auto;max-width:100%}svg{display:block;max-width:none}
svg text{fill:var(--ink2);font-size:12px}
svg .flabel{visibility:hidden;fill:var(--ink);font-weight:600;paint-order:stroke;
stroke:var(--surface);stroke-width:4px}svg .m:focus-within .flabel{visibility:visible}
svg .mark:focus{outline:2px solid var(--ink);outline-offset:1px}
.banner{background:var(--warn-bg);border-radius:8px;padding:8px 12px;font-size:13.5px}
.ok::before{content:"\\2714  ";color:var(--good)}.bad::before{content:"\\2716  ";color:var(--crit)}
"""


def _e(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def _pct(x: float | None, digits: int = 0) -> str:
    return "n/a" if x is None else f"{x * 100:.{digits}f}%"


def _k(x: Any) -> str:
    return "n/a" if x is None else _e(x)


def upper_bound(successes: int, n: int, alpha: float = 0.05) -> float:
    """Exact one-sided (Clopper-Pearson) upper limit on a rate: the p at which seeing at most
    `successes` in `n` trials has probability `alpha`. With none it is 1 - alpha ** (1 / n)."""
    if n <= 0:
        return 1.0
    if successes >= n:
        return 1.0
    if successes == 0:
        return 1 - alpha ** (1 / n)

    def cdf(p: float) -> float:
        return sum(math.comb(n, k) * p**k * (1 - p) ** (n - k) for k in range(successes + 1))

    lo, hi = successes / n, 1.0
    for _ in range(80):  # bisection: cdf falls as p rises
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if cdf(mid) > alpha else (lo, mid)
    return hi


def _legend(keys: Sequence[str]) -> str:
    items = "".join(
        f'<span><i style="background:var(--{_slot(k)})"></i>{_e(LAYER_LABEL[k])}</span>'
        for k in keys
    )
    return f'<div class="legend">{items}</div>'


def _slot(layer: str) -> str:
    return {"guard": "s1", "limit": "s2", "model": "s3", "quality_rule": "s4",
            "permissions": "s5", "leak": "crit"}[layer]  # fmt: skip


def _mark(tag: str, attrs: str, label: str, fx: float, fy: float) -> str:
    """A focusable mark labelled for hover (title), screen readers (aria-label) and keyboard
    focus (a text shown while focused)."""
    lab = _e(label)
    return (f'<g class="m"><{tag} class="mark" tabindex="0" role="img" aria-label="{lab}" '
            f"{attrs}><title>{lab}</title></{tag}>"
            f'<text class="flabel" x="{fx:.0f}" y="{fy:.0f}">{lab}</text></g>')  # fmt: skip


def _svg(width: int, height: int, label: str, body: str) -> str:
    return (f'<div class="chart"><svg width="{width}" height="{height}" viewBox="0 0 {width} '
            f'{height}" role="group" aria-label="{_e(label)}">{body}</svg></div>')  # fmt: skip


def _stack_rows(rows: list[tuple[str, dict[str, int]]], width: int = 820) -> str:
    """Horizontal 100% stacked bars, one row each; 2px surface gaps between segments."""
    label_w, bar_w, h, gap = 300, width - 300 - 50, 18, 10
    out = []
    for i, (label, counts) in enumerate(rows):
        y = i * (h + gap) + 16
        total = sum(counts.values()) or 1
        out.append(f'<text x="0" y="{y + 13}">{_e(label)}</text>')
        x = float(label_w)
        for layer in LAYERS:
            n = counts.get(layer, 0)
            if not n:
                continue
            w = max(bar_w * n / total - 2, 1)
            tip = f"{label}: {LAYER_LABEL[layer]} {n} of {total}"
            attrs = (f'x="{x:.1f}" y="{y}" width="{w:.1f}" height="{h}" rx="3" '
                     f'fill="var(--{_slot(layer)})"')  # fmt: skip
            out.append(_mark("rect", attrs, tip, label_w, y - 4))
            x += w + 2
        out.append(f'<text x="{width - 44}" y="{y + 13}">{total}</text>')
    return _svg(width, len(rows) * (h + gap) + 16, "Stopping layer by category", "".join(out))


def _bound_chart(rows: list[tuple[str, int, int]], width: int = 820) -> str:
    """Success rate with its exact 95% upper bound: a dot at the observed rate, a bar to the
    bound. The axis grows to the largest bound (in 25% steps), so no bound is cut off."""
    label_w, plot_w, h, gap = 330, width - 330 - 70, 14, 12
    bounds = [upper_bound(succ, n) for _, succ, n in rows]
    top = max(0.5, math.ceil(max(bounds, default=0.5) * 4) / 4)
    base = len(rows) * (h + gap) + 16
    out = [f'<line x1="{label_w}" x2="{label_w}" y1="16" y2="{base}" stroke="var(--line)"/>']
    for k in range(int(top * 4) + 1):
        tick = k / 4
        out.append(f'<text x="{label_w + plot_w * tick / top - 8:.0f}" y="{base + 14}">'
                   f"{int(tick * 100)}%</text>")  # fmt: skip
    for i, ((label, succ, n), hi) in enumerate(zip(rows, bounds, strict=True)):
        y = i * (h + gap) + 16
        rate = succ / n if n else 0.0
        x_hi = label_w + plot_w * hi / top
        x_pt = label_w + plot_w * rate / top
        tip = f"{label}: {succ} of {n} trials succeeded; 95% upper bound {_pct(hi, 1)}"
        out.append(f'<text x="0" y="{y + 11}">{_e(label)}</text>')
        attrs = (f'x="{label_w}" y="{y + 4}" width="{max(x_hi - label_w, 1):.1f}" height="6" '
                 'rx="3" fill="var(--s1)" opacity=".45"')  # fmt: skip
        out.append(_mark("rect", attrs, tip, label_w, y - 2))
        out.append(f'<circle cx="{x_pt:.1f}" cy="{y + 7}" r="5" fill="var(--s1)" '
                   'stroke="var(--surface)" stroke-width="2" aria-hidden="true"/>')  # fmt: skip
        out.append(f'<text x="{x_hi + 6:.0f}" y="{y + 11}">&lt; {_pct(hi)}</text>')
    return _svg(width, base + 20, "Attack success rate with its 95% upper bound", "".join(out))


def _bars(items: list[tuple[str, int]], width: int = 820) -> str:
    label_w, plot_w, h, gap = 160, width - 160 - 70, 16, 8
    top = max((v for _, v in items), default=1) or 1
    out = []
    for i, (label, v) in enumerate(items):
        y = i * (h + gap) + 16
        w = max(plot_w * v / top, 1 if v else 0)
        out.append(f'<text x="0" y="{y + 12}">{_e(label)}</text>')
        attrs = f'x="{label_w}" y="{y}" width="{w:.1f}" height="{h}" rx="3" fill="var(--s1)"'
        out.append(_mark("rect", attrs, f"{label}: {v:,}", label_w, y - 3))
        out.append(f'<text x="{label_w + w + 6:.0f}" y="{y + 12}">{v:,}</text>')
    return _svg(width, len(items) * (h + gap) + 16, "Flags per rule", "".join(out))


def _table(head: Sequence[str], rows: Sequence[Sequence[Any]], num: Sequence[int] = ()) -> str:
    def cell(tag: str, i: int, v: Any) -> str:
        cls = ' class="num"' if i in num else ""
        return f"<{tag}{cls}>{v}</{tag}>"

    th = "".join(cell("th", i, _e(h)) for i, h in enumerate(head))
    body = "".join("<tr>" + "".join(cell("td", i, c) for i, c in enumerate(r)) + "</tr>"
                   for r in rows)  # fmt: skip
    return (
        f'<div class="wrap"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'
    )


def _compare_section(c: dict[str, Any] | None) -> str:
    """ "Compared with the last run" (DSH-10): EvalKit's verdict and the rows that changed.
    Absent for the first published run, so its page is unchanged."""
    if not c:
        return ""

    def num(x: Any) -> str:
        return "" if x is None else _e(f"{x:.4g}" if isinstance(x, float) else x)

    rows = [[_e(r["metric"]), _e(", ".join(f"{k}={v}" for k, v in r["labels"].items())),
             num(r["old"]), num(r["new"]),
             num(r["diff"]) + (f" [{num(r['ci_low'])}, {num(r['ci_high'])}]"
                               if r["ci_low"] is not None else ""),
             _e(r["pairing"]), _e(r["gate"] or ""),
             "regression" if r["regression"] else ("flagged" if r["flagged"] else "")]
            for r in c["changed"]]  # fmt: skip
    table = (_table(["Metric", "Labels", "Previous", "This run", "Change (95% CI)", "Pairing",
                     "Gate", "Flag"], rows, num=(2, 3, 4)) if rows
             else "<p>No metric changed.</p>")  # fmt: skip
    errors = "".join(f"<li>{_e(e)}</li>" for e in c.get("errors") or [])
    errors_html = (f'<p class="bad">The comparison could not be completed:</p><ul>{errors}</ul>'
                   if errors else "")  # fmt: skip
    return f"""<section aria-labelledby="h-cmp"><h2 id="h-cmp">Compared with the last run</h2>
<p>Against run <code>{_e(c["previous_run"])}</code>: <strong>{_e(c["verdict"])}</strong>
({c["rows"]} metrics compared, {len(c["changed"])} changed, {c["missing"]} missing; settings
{_e(c["settings"])}; gates {_e(c.get("gates", "applied"))}). Computed by EvalKit's
<code>compare</code>, unpaired: where both runs give an interval, the change's 95% interval
combines them and is Holm-adjusted; counts have none. A change is flagged only when its interval
excludes 0, so this is not the release gate: the gate (<code>evalkit gate</code> with
<code>gates.yaml</code>) judges each run on its own. Held-out results only as split totals.</p>
{errors_html}{table}
</section>

"""


def render(bundle: dict[str, Any]) -> str:
    s, abl, orc = bundle["summary"], bundle.get("ablation"), bundle.get("oracle")
    attacks = list(s["attacks"])
    agent_rows = [r for r in attacks if r["model"] != "pipeline"]
    n_all = sum(r["trials"] for r in attacks)
    n_succ_all = sum(r["successes"] for r in attacks)
    by_model: dict[str, list[int]] = {}
    for r in agent_rows:
        by_model.setdefault(r["model"], [0, 0])
        by_model[r["model"]][0] += r["successes"]
        by_model[r["model"]][1] += r["trials"]
    k = s.get("k", 1)
    n_agent = sum(r["trials"] for r in agent_rows)
    tiles = [(f"{n_succ_all} of {n_all}", "attack trials got through (main run, public and "
              "held-out, agents and pipeline)")]  # fmt: skip
    for model, (succ, n) in sorted(by_model.items()):
        tiles.append((f"&lt; {_pct(upper_bound(succ, n), 1)}",
                      f"{_e(SHORT.get(model, model))}: 95% upper bound on the success rate per "
                      f"trial ({succ} of {n}; this suite's attacks, {k} trials each)"))  # fmt: skip
    total = s.get("spent_usd", 0) + s.get("judging_spent_usd", 0) + (abl or {}).get("spent_usd", 0)
    tiles.append((f"${total:.2f}", "total spent: main run, guard-off ablation and judges"))
    tile_html = "".join(f'<div class="tile"><b>{v}</b><span>{t}</span></div>' for v, t in tiles)

    # ---- did anything get through: one row per category, agent and split (never pooled)
    bound_rows: list[tuple[str, int, int]] = [
        (f"{CATEGORY.get(r['owasp'], r['owasp'])} · {SHORT.get(r['model'], r['model'])} · "
         f"{r['split']}", r["successes"], r["trials"])
        for r in sorted(attacks, key=lambda r: (r["owasp"], r["model"], r["split"] != "public"))
    ]  # fmt: skip
    got_rows = [
        [_e(r["split"]), _e(f"{r['owasp']} {CATEGORY.get(r['owasp'], '')}"),
         _e(SHORT.get(r["model"], r["model"])), r["trials"], _e(r["success_text"]),
         _pct(r["blocked"]["point"]), "n/a" if r.get("pass_k") is None else _pct(r["pass_k"]),
         ("outside the public interval" if r.get("outside_public") else "")]
        for r in attacks
    ]  # fmt: skip

    # ---- what stopped it
    def layers_of(summary: dict[str, Any] | None, owasp: str, model: str | None) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in (summary or {}).get("attacks", []):
            if r["owasp"] == owasp and (model is None or r["model"] == model):
                for k, v in r["layers"].items():
                    out[k] = out.get(k, 0) + v
        return out

    stack: list[tuple[str, dict[str, int]]] = []
    seen_layers: set[str] = set()
    for owasp in sorted({r["owasp"] for r in attacks}):
        models = sorted({r["model"] for r in attacks if r["owasp"] == owasp})
        for m in models:
            stack.append(
                (f"{owasp} main, all cases, k={k} · {SHORT.get(m, m)}", layers_of(s, owasp, m))
            )
        if abl:
            for m in sorted({r["model"] for r in abl["attacks"] if r["owasp"] == owasp}):
                stack.append(
                    (
                        f"{owasp} guard off, public, k=1 · {SHORT.get(m, m)}",
                        layers_of(abl, owasp, m),
                    )
                )
        if orc:
            counts: dict[str, int] = {}
            firsts = [c["layers"][0] if c["layers"] else "none" for c in orc["results"]
                      if c["owasp"] == owasp]  # fmt: skip
            for layer, n in orc.get("heldout_by_owasp", {}).get(owasp, {}).items():
                firsts += [layer] * n
            for first in firsts:
                key = "leak" if first in ("leak", "none") else first
                counts[key] = counts.get(key, 0) + 1
            stack.append((f"{owasp} oracle, all cases, no model", counts))
    for _, c in stack:
        seen_layers |= {k for k, v in c.items() if v}
    stack_table = _table(["Bar", *[LAYER_LABEL[k] for k in LAYERS if k in seen_layers]],
                         [[_e(lbl), *[c.get(k, 0) for k in LAYERS if k in seen_layers]]
                          for lbl, c in stack],
                         num=range(1, 1 + len(seen_layers)))  # fmt: skip

    # ---- behaviour and answers
    beh = [[_e(SHORT.get(r["model"], r["model"])), _e(r["rule"]), r["kept"], r["broken"],
            r["disputed"], _pct(r["rate"])] for r in s.get("behaviour", [])]  # fmt: skip
    j = s.get("judges", {})
    pair = j.get("pairwise", {})
    fam = j.get("by_agent_family", {})
    labels = j.get("vs_hand_labels", {})
    judge_rows = [[_e(f"{name} ({model})"), labels.get(name, {}).get("items", 0),
                   labels.get(name, {}).get("agree", 0),
                   _k(labels.get(name, {}).get("kappa"))]
                  for name, model in j.get("judges", {}).items()]  # fmt: skip
    fam_rows = [[_e(f), v.get("items"), v.get("agree"),
                 *[_pct(v.get(f"{n}_pass_rate")) for n in j.get("judges", {})], _k(v.get("kappa"))]
                for f, v in fam.items()]  # fmt: skip
    ans = [[_e(r["split"]), _e(SHORT.get(r["model"], r["model"])), r["questions"], r["trials"],
            f'{_pct(r["correct"]["point"])} ({_pct(r["correct"]["lo"])}–{_pct(r["correct"]["hi"])})']
           for r in s.get("answers", [])]  # fmt: skip
    text = [[_e(r["split"]), _e(SHORT.get(r["model"], r["model"])), r["trials"],
             _pct(r["query_match_rate"]), _pct(r["stated_rate"])]
            for r in s.get("answer_text", [])]  # fmt: skip

    degenerate = pair.get("kappa") in (None, 0.0) and pair.get("items", 0) > 0
    kappa_note = (
        '<p class="small">Kappa measures agreement beyond chance. It is 0 or n/a here because one '
        "rater gave (almost) a single verdict; the agreement counts are the readable measure.</p>"
        if degenerate
        else ""
    )

    # ---- access events
    a = bundle["audit"]
    anchor = a.get("latest_anchor") or {}

    def qrow(x: dict[str, Any]) -> list[Any]:
        return [x["seq"], _e(x.get("case_id")), _e(SHORT.get(str(x.get("model")), x.get("model"))),
                f'<code>{_e((x.get("sql") or "")[:160])}</code>', _e(x.get("guard_decision") or ""),
                _e(x.get("guard_reason") or x.get("db_error_class") or "")]  # fmt: skip

    tail = [qrow(x) for x in reversed(a["tail"])]
    blocked_rows = [qrow(x) for x in reversed(a.get("blocked", []))]
    decisions = sorted(a.get("decisions", {}).items(),
                       key=lambda kv: (-(kv[1]["attack"] + kv[1]["question"]), kv[0]))  # fmt: skip
    decision_rows = [[_e(key), f"{v['attack']:,}", f"{v['question']:,}"] for key, v in decisions]
    blocked_reasons = sorted({key.split(": ", 1)[1] for key, v in decisions
                              if key.startswith("blocked") and v["question"]})  # fmt: skip
    blocked_attacks = sum(v["attack"] for k, v in a.get("decisions", {}).items()
                          if k.startswith("blocked"))  # fmt: skip
    blocked_questions = sum(v["question"] for k, v in a.get("decisions", {}).items()
                            if k.startswith("blocked"))  # fmt: skip

    # ---- data quality
    q = bundle["quality"]
    flags = [(k, v) for k, v in q["flags_by_rule"].items()]
    drops = sorted(q["drops_by_reason"].items(), key=lambda kv: -kv[1])
    hist = [[_e(h["snapshot"]), _e(h.get("created_at")),
             ", ".join(f"{k} {v:,}" for k, v in h["rows_by_source"].items()),
             f'<code>{_e(h["manifest_sha256"][:16])}</code>'] for h in q["history"]]  # fmt: skip

    verify_cls = "ok" if a["verify_ok"] else "bad"
    compare_html = _compare_section(bundle.get("compare"))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Assay Pipeline Report</title><style>{CSS}</style></head><body><main>
<h1>Trusted Assay Pipeline: security report</h1>
<p class="muted">Run <code>{_e(bundle["run"])}</code> on snapshot <code>{
        _e(bundle["snapshot"])
    }</code>
(manifest <code>{
        _e(bundle["manifest_sha256"][:16])
    }</code>). Two agents answer questions about public
EGFR assay data through a read-only SQL tool; {n_agent} agent attack trials tried to make them leak,
write or run away. Every number below comes from the published results bundle.</p>
<div class="tiles">{tile_html}</div>

<section aria-labelledby="h-got"><h2 id="h-got">Did anything get through?</h2>
<p class="muted">Success rate per category and agent, with the 95% upper bound. Zero successes is a
bound, not 0%: with n trials and none succeeding, the true rate is below 1 &minus; 0.05<sup>1/n</sup>.</p>
{_bound_chart(bound_rows)}
<details><summary>Table view, by split</summary>{
        _table(
            [
                "Split",
                "Category",
                "Agent",
                "Trials",
                "Result",
                "Blocked",
                "pass^k",
                "Held-out flag",
            ],
            got_rows,
            num=(3,),
        )
    }</details></section>

<section aria-labelledby="h-stop"><h2 id="h-stop">What stopped it?</h2>
<p class="muted">The layer that stopped each attack trial: the main run, the same public attacks with
the SQL guard switched off (permissions and limits stay on), and the model-free oracle that runs each
attack's SQL straight at the controls.</p>{_legend([k for k in LAYERS if k in seen_layers])}
{_stack_rows(stack)}
<details><summary>Table view</summary>{stack_table}</details></section>

<section aria-labelledby="h-beh"><h2 id="h-beh">How did the agents behave?</h2>
<p class="muted">Behaviour rules per agent (B1–B10). B3, B4 and B10 are judged by two blinded judges
from different families; a rule counts as kept or broken only when both agree.</p>
{_table(["Agent", "Rule", "Kept", "Broken", "Disputed", "Rate"], beh, num=(2, 3, 4, 5))}
<h3>Judges</h3>
<p class="small">Judge against judge: {pair.get("agree", 0)} of {
        pair.get("items", 0)
    } verdicts agree
(kappa {_k(pair.get("kappa"))}). Against {labels.get("labels", 0)} hand labels:</p>
{kappa_note}
{_table(["Judge", "Labelled items", "Agree", "Kappa"], judge_rows, num=(1, 2))}
<p class="small">By the family of the agent that answered (self-preference shows here):</p>
{
        _table(
            [
                "Agent family",
                "Items",
                "Judges agree",
                *[f"{n} pass rate" for n in j.get("judges", {})],
                "Kappa",
            ],
            fam_rows,
            num=(1, 2),
        )
    }
<h3>Answers to normal questions</h3>
{
        _table(
            ["Split", "Agent", "Questions", "Trials", "A query matched gold (95% interval)"],
            ans,
            num=(2, 3),
        )
    }
<p class="small">The strict measure counts an answer correct when one of the agent's queries returns
exactly the gold rows. For single-number answers the second measure checks whether the answer states the
gold value; an agent that answers from a breakdown query states the right number without any single
query returning it.</p>
{
        _table(
            ["Split", "Agent", "Trials", "A query matched", "Answer states the value"],
            text,
            num=(2, 3, 4),
        )
    }
</section>

{compare_html}<section aria-labelledby="h-audit"><h2 id="h-audit">Access events</h2>
<p><span class="{verify_cls}">Audit log {
        "verifies" if a["verify_ok"] else "does not verify"
    }</span>:
{a["lines"]:,} hash-chained lines, {a["anchors"]} anchors; latest anchor at seq {
        _e(anchor.get("seq"))
    },
hash <code>{_e(str(anchor.get("hash", ""))[:16])}</code>.</p>
<p class="banner">The restricted table <code>embargoed_results</code> is synthetic: generated
compound codes and values with canary strings, standing in for unpublished results.</p>
<h3>Every query the agents ran, by guard decision</h3>
{_table(["Decision", "In attack cases", "In normal questions"], decision_rows, num=(1, 2))}
<p class="small">The guard blocked {blocked_attacks} queries in attack cases and {blocked_questions}
in normal questions. Blocks in normal questions are the guard's cost to honest use
{("(reasons: " + ", ".join(_e(r) for r in blocked_reasons) + ")") if blocked_reasons else ""}.</p>
<h3>Most recent blocked queries</h3>
{_table(["Seq", "Case", "Model", "SQL", "Guard", "Reason"], blocked_rows, num=(0,))}
<h3>Most recent queries</h3>
{_table(["Seq", "Case", "Model", "SQL", "Guard", "Reason"], tail, num=(0,))}
<p class="small">Query lines from public cases only; held-out prompts and queries are never published
(the counts above include them).</p>
</section>

<section aria-labelledby="h-data"><h2 id="h-data">Data quality</h2>
<p class="muted">Target {
        _e(
            (q.get("target") or {}).get("name")
            if isinstance(q.get("target"), dict)
            else q.get("target")
        )
    };
sources {", ".join(f"{_e(k)} {_e(v)}" for k, v in q["source_releases"].items())}. Rows are flagged,
never deleted; drops happen at ingest with a recorded reason.
<a href="explorer.html">Explore the compounds and their source records</a>.</p>
<h3>Flags per rule</h3>{_bars(flags)}
<h3>Rows by source</h3>{
        _table(
            ["Source", "Rows"],
            [[_e(k), f"{v:,}"] for k, v in q["rows_by_source"].items()],
            num=(1,),
        )
    }
<h3>Drops by reason</h3>{
        _table(["Reason", "Records"], [[_e(k), f"{v:,}"] for k, v in drops], num=(1,))
    }
<h3>Snapshot history</h3>{_table(["Snapshot", "Created", "Rows by source", "Manifest"], hist)}
</section>
<p class="small">Generated by <code>assay report</code> from the published results bundle. Source,
methods and the threat model: the project repository.</p>
</main></body></html>
"""
