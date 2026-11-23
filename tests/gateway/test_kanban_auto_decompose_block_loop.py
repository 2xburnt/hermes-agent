"""Block-loop escalations belong to humans, not the automatic specifier."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway import kanban_watchers_dispatcher as kwd
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp


@pytest.mark.parametrize("kind", ["needs_input", "capability", "transient", None])
def test_auto_decompose_leaves_block_loop_in_triage(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setattr(kwd, "_board_slugs", lambda _kb: ["default"])
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Needs owner decision", assignee="worker")
        kb.recompute_ready(conn)
        for attempt in range(kb.BLOCK_RECURRENCE_LIMIT):
            assert kb.claim_task(conn, tid, claimer="worker") is not None
            assert kb.block_task(conn, tid, reason="Unanswered decision", kind=kind)
            if attempt + 1 < kb.BLOCK_RECURRENCE_LIMIT:
                assert kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "triage"
        assert kb.list_events(conn, tid)[-1].kind == "block_loop_detected"
        # Later comments must not make an escalation look like ordinary triage.
        kb.add_comment(conn, tid, author="operator", body="Still awaiting decision")
        fresh = kb.create_task(conn, title="New specification", assignee="worker", triage=True)

    aux = Mock(side_effect=AssertionError("Escalation must not spend aux tokens"))
    monkeypatch.setattr(decomp, "_call_aux", aux)
    attempted = []

    def specify(task_id, *, author):
        attempted.append(task_id)
        with kbc.connect_closing() as conn:
            assert kb.specify_triage_task(conn, task_id, author=author)
        return SimpleNamespace(ok=True, fanout=False, child_ids=None)

    monkeypatch.setattr(decomp, "decompose_task", specify)
    settings = kwd._DispatcherSettings(60.0, None, None, 2, 0, True, None, None)
    dispatcher = kwd._KanbanDispatcher(SimpleNamespace(DEFAULT_BOARD="default"), settings)
    assert decomp.list_triage_ids() == [fresh]
    # The skipped escalation must not consume the per-tick budget.
    assert dispatcher.auto_decompose_tick(1) == 1
    assert dispatcher.auto_decompose_tick(1) == 0
    assert dispatcher.auto_decompose_tick(1) == 0
    assert attempted == [fresh]
    aux.assert_not_called()
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "triage"
        assert not [e for e in kb.list_events(conn, tid) if e.kind == "specified"]
        # Explicit human/dashboard specify still works; only discovery is gated.
        assert kb.specify_triage_task(conn, tid, body="Owner answered", author="human")
        assert kb.get_task(conn, tid).status == "ready"
