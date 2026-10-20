"""Hetz-only owner ingress for ChiefOfBots' private card-thread channel.

Do not turn this into a generic mention exemption: unsafe messages are consumed.
Verified owner replies route to one card or wake Chief in the same thread for
recovery. Owner-authored intake roots have a separate slagent-backed path; the
DM owner_handoff exception retains its existing guards.
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
_CHIEF_ITEM = re.compile(r"cos:[0-9a-f]+")


class UnmappedCardThread(Exception):
    """Slack identity is verified, but the root has no stable card binding."""


def mapped_thread_items(home: Path, thread_ts: str) -> list[str]:
    """Literal card/Chief item IDs only; never pass arbitrary relay text to a turn."""
    try:
        data = json.loads((home / "relay" / f"threads.{CHANNEL}.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    return [key for key, value in data.items()
            if (_CARD.fullmatch(key) or _CHIEF_ITEM.fullmatch(key)) and value == thread_ts]


def mapped_cards(home: Path, thread_ts: str) -> list[str]:
    """Chief's Linear items are recovery context, never board-card authority."""
    return [key for key in mapped_thread_items(home, thread_ts) if _CARD.fullmatch(key)]


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
                f"SELECT id, status, created_at FROM tasks WHERE id IN ({','.join('?' for _ in cards)})",
                cards))
    except (OSError, sqlite3.Error):
        logger.warning("[Slack] owner card-thread board lookup failed closed", exc_info=True)
        return None
    if not rows:
        return None
    live = [row for row in rows if row[1] not in {"done", "archived"}]
    priority = {"blocked": 0, "running": 1, "ready": 2, "todo": 2}
    # Never fan out. Creation time (not update time or relay order) breaks ties;
    # the ID is a stable final tie-break for cards created in the same second.
    chosen = min(live or rows, key=lambda row: (
        priority.get(row[1], 3) if live else 0, -row[2], row[0]))[0]
    if len(cards) > 1:
        logger.info("[Slack] owner card-thread selected root=%s card=%s candidates=%s rows=%s",
                    thread_ts, chosen, sorted(cards), sorted(rows))
    return chosen


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


def record_reply(home: Path, card: str, event: dict) -> tuple[str, int] | None:
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
                # A status change can rank a different card on a retry. Finish
                # the original wake, never drop it or comment on the new winner.
                card = prior["task_id"]
                delivered = conn.execute(
                    "SELECT 1 FROM task_events WHERE task_id=? AND kind='slack_owner_reply_woken' "
                    "AND json_extract(payload, '$.comment_id')=?", (card, prior["id"])).fetchone()
                return None if delivered else (card, prior["id"])
            return card, kb.add_comment(conn, card, f"slack:{OWNER}", body)


def record_wake(home: Path, card: str, comment_id: int) -> None:
    with connect_closing(home.parent.parent / "kanban.db") as conn, kb.write_txn(conn):
        kb._append_event(conn, card, "slack_owner_reply_woken", {"comment_id": comment_id})


async def wake_unmapped_thread(adapter, event: dict, team_id: str) -> bool:
    """Keep a verified owner's reply in its Chief session, even without a card."""
    home = get_profile_dir(PROFILE)
    adapter._remember_processed_message_ts(event["ts"])
    items = sorted(await asyncio.to_thread(mapped_thread_items, home, event["thread_ts"]))
    candidates = [key for key in items if _CARD.fullmatch(key)]
    chief_items = [key for key in items if _CHIEF_ITEM.fullmatch(key)]
    source = adapter.build_source(
        chat_id=CHANNEL, chat_name="chief-of-bots", chat_type="group",
        user_id=OWNER, user_name="twiceburnt", thread_id=event["thread_ts"],
        scope_id=team_id or None, is_bot=False)
    wake = MessageEvent(
        text=("[Verified owner thread reply; automatic card routing unavailable.]\n"
              f"Candidate cards (may be stale): {', '.join(candidates) or 'none'}.\n"
              f"ChiefOfBots-owned Linear relay items: {', '.join(chief_items) or 'none'}.\n"
              "Handle this reply here as ChiefOfBots. No card comment has been recorded by "
              "this fallback. Check the thread and candidates before choosing at most one card; "
              "do not fan out or duplicate an existing owner comment. If the binding cannot "
              "be recovered, resolve it with the owner here. This is NOT send approval; "
              "sends still require a Send tap.\n\nOwner reply:\n" + str(event.get("text") or "")),
        source=source, message_id=event["ts"], raw_message=event,
        allow_gateway_control=False, reply_expected=True)
    try:
        await adapter.handle_message(wake)
        if not wake._gateway_accepted:
            raise RuntimeError("owner card-thread fallback was not admitted")
    except BaseException:
        adapter._processed_message_ts.pop(event["ts"], None)
        event_ts = event.get("_slack_changed_event_ts") or event["ts"]
        adapter._dedup.discard(adapter._workspace_event_id(team_id, event_ts))
        raise
    logger.info("[Slack] owner card-thread fallback admitted channel=%s ts=%s candidates=%s items=%s",
                CHANNEL, event["ts"], candidates, chief_items)
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
        recorded = (await asyncio.to_thread(record_reply, home, card, event)
                    if card is not None else None)
    except (UnmappedCardThread, sqlite3.Error):
        return await wake_unmapped_thread(adapter, event, team_id)
    if card is None:
        logger.info("[Slack] owner card-thread rejected channel=%s ts=%s", CHANNEL, event.get("ts"))
        return True
    if recorded is None:
        return True
    card, comment_id = recorded
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
