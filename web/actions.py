# Action bridge — send reactions (👍/❤️) from Web UI to Telegram (Leo chat).
#
# When the owner clicks LIKE/DISLIKE in the web dashboard, the reaction
# must actually be sent to the Leo chat via the auto account so that Leo
# advances to the next profile.  This module bridges the Flask sync thread
# to the async event loop via ``run_coroutine_threadsafe`` — same pattern
# as ``web/photos.py``.

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from collectors.auto_action import AutoActionEngine

#: React constants matching auto_action.py.
LIKE_TEXT: str = "\u2764\ufe0f"
DISLIKE_TEXT: str = "\U0001F44E"

#: Reference to the auto-action engine (set from main.py).
_engine: AutoActionEngine | None = None


def set_action_engine(engine: AutoActionEngine | None) -> None:
    """Stores the AutoActionEngine reference for the web layer."""
    global _engine
    _engine = engine


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
