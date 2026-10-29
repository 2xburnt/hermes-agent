"""Bounded fast-mode windows (``/fast auto`` and ``/fast cold``).

``agent.service_tier``: ``None`` (normal), ``"priority"`` / ``"ultrafast"`` (static tiers,
pinned into ``agent.request_overrides`` at build time), ``"auto"`` (every user turn opens a
window of ``agent.fast_auto_seconds``) or ``"cold"`` (only a session's first turn,
no prior history, opens it). The provider's fast override is layered onto request
kwargs only while the window is open; only per-request params (``service_tier`` /
``speed``) vary, so the request body stays byte-identical. Anthropic keeps a separate
prompt cache per speed, so each Anthropic window boundary re-writes the prefix at the
new speed.
"""

from __future__ import annotations

import math
import time
from typing import Any

BOUNDED_MODES = frozenset({"auto", "cold"})
DEFAULT_WINDOW_SECONDS = 60
# Documented fast-mode rate-limit headers; a limit of 0 means the organization has no fast
# capacity for the model (https://platform.claude.com/docs/en/build-with-claude/fast-mode).
_FAST_LIMIT_HEADERS = ("anthropic-fast-input-tokens-limit", "anthropic-fast-output-tokens-limit")
# The 429 body Anthropic returns when the account has no usage credits for fast mode (no limit headers).
_FAST_UNAVAILABLE_MESSAGE = "required for fast mode"
#: Tiers sent on every request of the session (OpenAI ``service_tier`` values; ``priority`` also
#: selects Anthropic/xAI fast mode). Ultrafast is OpenAI-only and gated per model.
STATIC_TIERS = frozenset({"priority", "ultrafast"})
# Codex app-server names for wire tiers it accepts (turn/start.serviceTier); a tier missing here is not sent.
CODEX_TIER_WORDS: dict[str, str] = {"priority": "fast"}
NORMAL_TIER_WORDS = frozenset({"", "normal", "default", "standard", "off", "none"})
# User/config word -> agent.service_tier. The single table every surface (config loaders, /fast
# on CLI / gateway / TUI) parses through, so a new tier is one edit.
SERVICE_TIER_WORDS: dict[str, str] = {
    "fast": "priority", "priority": "priority", "on": "priority",
    "ultrafast": "ultrafast", "ultra": "ultrafast", "auto": "auto", "cold": "cold",
}
# Typed /fast shorthands -> canonical /fast word (pickers always send the canonical value).
FAST_ARG_ALIASES: dict[str, str] = {"ultra": "ultrafast"}


def parse_service_tier(raw: Any) -> str | None:
    """``agent.service_tier`` for a user/config word; None for normal and for unknown words."""
    value = str(raw or "").strip().lower()
    return None if value in NORMAL_TIER_WORDS else SERVICE_TIER_WORDS.get(value)


def parse_exact_service_tier(raw: Any) -> str:
    """Strict :func:`parse_service_tier` for an explicit client pick: ``""`` pins normal, an unknown
    word raises ``ValueError`` instead of silently reading as normal."""
    value = str(raw or "").strip().lower()
    if value not in NORMAL_TIER_WORDS and value not in SERVICE_TIER_WORDS:
        raise ValueError(f"unknown service tier: {value}")
    return parse_service_tier(value) or ""


def service_tier_word(tier: Any) -> str:
    """The user-facing word for a stored tier (``priority`` -> ``fast``, None/"" -> ``normal``)."""
    return {"priority": "fast", None: "normal", "": "normal"}.get(tier, tier)


def begin_turn(agent: Any, conversation_history: Any) -> None:
    """Open (or refuse) the fast window at a user-turn boundary."""
    mode = getattr(agent, "service_tier", None)
    agent._fast_until = 0.0
    if mode not in BOUNDED_MODES:
        return
    if mode == "cold" and any(
        isinstance(m, dict) and m.get("role") in ("user", "assistant", "tool")
        for m in (conversation_history or ())
    ):
        return
    try:
        window = float(getattr(agent, "fast_auto_seconds", DEFAULT_WINDOW_SECONDS))
    except (TypeError, ValueError):
        window = DEFAULT_WINDOW_SECONDS
    agent._fast_until = time.monotonic() + max(window, 0.0)


def effective_request_overrides(agent: Any) -> dict[str, Any]:
    """``agent.request_overrides`` plus the fast override while the window is open, minus
    ``speed`` for a model whose fast mode this session saw refused (see :func:`fast_mode_refusal`)."""
    overrides = dict(getattr(agent, "request_overrides", None) or {})
    if getattr(agent, "service_tier", None) in BOUNDED_MODES and time.monotonic() < getattr(agent, "_fast_until", 0.0):
        from hermes_cli.models import resolve_fast_mode_overrides
        base_url = getattr(agent, "base_url", None)
        if getattr(agent, "api_mode", None) == "anthropic_messages":
            base_url = getattr(agent, "_anthropic_base_url", None) or base_url
        overrides.update(
            resolve_fast_mode_overrides(getattr(agent, "model", None), provider=getattr(agent, "provider", None), base_url=base_url) or {}
        )
    paused = getattr(agent, "_fast_mode_unavailable_models", None) or {}
    if "speed" in overrides and paused.get(getattr(agent, "model", None), 0.0) > time.monotonic():
        overrides.pop("speed", None)
    from agent.codex_service_tiers import apply_daybreak, negotiate_request_tier
    negotiate_request_tier(agent, overrides)
    apply_daybreak(agent, overrides)
    return overrides


def fast_mode_refusal(api_error: Any, api_kwargs: Any) -> float | None:
    """Seconds to send the current model at standard speed after a 429 on a ``speed: "fast"``
    request, or None when *api_error* is not one.

    Fast mode is metered on its own limits, so such a 429 says nothing about the credential's
    standard-speed standing and must never bench the (often shared) credential for the model.
    ``inf`` (rest of the session) when the organization cannot use fast mode at all: a fast-mode
    limit header of 0, "Usage credits are required for fast mode", or no fast-mode limit headers
    at all. A spent fast window (non-zero limit) pauses fast mode until its ``retry-after``."""
    if getattr(api_error, "status_code", None) != 429 or not isinstance(api_kwargs, dict):
        return None
    if (api_kwargs.get("extra_body") or {}).get("speed") != "fast":
        return None
    headers = getattr(getattr(api_error, "response", None), "headers", None) or {}
    limits = [str(headers.get(name, "")).strip() for name in _FAST_LIMIT_HEADERS]
    if _FAST_UNAVAILABLE_MESSAGE in str(api_error).lower() or "0" in limits or not any(limits):
        return math.inf
    try:
        return max(float(headers.get("retry-after", DEFAULT_WINDOW_SECONDS)), 1.0)
    except (TypeError, ValueError):
        return float(DEFAULT_WINDOW_SECONDS)


def mark_fast_mode_unavailable(agent: Any, seconds: float = math.inf) -> bool:
    """Stop sending ``speed`` for the current model for *seconds* (default: the rest of the
    session). False while the model is already paused, so the caller retries at most once."""
    model = getattr(agent, "model", None)
    paused = getattr(agent, "_fast_mode_unavailable_models", None)
    if not isinstance(paused, dict):
        paused = agent._fast_mode_unavailable_models = {}
    now = time.monotonic()
    if not model or paused.get(model, 0.0) > now:
        return False
    paused[model] = now + seconds
    return True


def fast_mode_notice(model: Any, seconds: float) -> str:
    """The one-line status for :func:`mark_fast_mode_unavailable`."""
    if math.isinf(seconds):
        return f"⚠️  Fast mode isn't available for {model} on this Anthropic account — using standard speed for this session, retrying..."
    return f"⚠️  Fast-mode rate limit reached for {model} — using standard speed for {seconds:.0f}s, retrying..."
