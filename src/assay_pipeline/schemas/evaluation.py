"""Evaluation records (Tech Spec §3, §7): attack cases, normal questions, trial results."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import StrictBool, StringConstraints, model_validator

from ._base import FrozenMap, JsonValue, Name, NonBlank, NonNegFloat, NonNegInt, PosInt, Versioned

Owasp = Literal["LLM01", "LLM02", "LLM04", "LLM05", "LLM06", "LLM10"]
StopLayer = Literal["guard", "permissions", "limit", "quality_rule", "model"]
Split = Literal["public", "heldout"]
BEHAVIOUR_RULES: tuple[str, ...] = tuple(f"B{i}" for i in range(1, 11))
RuleId = Annotated[str, StringConstraints(pattern=r"^B([1-9]|10)$")]
OwaspAny = Annotated[str, StringConstraints(pattern=r"^LLM(0[1-9]|10)$")]


def _unique(values: tuple[str, ...], what: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{what} must be unique")


class AttackCase(Versioned):
    id: Annotated[str, StringConstraints(pattern=r"^atk-llm(01|02|04|05|06|10)-[0-9]{2}$")]
    owasp: Owasp
    owasp_secondary: tuple[OwaspAny, ...] = ()
    atlas: NonBlank
    prompt: NonBlank
    planted_data: FrozenMap[JsonValue] | None = None
    attacker_sql: tuple[NonBlank, ...]
    expected_stop: tuple[StopLayer, ...]
    split: Split

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.id[4:9] != self.owasp.lower():
            raise ValueError(f"id {self.id} does not match category {self.owasp}")
        if self.owasp in self.owasp_secondary:
            raise ValueError("owasp_secondary must not repeat the primary category")
        _unique(self.owasp_secondary, "owasp_secondary")
        if not self.expected_stop:
            raise ValueError("expected_stop names at least one layer")
        _unique(self.expected_stop, "expected_stop")
        if self.owasp == "LLM04":
            if not self.planted_data or self.attacker_sql:
                raise ValueError("an LLM04 case plants data and runs no attacker SQL")
            if self.expected_stop != ("quality_rule",):
                raise ValueError("an LLM04 case is stopped by the quality rules")
        else:
            if not self.attacker_sql:
                raise ValueError("an agent attack lists the SQL it wants executed (§7.3 oracle)")
            if "quality_rule" in self.expected_stop:
                raise ValueError("only LLM04 cases are stopped by the quality rules")
        return self


class NormalQuestion(Versioned):
    id: Annotated[str, StringConstraints(pattern=r"^nq-[0-9]{2}$")]
    question: NonBlank
    kind: Literal["stat", "censored", "ambiguous", "off_scope"]
    gold_sql: NonBlank | None
    rules: tuple[RuleId, ...]
    split: Split

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        needs_gold = self.kind in ("stat", "censored")
        if (self.gold_sql is not None) != needs_gold:
            raise ValueError(
                f"gold_sql is required for stat and censored questions only ({self.kind})"
            )
        if not self.rules:
            raise ValueError("a question tests at least one behaviour rule")
        _unique(self.rules, "rules")
        return self


class TrialResult(Versioned):
    case_id: Annotated[
        str, StringConstraints(pattern=r"^(atk-llm(01|02|04|05|06|10)|nq)-[0-9]{2}$")
    ]
    model: Name
    trial: PosInt
    outcome: Literal["blocked", "partial", "succeeded"] | None
    answer_correct: StrictBool | None
    stopping_layer: Literal["guard", "permissions", "limit", "quality_rule", "model", "leak"] | None
    behaviour: FrozenMap[StrictBool | None]
    audit_seq: tuple[NonNegInt, NonNegInt]
    cost_usd: NonNegFloat
    status: Literal["scored", "void", "transport"]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        unknown = set(self.behaviour) - set(BEHAVIOUR_RULES)
        if unknown:
            raise ValueError(f"unknown behaviour rules: {sorted(unknown)}")
        if self.audit_seq[0] > self.audit_seq[1]:
            raise ValueError("audit_seq is (first, last) with first <= last")
        scored = (self.outcome, self.answer_correct, self.stopping_layer)
        if self.status != "scored":
            if any(v is not None for v in scored):
                raise ValueError(f"a {self.status} trial carries no outcome")
            return self
        if re.match(r"^atk-", self.case_id):
            if (
                self.outcome is None
                or self.stopping_layer is None
                or self.answer_correct is not None
            ):
                raise ValueError("a scored attack has an outcome and a stopping layer only")
            if (self.stopping_layer == "leak") != (self.outcome == "succeeded"):
                raise ValueError("stopping_layer is 'leak' exactly when the attack succeeded")
            llm04 = self.case_id.startswith("atk-llm04-")
            if (self.stopping_layer == "quality_rule") != (llm04 and self.outcome != "succeeded"):
                raise ValueError("stopping_layer 'quality_rule' is for, and only for, LLM04 cases")
        elif self.outcome is not None or self.stopping_layer is not None:
            raise ValueError("a normal question has no attack outcome or stopping layer")
        return self
