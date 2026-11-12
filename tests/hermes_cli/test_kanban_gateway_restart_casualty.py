"""A gateway restart that kills a worker must not spend the card's failure budget.

Two shapes, both seen in production: the submitter outlives a long restart drain and gives up
(``KANBAN_OWNER_RESTART_EXIT_CODE``), and the managed worker loses its pipe to the dying gateway
(``worker_lost: EOFError``). Neither says anything about the card, so it is requeued with the
error stamped and ``consecutive_failures`` untouched. A card that is restart-killed more than
``_RESTART_CASUALTY_LIMIT`` times in a row is no longer a bystander and reaches the breaker.
"""

from __future__ import annotations

import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd

from tests.hermes_cli.test_kanban_managed_worker_loss import board  # noqa: F401  (fixture)


def _lose(board, tid, reason, *, failures=0):
    run_id = board.running(tid, claim_expires_in=900, heartbeat_age=30, worker_lost=reason)
    board.conn.execute("UPDATE tasks SET consecutive_failures = ? WHERE id = ?", (failures, tid))
    board.conn.commit()
    return run_id


def test_eoferror_worker_loss_is_a_restart_casualty_not_a_failure(board):
    # One failure short of the breaker: if this were counted the card would block.
    run_id = _lose(board, "t_restart", "EOFError", failures=kbd.DEFAULT_FAILURE_LIMIT - 1)
    board.tick()
    row, run = board.state("t_restart"), board.run(run_id)
    assert row["status"] == "ready"
    assert row["consecutive_failures"] == kbd.DEFAULT_FAILURE_LIMIT - 1
    assert row["last_failure_error"]
    assert run["outcome"] == "crashed"
    assert json.loads(run["metadata"])[kbd.GATEWAY_RESTART_MARKER] is True


def test_a_restart_that_kills_many_workers_is_not_a_systemic_error(board):
    for n in range(4):
        _lose(board, f"t_many{n}", "EOFError")
    board.tick()
    for n in range(4):
        row = board.state(f"t_many{n}")
        assert row["status"] == "ready" and row["consecutive_failures"] == 0


def test_other_worker_losses_still_count(board):
    _lose(board, "t_real", "managed_worker_failed")
    board.tick()
    row = board.state("t_real")
    assert row["consecutive_failures"] == 1
    assert kbd.GATEWAY_RESTART_MARKER not in json.loads(board.run(_last_run(board, "t_real"))["metadata"])


def _last_run(board, tid):
    return board.conn.execute(
        "SELECT id FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()["id"]


def test_a_card_that_keeps_being_restart_killed_reaches_the_breaker(board):
    tid = "t_loop"
    for n in range(kbd._RESTART_CASUALTY_LIMIT):
        _lose(board, tid, "EOFError") if n == 0 else _relose(board, tid)
        board.tick()
        assert board.state(tid)["consecutive_failures"] == 0, f"casualty {n + 1} must be free"
    _relose(board, tid)
    board.tick()
    assert board.state(tid)["consecutive_failures"] == 1  # the next one is charged


def _relose(board, tid):
    """Re-claim the same card with a new run, as the dispatcher would, then lose its worker."""
    board.conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))  # closed runs stay: they are the streak
    board.running(tid, claim_expires_in=900, heartbeat_age=30, worker_lost="EOFError")


def test_owner_restart_exit_code_is_booked_as_a_casualty():
    assert kbd._exit_code_kind(kb.KANBAN_OWNER_RESTART_EXIT_CODE) == ("owner_restart", kb.KANBAN_OWNER_RESTART_EXIT_CODE)
    dead = kbd._classify_dead_worker_exit(4242, "host:1", kb.KANBAN_OWNER_RESTART_EXIT_CODE)
    assert dead.restart_casualty and dead.run_outcome == "crashed"
    assert dead.event_payload[kbd.GATEWAY_RESTART_MARKER] is True
