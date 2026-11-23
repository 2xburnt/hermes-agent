"""A fast-mode refusal must stay a fast-mode problem: never a cooldown on the shared credential.

Anthropic answers a ``speed: "fast"`` request from an account without fast-mode credits with
``429 Usage credits are required for fast mode.`` and no fast-mode limit headers. Read as an
ordinary rate limit, that 429 wrote ``model_cooldowns[model]`` onto the pooled credential every
profile shares, and every later agent built for the model started with an empty key and died in
the SDK (``TypeError: Could not resolve authentication method``). Real ``AIAgent`` turn loop, real
credential pool in a temp auth store; only the wire call is faked.
"""
import json
import math
import time

import httpx
import pytest
from anthropic import RateLimitError
from anthropic.types import Message, TextBlock, Usage

from agent import fast_mode

KEY = "sk-ant-api03-" + "x" * 40
MODEL = "claude-opus-5-5"
_CREDITS = "Usage credits are required for fast mode."


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    root.mkdir()
    (tmp_path / "fakehome").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "fakehome"))
    for var in ("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("HERMES_HOME", str(root))
    import hermes_constants
    hermes_constants._default_hermes_root_memo = None  # type: ignore[attr-defined]
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    return root


def _write_pool(root, **entry):
    (root / "auth.json").write_text(json.dumps({"credential_pool": {"anthropic": [{
        "id": "shared", "label": "anthropic-oauth-2", "auth_type": "api_key", "priority": 0,
        "source": "manual", "access_token": KEY, **entry,
    }]}}))


def _persisted_entry(root):
    return json.loads((root / "auth.json").read_text())["credential_pool"]["anthropic"][0]


def _fast_429(message=_CREDITS, headers=None):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, request=request, headers=headers or {})
    body = {"type": "error", "error": {"type": "rate_limit_error", "message": message}}
    return RateLimitError(message, response=response, body=body)


def _reply(text="done"):
    return Message(id="msg_1", type="message", role="assistant", model=MODEL, stop_reason="end_turn",
                   stop_sequence=None, content=[TextBlock(type="text", text=text)],
                   usage=Usage(input_tokens=3, output_tokens=1))


def _agent(**kw):
    from run_agent import AIAgent

    agent = AIAgent(provider="anthropic", base_url="https://api.anthropic.com", model=MODEL,
                    api_mode="anthropic_messages", quiet_mode=True, skip_context_files=True,
                    skip_memory=True, enabled_toolsets=[], **kw)
    agent._disable_streaming = True
    agent._persist_session = lambda *a, **k: None
    agent._save_trajectory = lambda *a, **k: None
    return agent


def _wire(agent, outcomes):
    """Fake the transport call; record whether each request asked for fast speed."""
    sent = []

    def _call(api_kwargs):
        if not agent._anthropic_api_key:  # what the Anthropic SDK does with an empty key
            raise TypeError('"Could not resolve authentication method. Expected either api_key or auth_token to be set."')
        sent.append((api_kwargs.get("extra_body") or {}).get("speed"))
        outcome = outcomes(api_kwargs)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    agent._interruptible_api_call = _call
    return sent


def test_every_fast_429_is_fast_only_and_the_no_capacity_shapes_last_the_session():
    fast = {"model": MODEL, "extra_body": {"speed": "fast"}}
    # The live incident's shape: credits body, no fast-mode limit headers at all.
    assert fast_mode.fast_mode_refusal(_fast_429(), fast) == math.inf
    assert fast_mode.fast_mode_refusal(_fast_429("rate limited"), fast) == math.inf  # no limit headers
    spent = {"anthropic-fast-input-tokens-limit": "2000000", "anthropic-fast-output-tokens-limit": "80000",
             "retry-after": "7"}
    assert fast_mode.fast_mode_refusal(_fast_429("fast mode window spent", spent), fast) == 7.0
    assert fast_mode.fast_mode_refusal(_fast_429(), {"model": MODEL}) is None  # standard-speed 429


def test_a_spent_fast_window_pauses_fast_mode_only_until_it_resets(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(fast_mode.time, "monotonic", lambda: clock[0])
    from types import SimpleNamespace
    agent = SimpleNamespace(model=MODEL, service_tier="priority", request_overrides={"speed": "fast"},
                            _fast_mode_unavailable_models={}, provider="anthropic", api_mode="anthropic_messages")
    assert fast_mode.mark_fast_mode_unavailable(agent, 30.0) is True
    assert fast_mode.mark_fast_mode_unavailable(agent, 30.0) is False  # one retry per pause
    assert "speed" not in fast_mode.effective_request_overrides(agent)
    clock[0] += 31
    assert fast_mode.effective_request_overrides(agent)["speed"] == "fast"
    assert fast_mode.mark_fast_mode_unavailable(agent, 30.0) is True


def test_fast_credits_429_retries_at_standard_speed_and_never_cools_the_shared_credential(home):
    from agent.credential_pool import load_pool

    _write_pool(home)
    agent = _agent(api_key=KEY, credential_pool=load_pool("anthropic"))
    agent.request_overrides = {"speed": "fast"}  # agent.service_tier: fast / an open cold window
    sent = _wire(agent, lambda kw: _fast_429() if (kw.get("extra_body") or {}).get("speed") else _reply())

    result = agent.run_conversation("hello")

    assert result["completed"] is True and result["final_response"] == "done"
    assert sent == ["fast", None]  # one retry, same credential, standard speed
    assert not _persisted_entry(home).get("model_cooldowns")
    assert not _persisted_entry(home).get("last_status")
    fresh = load_pool("anthropic")
    assert fresh.select(model=MODEL) is not None  # every other profile still gets the credential


def test_agent_built_on_a_cooled_pool_names_the_cooldown_instead_of_an_sdk_type_error(home):
    until = time.time() + 3600
    _write_pool(home, model_cooldowns={MODEL: until})
    agent = _agent()
    assert agent._anthropic_api_key == ""  # the worker shape: no usable key at construction
    sent = _wire(agent, lambda kw: _reply())

    result = agent.run_conversation("hello")

    assert result["completed"] is False and sent == []
    error = str(result.get("error") or result.get("final_response"))
    assert "in cooldown for model 'claude-opus-5-5'" in error
    assert "Could not resolve authentication" not in error
    # Nothing was sent, so nothing new was learned about the credential.
    assert _persisted_entry(home)["model_cooldowns"] == {MODEL: until}
    assert not _persisted_entry(home).get("last_status")


def test_a_lifted_cooldown_heals_the_keyless_agent_on_its_next_request(home):
    _write_pool(home, model_cooldowns={MODEL: time.time() + 3600})
    agent = _agent()
    assert agent._anthropic_api_key == ""
    _write_pool(home)  # `hermes auth reset anthropic`, or the window passed
    sent = _wire(agent, lambda kw: _reply())

    result = agent.run_conversation("hello")

    assert result["completed"] is True and sent == [None]
    assert agent._anthropic_api_key == KEY


def test_pool_unavailable_error_falls_back_without_touching_the_pool():
    from agent.error_classifier import FailoverReason, classify_api_error
    from agent.errors import CredentialPoolUnavailableError

    verdict = classify_api_error(CredentialPoolUnavailableError("cooldown", provider="anthropic", model=MODEL),
                                 provider="anthropic", model=MODEL)
    assert verdict.reason == FailoverReason.rate_limit
    assert verdict.should_fallback is True and verdict.should_rotate_credential is False
