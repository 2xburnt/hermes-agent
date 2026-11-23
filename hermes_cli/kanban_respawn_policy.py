"""Local event ordering for deliberate retries of cards with PR comments."""

import json
import sqlite3


def has_pending_operator_requeue(conn: sqlite3.Connection, task_id: str) -> bool:
    """An explicit requeue authorizes one claim, including its follow-up context.

    Comments can be posted AFTER the operator unblocks a card to explain what
    to resume. They must not revoke that decision. Event IDs, not second-level
    timestamps, order the requeue and the claim that consumes it. Automatic
    reclaim/promotion must never authorize a duplicate implementation attempt.
    """
    events = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? "
        "AND id > COALESCE((SELECT MAX(id) FROM task_events "
        "WHERE task_id = ? AND kind = 'claimed'), 0) "
        "AND kind IN ('unblocked', 'status') ORDER BY id DESC",
        (task_id, task_id),
    ).fetchall()
    for event in events:
        try:
            data = json.loads(event["payload"]) if event["payload"] else {}
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        if event["kind"] == "unblocked":
            # Legacy ready -> unblock events have no payload.
            return data.get("status", "ready") in {"ready", "todo"}
        # A status move with a reason is an internal transition, e.g. an
        # ancestor reopening. Only an explicit move back to the queue counts.
        if not data.get("reason") and data.get("status") in {"ready", "todo"}:
            return True
    return False
