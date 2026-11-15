"""The query child process (D-28): `python -m assay_pipeline.agent._child`.

Reads one JSON request on stdin (database path, guard decision, limits), caps its address space
where the OS allows, runs the guard-approved query through the locked connection, and writes one
JSON result on stdout. The parent kills it at the deadline.
"""

from __future__ import annotations

import json
import sys

from assay_pipeline.agent.connection import open_agent_db
from assay_pipeline.config import Limits
from assay_pipeline.guard import execute_guarded
from assay_pipeline.schemas import GuardDecision

ADDRESS_SPACE_BYTES = 4 * 1024**3  # well above memory_limit; catches runaway intermediates


def cap_address_space() -> bool:
    if sys.platform == "win32":
        return False  # Windows relies on the deadline (D-28)
    import resource

    try:
        resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE_BYTES, ADDRESS_SPACE_BYTES))
    except (ValueError, OSError):
        return False  # macOS may refuse RLIMIT_AS; the deadline still holds
    return True


def main() -> int:
    request = json.loads(sys.stdin.read())
    capped = cap_address_space()
    limits = Limits.model_validate(request["limits"])
    con = open_agent_db(request["db_path"], limits)
    try:
        r = execute_guarded(con, GuardDecision.model_validate(request["decision"]), limits)
    finally:
        con.close()
    sys.stdout.write(
        json.dumps(
            {
                "ok": r.ok,
                "rows_returned": r.rows_returned,
                "rows_shown": r.rows_shown,
                "result_sha256": r.result_sha256,
                "db_error_class": r.db_error_class,
                "text": r.text,
                "address_space_capped": capped,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
