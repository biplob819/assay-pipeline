"""The guard-off ablation's stand-in guard (Tech Spec §7.3, D-10): it allows every statement
as written. Only the ablation uses it, on a sacrificial database copy; the read-only, locked,
SELECT-only connection and the limits still apply, so what stops an attack here is the model,
the permissions or the limits."""

from __future__ import annotations

from assay_pipeline.config import ViewsConfig
from assay_pipeline.schemas import GuardDecision


class PassThroughGuard:
    def __init__(self, views: ViewsConfig) -> None:
        self.limit = views.limits.max_rows_executed

    def check(self, sql: str) -> GuardDecision:
        return GuardDecision(
            decision="allowed",
            reason=None,
            rendered_sql=sql,
            tables=(),
            functions=(),
            limit_applied=self.limit,
        )
