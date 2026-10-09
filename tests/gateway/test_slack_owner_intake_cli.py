"""Intake wire contract: a real child process with a deliberately fake slagent CLI."""
import asyncio
import json
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.platforms.slack import owner_card_threads as cards
from plugins.platforms.slack import owner_thread_intake as intake
from tools.environments import local

TS = "1791479900.000001"
CARD = "t_1234abcd"
EVENT = {"channel": cards.CHANNEL, "user": cards.OWNER, "ts": TS, "text": "Owner request"}


@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / cards.PROFILE
    package = home / "plugins" / "slagent" / "slagent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    # Test transport only: not a creator, no live board/Slack/network access.
    (package / "cli.py").write_text(
        "import sys,json,os\nfrom pathlib import Path\n"
        "home=Path(sys.argv[-1])\npayload=json.load(sys.stdin)\n"
        "(home/'request.json').write_text(json.dumps({'payload':payload,'argv':sys.argv[1:],"
        "'env':{k:v for k,v in os.environ.items() if k.startswith('HERMES_')}}))\n"
        "spec=json.loads((home/'response.json').read_text())\n"
        "print(spec['stdout'])\nsys.exit(spec['exit'])\n")
    env = local.served_profile_child_env(target_home=home)
    env.update(HERMES_KANBAN_TASK="t_deadbeef", HERMES_KANBAN_BOARD="wrong-board")
    builder = MagicMock(return_value=env)
    monkeypatch.setattr(local, "served_profile_child_env", builder)
    return home, builder


def respond(home, value, code=0):
    (home / "response.json").write_text(json.dumps({
        "stdout": json.dumps(value), "exit": code}))


def receipt(**extra):
    return {"ok": True, "card_id": CARD, "channel_id": cards.CHANNEL,
            "thread_ts": TS, "created": True, **extra}


@pytest.mark.asyncio
async def test_real_child_receipt_and_profile_scoped_payload(cli_home):
    home, builder = cli_home
    respond(home, receipt())
    assert await intake.intake_card(home, EVENT, "T_TEST") == CARD
    got = json.loads((home / "request.json").read_text())
    assert got["payload"] == {"team_id": "T_TEST", "channel_id": cards.CHANNEL,
                              "user_id": cards.OWNER, "ts": TS, "text": "Owner request"}
    assert got["argv"] == ["intake", "--profile-home", str(home)]
    assert got["env"]["HERMES_HOME"] == str(home)
    assert got["env"]["HERMES_KANBAN_DB"] == str(home.parent.parent / "kanban.db")
    assert "HERMES_KANBAN_TASK" not in got["env"]
    assert "HERMES_KANBAN_BOARD" not in got["env"]
    builder.assert_called_once_with(target_home=home, inherit_credentials=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("value,code", [
    (receipt(card_id="t_BAD"), 0), (receipt(card_id=" t_1234abcd"), 0),
    (receipt(channel_id="C_OTHER"), 0), (receipt(thread_ts="1791479901.000001"), 0),
    (receipt(ok=False), 0), ([], 0), (None, 0),
    ({"ok": False, "error": "not_owner"}, 2),
    ({"ok": False, "error": "secret-owner-text"}, 1),
])
async def test_bad_or_refused_receipt_never_invents_tracking(cli_home, caplog, value, code):
    home, _ = cli_home
    respond(home, value, code)
    assert await intake.intake_card(home, EVENT, "") is None
    assert "secret-owner-text" not in caplog.text
    assert "Owner request" not in caplog.text
    assert ("dispatching untracked" in caplog.text) == (code != 2)


@pytest.mark.asyncio
async def test_missing_cli_is_untracked_not_a_crash(tmp_path, caplog):
    assert await intake.intake_card(tmp_path / "absent", EVENT, "") is None
    assert "dispatching untracked" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timed_out_or_cancelled_child_is_reaped(tmp_path, monkeypatch, cancel):
    process = MagicMock(returncode=None)
    process.communicate = AsyncMock(side_effect=asyncio.CancelledError if cancel else TimeoutError)
    process.wait = AsyncMock()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(intake.asyncio, "create_subprocess_exec", spawn)
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await intake.intake_card(tmp_path, EVENT, "")
    else:
        assert await intake.intake_card(tmp_path, EVENT, "") is None
    spawn.assert_awaited_once()
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()
