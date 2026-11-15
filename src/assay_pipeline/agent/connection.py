"""The only place the agent zone opens DuckDB (Tech Spec §6.2, D-4, D-25 boundary gate).

The connection is read-only, with external access, extension autoload and autoinstall,
community extensions and disk spilling off; memory and threads are capped from
`ViewsConfig.limits`; then the configuration is locked. On top, `LockedConnection` runs only
single SELECT statements (DuckDB's own parser decides the statement type), so with the guard
bypassed nothing can CREATE (not even a session-local TEMP view that would shadow an approved
view), SET, PRAGMA, LOAD, CALL, ATTACH or run two statements at once. Table-level read control
is the guard's job (DuckDB has no GRANTs, a stated limit).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import duckdb

from assay_pipeline.config import Limits

MEMORY = re.compile(r"^[0-9]+(MB|GB)$")
TEMP_OBJECTS = (
    "SELECT (SELECT count(*) FROM duckdb_tables() WHERE temporary) "
    "+ (SELECT count(*) FROM duckdb_views() WHERE temporary AND NOT internal) "
    "+ (SELECT count(*) FROM duckdb_functions() WHERE function_type LIKE '%macro%' "
    "AND NOT internal) "
    "+ (SELECT count(*) FROM duckdb_sequences() WHERE temporary) "
    "+ (SELECT count(*) FROM duckdb_indexes() WHERE database_name = 'temp')"
)


class ConnectionError_(RuntimeError):
    """The agent database cannot be opened safely."""


class PermissionException(RuntimeError):
    """A statement the agent connection never runs."""  # error classifier maps it to "permission"


class Result:
    """What a statement gives back: the column description and the rows, never the connection
    (so no caller can chain another, unchecked `execute` onto it)."""

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    @property
    def description(self) -> Any:
        return self._cursor.description

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchmany(self, size: int = 1) -> list[Any]:
        return self._cursor.fetchmany(size)

    def fetchall(self) -> list[Any]:
        return self._cursor.fetchall()


class LockedConnection:
    """Wraps the read-only DuckDB connection: one SELECT per call, nothing else. The raw
    connection stays reachable as `_con` from Python; D-25 does not defend against our own
    agent-zone code, only against SQL the model sends."""

    def __init__(self, con: Any) -> None:
        self._con = con
        self._poisoned = False

    def execute(self, query: str, parameters: Any = None) -> Result:
        if self._poisoned:
            raise PermissionException("this connection saw a temporary object and is closed")
        statements = self._con.extract_statements(query)  # DuckDB's parser, not ours
        if len(statements) != 1 or statements[0].type.name != "SELECT":
            kinds = [s.type.name for s in statements]
            raise PermissionException(f"only one SELECT statement may run, not {kinds}")
        row = self._con.execute(TEMP_OBJECTS).fetchone()
        if row is None or row[0] != 0:  # unreachable while the gate holds; fail closed if not
            self._poisoned = True
            self._con.close()
            raise PermissionException("a temporary object exists on the agent connection")
        if parameters is None:
            return Result(self._con.execute(query))
        return Result(self._con.execute(query, parameters))

    def interrupt(self) -> None:
        self._con.interrupt()

    def close(self) -> None:
        self._con.close()


def open_agent_db(path: Path | str, limits: Limits) -> LockedConnection:
    path = Path(path)
    if not path.is_file():
        raise ConnectionError_(f"{path} is not an agent database file")
    if not MEMORY.match(limits.memory_limit) or not 1 <= limits.threads <= 64:
        raise ConnectionError_("memory_limit or threads out of range")
    con = duckdb.connect(
        str(path),
        read_only=True,
        config={
            "enable_external_access": False,
            "autoload_known_extensions": False,
            "autoinstall_known_extensions": False,
            "allow_community_extensions": False,
            "temp_directory": "",
            "max_temp_directory_size": "0KB",
        },
    )
    try:
        con.execute(f"SET memory_limit = '{limits.memory_limit}'")  # validated above
        con.execute(f"SET threads = {int(limits.threads)}")
        con.execute("SET lock_configuration = true")
    except BaseException:
        con.close()
        raise
    return LockedConnection(con)
