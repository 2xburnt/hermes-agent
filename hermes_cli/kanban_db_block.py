"""Block routing and recurrence accounting, inside block_task's write transaction."""

import sqlite3
from typing import Optional


def _has_new_outside_input(conn: sqlite3.Connection, task_id: str) -> bool:
    from hermes_cli import kanban_db as kb

    # Row IDs order same-second replies correctly. Include escalations so a
    # manually resumed triage card cannot reuse input from an earlier block.
    rows = conn.execute(
        "SELECT e.payload, t.assignee FROM task_events e "
        "JOIN tasks t ON t.id = e.task_id "
        "WHERE e.task_id = ? AND e.kind = 'commented' AND e.id > ("
        "SELECT MAX(id) FROM task_events WHERE task_id = ? "
        "AND kind IN ('blocked', 'block_loop_detected'))",
        (task_id, task_id),
    )
    for row in rows:
        author = kb._nonblank_str(kb._json_dict(row["payload"]).get("author"))
        if author and author.strip() != row["assignee"]:
            return True
    return False


def route_block(
    conn: sqlite3.Connection, task_id: str,
    kind: Optional[str], reason: Optional[str], source_status: str, *,
    prev_kind: Optional[str], prev_recurrences: int,
) -> tuple[str, str, str, tuple, dict]:
    """Return status, event, SQL assignments, parameters and audit payload.

    Dependencies wait in todo. Other kinds count repeated blocks without
    fresh input, including untyped blocks (None == None). Outside comments
    since the last block reset the next block to one; self-comments and bare
    unblocks do not. CLI unblock --reason already records an authored comment,
    so operator evidence uses the same path without a new event/API shape.
    """
    from hermes_cli import kanban_db as kb

    payload = {"reason": reason, "kind": kind, "source_status": source_status}
    if kind == "dependency":
        return "todo", "dependency_wait", "block_kind    = ?", (kind,), payload
    recurrences = prev_recurrences + 1 if prev_kind == kind else 1
    if recurrences > 1 and _has_new_outside_input(conn, task_id):
        recurrences = 1
    set_sql = "block_kind    = ?,\n                       block_recurrences = ?"
    payload = {"reason": reason, "kind": kind, "recurrences": recurrences, "source_status": source_status}
    if recurrences >= kb.BLOCK_RECURRENCE_LIMIT:
        payload["limit"] = kb.BLOCK_RECURRENCE_LIMIT
        return "triage", "block_loop_detected", set_sql, (kind, recurrences), payload
    return "blocked", "blocked", set_sql, (kind, recurrences), payload
