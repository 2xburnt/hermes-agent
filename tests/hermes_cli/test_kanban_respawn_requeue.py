"""Deliberate retries survive PR context comments, without disabling crash guards."""

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

PR = "Resume existing https://github.com/example/repo/pull/625; do not duplicate it."


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Same-second ordering must work: event IDs establish which claim consumed
    # the explicit retry. No sleep or artificially backdated comments needed.
    monkeypatch.setattr(kbd.time, "time", lambda: 1900000000)
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


@pytest.mark.parametrize("comment_first", [True, False])
def test_unblock_permits_one_claim_despite_pr_context(board, comment_first):
    tid = kb.create_task(board, title="retry", assignee="dev", initial_status="blocked")
    if comment_first:
        kb.add_comment(board, tid, author="operator", body=PR)
    assert kb.unblock_task(board, tid)
    if not comment_first:
        kb.add_comment(board, tid, author="operator", body=PR)
    assert kbd.check_respawn_guard(board, tid) is None
    assert kb.claim_task(board, tid) is not None
    # A later crash/reclaim must not reuse the already-consumed permission.
    assert kbd.check_respawn_guard(board, tid) == "active_pr"


def test_actual_dispatch_selects_explicit_retry(board, monkeypatch):
    import hermes_cli.profiles as profiles
    import hermes_cli.config as config
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    monkeypatch.setattr(config, "load_config", lambda *a, **k: {})
    tid = kb.create_task(board, title="retry", assignee="dev", initial_status="blocked")
    kb.unblock_task(board, tid)
    kb.add_comment(board, tid, author="operator", body=PR)
    result = kbd.dispatch_once(board, dry_run=True)
    assert tid in [item[0] for item in result.spawned]
    assert tid not in dict(result.respawn_guarded)


@pytest.mark.parametrize("kind,payload,allowed", [
    ("status", {"status": "ready"}, True),
    ("status", {"status": "todo"}, True),
    ("status", {"status": "blocked"}, False),
    ("status", {"status": "todo", "reason": "ancestor_reopened"}, False),
    ("promoted", None, False),
    ("reclaimed", None, False),
    ("assigned", {"from": "dev", "assignee": "dev"}, False),
    ("unblocked", {"status": "todo", "resume_status": "ready"}, True),
    ("unblocked", {"status": "review"}, False),
])
def test_only_deliberate_queue_events_authorize_retry(board, kind, payload, allowed):
    tid = kb.create_task(board, title="retry", assignee="dev")
    kb.add_comment(board, tid, author="dev", body=PR)
    with kb.write_txn(board):
        kb._append_event(board, tid, kind, payload)
    assert kbd.check_respawn_guard(board, tid) == (None if allowed else "active_pr")


def test_later_explicit_unblock_renews_permission(board):
    tid = kb.create_task(board, title="retry", assignee="dev", initial_status="blocked")
    kb.unblock_task(board, tid)
    kb.add_comment(board, tid, author="operator", body=PR)
    kb.claim_task(board, tid)
    kb.block_task(board, tid, reason="failed again")
    assert kbd.check_respawn_guard(board, tid) == "active_pr"
    kb.unblock_task(board, tid)
    assert kbd.check_respawn_guard(board, tid) is None


def test_crashed_pr_owner_recovery_dispatches_same_profile(board, monkeypatch):
    """Recovery must renew the consumed retry, not duplicate or reassign the PR."""
    import hermes_cli.profiles as profiles
    import hermes_cli.config as config

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    monkeypatch.setattr(config, "load_config", lambda *a, **k: {})
    tid = kb.create_task(board, title="continue PR", assignee="dev", initial_status="blocked")
    kb.unblock_task(board, tid)
    kb.claim_task(board, tid)
    kb.add_comment(board, tid, author="dev", body=PR)
    kbd._set_worker_pid(board, tid, 98765)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
    assert kbd.detect_crashed_workers(board) == [tid]
    task = kb.get_task(board, tid)
    assert task is not None and task.status == "ready"
    assert kbd.check_respawn_guard(board, tid) == "active_pr"
    # Merely unblocking an already-ready card is a no-op.
    assert not kb.unblock_task(board, tid)

    # The operator has verified the old run is dead and this card still owns
    # the PR. A real block/unblock records fresh, one-claim retry permission.
    assert kb.block_task(board, tid, reason="verified existing PR continuation")
    assert kb.unblock_task(board, tid)
    kb.add_comment(board, tid, author="operator", body=PR)
    assert kbd.check_respawn_guard(board, tid) is None

    result = kbd.dispatch_once(board, dry_run=True)
    assert tid in [item[0] for item in result.spawned]
    assert tid not in dict(result.respawn_guarded)
    task = kb.get_task(board, tid)
    assert task is not None and task.assignee == "dev"


def test_requeue_does_not_override_auth_blocker(board):
    tid = kb.create_task(board, title="retry", assignee="dev", initial_status="blocked")
    kb.unblock_task(board, tid)
    kb.add_comment(board, tid, author="operator", body=PR)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET last_failure_error = 'invalid api key' WHERE id = ?", (tid,))
    assert kbd.check_respawn_guard(board, tid) == "blocker_auth"
