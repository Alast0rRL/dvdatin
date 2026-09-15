# Web bridge — send reactions (👍/❤️) from Web UI to Telegram (Leo chat)
# and switch the live mode (OBSERVE/SEMI_AUTO/AUTO) on the collector.
#
# When the owner clicks LIKE/DISLIKE in the web dashboard, the reaction
# must actually be sent to the Leo chat via the auto account so that Leo
# advances to the next profile.  When the owner switches the mode on the
# site, the running AutoActionEngine must be updated in place (not just the
# YAML file), otherwise the switch has no effect until restart.  This module
# bridges the Flask sync thread to the async event loop via
# ``run_coroutine_threadsafe`` — same pattern as ``web/photos.py``.

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from collectors.auto_action import AutoActionEngine
    from collectors.dvinchik_collector import DvinchikCollector
    from core.types import Mode

#: React constants matching auto_action.py.
LIKE_TEXT: str = "\u2764\ufe0f"
DISLIKE_TEXT: str = "\U0001F44E"

#: Reference to the auto-action engine (set from main.py).
_engine: AutoActionEngine | None = None

#: Reference to the collector (set from main.py) for live mode switching.
_collector: "DvinchikCollector | None" = None


def set_action_engine(engine: AutoActionEngine | None) -> None:
    """Stores the AutoActionEngine reference for the web layer."""
    global _engine
    _engine = engine


def set_collector(collector: "DvinchikCollector | None") -> None:
    """Stores the DvinchikCollector reference for web mode switching."""
    global _collector
    _collector = collector


def _loop():
    """Get the event loop from the engine's client."""
    if _engine is None:
        return None
    client = getattr(_engine, "_client", None)
    if client is None:
        # No client attached yet (or fake engine in tests): no way to bridge.
        return None
    return getattr(client, "_loop", None)


def send_reaction_sync(action: str) -> str:
    """Send a reaction (LIKE/DISLIKE) to Leo chat from the auto account.

    Called from the Flask sync thread (web quick_action).  Returns a
    human-readable status string ("SENT", "DISABLED", "ERROR").
    """
    if _engine is None:
        return "DISABLED"

    text = LIKE_TEXT if action == "LIKE" else DISLIKE_TEXT
    loop = _loop()
    if loop is None or loop.is_closed():
        logger.warning("Action bridge: loop unavailable")
        return "DISABLED"

    try:
        future = asyncio.run_coroutine_threadsafe(
            _engine.manual_reaction(text),
            loop,
        )
        result = future.result(timeout=30)
        return "SENT" if result else "DISABLED"
    except Exception as e:
        logger.error(
            f"Action bridge failed: {type(e).__name__}: {e!r} "
            f"(loop.running={loop.is_running()})"
        )
        return "ERROR"


def set_account_sync(session: str) -> str:
    """Switch the auto-action account on the running collector.

    Called from the Flask sync thread.  Updates the running AutoActionEngine
    in place so the change takes effect immediately (no restart needed),
    then kicks the stream on the new account.  Returns a status string
    ("OK", "DISABLED" when no collector/engine attached, "ERROR").
    """
    if _collector is None:
        return "DISABLED"

    try:
        ok = _collector.switch_auto_account(session)
    except Exception as e:
        logger.error(
            f"Account bridge: switch failed: {type(e).__name__}: {e!r}"
        )
        return "ERROR"
    if not ok:
        return "ERROR"

    # Аккаунт сменён; пробуем сразу оживить ленту на новом клиенте.
    loop = _loop()
    if loop is None or loop.is_closed():
        logger.warning("Account bridge: loop unavailable")
        return "OK"
    try:
        future = asyncio.run_coroutine_threadsafe(
            _collector.start_auto_stream(), loop,
        )
        future.result(timeout=30)
    except Exception as e:
        logger.error(
            f"Account bridge: stream kick failed: {type(e).__name__}: {e!r} "
            f"(loop.running={loop.is_running()})"
        )
    return "OK"


def set_mode_sync(mode: "Mode") -> str:
    """Switch the live mode on the running collector and kick the stream.

    Called from the Flask sync thread.  Unlike the persisted-only path
    (settings.py), this updates ``AutoActionEngine`` in place so the switch
    takes effect immediately — no restart needed.  Returns a status string
    ("OK", "DISABLED" when no collector is attached, "ERROR").
    """
    if _collector is None:
        return "DISABLED"

    try:
        _collector.set_mode(mode)
    except Exception as e:
        logger.error(f"Mode bridge: set_mode failed: {type(e).__name__}: {e!r}")
        return "ERROR"

    # Режим переключён; пробуем сразу оживить ленту (SEMI_AUTO/AUTO).
    loop = _loop()
    if loop is None or loop.is_closed():
        logger.warning("Mode bridge: loop unavailable")
        return "OK"
    try:
        future = asyncio.run_coroutine_threadsafe(
            _collector.start_auto_stream(), loop,
        )
        future.result(timeout=30)
    except Exception as e:
        logger.error(
            f"Mode bridge: stream kick failed: {type(e).__name__}: {e!r} "
            f"(loop.running={loop.is_running()})"
        )
    return "OK"
