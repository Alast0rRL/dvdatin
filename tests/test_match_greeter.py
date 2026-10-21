# Стикер-приветствие девушке после взаимного лайка.

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.config import AppConfig, MatchGreetingConfig
from collectors.dvinchik_collector import DvinchikCollector
from collectors.dvinchik_parser import DvinchikParser
from collectors.match_greeter import MatchGreeter
from collectors.raw_worker import RawTask
from core.types import Mode
from models.raw import MessageType, ParsedMatch
from tests.test_collector import make_config, make_db_mock

LEO = 1234060895
MATCH_URL = (
    "Отлично! Надеюсь хорошо проведете время 🙌\n\nНачинай общаться 👉 "
    "[Роза](https://t.me/Hisochkaaa?text=%D0%9F%D1%80%D0%B8%D0%B2%D0%B5%D1%82)"
)
MATCH_ID = (
    "Отлично! Надеюсь хорошо проведете время 🙌\n\nНачинай общаться 👉 "
    "[А](tg://user?id=8690954703)"
)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def make_sticker_set() -> SimpleNamespace:
    docs = [SimpleNamespace(id=1), SimpleNamespace(id=2), SimpleNamespace(id=3)]
    packs = [
        SimpleNamespace(emoticon="🔥", documents=[2]),
        SimpleNamespace(emoticon="👋", documents=[3]),
    ]
    return SimpleNamespace(documents=docs, packs=packs)


def make_client(history: list | None = None) -> AsyncMock:
    client = AsyncMock()
    client.get_input_entity = AsyncMock(return_value="peer")
    client.send_file = AsyncMock()
    client.side_effect = None
    client.return_value = make_sticker_set()

    async def iter_messages(*args, **kwargs):
        for m in history or []:
            yield m

    client.iter_messages = iter_messages
    return client


def make_db(state: str | None = None) -> MagicMock:
    db = MagicMock()
    db.get_state = AsyncMock(return_value=state)
    db.set_state = AsyncMock()
    return db


class TestMatchParsing:
    def test_username_link_without_default_text(self) -> None:
        parser = DvinchikParser(make_config().filters)
        assert parser.classify(MATCH_URL) is MessageType.MATCH
        m = parser.parse_match(MATCH_URL)
        assert m.name == "Роза" and m.telegram_username == "Hisochkaaa"

    def test_user_id_link_is_a_match(self) -> None:
        """Без @username Leo шлёт tg://user?id=… — это тоже взаимный лайк."""
        parser = DvinchikParser(make_config().filters)
        assert parser.classify(MATCH_ID) is MessageType.MATCH
        m = parser.parse_match(MATCH_ID)
        assert m.telegram_username == "" and m.telegram_user_id == 8690954703


class TestMatchGreeter:
    match = ParsedMatch(name="Роза", telegram_username="Hisochkaaa")

    def test_sends_only_sticker_once(self) -> None:
        db = make_db()
        client = make_client()
        greeter = MatchGreeter(MatchGreetingConfig(), db)
        assert run(greeter.greet(client, "dvai_2", self.match, "SEMI_AUTO")) == "SENT"
        client.get_input_entity.assert_awaited_once_with("Hisochkaaa")
        sent_doc = client.send_file.await_args.args[1]
        assert sent_doc.id == 3  # 👋 приоритетнее 🔥
        client.send_message.assert_not_called()  # никакого текста
        key = db.set_state.await_args.args[0]
        assert key == "match_greeting:dvai_2:Hisochkaaa"
        assert '"SENT"' in db.set_state.await_args.args[1]

    def test_already_greeted_is_skipped(self) -> None:
        client = make_client()
        greeter = MatchGreeter(MatchGreetingConfig(), make_db(state="{}"))
        assert run(greeter.greet(client, "dvai_2", self.match, "SEMI_AUTO")) == "ALREADY"
        client.send_file.assert_not_called()

    def test_existing_chat_is_skipped(self) -> None:
        client = make_client(history=[SimpleNamespace(out=True)])
        greeter = MatchGreeter(MatchGreetingConfig(), make_db())
        assert run(greeter.greet(client, "dvai_2", self.match, "SEMI_AUTO")) == "HAS_CHAT"
        client.send_file.assert_not_called()

    def test_her_message_alone_does_not_block(self) -> None:
        """Она написала первой — наш привет всё равно уходит."""
        client = make_client(history=[SimpleNamespace(out=False)])
        greeter = MatchGreeter(MatchGreetingConfig(), make_db())
        assert run(greeter.greet(client, "dvai_2", self.match, "SEMI_AUTO")) == "SENT"

    def test_observe_never_sends(self) -> None:
        client = make_client()
        greeter = MatchGreeter(MatchGreetingConfig(), make_db())
        assert run(greeter.greet(client, "dvai_2", self.match, "OBSERVE")) == "DISABLED"
        client.send_file.assert_not_called()

    def test_send_error_is_recorded_not_raised(self) -> None:
        db = make_db()
        client = make_client()
        client.send_file = AsyncMock(side_effect=RuntimeError("PEER_FLOOD"))
        greeter = MatchGreeter(MatchGreetingConfig(), db)
        assert run(greeter.greet(client, "dvai_2", self.match, "AUTO")) == "ERROR"
        assert "PEER_FLOOD" in db.set_state.await_args.args[1]

    def test_missing_sticker_set_does_not_mark_girl(self) -> None:
        db = make_db()
        client = make_client()
        client.side_effect = RuntimeError("STICKERSET_INVALID")
        greeter = MatchGreeter(MatchGreetingConfig(), db)
        assert run(greeter.greet(client, "dvai_2", self.match, "AUTO")) == "NO_STICKER"
        client.send_file.assert_not_called()
        db.set_state.assert_not_called()

    def test_user_id_target(self) -> None:
        client = make_client()
        greeter = MatchGreeter(MatchGreetingConfig(), make_db())
        m = ParsedMatch(name="А", telegram_user_id=8690954703)
        assert run(greeter.greet(client, "dvai_2", m, "SEMI_AUTO")) == "SENT"
        client.get_input_entity.assert_awaited_once_with(8690954703)

    def test_pick_sticker_by_index_and_fallback(self) -> None:
        s = make_sticker_set()
        by_index = MatchGreeter(MatchGreetingConfig(sticker_index=0), None)
        assert by_index.pick_sticker(s.documents, s.packs).id == 1
        no_emoji = MatchGreeter(MatchGreetingConfig(sticker_emojis=["🌚"]), None)
        assert no_emoji.pick_sticker(s.documents, s.packs).id == 1
        assert no_emoji.pick_sticker([], []) is None


class TestCollectorGreetsOnMatch:
    def _collector(self, mode: str) -> tuple[DvinchikCollector, AsyncMock, AsyncMock]:
        data = make_config().model_dump()
        data["telegram"] = {
            "accounts": [
                {"api_id": 1, "api_hash": "a" * 32, "session": "dvai"},
                {"api_id": 2, "api_hash": "b" * 32, "session": "dvai_2"},
            ]
        }
        data["project"] = {"mode": mode}
        data["auto_actions"] = {
            "enabled": True, "account_session": "dvai_2", "interval_sec": 0.0,
        }
        other, auto = AsyncMock(), AsyncMock()
        collector = DvinchikCollector([other, auto], make_db_mock(), AppConfig(**data))
        collector._match_greeter.greet = AsyncMock(return_value="SENT")
        collector._auto_engine.notify_owner = AsyncMock(return_value=True)
        return collector, other, auto

    @staticmethod
    def _task(client: AsyncMock) -> RawTask:
        msg = MagicMock()
        msg.client = client
        return RawTask(
            chat_id=LEO, message_id=36339, sender_id=LEO, sender_username="",
            sender_name="", text=MATCH_URL, media_type="", entities_json="[]",
            reply_to=None, received_at="2026-10-21T18:06:27+00:00",
            msg_date="2026-10-21T18:06:27+00:00", msg=msg, raw_id=1,
            account_session="dvai_2",
        )

    def test_match_on_auto_account_greets(self) -> None:
        collector, _, auto = self._collector("SEMI_AUTO")
        run(collector._process_message(self._task(auto)))
        collector._match_greeter.greet.assert_awaited_once()
        args = collector._match_greeter.greet.await_args.args
        assert args[0] is auto and args[1] == "dvai_2" and args[3] == "SEMI_AUTO"
        assert args[2].telegram_username == "Hisochkaaa"
        collector._auto_engine.notify_owner.assert_awaited_once()

    def test_observe_does_not_greet(self) -> None:
        collector, _, auto = self._collector("OBSERVE")
        run(collector._process_message(self._task(auto)))
        collector._match_greeter.greet.assert_not_called()

    def test_other_account_does_not_greet(self) -> None:
        collector, other, _ = self._collector("SEMI_AUTO")
        run(collector._process_message(self._task(other)))
        collector._match_greeter.greet.assert_not_called()

    def test_mode_value_passed(self) -> None:
        collector, _, auto = self._collector("AUTO")
        run(collector._process_message(self._task(auto)))
        assert collector._match_greeter.greet.await_args.args[3] == Mode.AUTO.value
