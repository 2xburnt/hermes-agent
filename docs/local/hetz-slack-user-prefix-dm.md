# Slack: resolve `user:U…` DM targets in cron delivery

Local patch (hetz). Not upstream yet; PR text below.

## Problem

A cron job with `deliver=slack:U<id>` goes through the Slack send-target parser
(`plugins/platforms/slack/send_targets.py`), which turns a bare user id into
`user:U<id>`. The cron path then calls the plugin's `_standalone_send` directly, without the
`send_message` tool's own `_resolve_slack_user_target` step, so the Slack plugin sees the prefixed
id. Both DM-resolution checks test the first character only:

- `_standalone_send`: `chat_id[:1] in ("U", "W")`
- `SlackAdapter._ensure_dm_conversation`: `cid[0] in ("U", "W")`

`u` of `user:` is lowercase, so neither matches, `conversations.open` is skipped and
`chat.postMessage` answers `channel_not_found`. Seen on every DM-targeted RoboReece cron delivery
from 2026-11-15 until the jobs were repointed at a `D…` id.

## Change

`_strip_slack_user_prefix()` removes an optional leading `user:` and is applied at the top of both
checks, so a `user:U…` id takes the existing `conversations.open` path (same per-(team, user) and
per-(token, user) caches as a bare id). Bare ids and `C/G/D` ids are unchanged.
`user_name:<handle>` (users.list) is deliberately not handled here; the `send_message` tool still
resolves it.

Tests (`tests/gateway/test_slack.py`): `test_user_prefixed_target_is_opened_as_dm`,
`test_user_prefixed_and_bare_share_one_cache_entry`,
`test_user_prefixed_target_resolves_dm_before_post`,
`test_user_prefixed_open_failure_reports_resolution_error`. All four fail on the base commit.

## Upstream PR text

**fix(slack): open the DM for `user:U…` targets in cron delivery**

`deliver=slack:U123…` is parsed to `user:U123…` by the Slack send-target parser. The `send_message`
tool resolves that prefix before sending, but cron delivery calls `_standalone_send` directly, and
`_standalone_send` / `SlackAdapter._ensure_dm_conversation` only recognise a bare `U`/`W` first
character. The prefixed id skips `conversations.open` and `chat.postMessage` fails with
`channel_not_found`. Strip an optional `user:` prefix in both places so user-targeted cron
delivery reaches the DM. `user_name:` resolution is unchanged. Tests cover both paths and fail
without the change.

## Verifying after adoption

Create a throwaway cron job with `deliver=slack:<own user id>`, run it once, confirm the DM arrives,
delete the job.
