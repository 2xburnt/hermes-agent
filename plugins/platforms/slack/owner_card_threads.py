"""Hetz-only owner ingress for ChiefOfBots' private card-thread channel.

Do not turn this into a generic mention exemption: unmapped/unsafe messages are
consumed, never handed to an LLM that could infer a card from their text. Owner-
authored intake roots have a separate slagent-backed path; bot-root routing and
the DM owner_handoff exception retain their existing guards.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from gateway.platforms.event import MessageEvent
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.profiles import get_profile_dir

logger = logging.getLogger(__name__)
CHANNEL = "C0C7T7SJ866"
OWNER = "U072NQJJ2HJ"
BOT = "U0C3ZSP1K09"
PROFILE = "chiefofbots"
_TS = re.compile(r"[0-9]{9,11}\.[0-9]{6}")
_CARD = re.compile(r"t_[0-9a-f]{8}")


class UnmappedCardThread(Exception):
    """Slack identity is verified, but the root has no stable, unique card."""


def mapped_cards(home: Path, thread_ts: str) -> list[str]:
    """Only literal card keys count; reports/home-* keys cannot authorize routing."""
    try:
        data = json.loads((home / "relay" / f"threads.{CHANNEL}.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    return [key for key, value in data.items()
            if _CARD.fullmatch(key) and value == thread_ts]


def mapped_card(home: Path, thread_ts: str) -> str | None:
    """Resolve bot-root work routing, not owner-root conversation ownership."""
    cards = mapped_cards(home, thread_ts)
    if not cards:
        return None
    # Follow-ups retain their terminal parent's root. Only the default board
    # can disambiguate them; never pick by map order or the process's board env.
    db_uri = (home.parent.parent / "kanban.db").resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(db_uri, uri=True, timeout=1)) as conn:
            rows = list(conn.execute(
                f"SELECT id, status FROM tasks WHERE id IN ({','.join('?' for _ in cards)})",
                cards))
    except (OSError, sqlite3.Error):
        logger.warning("[Slack] owner card-thread board lookup failed closed", exc_info=True)
        return None
    # Keep the existing ability to follow up on a single terminal card, but a
    # stale key for a deleted/nonexistent card must receive the same fallback.
    if len(cards) == 1:
        return rows[0][0] if rows else None
    live = [card for card, status in rows if status not in {"done", "archived"}]
    if len(live) == 1:
        return live[0]
    # An ambiguous root must never fan out an owner instruction to several cards.
    logger.warning("[Slack] ambiguous owner card-thread root=%s cards=%s live=%s",
                   thread_ts, cards, live)
    return None


async def verified_owner_conversation(client, channel_id: str) -> bool:
    """The private two-member channel (or owner's exact IM), never Slack Connect."""
    user = await client.users_info(user=OWNER)
    if not user.get("ok") or user.get("user", {}).get("is_bot") is not False:
        return False
    info = await client.conversations_info(channel=channel_id)
    channel = info.get("channel") or {}
    if (not info.get("ok") or channel.get("id") != channel_id
            or channel.get("is_mpim")
            or any(channel.get(k) for k in (
                "is_archived", "is_shared", "is_ext_shared", "is_org_shared",
                "is_pending_ext_shared", "pending_shared"))):
        return False
    if channel_id == "D0C4S9L5HJL":
        return channel.get("is_im") is True and channel.get("user") == OWNER
    if channel_id != CHANNEL or channel.get("is_private") is not True or channel.get("is_im"):
        return False
    members = await client.conversations_members(channel=channel_id, limit=20)
    return bool(members.get("ok")
                and sorted(members.get("members") or []) == sorted([OWNER, BOT])
                and not (members.get("response_metadata") or {}).get("next_cursor"))


async def verified_card(adapter, event: dict, team_id: str, home: Path) -> str | None:
    """Fail closed; only fully verified mapping failures raise UnmappedCardThread."""
    ts, root_ts = event.get("ts", ""), event.get("thread_ts", "")
    if (event.get("channel") != CHANNEL or event.get("user") != OWNER
            or not isinstance(ts, str) or not isinstance(root_ts, str)
            or not _TS.fullmatch(ts) or not _TS.fullmatch(root_ts) or root_ts == ts
            or adapter._event_declares_bot_sender(event)
            or event.get("_hermes_force_process")):
        return None
    bot_uid = adapter._team_bot_user_ids.get(team_id, adapter._bot_user_id)
    if bot_uid != BOT:
        return None
    card = await asyncio.to_thread(mapped_card, home, root_ts)
    client = adapter._get_client(CHANNEL, team_id=team_id or None)
    try:
        # Unlike the general allow_bots gate, this route never admits bot users.
        if not await verified_owner_conversation(client, CHANNEL):
            return None
        # history can fetch the parent with the bot's groups:history scope; replies
        # for a private channel would require a user token. Never trust root caches.
        history = await client.conversations_history(
            channel=CHANNEL, oldest=root_ts, latest=root_ts, inclusive=True, limit=1)
        roots = history.get("messages") or []
        if (not history.get("ok") or len(roots) != 1
                or roots[0].get("ts") != root_ts):
            return None
        root = roots[0]
        owner_root = (root.get("user") == OWNER
                      and not adapter._event_declares_bot_sender(root))
        if not owner_root and root.get("user") != BOT:
            return None
    except Exception:
        logger.warning("[Slack] owner card-thread verification failed closed", exc_info=True)
        return None
    # A verified owner root that intake could not route must explain the failure,
    # never steer a child merely because its notification binding is unique.
    if owner_root:
        raise UnmappedCardThread
    # The relay and card status can change while Slack calls are in flight.
    # Do not rebind mid-reply. Only now is it safe to send a fallback in-thread.
    current = await asyncio.to_thread(mapped_card, home, root_ts)
    if card is None or current != card:
        raise UnmappedCardThread
    return card


def record_reply(home: Path, card: str, event: dict) -> int | None:
    """Write once on the shared default board; a comment also steers a live worker."""
    source = f"slack:{CHANNEL}:{event['thread_ts']}:{event['ts']}"
    text = str(event.get("text") or "")
    body = (f"requested-by: twiceburnt (slack:{OWNER}) · trust: owner\n"
            f"source: {source}\n\n"
            "twiceburnt replied in the card thread (not send approval):\n> "
            + text.replace("\n", "\n> "))
    # Resolve from the owning profile, not the process's currently selected board.
    with connect_closing(home.parent.parent / "kanban.db") as conn:
        if kb.get_task(conn, card) is None:
            raise UnmappedCardThread
        # Cross-card source dedup preserves the original binding across relay remaps.
        # A comment without a wake receipt is pending, not completed: replay retries it.
        with kb.write_txn(conn):
            prefix = body.split("\n\n", 1)[0] + "\n\n"
            prior = conn.execute(
                "SELECT id, task_id FROM task_comments WHERE author=? AND substr(body,1,?)=?",
                (f"slack:{OWNER}", len(prefix), prefix)).fetchone()
            if prior is not None:
                if prior["task_id"] != card:
                    return None
                delivered = conn.execute(
                    "SELECT 1 FROM task_events WHERE task_id=? AND kind='slack_owner_reply_woken' "
                    "AND json_extract(payload, '$.comment_id')=?", (card, prior["id"])).fetchone()
                return None if delivered else prior["id"]
            return kb.add_comment(conn, card, f"slack:{OWNER}", body)


def record_wake(home: Path, card: str, comment_id: int) -> None:
    with connect_closing(home.parent.parent / "kanban.db") as conn, kb.write_txn(conn):
        kb._append_event(conn, card, "slack_owner_reply_woken", {"comment_id": comment_id})


async def send_unmapped_fallback(adapter, event: dict, team_id: str) -> bool:
    """Explain a verified routing failure; ingress dedup owns delivery retries."""
    was_claimed = event["ts"] in adapter._processed_message_ts
    adapter._remember_processed_message_ts(event["ts"])
    client = adapter._get_client(CHANNEL, team_id=team_id or None)
    try:
        response = await client.chat_postMessage(
            channel=CHANNEL, thread_ts=event["thread_ts"],
            text="couldn't tie this to one card; reply in the DM",
            reply_broadcast=False, unfurl_links=False, unfurl_media=False)
        if not response.get("ok"):
            raise RuntimeError("owner card-thread fallback was not sent")
    except BaseException:
        if not was_claimed:
            adapter._processed_message_ts.pop(event["ts"], None)
        event_ts = event.get("_slack_changed_event_ts") or event["ts"]
        adapter._dedup.discard(adapter._workspace_event_id(team_id, event_ts))
        raise
    logger.warning("[Slack] owner card-thread mapping rejected with fallback channel=%s ts=%s",
                CHANNEL, event["ts"])
    return True


async def route_owner_card_reply(adapter, event: dict, team_id: str) -> bool:
    """True = this special channel was consumed (including denied replies)."""
    from .owner_thread_intake import route_owner_thread
    if await route_owner_thread(adapter, event, team_id):
        return True
    if event.get("channel") != CHANNEL or adapter._owner_profile != PROFILE:
        return False
    home = get_profile_dir(PROFILE)
    try:
        card = await verified_card(adapter, event, team_id, home)
        comment_id = (await asyncio.to_thread(record_reply, home, card, event)
                      if card is not None else None)
    except UnmappedCardThread:
        return await send_unmapped_fallback(adapter, event, team_id)
    if card is None:
        logger.info("[Slack] owner card-thread rejected channel=%s ts=%s", CHANNEL, event.get("ts"))
        return True
    if comment_id is None:
        return True
    logger.info("[Slack] owner card-thread routed channel=%s ts=%s card=%s comment=%s",
                CHANNEL, event["ts"], card, comment_id)
    source = adapter.build_source(
        chat_id=CHANNEL, chat_name="chief-of-bots", chat_type="group",
        user_id=OWNER, user_name="twiceburnt", thread_id=event["thread_ts"],
        scope_id=team_id or None, is_bot=False)
    # Same ChiefOfBots card-thread workflow as the DM, but the card binding and
    # durable owner comment are decided in code, not inferred from message text.
    wake = MessageEvent(
        text=(f"[Verified card-thread reply: {card}; owner comment {comment_id} already recorded.]\n"
              f"Read only card {card}. Do not duplicate the comment or route to another card. "
              "Use the existing card-threads workflow to notify its assignee (do not message "
              "yourself or duplicate a running worker). Interpret any unblock request using "
              "that card's context; do not automatically unblock. Reply here with a brief "
              "routing acknowledgment. This is NOT approval to send: sends still require a Send tap."),
        source=source, message_id=event["ts"], raw_message=event,
        allow_gateway_control=False, reply_expected=True)
    try:
        await adapter.handle_message(wake)
        if not wake._gateway_accepted:
            raise RuntimeError("owner card-thread wake was not admitted")
        await asyncio.to_thread(record_wake, home, card, comment_id)
    except BaseException:
        event_ts = event.get("_slack_changed_event_ts") or event["ts"]
        adapter._dedup.discard(adapter._workspace_event_id(team_id, event_ts))
        raise
    return True
