"""Fresh outside input breaks a block loop; retries and self-commentary do not."""

import argparse

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    # Event ordering, not second-resolution timestamps, must define the window.
    monkeypatch.setattr(kb.time, "time", lambda: 1_800_000_000)
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn


def blocked_task(conn, kind=None):
    tid = kb.create_task(conn, title="Needs decisions", assignee="worker")
    kb.add_comment(conn, tid, author="owner", body="Original requirements")
    kb.recompute_ready(conn)
    assert kb.claim_task(conn, tid, claimer="worker")
    assert kb.block_task(conn, tid, reason="First question", kind=kind)
    return tid


@pytest.mark.parametrize("kind", [None, "needs_input", "capability", "transient"])
@pytest.mark.parametrize("author", ["slack:UOWNER", "chiefofbots", "another-profile", "worker", " worker ", None])
@pytest.mark.parametrize("after_unblock", [False, True])
def test_reblock_counts_only_without_new_outside_input(board, kind, author, after_unblock):
    tid = blocked_task(board, kind)
    if after_unblock:
        assert kb.unblock_task(board, tid)
    if author:
        kb.add_comment(board, tid, author=author, body="Decision or progress")
    if not after_unblock:
        assert kb.unblock_task(board, tid)
    assert kb.claim_task(board, tid, claimer="worker")
    assert kb.block_task(board, tid, reason="Next question", kind=kind)
    outside_input = author is not None and author.strip() != "worker"
    task = kb.get_task(board, tid)
    assert task.status == ("blocked" if outside_input else "triage")
    assert task.block_recurrences == (1 if outside_input else 2)
    assert kb.list_events(board, tid)[-1].kind == ("blocked" if outside_input else "block_loop_detected")
    if outside_input:
        # The same reply cannot buy unlimited subsequent retries.
        assert kb.unblock_task(board, tid)
        assert kb.claim_task(board, tid, claimer="worker")
        assert kb.block_task(board, tid, reason="Still unanswered", kind=kind)
        assert kb.get_task(board, tid).status == "triage"


def test_cli_unblock_with_evidence_is_new_input(board, monkeypatch):
    tid = blocked_task(board)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(cli, "_profile_author", lambda: "operator")
    assert cli._cmd_unblock(argparse.Namespace(
        task_ids=[tid], reason="Access restored; connection check passed",
    )) == 0
    assert kb.claim_task(board, tid, claimer="worker")
    assert kb.block_task(board, tid, reason="Need next decision")
    assert kb.get_task(board, tid).status == "blocked"
    assert kb.get_task(board, tid).block_recurrences == 1


def test_other_card_input_does_not_reset_loop(board):
    tid = blocked_task(board)
    other = kb.create_task(board, title="Unrelated", assignee="worker")
    kb.add_comment(board, other, author="owner", body="Unrelated decision")
    assert kb.unblock_task(board, tid)
    assert kb.claim_task(board, tid, claimer="worker")
    assert kb.block_task(board, tid, reason="Still unanswered")
    assert kb.get_task(board, tid).status == "triage"
