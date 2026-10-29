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


def _current_engine() -> "AutoActionEngine | None":
    """Движок основного аккаунта: с коллектора (если есть), иначе заданный."""
    if _collector is not None:
        getter = getattr(_collector, "auto_engine", None)
        if callable(getter):
            try:
                engine = getter()
            except Exception:
                engine = None
            if engine is not None:
                return engine
    return _engine


def _loop():
    """Event loop Telethon-клиентов (движка или любого аккаунта коллектора)."""
    clients = []
    engine = _current_engine()
    if engine is not None:
        clients.append(getattr(engine, "_client", None))
    if _collector is not None:
        clients.extend(getattr(_collector, "_clients", None) or [])
    for client in clients:
        loop = getattr(client, "_loop", None) if client is not None else None
        if isinstance(loop, asyncio.AbstractEventLoop):
            return loop
    return None


def _with_session(func, *args, session: str | None = None):
    """Вызов метода коллектора с аккаунтом только если он задан.

    Старые вызовы (и фейки в тестах) — без аргумента ``session``.
    """
    if session:
        return func(*args, session)
    return func(*args)


def send_reaction_sync(action: str) -> str:
    """Send a reaction (LIKE/DISLIKE) to Leo chat from the auto account.

    Called from the Flask sync thread (web quick_action).  Returns a
    human-readable status string ("SENT", "DISABLED", "ERROR").
    """
    engine = _current_engine()
    if engine is None:
        return "DISABLED"

    text = LIKE_TEXT if action == "LIKE" else DISLIKE_TEXT
    loop = _loop()
    if loop is None or loop.is_closed():
        logger.warning("Action bridge: loop unavailable")
        return "DISABLED"

    try:
        future = asyncio.run_coroutine_threadsafe(
            engine.manual_reaction(text),
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


def send_captcha_answer_sync(answer: str) -> str:
    """Send the owner's captcha answer to Leo chat from the auto account.

    Called from the Flask sync thread (captcha block in the app feed).  ``send_text`` is
    not rate-limited (same as pressing a reply button), so the stuck Leo
    stream resumes immediately and the next identical captcha is answered
    automatically from captcha_memory.  Returns "SENT"/"DISABLED"/"ERROR".
    """
    engine = _current_engine()
    if engine is None or not engine.enabled:
        return "DISABLED"

    text = (answer or "").strip()
    if not text:
        return "DISABLED"

    loop = _loop()
    if loop is None or loop.is_closed():
        logger.warning("Captcha bridge: loop unavailable")
        return "DISABLED"

    try:
        future = asyncio.run_coroutine_threadsafe(
            engine.send_text(text),
            loop,
        )
        result = future.result(timeout=30)
        return "SENT" if result else "DISABLED"
    except Exception as e:
        logger.error(
            f"Captcha bridge failed: {type(e).__name__}: {e!r} "
            f"(loop.running={loop.is_running()})"
        )
        return "ERROR"


def set_account_sync(session: str) -> str:
    """Делает аккаунт основным (лента сайта, ручные кнопки) на живом коллекторе.

    Режимы аккаунтов не меняются (у каждого свой движок); ленту основного
    аккаунта на всякий случай подталкиваем. Статус: "OK", "DISABLED", "ERROR".
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
            _with_session(_collector.start_auto_stream, session=session), loop,
        )
        future.result(timeout=30)
    except Exception as e:
        logger.error(
            f"Account bridge: stream kick failed: {type(e).__name__}: {e!r} "
            f"(loop.running={loop.is_running()})"
        )
    return "OK"


def mode_block_reason_sync(mode: "Mode", session: str | None = None) -> str | None:
    """Причина, по которой нельзя включить режим (лимит ❤️), иначе None.

    Вызывается ДО сохранения режима в config.yaml. Без коллектора/цикла
    (бот не запущен, тесты) проверить нечем — разрешаем.
    """
    if _collector is None:
        return None
    loop = _loop()
    if loop is None or loop.is_closed():
        return None
    try:
        future = asyncio.run_coroutine_threadsafe(
            _with_session(_collector.mode_block_reason, mode, session=session), loop,
        )
        return future.result(timeout=30)
    except Exception as e:
        logger.error(f"Mode bridge: limit check failed: {type(e).__name__}: {e!r}")
        return None


def set_mode_sync(mode: "Mode", session: str | None = None) -> str:
    """Switch the live mode of one account (None — main) and kick its stream.

    Called from the Flask sync thread.  Unlike the persisted-only path
    (settings.py), this updates ``AutoActionEngine`` in place so the switch
    takes effect immediately — no restart needed.  Returns a status string
    ("OK", "DISABLED" when no collector is attached, "ERROR").
    """
    if _collector is None:
        return "DISABLED"

    try:
        _with_session(_collector.set_mode, mode, session=session)
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
            _with_session(_collector.start_auto_stream, session=session), loop,
        )
        future.result(timeout=30)
    except Exception as e:
        logger.error(
            f"Mode bridge: stream kick failed: {type(e).__name__}: {e!r} "
            f"(loop.running={loop.is_running()})"
        )
    return "OK"


def set_controls_sync(session: str | None, scroll: bool, auto: bool) -> str:
    """«Скроллинг»/«Авто» аккаунта на живом коллекторе (+ толчок ленты).

    Статус: "OK", "DISABLED" (нет коллектора), "ERROR".
    """
    if _collector is None:
        return "DISABLED"
    try:
        _collector.set_controls(session, scroll=scroll, auto=auto)
    except Exception as e:
        logger.error(f"Controls bridge: set_controls failed: {type(e).__name__}: {e!r}")
        return "ERROR"
    if not scroll:
        return "OK"
    loop = _loop()
    if loop is None or loop.is_closed():
        logger.warning("Controls bridge: loop unavailable")
        return "OK"
    try:
        future = asyncio.run_coroutine_threadsafe(
            _with_session(_collector.start_auto_stream, session=session), loop,
        )
        future.result(timeout=30)
    except Exception as e:
        logger.error(
            f"Controls bridge: stream kick failed: {type(e).__name__}: {e!r} "
            f"(loop.running={loop.is_running()})"
        )
    return "OK"


def resume_search_sync(session: str | None = None) -> str:
    """Кнопка «Продолжить» в баннере остановки поиска (карточка лайка).

    Возвращает режим до остановки и продолжает ленту
    (``DvinchikCollector.resume_after_match_stop``). Статус: "OK",
    "DISABLED" (нет коллектора/цикла), "ERROR".
    """
    if _collector is None:
        return "DISABLED"
    loop = _loop()
    if loop is None or loop.is_closed():
        logger.warning("Resume bridge: loop unavailable")
        return "DISABLED"
    try:
        future = asyncio.run_coroutine_threadsafe(
            _with_session(_collector.resume_after_match_stop, session=session),
            loop,
        )
        future.result(timeout=30)
    except Exception as e:
        logger.error(f"Resume bridge: {type(e).__name__}: {e!r}")
        return "ERROR"
    return "OK"
