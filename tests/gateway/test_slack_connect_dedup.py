"""Regression coverage for overlapping Slack Connect message deliveries."""

import pytest

from tests.gateway.test_slack import adapter  # noqa: F401 - shared pytest fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("first_type", ["app_mention", "message"])
async def test_slack_connect_mention_dispatches_once_for_one_installation(
    adapter, first_type
):
    adapter._team_clients["T_LOCAL"] = adapter._app.client
    event = {
        "text": "<@U_BOT> hello",
        "user": "U_EXTERNAL",
        "channel": "C_SHARED",
        "channel_type": "channel",
        "ts": "171.000",
    }
    body = {
        "team_id": "T_LOCAL",
        "is_ext_shared_channel": True,
        "authorizations": [{"team_id": "T_LOCAL", "is_bot": True}],
    }
    events = {
        "app_mention": {**event, "type": "app_mention"},
        "message": {**event, "type": "message", "team": "T_EXTERNAL"},
    }
    second_type = "message" if first_type == "app_mention" else "app_mention"
    await adapter._handle_slack_message(events[first_type], body)
    await adapter._handle_slack_message(events[second_type], body)
    await adapter._handle_slack_message(events[first_type], body)

    adapter.handle_message.assert_awaited_once()
    source = adapter.handle_message.await_args.args[0].source
    assert source.chat_id == "C_SHARED"
    assert source.thread_id == "171.000"

    await adapter._handle_slack_message(
        {**events[first_type], "channel": "C_OTHER_SHARED"}, body
    )
    await adapter._handle_slack_message(
        {**events[first_type], "ts": "172.000", "thread_ts": "171.000"}, body
    )
    assert adapter.handle_message.await_count == 3
