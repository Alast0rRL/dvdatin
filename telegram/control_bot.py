# ControlBot: Telegram UI для управления режимом работы (Stage 7.5).
# Телеграм-панель управления: вкл/выкл авто-действий, статус, запуск потока,
# последние решения. Единственный слой, который дёргает живой коллектор.
#
# Безопасность:
# - Команды принимаются ТОЛЬКО от allowed_user_ids (по умолчанию 8525808108).
# - Любая команда требует знаний: структура бота и chat_id Дайвинчика из конфига.
# - Все операции оборачиваются в try/except — ошибки не роняют app.

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from telethon import Button, events

from core.types import Mode

if TYPE_CHECKING:
    from telethon import TelegramClient

    from app.config import AppConfig
    from database.database import Database
    from collectors.dvinchik_collector import DvinchikCollector

_SEP = "━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Валидные команды-ярлыки.
_CMD_SHORTCUTS = {
    "on": Mode.SEMI_AUTO,
    "off": Mode.OBSERVE,
    "semi": Mode.SEMI_AUTO,
    "observe": Mode.OBSERVE,
}


class ControlBot:
    """Регистрирует команды управления режимом работы."""

    def __init__(
        self,
        client: TelegramClient,
        config: AppConfig,
        collector: DvinchikCollector,
        db: Database,
    ) -> None:
        self._client = client
        self._config = config
        self._collector = collector
        self._db = db
        self._allowed = set(config.control.allowed_user_ids)

    def register(self) -> None:
        """Регистрирует обработчики команд и callback-запросов.

        Используем единый роутер вместо нескольких pattern-хендлеров,
        чтобы избежать конфликтов event-обработчиков на одном клиенте.
        """
        self._client.add_event_handler(self._on_message, events.NewMessage())
        self._client.add_event_handler(self._on_callback, events.CallbackQuery())
        logger.info("ControlBot registered: /status /mode /stream /recent /help")

    # ── Роутер ────────────────────────────────────────────────────────

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        """Единый обработчик входящих сообщений: разбор команды и вызов."""
        if not self._is_authorized(event.sender_id):
            return
        text = (event.message.text or "").strip()
        if not text:
            return
        logger.info(f"ControlBot command from {event.sender_id}: {text!r}")
        parts = text.split()
        cmd = parts[0].lower()
        if cmd == "/status" or cmd == "/start":
            await self._send_status(event)
        elif cmd == "/mode" and len(parts) >= 2:
            await self._cmd_mode(event, parts[1])
        elif cmd == "/stream":
            await self._cmd_stream(event)
        elif cmd == "/recent":
            await self._cmd_recent(event)
        elif cmd == "/help":
            await self._cmd_help(event)

    def _is_authorized(self, sender_id: int | None) -> bool:
        return sender_id in self._allowed

    def _deny(self, event: events.NewMessage.Event) -> None:
        logger.warning(f"ControlBot: отказ команды от user_id={event.sender_id}")

    # ── Команды ──────────────────────────────────────────────────────

    async def _cmd_start(self, event: events.NewMessage.Event) -> None:
        if not self._is_authorized(event.sender_id):
            return
        await self._send_status(event)

    async def _cmd_status(self, event: events.NewMessage.Event) -> None:
        if not self._is_authorized(event.sender_id):
            return
        await self._send_status(event)

    async def _cmd_help(self, event: events.NewMessage.Event) -> None:
        if not self._is_authorized(event.sender_id):
            return
        try:
            text = self._render_help()
            await event.client.send_message(
                event.chat_id,
                text,
                buttons=[[
                    Button.inline("🟢 ON", data=b"control:on"),
                    Button.inline("⭕ OFF", data=b"control:off"),
                ], [
                    Button.inline("📊 Статус", data=b"control:status"),
                    Button.inline("▶ Запустить поток", data=b"control:stream"),
                ]],
            )
        except Exception as e:
            logger.error(f"ControlBot error (/help): {e}")
            await event.respond("Ошибка.")

    async def _cmd_mode(self, event: events.NewMessage.Event, raw: str | None = None) -> None:
        if not self._is_authorized(event.sender_id):
            return
        if raw is None:
            return
        raw = raw.strip().lower()
        try:
            mode = _CMD_SHORTCUTS.get(raw)
            if mode is None:
                mode = Mode(raw.upper())
        except ValueError:
            await event.respond(
                f"Неизвестный режим: {raw}. Доступно: {[m.value for m in Mode]}"
            )
            return
        try:
            self._collector.set_mode(mode)
            await event.client.send_message(
                event.chat_id,
                f"Режим установлен: {mode.value}",
                buttons=self._toggle_buttons(),
            )
        except Exception as e:
            logger.error(f"ControlBot error (/mode): {e}")
            await event.respond("Ошибка смены режима.")

    async def _cmd_stream(self, event: events.NewMessage.Event) -> None:
        if not self._is_authorized(event.sender_id):
            return
        engine = self._collector.auto_engine()
        if engine is None or not engine.enabled:
            await event.respond(
                "Авто-действия выключены — поток не запущен. "
                "Сначала включите (ON)."
            )
            return
        try:
            await self._collector.start_auto_stream()
            await event.respond(
                f"Команда запуска потока отправлена ({engine.mode.value})."
            )
        except Exception as e:
            logger.error(f"ControlBot error (/stream): {e}")
            await event.respond("Ошибка запуска потока.")

    async def _cmd_recent(self, event: events.NewMessage.Event) -> None:
        if not self._is_authorized(event.sender_id):
            return
        try:
            text = await self._render_recent()
            await event.respond(text)
        except Exception as e:
            logger.error(f"ControlBot error (/recent): {e}")
            await event.respond("Ошибка загрузки последних решений.")

    # ── Callback (inline кнопки) ─────────────────────────────────────

    async def _on_callback(self, event: events.CallbackQuery.Event) -> None:
        # Callback-кнопки могут прийти от кого угодно — проверяем id.
        sender = event.sender_id
        if not self._is_authorized(sender):
            await event.answer("Нет доступа", alert=True)
            return
        raw = event.data.decode("utf-8", errors="replace")
        try:
            if not raw.startswith("control:"):
                return
            action = raw.split(":", 1)[1]
            if action == "on":
                self._collector.set_mode(Mode.SEMI_AUTO)
                text = self._render_status()
                await event.edit(text, buttons=self._toggle_buttons())
            elif action == "off":
                self._collector.set_mode(Mode.OBSERVE)
                text = self._render_status()
                await event.edit(text, buttons=self._toggle_buttons())
            elif action == "status":
                text = self._render_status()
                await event.edit(text, buttons=self._toggle_buttons())
            elif action == "stream":
                engine = self._collector.auto_engine()
                if engine is None or not engine.enabled:
                    await event.answer("Авто-действия выключены", alert=True)
                    return
                await self._collector.start_auto_stream()
                await event.answer("Команда потока отправлена")
            elif action == "recent":
                text = await self._render_recent()
                await event.edit(text)
        except Exception as e:
            logger.error(f"ControlBot callback error: {e}")
            try:
                await event.answer("Ошибка обработки", alert=True)
            except Exception:
                pass

    # ── Рендер ───────────────────────────────────────────────────────

    def _toggle_buttons(self) -> list:
        return [
            [Button.inline("🟢 ON  (SEMI_AUTO)", data=b"control:on"),
             Button.inline("⭕ OFF (OBSERVE)", data=b"control:off")],
            [Button.inline("📊 Статус", data=b"control:status"),
             Button.inline("▶ Поток", data=b"control:stream")],
            [Button.inline("🕘 Последние решения", data=b"control:recent")],
        ]

    def _render_help(self) -> str:
        return (
            f"{_SEP}\n"
            "CTG PANEL\n"
            f"{_SEP}\n\n"
            "Команды:\n"
            "/status — текущий режим и статус\n"
            "/mode on|off — SEMI_AUTO / OBSERVE\n"
            "/stream — запустить поток анкет сейчас\n"
            "/recent — последние 5 решений AI\n"
            "/help — эта справка\n\n"
            "Ниже кнопки быстрого управления."
        )

    def _render_status(self) -> str:
        engine = self._collector.auto_engine()
        mode = self._collector.mode.value
        cfg = self._config.auto_actions
        enabled = engine.enabled if engine is not None else False
        client_ok = engine.client is not None if engine is not None else False
        lines = [
            _SEP, "⚙️ CТАТУС", _SEP, "",
            f"Режим:      {mode}",
            f"Действия:   {'🟢 ВКЛ' if enabled else '⭕ ВЫКЛ'}",
            f"Авто-клиент: {'есть' if client_ok else 'НЕТ'}",
            f"Сессия:     {cfg.account_session or '—'}",
            f"Interval:   {cfg.interval_sec} сек",
        ]
        return "\n".join(lines)

    async def _send_status(self, event: events.NewMessage.Event) -> None:
        try:
            text = self._render_status()
            await event.client.send_message(
                event.chat_id, text, buttons=self._toggle_buttons()
            )
        except Exception as e:
            logger.error(f"ControlBot error (/status): {e}")
            await event.respond("Ошибка загрузки статуса.")

    async def _render_recent(self) -> str:
        rows = await self._db.get_all_ai_decisions()
        lines = [_SEP, "🕘 ПОСЛЕДНИЕ РЕШЕНИЯ", _SEP, ""]
        if not rows:
            lines.append("Решений пока нет.")
        for row in rows[-5:]:
            lines.append(
                f"#{row.get('profile_id','')} → {row.get('decision','')} "
                f"(score {row.get('combined_score', 0):.2f}) "
                f"@ {row.get('evaluated_at','')[:19]}"
            )
        lines.append("")
        lines.append(_SEP)
        return "\n".join(lines)
