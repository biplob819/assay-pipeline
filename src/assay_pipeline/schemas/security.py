"""Security-side records (Tech Spec §3, §6): guard decisions, audit lines, anchors."""

from __future__ import annotations

from typing import Annotated, Literal, Self, get_args

from pydantic import Field, model_validator

from ._base import (
    ZERO_HASH,
    Model,
    Name,
    NonBlank,
    NonNegInt,
    PosInt,
    Sha256,
    Timestamp,
    Versioned,
)

# Closed set of guard block reasons, in the order of the checks in Tech Spec §6.1.
GuardReason = Literal[
    "parse_error",
    "multiple_statements",
    "not_select",
    "recursive_cte",
    "table_not_allowed",
    "cte_shadows_view",
    "file_reference",
    "function_not_allowed",
    "catalog_access",
]
GUARD_REASONS: tuple[str, ...] = get_args(GuardReason)
DbErrorClass = Literal["timeout", "memory", "permission", "other"]
AuditEvent = Literal["genesis", "question", "query", "answer"]


class GuardDecision(Model):
    decision: Literal["allowed", "blocked"]
    reason: GuardReason | None
    rendered_sql: NonBlank | None  # what actually runs (re-rendered from the AST, D-5)
    tables: tuple[Name, ...]
    functions: tuple[Name, ...]
    limit_applied: PosInt | None

    @model_validator(mode="after")
    def _paired(self) -> Self:
        if self.decision == "allowed":
            if self.reason is not None or self.rendered_sql is None or self.limit_applied is None:
                raise ValueError("an allowed decision has rendered_sql and a limit, and no reason")
        elif self.reason is None or self.rendered_sql is not None or self.limit_applied is not None:
            raise ValueError("a blocked decision has a reason, and no rendered_sql or limit")
        return self


_QUERY_FIELDS = (
    "sql",
    "rendered_sql",
    "guard_decision",
    "guard_reason",
    "rows_returned",
    "rows_shown",
    "result_sha256",
    "db_error_class",
)


class AuditLine(Versioned):
    """One hash-chained line of the audit log (§6.3). Field presence follows `event`."""

    seq: NonNegInt
    at: Timestamp
    event: AuditEvent
    run_id: Name
    trial_id: Name | None
    case_id: Name | None
    model: Name | None
    question: NonBlank | None
    sql: NonBlank | None
    rendered_sql: NonBlank | None
    guard_decision: Literal["allowed", "blocked"] | None
    guard_reason: GuardReason | None
    rows_returned: NonNegInt | None
    rows_shown: NonNegInt | None
    result_sha256: Sha256 | None
    db_error_class: DbErrorClass | None
    answer_sha256: Sha256 | None
    prev_hash: Sha256
    hash: Sha256

    def _set(self, *names: str) -> list[str]:
        return [n for n in names if getattr(self, n) is not None]

    @model_validator(mode="after")
    def _by_event(self) -> Self:
        e = self.event
        if (e == "genesis") != (self.seq == 0):
            raise ValueError("seq 0 is the genesis line, and only the genesis line")
        if e == "genesis":
            extra = self._set("trial_id", "case_id", "model", "question", "answer_sha256")
            extra += self._set(*_QUERY_FIELDS)
            if extra or self.prev_hash != ZERO_HASH:
                raise ValueError("the genesis line carries only run_id and a zero prev_hash")
            return self
        if self.model is None or self.trial_id is None:
            raise ValueError(f"a {e} line names the model and the trial (audit coverage, §6.3)")
        if (self.question is not None) != (e == "question"):
            raise ValueError("question text appears on question lines only, and is required")
        if (self.answer_sha256 is not None) != (e == "answer"):
            raise ValueError("answer_sha256 appears on answer lines only, and is required")
        if e != "query":
            if self._set(*_QUERY_FIELDS):
                raise ValueError(f"a {e} line carries no SQL or guard fields")
            return self
        if self.sql is None or self.guard_decision is None:
            raise ValueError("a query line carries the model's SQL and the guard decision")
        if self.guard_decision == "blocked":
            if self.guard_reason is None:
                raise ValueError("a blocked query line names the guard reason")
            if self._set(
                "rendered_sql", "rows_returned", "rows_shown", "result_sha256", "db_error_class"
            ):
                raise ValueError("a blocked query never reached the database")
            return self
        if self.guard_reason is not None or self.rendered_sql is None:
            raise ValueError("an allowed query line has rendered_sql and no guard reason")
        ran = self._set("rows_returned", "rows_shown", "result_sha256")
        if self.db_error_class is not None:
            if ran:
                raise ValueError("a query that failed in the database returned no result")
        elif len(ran) != 3:
            raise ValueError("a query that ran records rows_returned, rows_shown, result_sha256")
        elif (self.rows_shown or 0) > (self.rows_returned or 0):
            raise ValueError("rows_shown cannot exceed rows_returned")
        return self


class Anchor(Versioned):
    """A (seq, hash) pair of the audit log kept outside it, so truncation is detected (D-7)."""

    log: NonBlank
    seq: Annotated[int, Field(ge=0, strict=True)]
    hash: Sha256
    at: Timestamp
