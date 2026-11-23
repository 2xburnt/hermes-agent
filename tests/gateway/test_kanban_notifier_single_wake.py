"""One kanban event wakes a notifier profile once, however many threads subscribe.

A card can hold several ``notify+wake`` subscriptions for the same profile:
``link_tasks`` copies every parent's subscription onto the child, and Slack card
threads subscribe descendants too. Each subscription keys a different thread
session, so waking each one started N concurrent agent turns for one event
(hetz incident t_984eb4af: one ``blocked`` event woke 17 ChiefOfBots sessions,
which then created competing corrective cards). Passive pings still go to
every subscription; only the wake leg is elected once per (board, task, profile).
"""

import asyncio

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn

KINDS = ["completed", "blocked", "gave_up", "crashed", "timed_out"]


class RecordingAdapter:
    def __init__(self, fail_wakes=0):
        self.sent = []
        self.handled = []
        self.fail_wakes = fail_wakes

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)
        if self.fail_wakes:
            self.fail_wakes -= 1
            raise RuntimeError("simulated wake failure")
        event._gateway_accepted = True


async def _tick(monkeypatch, runner):
    runner._running = True
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _blocked_task_with_threads(threads, *, mode="notify+wake"):
    """A card subscribed in several threads of one chat; created_at follows list order."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="qa card", assignee="worker")
        for i, thread in enumerate(threads):
            kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                               thread_id=thread, chat_type="group", delivery_mode=mode)
            conn.execute("UPDATE kanban_notify_subs SET created_at=? WHERE task_id=? AND thread_id=?",
                         (1000 + i, tid, thread))
        conn.commit()
        kb.claim_task(conn, tid)
        kb.block_task(conn, tid, reason="needs a decision")
        return tid
    finally:
        conn.close()


def _unseen(tid, thread):
    conn = kbc.connect()
    try:
        _, events = kbn.unseen_events_for_sub(conn, task_id=tid, platform="telegram",
                                              chat_id="chat-1", thread_id=thread, kinds=KINDS)
        return events
    finally:
        conn.close()


def test_one_event_wakes_one_session_but_pings_every_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "single-wake.db"))
    kb.init_db()
    threads = ["own-root", "parent-a", "parent-b", "parent-c"]
    tid = _blocked_task_with_threads(threads)

    adapter = RecordingAdapter()
    asyncio.run(_tick(monkeypatch, _runner(adapter)))

    assert len(adapter.handled) == 1, "one event must start exactly one agent turn per profile"
    assert adapter.handled[0].source.thread_id == "own-root", "the oldest subscription is the waker"
    assert sorted(m["metadata"].get("thread_id") for m in adapter.sent) == sorted(threads)
    assert all(_unseen(tid, t) == [] for t in threads), "every cursor settles"

    # A second tick has nothing left: no late duplicate wake.
    asyncio.run(_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.handled) == 1


def test_wake_only_routes_are_never_silenced(tmp_path, monkeypatch):
    """For a ``wake`` route the wake is the only delivery; election must not drop it."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wake-only.db"))
    kb.init_db()
    threads = ["t1", "t2", "t3"]
    tid = _blocked_task_with_threads(threads, mode="wake")

    adapter = RecordingAdapter()
    asyncio.run(_tick(monkeypatch, _runner(adapter)))

    assert adapter.sent == []
    assert len(adapter.handled) == len(threads)
    assert all(_unseen(tid, t) == [] for t in threads)


def test_failed_elected_wake_retries_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "retry.db"))
    kb.init_db()
    threads = ["own-root", "parent-a", "parent-b"]
    tid = _blocked_task_with_threads(threads)

    adapter = RecordingAdapter(fail_wakes=1)
    asyncio.run(_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.handled) == 1
    assert _unseen(tid, "own-root"), "the failed waker is rewound for retry"
    assert _unseen(tid, "parent-a") == [] and _unseen(tid, "parent-b") == []

    asyncio.run(_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.handled) == 2, "the retry wakes once, not once per thread"
    assert {e.source.thread_id for e in adapter.handled} == {"own-root"}
    assert _unseen(tid, "own-root") == []
