"""Slack adapter routing onto the slack-sdk Agent Sessions API (agents.sessions.*)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from tests.gateway.test_slack import SlackAdapter, _slack_mod


@pytest.fixture(autouse=True)
def _pin_legacy_assistant_threads_api():
    """Pin the SDK capability probe to the legacy assistant.threads API and
    restore it afterwards, so tests that flip the cached flag on don't leak."""
    prev = _slack_mod._AGENT_SESSIONS_SUPPORTED
    _slack_mod._AGENT_SESSIONS_SUPPORTED = False
    yield
    _slack_mod._AGENT_SESSIONS_SUPPORTED = prev


class TestAgentSessionsApiRouting:
    """slack-sdk 3.44.0 Agent Sessions API (assistant_view deprecation Feb 2027).

    When the installed slack-sdk ships agents.sessions.* typed methods, status
    and title calls route through them; older SDKs keep using the legacy
    assistant.threads.* methods (compat bridge on Slack's side).
    """

    def _adapter(self):
        config = PlatformConfig(enabled=True, token="xoxb-fake-token")
        a = SlackAdapter(config)
        a._app = MagicMock()
        a._app.client = AsyncMock()
        return a

    @pytest.mark.asyncio
    async def test_free_text_typing_stays_on_assistant_threads(self):
        # #110374: agents.sessions.setStatus only takes the lifecycle enum; free text
        # ("is thinking...") there fails with invalid_arguments and no status shows.
        _slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a._app.client.agents_sessions_setStatus = AsyncMock()
        a._app.client.assistant_threads_setStatus = AsyncMock()
        await a.send_typing("C123", metadata={"thread_id": "parent_ts"})
        a._app.client.assistant_threads_setStatus.assert_called_once_with(
            channel_id="C123",
            thread_ts="parent_ts",
            status="is thinking...",
        )
        a._app.client.agents_sessions_setStatus.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_status_phrase_stays_on_assistant_threads(self):
        _slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a._status_text = {"D123": "running terminal: pytest"}
        a._app.client.agents_sessions_setStatus = AsyncMock()
        a._app.client.assistant_threads_setStatus = AsyncMock()
        await a.send_typing("D123", metadata={"thread_id": "171234.0001"})
        a._app.client.assistant_threads_setStatus.assert_called_once_with(
            channel_id="D123",
            thread_ts="171234.0001",
            status="running terminal: pytest",
        )
        a._app.client.agents_sessions_setStatus.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_typing_clears_via_assistant_threads(self):
        _slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a._app.client.agents_sessions_setStatus = AsyncMock()
        a._app.client.assistant_threads_setStatus = AsyncMock()
        await a.send_typing("C123", metadata={"thread_id": "parent_ts"})
        a._app.client.assistant_threads_setStatus.reset_mock()
        await a.stop_typing("C123", metadata={"thread_id": "parent_ts"})
        a._app.client.assistant_threads_setStatus.assert_called_once_with(
            channel_id="C123",
            thread_ts="parent_ts",
            status="",
        )
        a._app.client.agents_sessions_setStatus.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["active", "processing", "suspended", "closed"])
    async def test_lifecycle_enum_uses_agent_sessions(self, status):
        _slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a._app.client.agents_sessions_setStatus = AsyncMock()
        a._app.client.assistant_threads_setStatus = AsyncMock()
        await a._set_thread_status("D123", "", "171234.0001", status, "failed")
        a._app.client.agents_sessions_setStatus.assert_called_once_with(
            channel_id="D123",
            thread_ts="171234.0001",
            status=status,
        )
        a._app.client.assistant_threads_setStatus.assert_not_called()

    @pytest.mark.asyncio
    async def test_lifecycle_enum_falls_back_on_old_sdk(self):
        _slack_mod._AGENT_SESSIONS_SUPPORTED = False
        try:
            a = self._adapter()
            a._app.client.agents_sessions_setStatus = AsyncMock()
            a._app.client.assistant_threads_setStatus = AsyncMock()
            await a._set_thread_status("D123", "", "171234.0001", "processing", "failed")
            a._app.client.assistant_threads_setStatus.assert_called_once()
            a._app.client.agents_sessions_setStatus.assert_not_called()
        finally:
            _slack_mod._AGENT_SESSIONS_SUPPORTED = None

    @pytest.mark.asyncio
    async def test_thread_title_uses_agents_sessions_rename(self):
        _slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a.config.extra["assistant_thread_titles"] = True
        a._app.client.agents_sessions_rename = AsyncMock()
        a._app.client.assistant_threads_setTitle = AsyncMock()
        await a._set_assistant_thread_title("D123", "171234.0001", "Summarize the incident")
        a._app.client.agents_sessions_rename.assert_called_once_with(
            channel_id="D123",
            thread_ts="171234.0001",
            title="Summarize the incident",
        )
        a._app.client.assistant_threads_setTitle.assert_not_called()
