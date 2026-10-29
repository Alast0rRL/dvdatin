# Аккаунты коллектора: свой AutoActionEngine на аккаунт, переключатели «Скроллинг»/«Авто», контекст аккаунта.
# Миксин DvinchikCollector (вынесен из collectors/dvinchik_collector.py без изменения логики).

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import TYPE_CHECKING, Iterator

from loguru import logger

from app.config import AccountControls
from collectors.auto_action import AutoActionEngine
from collectors.leo_markers import _CURRENT_SESSION
from core.types import Mode

if TYPE_CHECKING:
    from telethon import TelegramClient


class AccountEnginesMixin:
    """Движки авто-действий и переключатели аккаунтов (миксин DvinchikCollector)."""

    @property
    def _auto_engine(self) -> AutoActionEngine:
        """Движок аккаунта, от имени которого идёт обработка (см. ``_CURRENT_SESSION``)."""
        session = _CURRENT_SESSION.get()
        if session is not None and session in self._engines:
            return self._engines[session]
        return self._engines[self._primary_session]

    @_auto_engine.setter
    def _auto_engine(self, engine: AutoActionEngine) -> None:
        """Подменяет движок (тесты): он становится основным и движком своего клиента."""
        session = self._session_for_client(getattr(engine, "client", None))
        key = session or self._primary_session
        self._engines[key] = engine
        self._primary_session = key

    @contextlib.contextmanager
    def _session_scope(self, session: str | None) -> Iterator[None]:
        """Обработка от имени аккаунта ``session`` (его движок, его режим)."""
        token = _CURRENT_SESSION.set(session or None)
        try:
            yield
        finally:
            _CURRENT_SESSION.reset(token)

    def _bind_client(self, client: object) -> None:
        """Привязывает текущую задачу к аккаунту клиента (вход хендлера)."""
        _CURRENT_SESSION.set(self._session_for_client(client) or None)

    def _current_session(self) -> str:
        """Сессия аккаунта, от имени которого идёт обработка."""
        session = _CURRENT_SESSION.get()
        if session is not None and session in self._engines:
            return session
        return self._primary_session

    def _resolve_session(self, session: str | None) -> str:
        """None/неизвестный → основной аккаунт."""
        if session and session in self._engines:
            return session
        return self._primary_session

    @property
    def primary_session(self) -> str:
        """Основной аккаунт (лента сайта, ручные кнопки, ControlBot)."""
        return self._primary_session

    def account_sessions(self) -> list[str]:
        """Аккаунты, у которых есть движок (вошедшие + основной)."""
        return [s for s in self._engines if s]

    @property
    def _mode_label(self) -> str:
        """Дружелюбная строка текущего режима для вывода решений."""
        eng = self._auto_engine
        mode = eng.mode.value if eng is not None else self._config.project.mode.value
        session = eng.account_session if eng is not None else ""
        prefix = f"{session}: " if session and len(self._engines) > 1 else ""
        if eng is None or not eng.enabled:
            return f"{prefix}{mode} (действий нет)"
        return f"{prefix}{mode} (авто-действия ❤️/👎 активны)"

    @property
    def mode(self) -> Mode:
        """Режим основного аккаунта."""
        return self.mode_for(None)

    def mode_for(self, session: str | None) -> Mode:
        """Режим аккаунта ``session`` (None — основной)."""
        engine = self._engines.get(self._resolve_session(session))
        if engine is not None:
            return engine.mode
        return self._config.mode_for(session or self._primary_session)

    def auto_for(self, session: str | None = None) -> bool:
        """Включена ли у аккаунта автопереписка в личке («Авто»)."""
        target = self._resolve_session(session)
        if self._auto_flags.get(target):
            return True
        engine = self._engines.get(target)
        return engine is not None and engine.mode == Mode.AUTO

    def controls_for(self, session: str | None = None) -> AccountControls:
        """Переключатели аккаунта: «Скроллинг» ленты и «Авто» (личка)."""
        return AccountControls(
            scroll=self.mode_for(session) != Mode.OBSERVE,
            auto=self.auto_for(session),
        )

    def _dialog_mode(self, session: str | None) -> str:
        """Режим для автопереписки/приветствий (``match_*.modes``).

        «Авто» включено → ``AUTO`` (автопереписка работает), иначе — режим
        ленты: автопереписка (``match_intro.modes = [AUTO]``) молчит, а
        стикер-привет (все режимы) уходит как раньше.
        """
        if self.auto_for(session):
            return Mode.AUTO.value
        return self.mode_for(session).value

    def set_controls(
        self, session: str | None = None, *,
        scroll: bool | None = None, auto: bool | None = None,
    ) -> AccountControls:
        """Меняет переключатели аккаунта на лету и сохраняет в config.yaml.

        ``None`` — переключатель не трогаем. Скроллинг и «Авто» независимы:
        оба выключены — наблюдение (бывший OBSERVE).
        """
        target = self._resolve_session(session)
        current = self.controls_for(target)
        new = AccountControls(
            scroll=current.scroll if scroll is None else bool(scroll),
            auto=current.auto if auto is None else bool(auto),
        )
        engine = self._engines.get(target)
        if engine is not None:
            # Движку ленты важен только скроллинг; AUTO = скроллинг + «Авто».
            engine.mode = new.legacy_mode
        self._auto_flags[target] = new.auto
        if target == self._primary_session:
            self._config.project.mode = new.legacy_mode
        try:
            if target:
                self._config.persist_account_controls(self._config_path, target, new)
            else:
                self._config.persist_mode(self._config_path, new.legacy_mode)
            logger.info(
                f"{target or 'Аккаунт'}: скроллинг "
                f"{'вкл' if new.scroll else 'выкл'}, авто {'вкл' if new.auto else 'выкл'} "
                "(сохранено в config.yaml)"
            )
        except Exception as e:
            logger.error(f"Не удалось сохранить режим в config.yaml: {e}")
        return new

    def redirect_for(self, session: str | None) -> str:
        """Куда перенаправлена личка аккаунта ('' — пишем с него самого).

        Цель учитывается, только если это ДРУГОЙ вошедший аккаунт: без
        клиента писать не с чего — тогда пишем как раньше, со своего.
        """
        target = (self._config.auto_actions.account_redirects or {}).get(
            session or "", "",
        )
        if not target or target == session or target not in self._client_sessions:
            return ""
        return target

    def set_redirect(self, session: str, target: str) -> None:
        """Меняет перенаправление лички на лету (сайт сохраняет его в config.yaml)."""
        redirects = dict(self._config.auto_actions.account_redirects or {})
        if target and target != session:
            redirects[session] = target
        else:
            redirects.pop(session, None)
        self._config.auto_actions.account_redirects = redirects
        logger.info(
            f"{session}: личка → {target}" if session in redirects
            else f"{session}: перенаправление лички снято"
        )

    def set_mode(self, mode: Mode, session: str | None = None) -> None:
        """Старый интерфейс режима (ControlBot, стоп на лайке, «Продолжить»).

        OBSERVE — выключает только скроллинг («Авто» в личке не трогаем:
        лайк в ленте — не повод бросать переписку), SEMI_AUTO — включает
        скроллинг, AUTO — скроллинг и «Авто». ``session`` None — основной.
        """
        self.set_controls(
            session,
            scroll=mode != Mode.OBSERVE,
            auto=True if mode == Mode.AUTO else None,
        )

    async def mode_block_reason(
        self, mode: Mode, session: str | None = None,
    ) -> str | None:
        """Почему нельзя включить режим ``mode`` аккаунту (None — можно).

        SEMI_AUTO/AUTO не включаем, пока у аккаунта исчерпан дневной лимит
        ❤️: на первой же анкете, которую бот решит лайкнуть, он ничего не
        отправит (``LIKE_LIMIT``), Leo будет ждать реакцию — лента встанет
        (прод 2026-10-29). Ошибка чтения состояния не мешает переключению.
        """
        engine = self._engines.get(self._resolve_session(session))
        if mode not in (Mode.SEMI_AUTO, Mode.AUTO) or engine is None:
            return None
        try:
            until = await engine.like_limit_until()
        except Exception as e:
            logger.error(f"Не удалось проверить лимит ❤️ перед сменой режима: {e}")
            return None
        if until is None:
            return None
        reset = datetime.fromtimestamp(until).strftime("%H:%M")
        name = engine.account_session or "аккаунт"
        return (
            f"Лимит ❤️ у {name} исчерпан до {reset} — {mode.value} не "
            "включаю: на первой анкете для лайка лента встанет."
        )

    async def request_mode(
        self, mode: Mode, session: str | None = None,
    ) -> str | None:
        """Меняет режим, если это разрешено; иначе возвращает причину отказа."""
        reason = await self.mode_block_reason(mode, session)
        if reason is not None:
            logger.warning(f"Смена режима на {mode.value} отклонена: {reason}")
            return reason
        self.set_mode(mode, session)
        return None

    def auto_engine(self, session: str | None = None) -> AutoActionEngine | None:
        """Движок авто-действий аккаунта (None — основной)."""
        return self._engines.get(self._resolve_session(session))

    def _build_auto_engine(self, session: str) -> AutoActionEngine:
        """Строит AutoActionEngine аккаунта ``session``.

        Клиент — по имени сессии; для авто-уведомлений — «другой» аккаунт
        (на него пересылается карточка анкеты с причиной, Бармалей↔Меланхолик).
        См. ``_clients_for_session``. Режим — ``AppConfig.mode_for(session)``.
        """
        auto_cfg = self._config.auto_actions
        client, notify_client = (
            self._clients_for_session(session) if session else (None, None)
        )

        if (
            auto_cfg.enabled and client is None
            and session == auto_cfg.account_session
        ):
            logger.warning(
                f"AutoAction: account_session={session!r} не найден среди "
                f"accounts; авто-действия основного аккаунта отключены"
            )

        return AutoActionEngine(
            client=client,
            config=auto_cfg,
            mode=self._config.controls_for(session).legacy_mode,
            chat_id=self._dvinchik_chat_id,
            notify_client=notify_client,
            db=self._db,
            account_session=session,
        )

    def switch_auto_account(self, session: str) -> bool:
        """Делает аккаунт ``session`` основным (Веб-панель).

        Основной аккаунт — лента сайта, ручные ❤️/👎 и ответы на капчи с сайта,
        ControlBot. Режимы аккаунтов не меняются: у каждого свой движок.
        Сохраняет выбор в config.yaml (переживает restart).
        """
        client, _ = self._clients_for_session(session)
        if client is None or session not in self._engines:
            logger.warning(
                f"AutoAction: account_session={session!r} не найден среди accounts"
            )
            return False

        old = self._primary_session
        if old != session and old not in self._client_sessions:
            # Движок-заглушка невошедшего аккаунта больше не нужен.
            self._engines.pop(old, None)
        self._primary_session = session
        self._config.auto_actions.account_session = session
        try:
            self._config.persist_account_session(self._config_path, session)
            logger.info(f"Основной аккаунт сменён на {session!r} (сохранён в config.yaml)")
        except Exception as e:
            logger.error(f"Не удалось сохранить основной аккаунт в config.yaml: {e}")
        return True

    def _resolve_client_sessions(
        self, client_sessions: list[str] | None,
    ) -> list[str]:
        """Имена сессий, параллельные ``self._clients``.

        Явный список из main.py (имена, с которыми реально создавались
        клиенты) — главный источник. Без него (тесты/отладка) — прежняя
        эвристика по позиции в ``telegram.accounts``: ``acc.session`` или
        дефолт ``dvai``/``dvai_{idx}``, как в main.py.
        """
        if client_sessions is not None and len(client_sessions) == len(
            self._clients
        ):
            return list(client_sessions)
        accounts = self._config.telegram.accounts or []
        sessions: list[str] = []
        for i in range(len(self._clients)):
            if i < len(accounts) and accounts[i].session:
                sessions.append(accounts[i].session)
            else:
                sessions.append("dvai" if len(accounts) == 1 else f"dvai_{i}")
        return sessions

    def _clients_for_session(
        self, session: str,
    ) -> tuple[TelegramClient | None, TelegramClient | None]:
        """(клиент сессии ``session``, первый ДРУГОЙ клиент — для уведомлений)."""
        client = None
        notify_client = None
        for c, name in zip(self._clients, self._client_sessions):
            if name == session and client is None:
                client = c
            elif notify_client is None:
                notify_client = c
        return client, notify_client

    def _session_for_client(self, client: object) -> str:
        """Возвращает session-имя аккаунта, которому принадлежит client.

        По нему ключуются ``raw_messages.account_session``, дедуп и контекст
        анкеты, а веб выбирает клиент для скачивания фото. '' — если client
        не найден (например, backlog-рекавери без живого msg).
        """
        if client is None:
            return ''
        for c, name in zip(self._clients, self._client_sessions):
            if c is client:
                return name
        return ''
