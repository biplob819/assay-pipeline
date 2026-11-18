"""Load and check the attack suite and normal questions (Tech Spec §7.1, ATK-01..09).

The public split ships inside the package; the held-out split lives in `heldout/` (git-ignored)
and is pinned by `heldout/MANIFEST.json` (sha256 per file, counts per category), which is
committed so a held-out run can prove which items it used (D-12).

The held-out split runs the agents once per suite version (D-12), so its files never change
under one version: the committed manifest names its `suite_version`, a test pins each
version's manifest hash (`SUITE_MANIFESTS`), and a retired id is never given to new content
(D-45). A manifest with no `suite_version` is suite 1, so an archived suite-1 folder still
verifies and loads with its retired items, as an archived suite-2 folder does with its own
(OWNER D-47). The runner and the assessor load the held-out split pinned: its manifest must be
the very one pinned for its version, so a locally rewritten manifest is refused before any
model sees a held-out item.
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
SUITE_VERSION = 3
# sha256 of the committed heldout/MANIFEST.json of each held-out suite version (D-45)
SUITE_MANIFESTS = {
    1: "2b475b09512a5d41bc4769c5238fb3a15c3f5d7caa9192e001ae9dcb568a8719",  # main-2026-11-16
    2: "e5d7b1eae570a31aefe48ab20a4e773bcb9726f568e9a71fa2c2c2eb9587bdbe",  # never ran
    3: "8fd4311bf3c67d736cfd1f4e1344cbb9ee0aa98f96005457ad87ec7bf20d9a43",  # OWNER D-47
}
# id -> the suite version that retired it; the id is refused from that version on (D-45)
# nq-26 and nq-30: their wording was published (D-12 update, 2026-11-16); nq-14: its wording is
# close to a public question's (OWNER D-47)
RETIRED = {"nq-26": 2, "nq-30": 2, "nq-14": 3}


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
    heldout_version: int | None = None  # the held-out folder's suite version, when loaded


def load_suite(heldout_dir: Path | str | None = None, *, pinned: bool = False) -> Suite:
    """The public split, plus the held-out split when `heldout_dir` is given (hash-checked;
    with `pinned`, its manifest must also be the one pinned for its suite version)."""
    attacks = _cases(_packaged("attacks.yaml"), "data/attacks.yaml")
    questions = _questions(_packaged("questions.yaml"), "data/questions.yaml")
    _same_split([*attacks, *questions], "public", "data/")
    version: int | None = None
    if heldout_dir is not None:
        d = Path(heldout_dir)
        version = verify_heldout(d, pinned=pinned)
        held_a = _cases((d / "attacks.yaml").read_text(encoding="utf-8"), "heldout/attacks")
        held_q = _questions((d / "questions.yaml").read_text(encoding="utf-8"), "heldout/questions")
        _same_split([*held_a, *held_q], "heldout", "heldout/")
        attacks += held_a
        questions += held_q
    suite = Suite(attacks, questions, heldout_dir is not None, version)
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
    held = SUITE_VERSION if suite.heldout_version is None else suite.heldout_version
    for item in [*suite.attacks, *suite.questions]:  # an archived version exempts its own items
        current = held if item.split == "heldout" else SUITE_VERSION
        if item.id in RETIRED and current >= RETIRED[item.id]:
            problems.append(f"{item.id}: retired in suite {RETIRED[item.id]}; "
                            "new content needs a new id")  # fmt: skip
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


def write_heldout_manifest(d: Path | str, suite_version: int = SUITE_VERSION) -> Path:
    if suite_version not in SUITE_MANIFESTS or suite_version < 2:
        raise ConfigError(f"suite version {suite_version}: not a writable version (D-45)")
    d = Path(d)
    path = d / MANIFEST
    body = {**heldout_manifest(d), "suite_version": suite_version}
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def verify_heldout(d: Path | str, *, pinned: bool = False) -> int:
    """Check the held-out files against their manifest; return its suite version. With
    `pinned`, the manifest must also be byte for byte the one pinned for that version."""
    d = Path(d)
    try:
        raw = (d / MANIFEST).read_bytes()
        recorded = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{d}: no valid {MANIFEST} ({exc})") from exc
    if not isinstance(recorded, dict):
        raise ConfigError(f"{d}: no valid {MANIFEST} (not an object)")
    # suite 1's manifest predates the field, so only a missing key means suite 1; an explicit
    # version is one written from suite 2 on
    explicit = "suite_version" in recorded
    version = recorded.pop("suite_version") if explicit else 1
    if type(version) is not int or version not in SUITE_MANIFESTS or (explicit and version < 2):
        raise ConfigError(f"{d}: {MANIFEST} names an unknown suite version {version!r}")
    if pinned and hashlib.sha256(raw).hexdigest() != SUITE_MANIFESTS[version]:
        raise ConfigError(f"{d}: {MANIFEST} is not the manifest pinned for suite {version}")
    if heldout_manifest(d) != recorded:
        raise ConfigError(f"{d}: the held-out files do not match {MANIFEST}")
    return version
