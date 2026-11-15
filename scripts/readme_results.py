"""Write the README's results block from the published bundle (REL-01).

Every number in the block comes from `docs/results/<LATEST>` (the bundle the Pages job renders),
so the README cannot drift from the published run: `tests/harness/test_release.py` regenerates
the block and compares it with the README.

    uv run python scripts/readme_results.py          # rewrite the block in README.md
    uv run python scripts/readme_results.py --check  # exit 1 if README.md is stale
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

from assay_pipeline.report.html import upper_bound

ROOT = Path(__file__).resolve().parents[1]
START, END = "<!-- results:start -->", "<!-- results:end -->"
AGENTS = {"claude-sonnet-5-5": "Claude Sonnet 5.5", "gpt-6.1-sol": "GPT-6.1 Sol"}


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def _pct1(x: float) -> str:
    return f"{100 * x:.1f}%"


def _by_count(counts: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))


def _totals(rows: list[dict]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        out[r["model"]][0] += r["successes"]
        out[r["model"]][1] += r["trials"]
    return out


def block(bundle: dict, bundle_rel: str) -> str:
    s, q, a, o = bundle["summary"], bundle["quality"], bundle["audit"], bundle["oracle"]
    att, abl = _totals(s["attacks"]), _totals(bundle["ablation"]["attacks"])
    layers: dict[str, int] = defaultdict(int)
    for r in s["attacks"]:
        for k, v in r["layers"].items():
            layers[k] += v
    total_trials = sum(t for _, t in att.values())
    total_succ = sum(x for x, _ in att.values())
    abl_succ = sum(x for x, _ in abl.values())
    beh = {(r["model"], r["rule"]): r["rate"] for r in s["behaviour"]}
    stated = {r["model"]: r for r in s["answer_text"] if r["split"] == "public"}
    j = s["judges"]
    spent = s["spent_usd"] + s.get("judging_spent_usd", 0) + bundle["ablation"]["spent_usd"]
    oracle_layers: dict[str, int] = defaultdict(int)
    for r in o["results"] if o else []:
        oracle_layers[r["layers"][0]] += 1  # the first layer that stopped it
    for counts in (o["heldout_by_owasp"] if o else {}).values():
        for k, v in counts.items():
            oracle_layers[k] += v

    lines = [
        START,
        f"Run `{bundle['run']}` on snapshot `{bundle['snapshot']}`, from "
        f"[`{bundle_rel}`]({bundle_rel}) ([dashboard](https://biplob819.github.io/assay-pipeline/)).",
        "",
        "| | |",
        "| --- | --- |",
        f"| Snapshot | {sum(q['rows_by_source'].values()):,} {q['target']} rows "
        f"({', '.join(f'{k} {v:,}' for k, v in sorted(q['rows_by_source'].items()))}); "
        f"flagged, never deleted: "
        f"{', '.join(f'{k} {v:,}' for k, v in sorted(q['flags_by_rule'].items()) if v)} |",
        f"| Attack trials that succeeded | **{total_succ} of {total_trials}**: "
        f"{sum(t for m, (_, t) in att.items() if m in AGENTS)} agent trials (k={s['k']} per case, "
        f"public and held-out cases) and {att['pipeline'][1]} poisoning cases met by the "
        "pipeline itself |",
    ]
    for m, name in AGENTS.items():
        x, n = att[m]
        bound = _pct1(upper_bound(x, n))
        lines.append(f"| {name} | {x} of {n}; 95% upper bound {bound} per trial |")
    lines += [
        f"| What stopped them | {_by_count(layers)}"
        + (
            " (most attacks were refused by the model before any query ran)"
            if 2 * layers.get("model", 0) > total_trials
            else ""
        )
        + " |",
        f"| Guard off (ablation) | {abl_succ} of {sum(t for _, t in abl.values())} succeeded"
        + (": the models refused on their own" if abl_succ == 0 else "")
        + " |",
        (
            f"| Scripted attacker (no model) | {o['passed']} of {o['cases']} cases stopped: "
            f"{_by_count(oracle_layers)} |"
            if o
            else "| Scripted attacker (no model) | not run for this bundle |"
        ),
        "| Normal questions (public split) | "
        + "; ".join(
            f"{name}: query matches gold {_pct(stated[m]['query_match_rate'])}, "
            f"answer states the gold value {_pct(stated[m]['stated_rate'])}"
            for m, name in AGENTS.items()
        )
        + " |",
        "| SQL shown in the answer is what ran (B1, all questions) | "
        + "; ".join(f"{name} {_pct(beh[(m, 'B1')])}" for m, name in AGENTS.items())
        + " |",
        f"| Judges | agree with each other on {j['pairwise']['agree']} of "
        f"{j['pairwise']['items']}; "
        f"against {j['vs_hand_labels']['labels']} hand labels, Opus "
        f"{j['vs_hand_labels']['opus']['agree']} and Sol {j['vs_hand_labels']['sol']['agree']} |",
        f"| Audit log | {a['lines']:,} lines, {a['anchors']} anchors, "
        f"{'verifies' if a['verify_ok'] else 'DOES NOT VERIFY'} |",
        f"| Spend | ${spent:.2f} for the run, the guard-off ablation and the judges |",
        END,
    ]
    return "\n".join(lines)


def current() -> str:
    rel = (ROOT / "docs/results/LATEST").read_text(encoding="utf-8").strip()
    bundle = json.loads((ROOT / "docs/results" / rel).read_text(encoding="utf-8"))
    return block(bundle, f"docs/results/{rel}")


def splice(readme: str, new: str) -> str:
    i, k = readme.index(START), readme.index(END) + len(END)
    return readme[:i] + new + readme[k:]


def main(argv: list[str]) -> int:
    path = ROOT / "README.md"
    text = path.read_text(encoding="utf-8")
    want = splice(text, current())
    if "--check" in argv:
        return 0 if want == text else 1
    path.write_text(want, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
