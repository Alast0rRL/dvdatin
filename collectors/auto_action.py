# Авто-действия (Stage 7, SEMI_AUTO): отправка ❤️/👎 на анкеты.
# Telegram-bound слой — работает только здесь (имеет доступ к клиентам).
# Решение (LIKE/DISLIKE/REVIEW) принимает DecisionService; этот модуль лишь
# выполняет действие (отправляет текст ❤️/👎) от имени настроенного аккаунта.

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING

from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from core.types import Mode
from models.decision import AIDecision

if TYPE_CHECKING:
    from telethon import TelegramClient

    from app.config import AutoActionsConfig
    from database.database import Database

console = Console(force_terminal=True)

# Текст команд Дайвинчика (реверс механики):
LIKE_TEXT: str = "\u2764\ufe0f"      # ❤️ — лайк
DISLIKE_TEXT: str = "\U0001F44E"     # 👎 — дизлайк


class AutoActionError(Exception):
    """Ошибка выполнения авто-действия."""


class AutoActionEngine:
    """Выполняет действия (лайк/дизлайк) на анкетах от имени аккаунта.

    Гарантии:
    - Работает ТОЛЬКО если ``mode >= SEMI_AUTO`` и ``enabled`` в конфиге.
    - Интервальный rate-limiter по ``interval_sec`` (по умолчанию 10 сек = 6/мин),
      без жёсткого дневного лимита.
    - Ошибки отправки не роняют pipeline: перехватываются и логируются,
      RAW-сообщения не теряются.
    - REVIEW → 👎 (двигаем ленту Leo): Leo не продолжает поток, пока на
      показанную анкету не отправлена реакция. Чтобы лента не замирала,
      неоднозначные REVIEW-анкеты получают 👎 (действие DISLIKE), при этом
      сам AI-результат и сам профиль остаются в БД для ReviewBot.
    """

    def __init__(
        self,
        client: TelegramClient | None,
        config: AutoActionsConfig,
        mode: Mode,
        chat_id: int,
        notify_client: TelegramClient | None = None,
        db: Database | None = None,
    ) -> None:
        self._client = client
        self._config = config
        self._mode = mode
        self._chat_id = chat_id
        self._notify_chat_id = config.notify_chat_id
        # БД нужна для пересылки ВСЕХ фото анкеты (PROFILE + связанные
        # MEDIA_ONLY) владельцу, а не только одного PROFILE-сообщения.
        self._db = db
        # Клиент «другого» аккаунта для авто-уведомлений (0 в конфиге): движок
        # сам определяет его user_id и шлёт уведомления туда. Так пересылка
        # идёт с Бармалея на Меланхолика и обратно, независимо от того, какой
        # аккаунт — авто.
        self._notify_client = notify_client
        self._notify_user_id: int | None = None
        self._last_action_at: float = 0.0
        self._actions: list[float] = []
        self._lock = asyncio.Lock()

    @property
    def mode(self) -> Mode:
        """Режим работы движка (OBSERVE/SEMI_AUTO/AUTO)."""
        return self._mode

    @mode.setter
    def mode(self, value: Mode) -> None:
        """Динамически меняет режим на лету (управление через ControlBot)."""
        self._mode = value

    @property
    def enabled(self) -> bool:
        """Гейт: авто-действия активны только при SEMI_AUTO/AUTO + enabled."""
        if self._mode not in (Mode.SEMI_AUTO, Mode.AUTO):
            return False
        if not self._config.enabled:
            return False
        if self._client is None:
            return False
        return True

    @property
    def client(self) -> TelegramClient | None:
        """Клиент, от имени которого выполняются действия."""
        return self._client

    @property
    def mode_label(self) -> str:
        return self._mode.value

    async def maybe_act(
        self,
        decision: AIDecision | None,
        profile_id: int | None = None,
        *,
        message_id: int | None = None,
        reasons: list[str] | None = None,
        card_text: str | None = None,
    ) -> str:
        """Выполняет действие по решению (LIKE/DISLIKE), если он enabled.

        Возвращает строку-описание совершённого действия ("SKIP", "LIKE",
        "DISLIKE") либо слово "GATE" если авто-действия выключены.

        ``card_text`` — сырой текст ТЕКУЩЕЙ карточки (сообщения). Используется
        в уведомлении: текст-свидетель из причин цитируется только если он
        реально присутствует в показанной карточке (иначе это вводит в
        заблуждение — причину мог найти прошлый повторный показ с описанием,
        а сейчас карточка усечённая).

        Идемпотентность по ``telegram_message_id`` обеспечивается на уровне
        collector'а (``has_auto_action_for_message`` в БД), а не движка.
        """
        if not self.enabled:
            return "GATE"
        if decision is None:
            return "SKIP"
        if decision == AIDecision.REVIEW:
            # REVIEW → НЕ действуем сами: бот «не справляется», ждём решение
            # владельца. Анкета остаётся активной, владельцу отправляется
            # уведомление, что нужно его ручное действие (❤️/👎/сообщение) в
            # Дайвинчике. Пойманное действие фиксируется в журнале ручных
            # решений. Возврат "REVIEW" — сигнал, что действие не отправлено.
            logger.info(
                "AutoAction: REVIEW → жду ручное решение владельца "
                "(авто-👎 не отправляю, анкета остаётся активной)"
            )
            await self._notify_needs_action(
                profile_id=profile_id,
                message_id=message_id,
                reasons=reasons,
                card_text=card_text,
            )
            return "REVIEW"

        text = LIKE_TEXT if decision == AIDecision.LIKE else DISLIKE_TEXT
        action = decision.value
        notify_action = action

        async with self._lock:
            await self._rate_limit_locked()
            await self._send(text)
        logger.info(f"AutoAction: отправил {text!r} ({action}) на chat={self._chat_id}")
        self._print_action(action, text)
        await self._notify(
            notify_action,
            profile_id=profile_id,
            message_id=message_id,
            reasons=reasons,
            card_text=card_text,
        )
        return action

    async def start_stream(self) -> bool:
        """Отправляет явно настроенную команду открытия потока, если она есть."""
        if not self.enabled:
            return False
        if not self._config.start_command:
            logger.warning(
                "AutoAction: автозапуск потока отключён — команда не задана"
            )
            return False
        await self._send(self._config.start_command)
        logger.info(f"AutoAction: запущен поток анкет ({self._config.start_command!r})")
        return True

    async def send_text(self, text: str) -> bool:
        """Отправляет произвольный текст в чат (нажатие reply-кнопки Leo).

        Используется для кнопки «🚀 Смотреть анкеты»: Leo при исчерпании ленты
        шлёт промо-сообщение с кнопкой, нажатие продолжает поток. Нажатие
        reply-кнопки = отправка её текста обычным сообщением.
        """
        if not self.enabled:
            return False
        await self._send(text)
        logger.info(f"AutoAction: нажата кнопка {text!r} на chat={self._chat_id}")
        return True

    async def _send(self, text: str) -> None:
        """Отправляет текст в чат от имени сконфигурированного клиента."""
        if self._client is None:
            msg = "AutoAction: клиент не задан"
            logger.warning(msg)
            raise AutoActionError(msg)
        try:
            await self._client.send_message(self._chat_id, text)
        except Exception as e:
            logger.error(f"AutoAction: ошибка отправки {text!r}: {e}")
            raise AutoActionError(str(e)) from e

    async def _rate_limit_locked(self) -> None:
        """Применяет rate-limit при уже взятой блокировке движка."""
        now = time.time()
        if self._last_action_at:
            elapsed = now - self._last_action_at
            if elapsed < self._config.interval_sec:
                wait = self._config.interval_sec - elapsed
                logger.info(f"AutoAction: rate-limit, жду {wait:.1f} сек")
                await asyncio.sleep(wait)
        self._last_action_at = time.time()
        self._actions.append(self._last_action_at)

    def _print_action(self, action: str, text: str) -> None:
        color = "green" if action == "LIKE" else "red"
        table = Table(show_header=False, border_style="dim", padding=(0, 1))
        table.add_column("Key", style="bold magenta", width=14)
        table.add_column("Value")
        table.add_row("Chat ID", str(self._chat_id))
        table.add_row("Action", f"[bold {color}]{action}[/bold {color}]")
        table.add_row("Sent", repr(text))
        table.add_row("Mode", self.mode_label)
        console.print(Panel(
            table,
            title=f"[bold {color}]AUTO ACTION[/]",
            border_style=color,
        ))

    async def _notify(
        self,
        action: str,
        profile_id: int | None = None,
        message_id: int | None = None,
        reasons: list[str] | None = None,
        card_text: str | None = None,
    ) -> None:
        """Пересылает карточку анкеты владельцу с объяснением причины.

        Получатель: если ``notify_chat_id > 0`` — явный user_id; если 0 —
        авто-режим: user_id «другого» аккаунта (``notify_client``), чтобы
        уведомления шли с Бармалея на Меланхолика и обратно.
        Пересылаются ВСЕ сообщения анкеты (PROFILE + связанные MEDIA_ONLY
        фото через ``_profile_message_ids``), а не только одно.
        Ошибки пересылки не ломают основной пайплайн.
        """
        if self._client is None or message_id is None:
            return
        target = await self._resolve_notify_target()
        if target is None:
            return
        try:
            ids = await self._profile_message_ids(profile_id, message_id)
            await self._client.forward_messages(
                target, ids, from_peer=self._chat_id,
            )
            text = self._format_reason(action, reasons, card_text=card_text)
            if text:
                await self._client.send_message(target, text)
            logger.info(
                f"AutoAction: уведомление отправлено в chat={target}"
            )
        except Exception as e:
            logger.error(f"AutoAction: ошибка уведомления: {e}")

    async def _notify_needs_action(
        self,
        profile_id: int | None = None,
        message_id: int | None = None,
        reasons: list[str] | None = None,
        card_text: str | None = None,
    ) -> None:
        """Уведомляет владельца, что нужно его РУЧНОЕ решение по REVIEW-анкете.

        REVIEW → бот не действует сам; владельцу пересылается карточка анкеты
        (все фото + текст) и сообщение, что нужно поставить лайк/дизлайк (или
        написать) в Дайвинчике с того же аккаунта. Пересылаем даже без причины
        — главное, что владельцу нужно действие.
        """
        if self._client is None or message_id is None:
            return
        target = await self._resolve_notify_target()
        if target is None:
            return
        try:
            ids = await self._profile_message_ids(profile_id, message_id)
            await self._client.forward_messages(
                target, ids, from_peer=self._chat_id,
            )
            lines = ["⚠️ Нужно твоё решение (REVIEW)", ""]
            if reasons:
                reason_text = self._format_reason("REVIEW", reasons, card_text=card_text)
                if reason_text:
                    lines.append(reason_text)
                    lines.append("")
            lines.append(
                "Зайди в Дайвинчик и поставь лайк (❤️), дизлайк (👎) "
                "или напиши сообщение этой анкете — я запишу твой выбор."
            )
            await self._client.send_message(target, "\n".join(lines))
            logger.info(
                f"AutoAction: уведомление «нужно действие» отправлено в chat={target}"
            )
        except Exception as e:
            logger.error(f"AutoAction: ошибка уведомления «нужно действие»: {e}")

    async def _resolve_notify_target(self) -> int | None:
        """Определяет chat_id получателя уведомления.

        Приоритет:
        1. ``notify_chat_id > 0`` — явный user_id из конфига.
        2. ``notify_client`` — другой аккаунт: берём его ``get_me().id``
           (кешируем, чтобы не дёргать сеть на каждое действие).
        """
        if self._notify_chat_id:
            return self._notify_chat_id
        if self._notify_user_id is not None:
            return self._notify_user_id
        if self._notify_client is None:
            return None
        try:
            me = await self._notify_client.get_me()
            if me is not None and getattr(me, "id", None):
                self._notify_user_id = me.id
                return self._notify_user_id
        except Exception as e:
            logger.error(f"AutoAction: не удалось определить user_id получателя: {e}")
        return None

    async def _profile_message_ids(
        self,
        profile_id: int | None,
        fallback: int | None,
    ) -> list[int] | int:
        """Возвращает список telegram_message_id анкеты для пересылки.

        Профиль приходит от Дайвинчика как минимум одним PROFILE-сообщением,
        а дополнительные фото — отдельными MEDIA_ONLY-сообщениями, привязанными
        к профилю через ``profile_messages``. Чтобы владелец увидел ВСЕ фото
        (1–3), а не только одно, пересылаем все связанные сообщения.

        Если по profile_id нельзя получить доп. сообщения (нет БД / нет
        профиля / связан только сам fallback) — возвращаем fallback как есть,
        чтобы сохранить прежнее поведение.
        """
        if profile_id is None or self._db is None:
            return fallback if fallback is not None else []
        try:
            rows = await self._db.get_profile_messages(profile_id)
        except Exception as e:
            logger.error(f"AutoAction: не удалось получить сообщения профиля: {e}")
            return fallback if fallback is not None else []
        if not rows:
            return fallback if fallback is not None else []
        msg_ids = [
            row["telegram_message_id"]
            for row in rows
            if row.get("telegram_message_id") is not None
        ]
        # Помимо связанных сообщений из БД всегда включаем сам PROFILE-месседж
        # (fallback), если его там вдруг нет — чтобы карточка не потерялась.
        if fallback is not None and fallback not in msg_ids:
            msg_ids.append(fallback)
        if len(msg_ids) <= 1:
            return msg_ids[0] if msg_ids else (fallback if fallback is not None else [])
        return msg_ids

    @staticmethod
    def _format_reason(
        action: str,
        reasons: list[str] | None,
        card_text: str | None = None,
    ) -> str:
        """Формирует человеко-читаемое объяснение причины лайка/дизлайка.

        Поддерживает как старый формат (HARD_NEGATIVE:..., POSITIVE:...),
        так и legacy формат (USER_SKIP:..., FILTER_REJECTED и т.д.).
        Коды решений пропускаются — показываются только смысловые причины.

        ``card_text`` — сырой текст текущей карточки (необязательно). Если
        передан, текст-свидетель для HARD_NEGATIVE/POSITIVE цитируется ТОЛЬКО
        когда он реально присутствует в показанной карточке. Иначе цитата
        опускается (остаётся только смысловой ярлык) — иначе уведомление
        цитирует текст из старого повторного показа, которого в текущей
        усечённой карточке нет.
        """
        if not reasons:
            return ""

        _FILTER_LABELS: dict[str, str] = {
            "AGE_OUT_OF_RANGE": "Возраст не подходит",
            "CITY_OUT_OF_RANGE": "Город не в списке",
            "AGE_UNKNOWN": "Возраст неизвестен",
            "CITY_UNKNOWN": "Город неизвестен",
            "INSUFFICIENT_DATA": "Недостаточно данных",
        }

        _DECISION_CODES: set[str] = {
            "LIKE_THRESHOLD", "LOW_CONFIDENCE", "REVIEW_THRESHOLD",
            "BELOW_THRESHOLDS", "FILTER_REJECTED", "FILTER_REVIEW",
            "USER_SKIP", "USER_LIKE", "AI_UNAVAILABLE",
        }

        # Review-коды скоринга, которые показываем человеко-читаемо (а не
        # вырезаем как внутренние). Stage 8: частое REVIEW из-за отсутствия
        # описания → «Мало информации в анкете».
        _REVIEW_DATA_LABELS: dict[str, str] = {
            "NO_FEATURES_FOUND": "Мало информации в анкете",
            "INSUFFICIENT_DATA": "Недостаточно данных",
        }

        _NEGATIVE_LABELS: dict[str, str] = {
            "not_relationships": "Не ищет отношения",
            "has_boyfriend": "Есть парень",
            "smoking": "Курит",
            "alcohol": "Пьёт",
            "bad_habits": "Вредные привычки",
            "pokatayte": "Покатайте/прокат",
            "short_hair": "Волосы короче каре",
            "instagram": "Instagram",
            "plus_size": "+size",
            "age_mismatch": "Возраст в анкете не совпадает",
        }

        _POSITIVE_LABELS: dict[str, str] = {
            "spbpu": "СПбПУ",
            "anime": "Аниме",
            "games": "Игры",
            "relocated_to_spb": "Переехала в СПб",
        }

        if action == "REVIEW":
            label = "👎 На ревью"
        elif action == "LIKE":
            label = "❤️ Лайк"
        else:
            label = "👎 Дизлайк"
        lines = [label]

        for reason in reasons:
            if reason in _DECISION_CODES:
                continue
            if reason.startswith("USER_SKIP:"):
                tag = reason.split(":", 1)[1]
                lines.append(f"• Ваша стоп-метка: {tag}")
            elif reason.startswith("USER_LIKE:"):
                tag = reason.split(":", 1)[1]
                lines.append(f"• Ваша метка интереса: {tag}")
            elif reason.startswith("HARD_NEGATIVE:"):
                parts = reason.split(":", 2)
                name = parts[1] if len(parts) > 1 else ""
                evidence = parts[2].strip("«»") if len(parts) > 2 else ""
                label_text = _NEGATIVE_LABELS.get(name, name)
                if evidence and AutoActionEngine._evidence_in_card(
                    evidence, card_text
                ):
                    lines.append(f"• {label_text}: {evidence}")
                else:
                    lines.append(f"• {label_text}")
            elif reason.startswith("POSITIVE:"):
                parts = reason.split(":", 2)
                name = parts[1] if len(parts) > 1 else ""
                evidence = parts[2].strip("«»") if len(parts) > 2 else ""
                label_text = _POSITIVE_LABELS.get(name, name)
                if evidence and AutoActionEngine._evidence_in_card(
                    evidence, card_text
                ):
                    lines.append(f"• {label_text}: {evidence}")
                else:
                    lines.append(f"• {label_text}")
            elif reason in _REVIEW_DATA_LABELS:
                lines.append(f"• {_REVIEW_DATA_LABELS[reason]}")
            elif reason in _FILTER_LABELS:
                lines.append(f"• {_FILTER_LABELS[reason]}")
            else:
                lines.append(f"• {reason}")

        return "\n".join(lines)

    @staticmethod
    def _evidence_in_card(evidence: str, card_text: str | None) -> bool:
        """Проверяет, что текст-свидетель реально присутствует в карточке.

        Если ``card_text`` не передан — считаем, что цитату показывать можно
        (обратная совместимость: раньше evidence всегда цитировался).

        Иначе вырезаем из карточки шапку «Имя, Возраст, Город» (по разделителю
        ``–``/``—``) и смотрим, что осталось — описание. Цитируем только если
        описание карточки пересекается с evidence (нормализованно). Это чинит
        случай повторного показа усечённой карточки (только заголовок, без
        описания): тогда evidence из старого полного описания цитировать нельзя.
        """
        if card_text is None:
            return True
        if not evidence:
            return False
        # Шапка «... – описание»: описание — всё после первого –/—.
        header = re.split(r"\s*[–—]\s*", card_text, maxsplit=1)
        card_desc = header[1] if len(header) > 1 else ""
        if not card_desc:
            return False
        norm_ev = "".join(evidence.lower().split())
        norm_card = "".join(card_desc.lower().split())
        # Сравниваем по пересечению (evidence часто включает шапку целиком).
        return norm_ev in norm_card or (
            len(norm_ev) >= 6 and
            any(norm_ev[i:i + 6] in norm_card for i in range(0, max(len(norm_ev) - 5, 1), 3))
        )
