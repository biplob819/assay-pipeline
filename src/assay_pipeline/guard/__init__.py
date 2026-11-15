"""The SQL guard: the only path from the agent to any row (Tech Spec §2.3, §6.1)."""

from .execute import GENERIC_MESSAGES, QueryResult, execute_guarded
from .sql import FUNCTION_CLASSES, SqlGuard

__all__ = ["FUNCTION_CLASSES", "GENERIC_MESSAGES", "QueryResult", "SqlGuard", "execute_guarded"]
