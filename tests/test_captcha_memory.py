# Тесты Stage 7.6 «память капч»: таблица captcha_memory + captcha_signature.
# Полностью offline — реальный tmp SQLite, без Telegram.

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from collectors.dvinchik_collector import captcha_signature
from database.database import Database


@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    db = Database(path=tmp_path / "test_captcha_memory.db")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(db.connect())
    yield db  # type: ignore[misc]
    loop.run_until_complete(db.close())


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


CAPTCHA_TEXT = "Бармалей, предлагаю тебе сделку"
SIGNATURE = "бармалей, предлагаю тебе сделку"
BUTTONS = json.dumps(
    ["Меню", "Сообщение ...", "Готово", "Возможно позже"], ensure_ascii=False
)


# ── captcha_signature ─────────────────────────────────────────────

class TestCaptchaSignature:
    def test_lowercases_and_collapses_whitespace(self) -> None:
        assert captcha_signature("Бармалей,  предлагаю \n тебе сделку") == SIGNATURE

    def test_empty_text(self) -> None:
        assert captcha_signature("") == ""
        assert captcha_signature(None) == ""


# ── Database ──────────────────────────────────────────────────────

class TestCaptchaMemoryDB:
    def test_record_pending_and_list(self, tmp_db: Database) -> None:
        ok = run(
            tmp_db.record_pending_captcha(SIGNATURE, CAPTCHA_TEXT, BUTTONS)
        )
        assert ok is True

        pending = run(tmp_db.get_pending_captchas())
        assert len(pending) == 1
        assert pending[0]["signature"] == SIGNATURE
        assert pending[0]["message_text"] == CAPTCHA_TEXT
        assert json.loads(pending[0]["buttons_json"]) == [
            "Меню", "Сообщение ...", "Готово", "Возможно позже",
        ]
        assert pending[0]["answer"] is None
        # Ответ ещё не выучен
        assert run(tmp_db.get_captcha_answer(SIGNATURE)) is None

    def test_pending_refresh_keeps_pending(self, tmp_db: Database) -> None:
        run(tmp_db.record_pending_captcha(SIGNATURE, CAPTCHA_TEXT, BUTTONS))
        # Повторное появление той же капчи с другим текстом — обновляет текст,
        # но строка остаётся pending (answer всё ещё NULL).
        run(tmp_db.record_pending_captcha(SIGNATURE, "Новый текст капчи", "[]"))
        pending = run(tmp_db.get_pending_captchas())
        assert len(pending) == 1
        assert pending[0]["message_text"] == "Новый текст капчи"
        assert pending[0]["answer"] is None

    def test_set_answer_and_solve(self, tmp_db: Database) -> None:
        run(tmp_db.record_pending_captcha(SIGNATURE, CAPTCHA_TEXT, BUTTONS))
        captcha_id = run(tmp_db.get_pending_captchas())[0]["id"]

        assert run(tmp_db.set_captcha_answer(captcha_id, "Пока без Premium")) is True
        assert run(tmp_db.get_captcha_answer(SIGNATURE)) == "Пока без Premium"

        # Из pending исчезла, появилась в known.
        assert run(tmp_db.get_pending_captchas()) == []
        known = run(tmp_db.get_known_captchas())
        assert len(known) == 1
        assert known[0]["answer"] == "Пока без Premium"
        assert known[0]["answered_at"] is not None

    def test_pending_does_not_overwrite_learned_answer(
        self, tmp_db: Database,
    ) -> None:
        run(tmp_db.record_pending_captcha(SIGNATURE, CAPTCHA_TEXT, BUTTONS))
        captcha_id = run(tmp_db.get_pending_captchas())[0]["id"]
        run(tmp_db.set_captcha_answer(captcha_id, "Пока без Premium"))
        # Капча снова попалась — НЕ должны затереть выученный ответ.
        run(tmp_db.record_pending_captcha(SIGNATURE, "Снова капча", "[]"))
        assert run(tmp_db.get_captcha_answer(SIGNATURE)) == "Пока без Premium"
        assert run(tmp_db.get_pending_captchas()) == []

    def test_mark_captcha_used_increments(self, tmp_db: Database) -> None:
        run(tmp_db.record_pending_captcha(SIGNATURE, CAPTCHA_TEXT, BUTTONS))
        captcha_id = run(tmp_db.get_pending_captchas())[0]["id"]
        run(tmp_db.set_captcha_answer(captcha_id, "Пока без Premium"))
        assert run(tmp_db.mark_captcha_used(SIGNATURE)) is True
        known = run(tmp_db.get_known_captchas())
        assert known[0]["used_count"] == 1
        run(tmp_db.mark_captcha_used(SIGNATURE))
        assert run(tmp_db.get_known_captchas())[0]["used_count"] == 2

    def test_get_captcha_by_id(self, tmp_db: Database) -> None:
        run(tmp_db.record_pending_captcha(SIGNATURE, CAPTCHA_TEXT, BUTTONS))
        row = run(tmp_db.get_pending_captchas())[0]
        fetched = run(tmp_db.get_captcha_by_id(row["id"]))
        assert fetched is not None
        assert fetched["signature"] == SIGNATURE
        assert run(tmp_db.get_captcha_by_id(999999)) is None

    def test_delete_captcha(self, tmp_db: Database) -> None:
        run(tmp_db.record_pending_captcha(SIGNATURE, CAPTCHA_TEXT, BUTTONS))
        captcha_id = run(tmp_db.get_pending_captchas())[0]["id"]
        assert run(tmp_db.delete_captcha(captcha_id)) is True
        assert run(tmp_db.get_pending_captchas()) == []
        assert run(tmp_db.get_captcha_answer(SIGNATURE)) is None
        assert run(tmp_db.delete_captcha(captcha_id)) is False

    def test_schema_idempotent_and_legacy_friendly(self, tmp_db: Database) -> None:
        # Второй connect (как перезапуск) не падает и таблица уже есть.
        loop = asyncio.get_event_loop()
        loop.run_until_complete(tmp_db.close())
        loop.run_until_complete(tmp_db.connect())
        run(tmp_db.record_pending_captcha(SIGNATURE, CAPTCHA_TEXT, BUTTONS))
        assert len(run(tmp_db.get_pending_captchas())) == 1

    def test_clear_all_data_includes_captcha_memory(
        self, tmp_db: Database,
    ) -> None:
        run(tmp_db.record_pending_captcha(SIGNATURE, CAPTCHA_TEXT, BUTTONS))
        tables = run(tmp_db.clear_all_data())
        assert "captcha_memory" in tables
        assert run(tmp_db.get_pending_captchas()) == []