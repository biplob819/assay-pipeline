"""Load and check the attack suite and normal questions (Tech Spec §7.1, ATK-01..07).

The public split ships inside the package; the held-out split lives in `heldout/` (git-ignored)
and is pinned by `heldout/MANIFEST.json` (sha256 per file, counts per category), which is
committed so a held-out run can prove which items it used (D-12).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from assay_pipeline.config import ConfigError, _UniqueKeyLoader
from assay_pipeline.schemas import AttackCase, NormalQuestion

CATEGORIES = ("LLM01", "LLM02", "LLM04", "LLM05", "LLM06", "LLM10")
PER_CATEGORY = 10
HELDOUT_PER_CATEGORY = 2
QUESTION_KINDS = {"stat": 14, "censored": 8, "ambiguous": 4, "off_scope": 4}
HELDOUT_QUESTIONS = 6
CANARY_PATTERN = re.compile(r"(EMB|PRM)CANARY", re.IGNORECASE)
MANIFEST = "MANIFEST.json"


def _yaml(text: str, name: str) -> Any:
    try:
        return yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{name}: not valid YAML: {exc}") from exc


def _packaged(name: str) -> str:
    return resources.files("assay_pipeline").joinpath(f"data/{name}").read_text(encoding="utf-8")


def load_atlas() -> dict[str, str]:
    data = _yaml(_packaged("atlas.yaml"), "data/atlas.yaml")
    techniques = data.get("techniques") if isinstance(data, dict) else None
    if not isinstance(techniques, dict) or not data.get("checked") or not data.get("version"):
        raise ConfigError("data/atlas.yaml needs techniques, a version and a check date")
    return {str(k): str(v) for k, v in techniques.items()}


def _cases(text: str, name: str) -> tuple[AttackCase, ...]:
    data = _yaml(text, name)
    rows = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ConfigError(f"{name}: expected a `cases` list")
    try:
        return tuple(AttackCase.model_validate(r) for r in rows)
    except ValueError as exc:
        raise ConfigError(f"{name}: {exc}") from exc


def _questions(text: str, name: str) -> tuple[NormalQuestion, ...]:
    data = _yaml(text, name)
    rows = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ConfigError(f"{name}: expected a `questions` list")
    try:
        return tuple(NormalQuestion.model_validate(r) for r in rows)
    except ValueError as exc:
        raise ConfigError(f"{name}: {exc}") from exc


def _same_split(rows: Sequence[AttackCase | NormalQuestion], split: str, where: str) -> None:
    wrong = [r.id for r in rows if r.split != split]
    if wrong:
        raise ConfigError(f"{where}: {', '.join(wrong)} must be split {split!r}")


@dataclass(frozen=True)
class Suite:
    attacks: tuple[AttackCase, ...]
    questions: tuple[NormalQuestion, ...]
    includes_heldout: bool


def load_suite(heldout_dir: Path | str | None = None) -> Suite:
    """The public split, plus the held-out split when `heldout_dir` is given (hash-checked)."""
    attacks = _cases(_packaged("attacks.yaml"), "data/attacks.yaml")
    questions = _questions(_packaged("questions.yaml"), "data/questions.yaml")
    _same_split([*attacks, *questions], "public", "data/")
    if heldout_dir is not None:
        d = Path(heldout_dir)
        verify_heldout(d)
        held_a = _cases((d / "attacks.yaml").read_text(encoding="utf-8"), "heldout/attacks")
        held_q = _questions((d / "questions.yaml").read_text(encoding="utf-8"), "heldout/questions")
        _same_split([*held_a, *held_q], "heldout", "heldout/")
        attacks += held_a
        questions += held_q
    suite = Suite(attacks, questions, heldout_dir is not None)
    problems = check_suite(suite)
    if problems:
        raise ConfigError("attack suite: " + "; ".join(problems))
    return suite


def check_suite(suite: Suite) -> list[str]:
    from assay_pipeline.pipeline.planted import load_planted

    problems: list[str] = []
    atlas = load_atlas()
    ids = [c.id for c in suite.attacks] + [q.id for q in suite.questions]
    if len(ids) != len(set(ids)):
        problems.append("an id repeats")
    planted = {c.id for c in load_planted().cases}
    for c in suite.attacks:
        if c.atlas not in atlas:
            problems.append(f"{c.id}: ATLAS {c.atlas} is not in data/atlas.yaml")
        texts = [c.prompt, *c.attacker_sql, json.dumps(c.planted_data or {})]
        if any(CANARY_PATTERN.search(t) for t in texts):
            problems.append(f"{c.id}: a canary appears in a prompt or planted text (ATK-06)")
        if c.owasp == "LLM04":
            pc = (c.planted_data or {}).get("planted_case")
            if pc not in planted:
                problems.append(f"{c.id}: planted_case {pc!r} is not a Q2.1 planted case")
            if tuple(c.expected_stop) != ("quality_rule",):
                problems.append(f"{c.id}: an LLM04 case is stopped by quality_rule only")
        else:
            if c.planted_data is not None and not (c.planted_data.get("description_texts")):
                problems.append(f"{c.id}: planted data for an agent case needs description_texts")
            if not ENFORCED & set(c.expected_stop):
                problems.append(f"{c.id}: expected_stop names no enforced layer (ATK-02)")
            if c.atlas == INDIRECT_ATLAS and not (c.planted_data or {}).get("description_texts"):
                problems.append(f"{c.id}: an indirect-injection case needs planted descriptions")
    for q in suite.questions:
        if CANARY_PATTERN.search(q.question):
            problems.append(f"{q.id}: a canary appears in a question (ATK-06)")
    public = [c for c in suite.attacks if c.split == "public"]
    per = Counter(c.owasp for c in public)
    want_public = PER_CATEGORY - HELDOUT_PER_CATEGORY
    for cat in CATEGORIES:
        if per.get(cat, 0) != want_public:
            problems.append(f"{cat}: {per.get(cat, 0)} public cases, want {want_public}")
    if suite.includes_heldout:
        allc = Counter(c.owasp for c in suite.attacks)
        for cat in CATEGORIES:
            if allc.get(cat, 0) != PER_CATEGORY:
                problems.append(f"{cat}: {allc.get(cat, 0)} cases, want {PER_CATEGORY}")
        held_q = sum(1 for q in suite.questions if q.split == "heldout")
        if held_q != HELDOUT_QUESTIONS:
            problems.append(f"{held_q} held-out questions, want {HELDOUT_QUESTIONS}")
        kinds = Counter(q.kind for q in suite.questions)
        if dict(kinds) != QUESTION_KINDS:
            problems.append(f"question kinds {dict(kinds)}, want {QUESTION_KINDS}")
    return problems


HELDOUT_FILES = ("attacks.yaml", "questions.yaml", "gold.json")
ENFORCED = {"guard", "permissions", "limit"}
INDIRECT_ATLAS = "AML.T0051.001"


def heldout_manifest(d: Path) -> dict[str, Any]:
    files = {
        n: hashlib.sha256((d / n).read_bytes()).hexdigest()
        for n in HELDOUT_FILES  # gold.json too: an edited held-out gold must be refused
    }
    attacks = _cases((d / "attacks.yaml").read_text(encoding="utf-8"), "heldout/attacks")
    questions = _questions((d / "questions.yaml").read_text(encoding="utf-8"), "heldout/questions")
    return {
        "files": files,
        "attacks_per_category": dict(sorted(Counter(c.owasp for c in attacks).items())),
        "questions_per_kind": dict(sorted(Counter(q.kind for q in questions).items())),
    }


def write_heldout_manifest(d: Path | str) -> Path:
    d = Path(d)
    path = d / MANIFEST
    path.write_text(
        json.dumps(heldout_manifest(d), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def verify_heldout(d: Path | str) -> None:
    d = Path(d)
    try:
        recorded = json.loads((d / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{d}: no valid {MANIFEST} ({exc})") from exc
    if heldout_manifest(d) != recorded:
        raise ConfigError(f"{d}: the held-out files do not match {MANIFEST}")
