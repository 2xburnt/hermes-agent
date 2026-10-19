"""Hetz-only owner-authored Chief conversations, not a general mention exemption.

slagent owns card creation: its stdlib intake CLI accepts one JSON event on stdin
and returns {ok, card_id, channel_id, thread_ts, created}. A failed intake leaves
chat available but explicitly untracked; no adapter-side creator or retry loop.
Bot-authored card roots continue through owner_card_threads unchanged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from . import owner_card_threads as cards

logger = logging.getLogger(__name__)
DM = "D0C4S9L5HJL"
INTAKE_TIMEOUT = 45


async def intake_card(home: Path, event: dict, team_id: str) -> str | None:
    """Call the sole creator once; never trust a response for a different thread."""
    from tools.environments.local import served_profile_child_env

    payload = {"team_id": team_id, "channel_id": event["channel"],
               "user_id": event["user"], "ts": event["ts"],
               "text": event.get("text") or ""}
    if event.get("thread_ts"):
        payload["thread_ts"] = event["thread_ts"]
    process = None
    try:
        env = served_profile_child_env(target_home=home, inherit_credentials=True)
        # A gateway child must never inherit a dispatch worker's task/board.
        for key in tuple(env):
            if key.startswith("HERMES_KANBAN_"):
                env.pop(key)
        env["HERMES_KANBAN_DB"] = str(home.parent.parent / "kanban.db")
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "slagent.cli", "intake", "--profile-home", str(home),
            cwd=home / "plugins" / "slagent", env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        stdout, _ = await asyncio.wait_for(
            process.communicate(json.dumps(payload).encode()), timeout=INTAKE_TIMEOUT)
        if process.returncode == 2:
            return None  # not trackable (e.g. empty); normal chat behaviour
        result = json.loads(stdout)
        card = result.get("card_id") if isinstance(result, dict) else None
        if (process.returncode != 0 or not isinstance(result, dict) or result.get("ok") is not True
                or not isinstance(card, str) or not cards._CARD.fullmatch(card)
                or result.get("channel_id") != event["channel"]
                or result.get("thread_ts") != event["ts"]):
            raise ValueError("invalid intake receipt")
        return card
    except (OSError, ValueError, TimeoutError):
        # No raw stdout/stderr or exception text: either can contain owner text.
        logger.warning("[Slack] owner intake unavailable; dispatching untracked channel=%s ts=%s",
                       event["channel"], event["ts"])
        return None
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=5)


def continuation_card(home: Path, root_ts: str) -> str | None:
    """Only slagent intake roots gain conversational continuation, never old cards."""
    uri = (home.parent.parent / "kanban.db").resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as conn:
            row = conn.execute(
                "SELECT id FROM tasks WHERE created_by='slagent' "
                "AND assignee=? AND idempotency_key=?",
                (cards.PROFILE, f"slack-thread:{cards.CHANNEL}:{root_ts}")).fetchone()
        # Children share notification roots; the intake owns the conversation.
        return row[0] if row and row[0] in cards.mapped_cards(home, root_ts) else None
    except sqlite3.Error:
        logger.warning("[Slack] owner intake continuation lookup failed closed")
        return None


def eligible_owner(adapter, event: dict, team_id: str) -> bool:
    ts = event.get("ts")
    root_ts = event.get("thread_ts") or ts
    return bool(
        event.get("channel") in {cards.CHANNEL, DM}
        and getattr(adapter, "_owner_profile", None) == cards.PROFILE
        and event.get("user") == cards.OWNER
        and isinstance(ts, str) and cards._TS.fullmatch(ts)
        and isinstance(root_ts, str) and cards._TS.fullmatch(root_ts)
        and not adapter._event_declares_bot_sender(event)
        and not event.get("_hermes_force_process")
        and adapter._team_bot_user_ids.get(team_id, adapter._bot_user_id) == cards.BOT)


async def verified_owner_root(adapter, event: dict, team_id: str, root_ts: str) -> bool:
    client = adapter._get_client(event["channel"], team_id=team_id or None)
    try:
        if not await cards.verified_owner_conversation(client, event["channel"]):
            return False
        history = await client.conversations_history(
            channel=event["channel"], oldest=root_ts, latest=root_ts, inclusive=True, limit=1)
        roots = history.get("messages") or []
        return bool(history.get("ok") and len(roots) == 1
                    and roots[0].get("ts") == root_ts
                    and roots[0].get("user") == cards.OWNER
                    and not adapter._event_declares_bot_sender(roots[0]))
    except Exception as exc:  # health: allow BLE001 -- fail closed without logging private Slack SDK response bodies
        logger.warning("[Slack] owner intake Slack verification failed closed (%s)", type(exc).__name__)
        return False


async def build_turn(adapter, event: dict, team_id: str, root_ts: str, card: str | None):
    # Keep the adapter's rich text/media pipeline, without the general mention gate.
    from .adapter import _rewrite_known_bang_command

    text = event.get("text") or ""
    text = text.replace(f"<@{cards.BOT}>", "").strip()
    command = _rewrite_known_bang_command(text.lstrip())
    is_command = command.startswith("/")
    if not is_command:
        text = adapter._append_block_text(text, event.get("blocks") or [], cards.BOT)
        text = adapter._append_link_unfurls(text, event.get("attachments") or [])
    media_urls, media_types, media_inlined, text = await adapter._collect_inbound_media(
        event, event["channel"], team_id, text, [], [])
    turn = await adapter._build_message_event(
        event, text=text, original_text=event.get("text") or "", command_probe_text=command,
        is_command_text=is_command, channel_id=event["channel"], team_id=team_id,
        ts=event["ts"], user_id=cards.OWNER, thread_ts=root_ts, is_dm=event["channel"] == DM,
        media_urls=media_urls, media_types=media_types, media_text_inlined=media_inlined,
        channel_context=None, reply_expected=True)
    if card and not is_command:
        turn.text = f"[Tracked as card {card}]\n{turn.text}"
    return turn


async def route_owner_thread(adapter, event: dict, team_id: str) -> bool:
    """Consume verified owner roots and mapped owner-root channel continuations."""
    if not eligible_owner(adapter, event, team_id):
        return False
    home = cards.get_profile_dir(cards.PROFILE)
    ts = event["ts"]
    root_ts = event.get("thread_ts") or ts
    card = None
    if root_ts != ts:
        if event["channel"] == DM:
            return False  # preserve normal DM reply handling
        card = await asyncio.to_thread(continuation_card, home, root_ts)
        if card is None:
            return False  # existing bot-root card routing owns the rest
    # Claim before Slack awaits so a mid-flight unfurl/edit cannot start a second turn.
    adapter._remember_processed_message_ts(ts)
    if not await verified_owner_root(adapter, event, team_id, root_ts):
        return True
    if root_ts != ts and card != await asyncio.to_thread(continuation_card, home, root_ts):
        return await cards.send_unmapped_fallback(adapter, event, team_id)
    try:
        from .adapter import _rewrite_known_bang_command
        canonical = (event.get("text") or "").replace(f"<@{cards.BOT}>", "").strip()
        if root_ts == ts and not _rewrite_known_bang_command(canonical).startswith("/"):
            card = await intake_card(home, event, team_id)
        turn = await build_turn(adapter, event, team_id, root_ts, card)
        await adapter.handle_message(turn)
        if not turn._gateway_accepted:
            raise RuntimeError("owner intake turn was not admitted")
    except BaseException:
        event_ts = event.get("_slack_changed_event_ts") or ts
        adapter._dedup.discard(adapter._workspace_event_id(team_id, event_ts))
        raise
    logger.info("[Slack] owner intake admitted channel=%s ts=%s card=%s",
                event["channel"], ts, card or "untracked")
    return True
