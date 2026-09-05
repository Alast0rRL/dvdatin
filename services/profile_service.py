# ProfileService: CRUD + upsert + fingerprint для профилей.

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger

from models.profile import Profile, ProfileStatus, compute_fingerprint
from models.raw import ParsedProfile

if TYPE_CHECKING:
    from database.database import Database


class ProfileService:
    """Сервис управления профилями."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create_profile(
        self, parsed: ParsedProfile,
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
        self, chat_id: int, telegram_message_id: int,
    ) -> Profile | None:
        """Ищет профиль по telegram message."""
        row = await self._db.find_profile_by_message(
            chat_id, telegram_message_id,
        )
        if row is None:
            return None
        return self._row_to_profile(row)

    async def upsert_profile(self, parsed: ParsedProfile) -> Profile:
        """Находит существующий профиль или создаёт новый.

        Стратегия поиска: fingerprint (normalized_name + age + city).
        """
        fp = compute_fingerprint(
            parsed.name, parsed.age or 0, parsed.normalized_city,
        )

        existing = await self._db.find_profile_by_fingerprint(fp)

        if existing:
            return await self._update_existing(existing, parsed, fp)

        return await self.create_profile(parsed)

    async def _update_existing(
        self,
        existing: dict,
        parsed: ParsedProfile,
        fingerprint: str,
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
    ) -> None:
        """Связывает сообщение с профилем."""
        now = datetime.now(timezone.utc).isoformat()
        await self._db.link_profile_message(
            profile_id=profile_id,
            telegram_message_id=telegram_message_id,
            chat_id=chat_id,
            created_at=now,
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
