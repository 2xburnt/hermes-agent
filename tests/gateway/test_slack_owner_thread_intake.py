"""Owner-authored Chief roots use a tracked, root-scoped gateway turn."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from tests.gateway.test_slack import adapter  # noqa: F401
from tests.gateway.test_slack_owner_card_threads import setup as setup, comments, TS, ROOT
from plugins.platforms.slack import owner_card_threads as route
from plugins.platforms.slack import owner_thread_intake as intake
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing


@pytest.fixture
def tracked(setup, monkeypatch):
    a, event, home, card, other, mapping = setup
    event.pop("thread_ts")
    a._app.client.conversations_history.return_value = {"ok": True, "messages": [{**event}]}
    caller = AsyncMock(return_value=card)
    monkeypatch.setattr(intake, "intake_card", caller)
    return a, event, home, card, other, mapping, caller


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", [route.CHANNEL, intake.DM])
@pytest.mark.parametrize("root_field", [False, True])
async def test_owner_root_reaches_chief_and_reply_uses_same_thread(tracked, channel, root_field):
    a, event, home, card, other, _, caller = tracked
    event["channel"] = channel
    a.config.extra.update(reply_in_thread=False, dm_top_level_threads_as_sessions=False)
    if root_field:
        event["thread_ts"] = TS
    if channel == intake.DM:
        event["channel_type"] = "im"
        a._app.client.conversations_info.return_value = {"ok": True, "channel": {
            "id": channel, "is_im": True, "user": route.OWNER}}
    await a._handle_slack_message(event)
    a.handle_message.assert_awaited_once()
    caller.assert_awaited_once()
    turn = a.handle_message.await_args.args[0]
    assert turn.source.thread_id == TS
    assert turn.source.chat_id == channel
    assert turn.source.user_id == route.OWNER
    assert turn.reply_expected is True
    assert turn.text == f"[Tracked as card {card}]\n{event['text']}"
    assert turn.metadata["tracked_card_id"] == card
    assert comments(home, card) == comments(home, other) == []
    result = await a.send(turn.source.chat_id, "ack", metadata={"thread_id": turn.source.thread_id})
    assert result.success
    assert a._app.client.chat_postMessage.await_args.kwargs["thread_ts"] == TS


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [
    {"user": "U_OTHER"}, {"user": ""}, {"bot_id": "B_OTHER"},
    {"bot_profile": {"id": "B_OTHER"}}, {"user_profile": {"is_bot": True}},
    {"subtype": "bot_message"}, {"_hermes_force_process": True},
    {"ts": "bad"}, {"ts": None}, {"subtype": "channel_join"},
])
async def test_nonowner_and_bot_roots_stay_dropped(tracked, override):
    a, event, _, _, _, _, caller = tracked
    await a._handle_slack_message({**event, **override})
    a.handle_message.assert_not_awaited()
    caller.assert_not_awaited()
    a._app.client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", [
    "other_channel", "other_profile", "other_bot", "team_bot", "bot_user", "public",
    "shared", "members", "cursor", "root_bot", "wrong_root", "no_history", "api_error",
])
async def test_no_new_admission_when_identity_or_conversation_is_unverified(tracked, guard):
    a, event, _, _, _, _, caller = tracked
    client = a._app.client
    mutations = {
        "other_channel": lambda: event.update(channel="C_OTHER"),
        "other_profile": lambda: setattr(a, "_owner_profile", "crucible"),
        "other_bot": lambda: setattr(a, "_bot_user_id", "U_OTHER_BOT"),
        "team_bot": lambda: (event.update(team="T_OTHER"), a._team_bot_user_ids.update(T_OTHER="U_OTHER_BOT")),
        "bot_user": lambda: client.users_info.return_value["user"].update(is_bot=True),
        "public": lambda: client.conversations_info.return_value["channel"].update(is_private=False),
        "shared": lambda: client.conversations_info.return_value["channel"].update(is_ext_shared=True),
        "members": lambda: client.conversations_members.return_value["members"].append("U_OTHER"),
        "cursor": lambda: client.conversations_members.return_value.update(response_metadata={"next_cursor": "more"}),
        "root_bot": lambda: client.conversations_history.return_value["messages"][0].update(bot_id="B_BOT"),
        "wrong_root": lambda: client.conversations_history.return_value["messages"][0].update(ts=ROOT),
        "no_history": lambda: client.conversations_history.return_value.update(messages=[]),
        "api_error": lambda: setattr(client.conversations_history, "side_effect", RuntimeError("offline")),
    }
    mutations[guard]()
    await a._handle_slack_message(event)
    caller.assert_not_awaited()
    a.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_and_edit_do_not_create_or_dispatch_twice(tracked):
    a, event, _, _, _, _, caller = tracked
    await a._handle_slack_message(event)
    await a._handle_slack_message(event)
    await a._handle_slack_message({"type": "message", "subtype": "message_changed",
        "channel": route.CHANNEL, "ts": "1791479901.000001", "message": event})
    caller.assert_awaited_once()
    a.handle_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "refused"])
async def test_gateway_failure_releases_dedup_for_idempotent_retry(tracked, failure):
    a, event, _, _, _, _, caller = tracked
    accept = a.handle_message.side_effect
    a.handle_message.side_effect = RuntimeError("offline") if failure == "exception" else None
    with pytest.raises(RuntimeError):
        await a._handle_slack_message(event)
    a.handle_message.side_effect = accept
    await a._handle_slack_message(event)
    assert caller.await_count == 2
    assert a.handle_message.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/help", "!help", f"<@{route.BOT}> !help"])
async def test_commands_keep_canonical_dispatch_without_card(tracked, text):
    a, event, _, _, _, _, caller = tracked
    await a._handle_slack_message({**event, "text": text})
    caller.assert_not_awaited()
    turn = a.handle_message.await_args.args[0]
    assert turn.text == "/help"
    assert "tracked_card_id" not in turn.metadata
    assert turn.source.thread_id == TS


@pytest.mark.asyncio
async def test_intake_unavailable_leaves_turn_untracked(tracked):
    a, event, _, _, _, _, caller = tracked
    caller.return_value = None
    await a._handle_slack_message(event)
    turn = a.handle_message.await_args.args[0]
    assert turn.text == event["text"]
    assert "tracked_card_id" not in turn.metadata


@pytest.mark.asyncio
async def test_owner_root_continuation_uses_bound_card_not_a_new_intake(tracked):
    import json
    a, event, home, _, _, mapping, caller = tracked
    with connect_closing(home.parent.parent / "kanban.db") as conn:
        card = kb.create_task(conn, title="owner chat", assignee=route.PROFILE,
            created_by="slagent", initial_status="blocked",
            idempotency_key=f"slack-thread:{route.CHANNEL}:{TS}")
    mapping.write_text(json.dumps({card: TS}))
    await a._handle_slack_message({**event, "ts": "1791479901.000001", "thread_ts": TS})
    caller.assert_not_awaited()
    turn = a.handle_message.await_args.args[0]
    assert turn.text.startswith(f"[Tracked as card {card}]\n")
    assert turn.source.thread_id == TS
    assert comments(home, card) == []


@pytest.mark.asyncio
async def test_dm_replies_do_not_invoke_intake(tracked):
    a, event, _, _, _, _, caller = tracked
    await a._handle_slack_message({**event, "channel": intake.DM, "channel_type": "im", "thread_ts": ROOT})
    caller.assert_not_awaited()
    a.handle_message.assert_awaited_once()
    assert "tracked_card_id" not in a.handle_message.await_args.args[0].metadata


@pytest.mark.asyncio
async def test_unfurl_while_verifying_cannot_dispatch_twice(tracked):
    a, event, _, _, _, _, caller = tracked
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed_user(**kwargs):
        entered.set()
        await release.wait()
        return {"ok": True, "user": {"is_bot": False}}
    a._app.client.users_info.side_effect = delayed_user
    pending = asyncio.create_task(a._handle_slack_message(event))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await a._handle_slack_message({"type": "message", "subtype": "message_changed",
            "channel": route.CHANNEL, "ts": "1791479901.000001", "message": event})
    finally:
        release.set()
        await pending
    caller.assert_awaited_once()
    a.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_media_is_preserved_in_tracked_turn(tracked):
    a, event, _, _, _, _, _ = tracked
    a._collect_inbound_media = AsyncMock(return_value=(["/test/photo.png"], ["image/png"], [False], "Photo"))
    await a._handle_slack_message({**event, "files": [{"id": "F_TEST"}]})
    turn = a.handle_message.await_args.args[0]
    assert turn.media_urls == ["/test/photo.png"]
    assert turn.media_types == ["image/png"]
    assert turn.text.endswith("\nPhoto")


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_ts", [None, ROOT])
async def test_early_user_authorization_remains_in_force(tracked, monkeypatch, thread_ts):
    a, event, home, card, _, _, caller = tracked
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    seen = []

    def reject(user_id, channel_id, is_dm, *, thread_id, **_):
        seen.append((user_id, channel_id, is_dm, thread_id))
        return True

    monkeypatch.setattr(a, "_early_reject_unauthorized", reject)
    await a._handle_slack_message(event)
    assert seen == [(route.OWNER, route.CHANNEL, False, thread_ts or TS)]
    assert comments(home, card) == []
    caller.assert_not_awaited()
    a.handle_message.assert_not_awaited()
