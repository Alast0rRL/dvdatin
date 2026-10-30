# ProfileService: CRUD + upsert + fingerprint для профилей.

from __future__ import annotations

import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from loguru import logger

from models.profile import Profile, ProfileStatus, compute_fingerprint
from models.raw import ParsedProfile

if TYPE_CHECKING:
    from database.database import Database


# Порог похожести описаний, при котором карточка считается той же анкетой.
# Небольшая правка (смайлик, пара слов) — та же девушка; другое описание при
# тех же «имя, возраст, город» — другая (прод 2026-10-30: №1367 «Мяу, 18»
# склеил анкеты с подписями 24 / 55 / 140 / 406 символов).
DESCRIPTION_MATCH_RATIO = 0.7

_NON_WORD_RE = re.compile(r"[\W_]+", re.UNICODE)


def normalize_description(text: str) -> str:
    """Описание для сравнения: регистр, пунктуация, эмодзи и пробелы не важны."""
    return _NON_WORD_RE.sub(" ", (text or "").casefold()).strip()


def description_similarity(a: str, b: str) -> float:
    """Похожесть двух описаний анкеты, 0..1 (оба пустые — 1.0)."""
    na, nb = normalize_description(a), normalize_description(b)
    if na == nb:
        return 1.0
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb, autojunk=False).ratio()


class ProfileService:
    """Сервис управления профилями."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create_profile(
        self, parsed: ParsedProfile, account_session: str = '',
    ) -> Profile:
        """Создаёт новый профиль из ParsedProfile."""
        now = datetime.now(timezone.utc).isoformat()
        fp = compute_fingerprint(
            parsed.name, parsed.age or 0, parsed.normalized_city,
        )

        profile_id = await self._db.insert_profile(
            name=parsed.name,
            age=parsed.age or 0,
            raw_city=parsed.raw_city,
            normalized_city=parsed.normalized_city,
            description=parsed.description,
            fingerprint=fp,
            source_chat_id=parsed.source_chat_id,
            source_message_id=parsed.source_message_id,
            first_seen_at=now,
            last_seen_at=now,
            status=ProfileStatus.NEW,
        )

        await self._db.link_profile_message(
            profile_id=profile_id,
            telegram_message_id=parsed.source_message_id,
            chat_id=parsed.source_chat_id,
            created_at=now,
            account_session=account_session,
        )

        logger.info(f"Profile created: id={profile_id}, name={parsed.name}")

        return Profile(
            id=profile_id,
            name=parsed.name,
            age=parsed.age or 0,
            raw_city=parsed.raw_city,
            normalized_city=parsed.normalized_city,
            description=parsed.description,
            fingerprint=fp,
            source_chat_id=parsed.source_chat_id,
            source_message_id=parsed.source_message_id,
            first_seen_at=now,
            last_seen_at=now,
            status=ProfileStatus.NEW,
        )

    async def get_profile(self, profile_id: int) -> Profile | None:
        """Получает профиль по ID."""
        row = await self._db.get_profile_by_id(profile_id)
        if row is None:
            return None
        profile = self._row_to_profile(row)
        profile.message_count = await self._db.get_profile_message_count(profile_id)
        return profile

    async def find_profile_by_message(
        self,
        chat_id: int,
        telegram_message_id: int,
        account_session: str | None = None,
    ) -> Profile | None:
        """Ищет профиль по telegram message (при заданной сессии — в её рамках)."""
        row = await self._db.find_profile_by_message(
            chat_id, telegram_message_id, account_session,
        )
        if row is None:
            return None
        return self._row_to_profile(row)

    async def upsert_profile(
        self, parsed: ParsedProfile, account_session: str = '',
    ) -> Profile:
        """Находит существующий профиль или создаёт новый.

        fingerprint (имя + возраст + город) даёт кандидатов, а та же анкета —
        кандидат с похожим описанием (``DESCRIPTION_MATCH_RATIO``). Разные
        девушки с одинаковым «имя, возраст» получают свои profile_id; при
        равной похожести берётся самый свежий кандидат.
        """
        fp = compute_fingerprint(
            parsed.name, parsed.age or 0, parsed.normalized_city,
        )

        existing = await self._find_same_profile(fp, parsed.description)

        if existing:
            return await self._update_existing(
                existing, parsed, fp, account_session=account_session,
            )

        return await self.create_profile(parsed, account_session=account_session)

    async def _find_same_profile(
        self, fingerprint: str, description: str,
    ) -> dict | None:
        """Кандидат с тем же fingerprint и похожим описанием или None."""
        candidates = await self._db.find_profiles_by_fingerprint(fingerprint)
        best: dict | None = None
        best_ratio = 0.0
        for row in candidates:  # свежие первыми: при равенстве побеждает свежий
            ratio = description_similarity(
                description, row.get("description", "") or "",
            )
            if ratio >= DESCRIPTION_MATCH_RATIO and ratio > best_ratio:
                best, best_ratio = row, ratio
                if ratio == 1.0:
                    break
        if best is None and candidates:
            logger.info(
                f"Profile fingerprint shared by {len(candidates)} profile(s), "
                "description differs — new profile"
            )
        return best

    async def _update_existing(
        self,
        existing: dict,
        parsed: ParsedProfile,
        fingerprint: str,
        account_session: str = '',
    ) -> Profile:
        """Обновляет существующий профиль."""
        profile_id = existing["id"]
        now = datetime.now(timezone.utc).isoformat()

        await self._db.update_profile_last_seen(profile_id, now)

        if parsed.description and parsed.description != existing.get("description", ""):
            await self._db.update_profile_description(
                profile_id, parsed.description,
            )
            logger.info(f"Profile {profile_id}: description updated")

        if parsed.raw_city and parsed.raw_city != existing.get("raw_city", ""):
            await self._db.update_profile_raw_city(profile_id, parsed.raw_city)

        await self._db.link_profile_message(
            profile_id=profile_id,
            telegram_message_id=parsed.source_message_id,
            chat_id=parsed.source_chat_id,
            created_at=now,
            account_session=account_session,
        )

        msg_count = await self._db.get_profile_message_count(profile_id)

        logger.info(
            f"Profile {profile_id} updated: "
            f"last_seen={now}, messages={msg_count}"
        )

        row = await self._db.get_profile_by_id(profile_id)
        profile = self._row_to_profile(row)  # type: ignore[arg-type]
        profile.message_count = msg_count
        return profile

    async def link_message_to_profile(
        self,
        profile_id: int,
        telegram_message_id: int,
        chat_id: int,
        account_session: str = '',
    ) -> None:
        """Связывает сообщение с профилем."""
        now = datetime.now(timezone.utc).isoformat()
        await self._db.link_profile_message(
            profile_id=profile_id,
            telegram_message_id=telegram_message_id,
            chat_id=chat_id,
            created_at=now,
            account_session=account_session,
        )
        logger.info(
            f"Linked message {telegram_message_id} to profile {profile_id}"
        )

    @staticmethod
    def _row_to_profile(row: dict) -> Profile:
        """Преобразует dict из БД в Profile."""
        msg_count = row.get("message_count", 0)
        return Profile(
            id=row["id"],
            name=row["name"],
            age=row["age"],
            raw_city=row.get("raw_city", ""),
            normalized_city=row.get("normalized_city", ""),
            description=row.get("description", ""),
            fingerprint=row.get("fingerprint", ""),
            source_chat_id=row.get("source_chat_id", 0),
            source_message_id=row.get("source_message_id", 0),
            first_seen_at=row.get("first_seen_at", ""),
            last_seen_at=row.get("last_seen_at", ""),
            status=row.get("status", "NEW"),
            message_count=msg_count,
        )
