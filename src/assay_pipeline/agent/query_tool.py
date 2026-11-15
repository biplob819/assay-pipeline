"""The agent's one tool, `query(sql)` (Tech Spec §2.3, D-28).

The guard checks the SQL in this process. An allowed query runs in a child process that opens
the locked connection, executes the guard's re-rendered SQL with the interrupt timer and the
fetch budget, and returns the model-facing text. The parent kills the child at the timeout plus
a short grace, so DuckDB's bind and plan phases cannot outlive the deadline, and the child caps
its address space where the OS supports it (RLIMIT_AS). Every call writes an audit line in a
`finally` block: a block, a timeout, a crash and a database error are all logged.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from assay_pipeline.audit import AuditLog
from assay_pipeline.config import Limits, ViewsConfig
from assay_pipeline.guard import GENERIC_MESSAGES, SqlGuard

GRACE_SECONDS = 2.0
# -I: isolated mode, so no module in the working directory (or PYTHON* variable) can load into
# the child that holds the database connection (S3.3 verifier)
CHILD_COMMAND = (sys.executable, "-I", "-m", "assay_pipeline.agent._child")
SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
CHILD_ENV_ALLOW = frozenset({
    "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "VIRTUAL_ENV",
})  # fmt: skip


def child_env() -> dict[str, str]:
    """The child needs no credentials: only an allowlist of plain process variables is passed
    on (a denylist by name missed DATABASE_URL, GH_PAT and the like), and never one whose name
    marks a secret."""
    return {
        k: v
        for k, v in os.environ.items()
        if k.upper() in CHILD_ENV_ALLOW and not any(m in k.upper() for m in SECRET_MARKERS)
    }


@dataclass(frozen=True)
class ToolResult:
    text: str  # what the model sees
    allowed: bool
    reason: str | None  # the guard's reason when blocked
    error_class: str | None  # timeout | memory | permission | other | crash


class QueryTool:
    def __init__(
        self,
        db_path: Path | str,
        views: ViewsConfig,
        audit: AuditLog,
        *,
        trial_id: str,
        model: str,
        case_id: str | None = None,
        child_command: Sequence[str] = CHILD_COMMAND,
        guard: Any = None,
    ) -> None:
        self.db_path = str(db_path)
        self.views = views
        self.limits: Limits = views.limits
        # `guard` overrides the SQL guard only for the guard-off ablation (D-10), which runs on a
        # sacrificial copy; the locked connection and the limits stay on
        self.guard = guard if guard is not None else SqlGuard(views)
        self.audit = audit
        self.trial_id, self.model, self.case_id = trial_id, model, case_id
        self._command = list(child_command)

    def _run(self, decision: dict[str, Any]) -> dict[str, Any] | None:
        """Run the query in a child process; None when the deadline passed (child killed)."""
        request = json.dumps(
            {"db_path": self.db_path, "decision": decision, "limits": self.limits.model_dump()}
        )
        proc = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            env=child_env(),
        )
        try:
            out, _ = proc.communicate(request, timeout=self.limits.timeout_seconds + GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return None
        if proc.returncode != 0 or not out.strip():
            return {"crashed": True}  # killed by the address-space cap, or failed outright
        try:
            result = json.loads(out)
        except json.JSONDecodeError:
            return {"crashed": True}
        return result if isinstance(result, dict) else {"crashed": True}

    def query(self, sql: str) -> ToolResult:
        decision = self.guard.check(sql)
        result: dict[str, Any] | None = None
        tool_result = ToolResult(GENERIC_MESSAGES["other"], False, None, "other")
        try:
            if decision.decision != "allowed":
                tool_result = ToolResult(GENERIC_MESSAGES["blocked"], False, decision.reason, None)
                return tool_result
            result = self._run(decision.model_dump(mode="json"))
            if result is None:
                tool_result = ToolResult(GENERIC_MESSAGES["timeout"], True, None, "timeout")
            elif result.get("crashed"):
                tool_result = ToolResult(GENERIC_MESSAGES["memory"], True, None, "crash")
            else:
                tool_result = ToolResult(result["text"], True, None, result["db_error_class"])
            return tool_result
        finally:
            ran = result is not None and not result.get("crashed") and result.get("ok")
            self.audit.append(
                event="query",
                trial_id=self.trial_id,
                case_id=self.case_id,
                model=self.model,
                sql=sql,
                guard_decision=decision.decision,
                guard_reason=decision.reason,
                rendered_sql=decision.rendered_sql,
                rows_returned=result["rows_returned"] if ran and result else None,
                rows_shown=result["rows_shown"] if ran and result else None,
                result_sha256=result["result_sha256"] if ran and result else None,
                db_error_class=_audit_error_class(tool_result.error_class),
            )


def _audit_error_class(cls: str | None) -> str | None:
    return {"crash": "memory"}.get(cls or "", cls)


__all__ = ["QueryTool", "ToolResult"]
