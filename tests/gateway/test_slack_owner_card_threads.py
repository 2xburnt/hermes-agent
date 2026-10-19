"""Hetz private card-thread admission through the real Slack ingress handler."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gateway.session_envelope import _validate_native

import pytest

from tests.gateway.test_slack import adapter  # shared fake transport fixture
from plugins.platforms.slack import owner_card_threads as route
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing

ROOT = "1791473734.379039"
TS = "1791479900.000001"


@pytest.fixture
def setup(adapter, tmp_path, monkeypatch):
    home = tmp_path / "profiles" / route.PROFILE
    (home / "relay").mkdir(parents=True)
    monkeypatch.setattr(route, "get_profile_dir", lambda name: home)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    with connect_closing(tmp_path / "kanban.db") as conn:
        card = kb.create_task(conn, title="mapped card", assignee="earnosbot", initial_status="blocked")
        other = kb.create_task(conn, title="other card", assignee="burntbot", initial_status="blocked")
    mapping = home / "relay" / f"threads.{route.CHANNEL}.json"
    mapping.write_text(json.dumps({card: ROOT, other: "1791473735.000001"}))
    adapter._owner_profile = route.PROFILE
    async def accept(wake):
        wake._gateway_accepted = True
    adapter.handle_message.side_effect = accept
    adapter._bot_user_id = route.BOT
    adapter.config.extra.update(api_human_users=[route.OWNER], allow_bots="all")
    client = adapter._app.client
    client.chat_postMessage.return_value = {"ok": True, "ts": "1791479902.000001"}
    client.users_info.return_value = {"ok": True, "user": {"is_bot": False}}
    client.conversations_info.return_value = {"ok": True, "channel": {
        "id": route.CHANNEL, "is_private": True, "is_im": False, "is_mpim": False,
        "is_archived": False, "is_shared": False, "is_ext_shared": False,
        "is_org_shared": False, "is_pending_ext_shared": False}}
    client.conversations_members.return_value = {"ok": True, "members": [route.OWNER, route.BOT]}
    client.conversations_history.return_value = {"ok": True, "messages": [{"ts": ROOT, "user": route.BOT}]}
    event = {"type": "message", "text": "send it\ncheck this", "user": route.OWNER,
             "channel": route.CHANNEL, "channel_type": "group", "ts": TS,
             "thread_ts": ROOT, "client_msg_id": "typed-human-reply"}
    return adapter, event, home, card, other, mapping


def comments(home, card):
    with connect_closing(home.parent.parent / "kanban.db") as conn:
        return kb.list_comments(conn, card)


@pytest.mark.asyncio
async def test_top_level_root_with_its_own_card_mapping_is_rejected(setup):
    a, event, home, card, _, _ = setup
    await a._handle_slack_message({**event, "ts": ROOT, "thread_ts": ROOT})
    assert comments(home, card) == []
    a.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_verifier_rejects_wrong_channel_independently(setup):
    a, event, home, _, _, _ = setup
    assert await route.verified_card(a, {**event, "channel": "C_OTHER"}, "", home) is None


@pytest.mark.asyncio
async def test_unmapped_discussion_thread_does_not_route(setup):
    a, event, home, card, _, mapping = setup
    mapping.write_text(json.dumps({card: ROOT, "disc:slack:C_TEST": TS}))
    a._app.client.conversations_history.return_value["messages"][0]["ts"] = TS
    await a._handle_slack_message({**event, "ts": "1791479901.000001", "thread_ts": TS})
    assert comments(home, card) == []
    a.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_mapped_owner_reply_records_only_target_and_wakes_without_mention(setup):
    a, event, home, card, other, _ = setup
    await a._handle_slack_message(event)
    rows = comments(home, card)
    assert len(rows) == 1
    assert rows[0].author == f"slack:{route.OWNER}"
    assert "trust: owner" in rows[0].body
    assert "> send it\n> check this" in rows[0].body
    assert "not send approval" in rows[0].body
    assert comments(home, other) == []
    a.handle_message.assert_awaited_once()
    wake = a.handle_message.await_args.args[0]
    runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=False),
        _adapter_flag=lambda *args: False,
        _adapter_profile_for_source=lambda source: route.PROFILE,
        _is_user_authorized_for_source=lambda source, **kwargs: source.user_id == route.OWNER,
        _adapter_for_source=lambda source: a,
    )
    admitted_source = _validate_native(runner, wake)
    assert admitted_source["chat_id"] == route.CHANNEL
    assert admitted_source["thread_id"] == ROOT
    assert wake.metadata == {}
    assert f"{card}; owner comment {rows[0].id} already recorded" in wake.text
    assert wake.source.chat_id == route.CHANNEL
    assert wake.source.thread_id == ROOT
    assert wake.source.user_id == route.OWNER
    assert wake.allow_gateway_control is False
    assert "Send tap" in wake.text
    with connect_closing(home.parent.parent / "kanban.db") as conn:
        assert kb.get_task(conn, card).status == "blocked"  # no implicit unblock/send
    a._app.client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [
    {"user": "U_OTHER"}, {"user": ""},
    {"subtype": "bot_message"}, {"bot_id": "B_OTHER"},
    {"bot_profile": {"id": "B_OTHER"}}, {"user_profile": {"is_bot": True}},
    {"thread_ts": None}, {"thread_ts": TS}, {"thread_ts": "bad"},
    {"thread_ts": "1791473736.000001"}, {"ts": "bad"},
    {"_hermes_force_process": True},
    {"bot_id": "B_APP", "app_id": "A_APP", "client_msg_id": None,
     "metadata": {"event_type": "owner_handoff"}},
])
async def test_event_guards_even_with_mentions_and_allow_bots_all(setup, override):
    a, event, home, card, other, _ = setup
    await a._handle_slack_message({**event, "text": f"<@{route.BOT}> {card}", **override})
    assert comments(home, card) == comments(home, other) == []
    a.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["C_OTHER", "D0C4S9L5HJL"])
async def test_wrong_channel_does_not_route_and_preserves_normal_handling(setup, channel):
    a, event, home, card, _, _ = setup
    await a._handle_slack_message({**event, "channel": channel, "text": f"<@{route.BOT}> hello"})
    assert comments(home, card) == []
    a.handle_message.assert_awaited_once()
    assert "owner_card_thread" not in a.handle_message.await_args.args[0].metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("is_private", False), ("is_private", None), ("id", "C_OTHER"),
    ("is_im", True), ("is_mpim", True), ("is_archived", True),
    ("is_shared", True), ("is_ext_shared", True), ("is_org_shared", True),
    ("is_pending_ext_shared", True), ("pending_shared", ["T_OTHER"]),
])
async def test_channel_guards(setup, field, value):
    a, event, home, card, _, _ = setup
    a._app.client.conversations_info.return_value["channel"][field] = value
    await a._handle_slack_message(event)
    assert comments(home, card) == []
    a.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"ok": False}, {"ok": True, "members": [route.OWNER]},
    {"ok": True, "members": [route.OWNER, route.BOT, "U_OTHER"]},
    {"ok": True, "members": [route.OWNER, route.OWNER]},
    {"ok": True, "members": [route.OWNER, route.BOT], "response_metadata": {"next_cursor": "more"}},
])
async def test_members_guards(setup, response):
    a, event, home, card, _, _ = setup
    a._app.client.conversations_members.return_value = response
    await a._handle_slack_message(event)
    assert comments(home, card) == []
    a.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"ok": False}, {"ok": True, "messages": []},
    {"ok": True, "messages": [{"ts": ROOT, "user": route.OWNER}]},
    {"ok": True, "messages": [{"ts": TS, "user": route.BOT}]},
    {"ok": True, "messages": [{"ts": ROOT, "user": "U_OTHER_BOT", "bot_id": "B_BOT"}]},
])
async def test_root_guards(setup, response):
    a, event, home, card, _, _ = setup
    a._app.client.conversations_history.return_value = response
    await a._handle_slack_message(event)
    assert comments(home, card) == []
    a.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["users_info", "conversations_info", "conversations_members", "conversations_history"])
async def test_api_errors_fail_closed(setup, method):
    a, event, home, card, _, _ = setup
    getattr(a._app.client, method).side_effect = RuntimeError("offline")
    await a._handle_slack_message(event)
    assert comments(home, card) == []
    a.handle_message.assert_not_awaited()
    a._app.client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [None, "{bad", "[]", "{}", '{"home-123": "1791473734.379039"}',
    '{"t_1234abcd": "1791473734.379039", "t_1234abce": "1791473734.379039"}',
    '{"t_1234abcd": "1791473734.379039"}'])
async def test_unmapped_malformed_ambiguous_or_nonexistent_card(setup, raw):
    a, event, home, card, _, mapping = setup
    if raw is None:
        mapping.unlink()
    else:
        mapping.write_text(raw)
    await a._handle_slack_message(event)
    assert comments(home, card) == []
    a.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_rechecks_members_and_map_without_positive_cache(setup):
    a, event, home, card, _, mapping = setup
    await a._handle_slack_message(event)
    a._app.client.conversations_members.return_value["members"].append("U_NEW")
    await a._handle_slack_message({**event, "ts": "1791479900.000002"})
    assert len(comments(home, card)) == 1
    assert a._app.client.conversations_members.await_count == 2
    a._app.client.conversations_members.return_value["members"].pop()
    mapping.write_text("{}")
    await a._handle_slack_message({**event, "ts": "1791479900.000003"})
    assert len(comments(home, card)) == 1
    assert a.handle_message.await_count == 1


@pytest.mark.asyncio
async def test_reconnect_dedup_and_user_bot_lookup(setup):
    a, event, home, card, _, _ = setup
    await route.route_owner_card_reply(a, event, "")
    await route.route_owner_card_reply(a, event, "")
    assert len(comments(home, card)) == 1
    assert a.handle_message.await_count == 1
    a._app.client.users_info.return_value["user"]["is_bot"] = True
    await route.route_owner_card_reply(a, {**event, "ts": "1791479900.000002"}, "")
    assert len(comments(home, card)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "refused"])
async def test_failed_wake_retries_without_duplicate_comment(setup, failure):
    a, event, home, card, _, _ = setup
    accepted = a.handle_message.side_effect
    a.handle_message = AsyncMock(side_effect=RuntimeError("failed") if failure == "exception" else None)
    with pytest.raises(RuntimeError):
        await a._handle_slack_message(event)
    assert len(comments(home, card)) == 1
    a.handle_message.side_effect = accepted
    await a._handle_slack_message(event)
    assert len(comments(home, card)) == 1
    assert a.handle_message.await_count == 2
    await route.route_owner_card_reply(a, event, "")
    assert a.handle_message.await_count == 2


@pytest.mark.asyncio
async def test_replayed_reply_cannot_follow_remapped_card(setup):
    a, event, home, card, other, mapping = setup
    await a._handle_slack_message(event)
    mapping.write_text(json.dumps({other: ROOT}))
    await route.route_owner_card_reply(a, event, "")
    assert len(comments(home, card)) == 1
    assert comments(home, other) == []
    assert a.handle_message.await_count == 1


@pytest.mark.asyncio
async def test_discussion_alias_is_ignored_but_card_alias_routes(setup):
    a, event, home, card, _, mapping = setup
    mapping.write_text(json.dumps({"disc:slack:C123:123.000001": ROOT, card: ROOT}))
    await a._handle_slack_message(event)
    assert len(comments(home, card)) == 1
    a.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_map_rechecked_after_slack_calls(setup):
    a, event, home, card, _, mapping = setup
    result = a._app.client.conversations_history.return_value
    async def changed(**kwargs):
        mapping.write_text("{}")
        return result
    a._app.client.conversations_history.side_effect = changed
    await a._handle_slack_message(event)
    assert comments(home, card) == []
    a.handle_message.assert_not_awaited()


def test_map_ambiguity_is_logged(setup, caplog):
    _, _, home, card, other, mapping = setup
    mapping.write_text(json.dumps({card: ROOT, other: ROOT, "disc:test": ROOT}))
    assert route.mapped_card(home, ROOT) is None
    assert "ambiguous owner card-thread" in caplog.text


def set_status(home, card, status):
    with connect_closing(home.parent.parent / "kanban.db") as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, card))


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["done", "archived"])
@pytest.mark.parametrize("live", ["todo", "ready", "running", "blocked", "review", "triage", "scheduled"])
async def test_multi_card_root_routes_only_live_card(setup, monkeypatch, terminal, live):
    a, event, home, card, other, mapping = setup
    set_status(home, card, live)
    set_status(home, other, terminal)
    # A selected alternate board must not change the default-board resolution.
    alternate = home.parent.parent / "alternate.db"
    with connect_closing(alternate):
        pass  # initialize an empty alternate board
    monkeypatch.setenv("HERMES_KANBAN_DB", str(alternate))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "alternate")
    mapping.write_text(json.dumps({other: ROOT, card: ROOT, "disc:test": ROOT}))
    await a._handle_slack_message(event)
    assert len(comments(home, card)) == 1
    assert comments(home, other) == []
    a.handle_message.assert_awaited_once()
    wake = a.handle_message.await_args.args[0]
    assert wake.metadata == {}
    assert wake.text.startswith(f"[Verified card-thread reply: {card};")
    a._app.client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unmapped", "two_live", "zero_live", "missing_card"])
@pytest.mark.parametrize("root_author", [route.BOT, route.OWNER])
async def test_verified_mapping_rejection_replies_in_thread_once(setup, kind, root_author, caplog):
    a, event, home, card, other, mapping = setup
    a._app.client.conversations_history.return_value["messages"][0]["user"] = root_author
    mapping.write_text(json.dumps({} if kind == "unmapped" else {card: ROOT, other: ROOT}))
    if kind == "missing_card":
        mapping.write_text(json.dumps({"t_00000000": ROOT}))
    if kind == "zero_live":
        set_status(home, card, "done")
        set_status(home, other, "archived")
    await a._handle_slack_message(event)
    await a._handle_slack_message(event)  # Slack delivery dedup
    await a._handle_slack_message({"type": "message", "subtype": "message_changed",
        "channel": route.CHANNEL, "ts": "1791479903.000001", "message": event})
    assert comments(home, card) == comments(home, other) == []
    a.handle_message.assert_not_awaited()
    a._app.client.chat_postMessage.assert_awaited_once_with(
        channel=route.CHANNEL, thread_ts=ROOT,
        text="couldn't tie this to one card; reply in the DM",
        reply_broadcast=False, unfurl_links=False, unfurl_media=False)
    a._app.client.conversations_history.assert_awaited_once()
    assert "owner card-thread mapping rejected with fallback" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["users_info", "conversations_info", "conversations_members", "conversations_history"])
async def test_unmapped_verification_failure_stays_silent(setup, method):
    a, event, home, card, other, mapping = setup
    mapping.write_text("{}")
    getattr(a._app.client, method).return_value = {"ok": False}
    await a._handle_slack_message(event)
    a.handle_message.assert_not_awaited()
    a._app.client.chat_postMessage.assert_not_awaited()
    assert comments(home, card) == comments(home, other) == []


@pytest.mark.asyncio
async def test_live_status_rechecked_after_slack_calls(setup):
    a, event, home, card, other, mapping = setup
    mapping.write_text(json.dumps({card: ROOT, other: ROOT}))
    set_status(home, other, "done")
    result = a._app.client.conversations_history.return_value
    async def changed(**kwargs):
        set_status(home, card, "done")
        set_status(home, other, "blocked")
        return result
    a._app.client.conversations_history.side_effect = changed
    await a._handle_slack_message(event)
    assert comments(home, card) == comments(home, other) == []
    a.handle_message.assert_not_awaited()
    a._app.client.chat_postMessage.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "not_ok"])
@pytest.mark.parametrize("root_author", [route.BOT, route.OWNER])
@pytest.mark.parametrize("edited", [False, True])
async def test_fallback_send_failure_allows_retry(setup, failure, root_author, edited):
    a, event, _, _, _, mapping = setup
    mapping.write_text("{}")
    client = a._app.client
    client.conversations_history.return_value["messages"][0]["user"] = root_author
    if edited:
        event = {"type": "message", "subtype": "message_changed", "channel": route.CHANNEL,
                 "ts": "1791479903.000001", "message": event}
    if failure == "exception":
        client.chat_postMessage.side_effect = RuntimeError("offline")
    else:
        client.chat_postMessage.return_value = {"ok": False}
    with pytest.raises(RuntimeError):
        await a._handle_slack_message(event)
    client.chat_postMessage.side_effect = None
    client.chat_postMessage.return_value = {"ok": True}
    await a._handle_slack_message(event)
    assert client.chat_postMessage.await_count == 2
    a.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_profile_and_bot_identity_guards(setup):
    a, event, home, card, _, _ = setup
    a._owner_profile = "crucible"
    assert await route.route_owner_card_reply(a, event, "") is False
    a._owner_profile = route.PROFILE
    a._bot_user_id = "U_OTHER_BOT"
    assert await route.route_owner_card_reply(a, event, "") is True
    assert comments(home, card) == []
    a.handle_message.assert_not_awaited()
    a._app.client.chat_postMessage.assert_not_awaited()


@pytest.mark.parametrize("status", ["done", "archived"])
def test_single_terminal_card_keeps_existing_binding(setup, status):
    _, _, home, card, _, _ = setup
    set_status(home, card, status)
    assert route.mapped_card(home, ROOT) == card


@pytest.mark.parametrize("board_state", ["missing", "malformed"])
def test_mapping_lookup_fails_closed_without_creating_board(setup, board_state):
    _, _, home, card, other, mapping = setup
    mapping.write_text(json.dumps({card: ROOT, other: ROOT}))
    db = home.parent.parent / "kanban.db"
    db.unlink()
    if board_state == "malformed":
        db.write_text("not sqlite")
    assert route.mapped_card(home, ROOT) is None
    assert db.exists() == (board_state == "malformed")


@pytest.mark.asyncio
async def test_card_removed_before_record_gets_fallback(setup, monkeypatch):
    a, event, home, card, _, _ = setup
    monkeypatch.setattr(kb, "get_task", lambda conn, task_id: None)
    await a._handle_slack_message(event)
    assert comments(home, card) == []
    a.handle_message.assert_not_awaited()
    a._app.client.chat_postMessage.assert_awaited_once()
