"""Приветствие девушке после взаимного лайка.

Leo на взаимный лайк присылает «Начинай общаться 👉 [Имя](ссылка)», и
ссылка подставляет текст «Привет! Я с Дайвинчика ✨». Вместо него бот сам
пишет девушке в личку — только стикер из набора (по умолчанию ``Flame``).

Правила:
- только при включённых авто-действиях и режиме из ``match_greeting.modes``;
- один раз на девушку и аккаунт (метка в ``app_state``);
- если переписка с ней уже есть (наше исходящее в её чате) — не пишем;
- ошибки Telegram (нет сущности, PeerFlood и т.п.) не ломают pipeline.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from loguru import logger
from telethon.tl.functions.messages import GetStickerSetRequest
from telethon.tl.types import InputStickerSetShortName

if TYPE_CHECKING:
    from app.config import MatchGreetingConfig
    from database.database import Database
    from models.raw import ParsedMatch

#: Префикс ключа app_state: ``match_greeting:<session>:<цель>``.
GREETING_STATE_PREFIX: str = "match_greeting"
#: Сколько последних сообщений её чата смотреть, чтобы понять, писали ли мы.
HISTORY_CHECK_LIMIT: int = 20


class MatchGreeter:
    """Отправляет стикер-приветствие девушке после взаимного лайка."""

    def __init__(self, config: MatchGreetingConfig, db: Database | None) -> None:
        self._config = config
        self._db = db
        # Стикер кэшируется на клиента: у аккаунтов свои file_reference.
        self._stickers: dict[int, Any] = {}

    @staticmethod
    def target_of(match: ParsedMatch) -> str | int | None:
        """Кому писать: @username или user_id (ссылка tg://user?id=…)."""
        if match.telegram_username:
            return match.telegram_username
        if match.telegram_user_id:
            return match.telegram_user_id
        return None

    def allowed_in(self, mode: str) -> bool:
        return bool(self._config.enabled) and mode in self._config.modes

    async def greet(
        self,
        client: Any,
        account_session: str,
        match: ParsedMatch,
        mode: str,
    ) -> str:
        """Пишет девушке. Статус: SENT / DISABLED / NO_TARGET / ALREADY /
        HAS_CHAT / NO_STICKER / ERROR."""
        if not self.allowed_in(mode):
            return "DISABLED"
        target = self.target_of(match)
        if target is None:
            return "NO_TARGET"
        key = f"{GREETING_STATE_PREFIX}:{account_session}:{target}"
        if await self._get_state(key):
            return "ALREADY"
        try:
            sticker = await self._sticker(client)
        except Exception as e:
            # Набор недоступен (опечатка в имени, сеть) — девушку не помечаем:
            # проблема не в ней, исправленный конфиг сработает на следующей.
            logger.error(
                f"MatchGreeting: не загрузил набор «{self._config.sticker_set}»: {e}"
            )
            return "NO_STICKER"
        if sticker is None:
            return "NO_STICKER"
        try:
            peer = await client.get_input_entity(target)
            if await self._we_already_wrote(client, peer):
                await self._mark(key, "HAS_CHAT", match)
                logger.info(
                    f"MatchGreeting: с {match.name} ({target}) уже есть "
                    "переписка — не пишу"
                )
                return "HAS_CHAT"
            await client.send_file(peer, sticker)
        except Exception as e:
            logger.error(
                f"MatchGreeting: не удалось написать {match.name} ({target}): {e}"
            )
            # Не повторяем на каждое сообщение: ошибка отправки (PeerFlood,
            # приватность) почти всегда постоянная.
            await self._mark(key, f"ERROR: {e}", match)
            return "ERROR"
        await self._mark(key, "SENT", match)
        logger.info(f"MatchGreeting: отправил стикер {match.name} ({target}) 🔥")
        return "SENT"

    async def _we_already_wrote(self, client: Any, peer: Any) -> bool:
        async for msg in client.iter_messages(peer, limit=HISTORY_CHECK_LIMIT):
            if getattr(msg, "out", False):
                return True
        return False

    async def _sticker(self, client: Any) -> Any:
        """Документ стикера из набора (кэш на клиента)."""
        cached = self._stickers.get(id(client))
        if cached is not None:
            return cached
        result = await client(
            GetStickerSetRequest(
                stickerset=InputStickerSetShortName(
                    short_name=self._config.sticker_set
                ),
                hash=0,
            )
        )
        sticker = self.pick_sticker(
            list(getattr(result, "documents", []) or []),
            list(getattr(result, "packs", []) or []),
        )
        if sticker is None:
            logger.error(
                f"MatchGreeting: набор «{self._config.sticker_set}» пуст"
            )
            return None
        self._stickers[id(client)] = sticker
        return sticker

    def pick_sticker(self, documents: list[Any], packs: list[Any]) -> Any:
        """Выбор стикера: явный номер → первый с нужным эмодзи → первый."""
        if not documents:
            return None
        index = self._config.sticker_index
        if 0 <= index < len(documents):
            return documents[index]
        by_id = {getattr(d, "id", None): d for d in documents}
        for emoji in self._config.sticker_emojis:
            for pack in packs:
                if getattr(pack, "emoticon", "") != emoji:
                    continue
                for doc_id in getattr(pack, "documents", []) or []:
                    if doc_id in by_id:
                        return by_id[doc_id]
        return documents[0]

    async def _get_state(self, key: str) -> str | None:
        if self._db is None:
            return None
        try:
            return await self._db.get_state(key)
        except Exception as e:
            logger.error(f"MatchGreeting: не прочитал app_state: {e}")
            return None

    async def _mark(self, key: str, status: str, match: ParsedMatch) -> None:
        if self._db is None:
            return
        try:
            await self._db.set_state(
                key,
                json.dumps(
                    {
                        "status": status,
                        "name": match.name,
                        "at": datetime.now(timezone.utc).isoformat(),
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception as e:
            logger.error(f"MatchGreeting: не записал app_state: {e}")
