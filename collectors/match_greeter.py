"""Приветствие девушке после взаимного лайка.

Leo на взаимный лайк присылает «Начинай общаться 👉 [Имя](ссылка)», и
ссылка подставляет текст «Привет! Я с Дайвинчика ✨». Вместо него бот сам
пишет девушке в личку — только стикер из набора (по умолчанию ``Flame``).

Правила:
- только при включённых авто-действиях и режиме из ``match_greeting.modes``;
- один раз на девушку и аккаунт (метка в ``app_state``);
- если переписка с ней уже есть (наше исходящее в её чате) — не пишем;
- ошибки Telegram (нет сущности, приватность и т.п.) не ломают pipeline;
- спам-блок (``PeerFloodError`` — «Too many requests», аккаунту нельзя
  писать незнакомым) и ``FloodWaitError`` — НЕ ошибка девушки: аккаунт
  ставится на паузу (``match_greeting_pause:<session>``), девушка — в очередь
  (``match_greeting_queue:<session>``); после паузы ``retry_queued`` дописывает
  всем. Пока пауза — незнакомым не пишем вовсе (попытки продлевают блок).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from loguru import logger
from telethon.errors import FloodWaitError, PeerFloodError
from telethon.tl.functions.messages import GetStickerSetRequest
from telethon.tl.types import InputStickerSetShortName

from models.raw import ParsedMatch

if TYPE_CHECKING:
    from app.config import MatchGreetingConfig
    from database.database import Database

#: Префикс ключа app_state: ``match_greeting:<session>:<цель>``.
GREETING_STATE_PREFIX: str = "match_greeting"
#: Сколько последних сообщений её чата смотреть, чтобы понять, писали ли мы.
HISTORY_CHECK_LIMIT: int = 20
#: Ключи паузы и очереди при спам-блоке: ``<префикс>:<session>``.
PAUSE_STATE_PREFIX: str = "match_greeting_pause"
QUEUE_STATE_PREFIX: str = "match_greeting_queue"
#: Пауза после спам-блока (PeerFlood не сообщает срок).
SPAM_BLOCK_PAUSE_SEC: int = 24 * 3600
#: Запас к сроку FloodWait.
FLOOD_WAIT_EXTRA_SEC: int = 60
#: Сколько девушек максимум держать в очереди на аккаунт.
QUEUE_MAX: int = 100


def flood_pause_sec(error: BaseException) -> int | None:
    """Сколько ждать, если ошибка — спам-блок/флуд; None — обычная ошибка."""
    if isinstance(error, FloodWaitError):
        return int(getattr(error, "seconds", 0) or 0) + FLOOD_WAIT_EXTRA_SEC
    if isinstance(error, PeerFloodError):
        return SPAM_BLOCK_PAUSE_SEC
    text = str(error)
    if "PEER_FLOOD" in text or "Too many requests" in text:
        return SPAM_BLOCK_PAUSE_SEC
    return None


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
        HAS_CHAT / NO_STICKER / PAUSED / SPAM_BLOCK / ERROR.

        PAUSED — аккаунт на паузе после спам-блока, девушка в очереди.
        SPAM_BLOCK — блок пойман сейчас (пауза поставлена, девушка в очереди);
        по нему коллектор один раз уведомляет владельца.
        """
        if not self.allowed_in(mode):
            return "DISABLED"
        target = self.target_of(match)
        if target is None:
            return "NO_TARGET"
        key = f"{GREETING_STATE_PREFIX}:{account_session}:{target}"
        if await self._get_state(key):
            return "ALREADY"
        if await self.paused_until(account_session) is not None:
            await self._enqueue(account_session, match)
            logger.info(
                f"MatchGreeting: {account_session} на паузе (спам-блок) — "
                f"{match.name} ({target}) в очередь"
            )
            return "PAUSED"
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
            pause = flood_pause_sec(e)
            if pause is not None:
                # Спам-блок аккаунта, а не проблема девушки: не помечаем её,
                # ставим в очередь и не пишем незнакомым до конца паузы.
                await self._set_pause(account_session, pause)
                await self._enqueue(account_session, match)
                logger.warning(
                    f"MatchGreeting: {account_session} в спам-блоке ({e}) — "
                    f"пауза {pause // 60} мин, {match.name} ({target}) в очередь"
                )
                return "SPAM_BLOCK"
            logger.error(
                f"MatchGreeting: не удалось написать {match.name} ({target}): {e}"
            )
            # Не повторяем на каждое сообщение: прочие ошибки отправки
            # (приватность, удалённый аккаунт) почти всегда постоянные.
            await self._mark(key, f"ERROR: {e}", match)
            return "ERROR"
        await self._mark(key, "SENT", match)
        logger.info(f"MatchGreeting: отправил стикер {match.name} ({target}) 🔥")
        return "SENT"

    async def retry_queued(
        self, client: Any, account_session: str, mode: str,
    ) -> list[tuple[ParsedMatch, str]]:
        """После паузы дописывает девушкам из очереди. [(матч, статус)].

        Очередь снимается целиком; кто снова упрётся в блок — вернётся в неё
        сам (``greet`` → SPAM_BLOCK/PAUSED). Режим выключен — очередь ждёт.
        """
        if not self.allowed_in(mode):
            return []
        if await self.paused_until(account_session) is not None:
            return []
        queue = await self._load_queue(account_session)
        if not queue:
            return []
        await self._save_queue(account_session, [])
        results: list[tuple[ParsedMatch, str]] = []
        for item in queue:
            match = ParsedMatch(
                name=str(item.get("name") or ""),
                telegram_username=str(item.get("username") or ""),
                telegram_user_id=int(item.get("user_id") or 0),
            )
            status = await self.greet(client, account_session, match, mode)
            results.append((match, status))
        return results

    async def paused_until(self, account_session: str) -> datetime | None:
        """Срок паузы аккаунта после спам-блока; None — паузы нет."""
        raw = await self._get_state(f"{PAUSE_STATE_PREFIX}:{account_session}")
        if not raw:
            return None
        try:
            until = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until if until > datetime.now(timezone.utc) else None

    async def _set_pause(self, account_session: str, seconds: int) -> None:
        until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        await self._set_state(
            f"{PAUSE_STATE_PREFIX}:{account_session}", until.isoformat()
        )

    async def _load_queue(self, account_session: str) -> list[dict[str, Any]]:
        raw = await self._get_state(f"{QUEUE_STATE_PREFIX}:{account_session}")
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict)]

    async def _save_queue(
        self, account_session: str, queue: list[dict[str, Any]],
    ) -> None:
        await self._set_state(
            f"{QUEUE_STATE_PREFIX}:{account_session}",
            json.dumps(queue[-QUEUE_MAX:], ensure_ascii=False),
        )

    async def _enqueue(self, account_session: str, match: ParsedMatch) -> None:
        target = self.target_of(match)
        queue = await self._load_queue(account_session)
        for item in queue:
            if (item.get("username") or item.get("user_id")) == target:
                return
        queue.append({
            "name": match.name,
            "username": match.telegram_username or "",
            "user_id": match.telegram_user_id,
        })
        await self._save_queue(account_session, queue)

    async def _set_state(self, key: str, value: str) -> None:
        if self._db is None:
            return
        try:
            await self._db.set_state(key, value)
        except Exception as e:
            logger.error(f"MatchGreeting: не записал app_state: {e}")

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
