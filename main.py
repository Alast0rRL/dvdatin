# DvAI — Точка входа приложения.
# Загружает конфигурацию, инициализирует все модули и запускает главный цикл.

from __future__ import annotations

import os
import sys
import io

os.environ["PYTHONUTF8"] = "1"
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import asyncio
from pathlib import Path

from loguru import logger

from app.banner import print_banner
from app.config import AppConfig
from app.logging import setup_logging
from app.preferences import load_preferences
from collectors.dvinchik_collector import DvinchikCollector
from collectors.raw_worker import DvinchikRawWorker
from collectors.stats import CollectorStats
from database.database import Database, DB_PATH
from services.filter_engine import FilterEngine
from services.filter_service import FilterService
from services.profile_service import ProfileService
from services.decision_service import DecisionService
from services.analysis_export import EXPORTS_DIR, export_analysis_csv, clear_database
from services.manual_review import ManualReviewRecorder
from services.photo_vice import PhotoViceDetector
from telegram.control_bot import ControlBot
from telegram.client import authorize, create_client

CONFIG_PATH = Path("config/config.yaml")


async def export_analysis() -> None:
    """Экспортирует все анкеты в CSV для анализа (liked/skipped/etc.).

    Не требует Telegram-авторизации.
    """
    config = AppConfig.load(CONFIG_PATH)
    setup_logging(config)
    db = Database()
    await db.connect()
    try:
        path, summary = await export_analysis_csv(db, EXPORTS_DIR)
        print(f"Анализ экспортирован: {path}")
        print(f"Всего анкет: {summary['total']}")
        for action, count in summary["breakdown"].items():
            print(f"  {action}: {count}")
    except RuntimeError as e:
        logger.error(f"Анализ не выполнен: {e}")
        print(f"Ошибка: {e}")
    finally:
        await db.close()


async def clear_db() -> None:
    """Создаёт бэкап и полностью очищает БД.

    Не требует Telegram-авторизации. Перед удалением копия БД
    сохраняется в data/exports/backups/.
    """
    config = AppConfig.load(CONFIG_PATH)
    setup_logging(config)
    db = Database()
    await db.connect()
    try:
        backup = await clear_database(db)
        print(f"БД очищена. Бэкап: {backup}")
    except RuntimeError as e:
        logger.error(f"Очистка не выполнена: {e}")
        print(f"Ошибка: {e}")
    finally:
        await db.close()


async def main() -> None:
    """Главная асинхронная функция приложения."""
    try:
        config = AppConfig.load(CONFIG_PATH)
    except (FileNotFoundError, ValueError) as e:
        logger.error(f"Ошибка конфигурации: {e}")
        return

    setup_logging(config)

    db = Database()
    await db.connect()
    db_ok = await db.check()

    # === Авторизация всех аккаунтов (multi-account) ===
    clients: list = []
    client_sessions: list[str] = []
    statuses: list[str] = []
    for idx, acc in enumerate(config.telegram.accounts):
        session_name = acc.session or (
            "dvai" if len(config.telegram.accounts) == 1 else f"dvai_{idx}"
        )
        client = create_client(acc, session_name=session_name)
        name = None
        if acc.phone:
            try:
                name = await authorize(client, acc.phone)
            except Exception as e:
                logger.error(f"Ошибка Telegram (аккаунт #{idx + 1}): {e}")
                name = None
        if name:
            clients.append(client)
            client_sessions.append(session_name)
            statuses.append(f"acc{idx + 1}:[green]{name}[/green]")
        else:
            statuses.append(f"acc{idx + 1}:[yellow]Not Authorized[/yellow]")
            if client.is_connected():
                await client.disconnect()

    session_status = "; ".join(statuses) if statuses else "[yellow]No accounts[/yellow]"
    print_banner(config, db_ok, session_status)

    if not clients:
        logger.info("Нет авторизованных аккаунтов. Выход...")
        await db.close()
        return

    # Разметка истории raw_messages по аккаунтам: каждый аккаунт слушает один и
    # тот же чат Лео, но пространства message_id у них свои. Без этого лента
    # показывала бы оба аккаунта разом (не совпадая ни с одним TG-чатом).
    # sender_id исходящих = user id аккаунта, поэтому строим карту из get_me().
    if len(clients) > 1:
        sender_to_session: dict[int, str] = {}
        for client, sess in zip(clients, client_sessions):
            try:
                me = await client.get_me()
                if me and getattr(me, "id", None):
                    sender_to_session[int(me.id)] = sess
            except Exception as e:
                logger.error(f"Не удалось получить user id аккаунта {sess!r}: {e}")
        if len(sender_to_session) > 1:
            try:
                marked = await db.backfill_raw_account_session(sender_to_session)
                if marked:
                    logger.info(
                        f"RAW размечены по аккаунтам: {marked} строк "
                        f"({', '.join(f'{s}:{i}' for i, s in sender_to_session.items())})"
                    )
            except Exception as e:
                logger.error(f"Не удалось разметить RAW по аккаунтам: {e}")

    if config.dvinchik.chat_id == 0:
        logger.info(
            "dvinchik.chat_id не установлен. "
            "Все входящие сообщения будут отображаться в консоли. "
            "Узнайте chat_id Дайвинчика и вставьте в config.yaml."
        )

    stats = CollectorStats()
    profile_service = ProfileService(db)
    filter_engine = FilterEngine(config)
    filter_service = FilterService(db, profile_service, filter_engine)

    # Предпочтения пользователя (SKIP/LIKE) подгружаются из отдельного файла.
    preferences = load_preferences()
    if preferences.enabled:
        logger.info(
            "Предпочтения scoring загружены: "
            f"skip={len(preferences._prefs.skip)} like={len(preferences._prefs.like)} "
            f"like_hard={len(preferences._prefs.like_hard)}"
        )

    # Deterministic Decision Engine (Stage 8): детерминированный scoring
    # без LLM/CLIP. Решение = правила + извлечённые признаки.
    decision_service = DecisionService(
        db,
        config,
        profile_service,
        filter_service,
        preferences=preferences,
    )

    # Stage 8: ручные решения владельца по REVIEW-анкетам (журнал в файле).
    mr_cfg = config.manual_review
    manual_review = ManualReviewRecorder(
        db,
        path=Path(mr_cfg.file),
        enabled=mr_cfg.enabled,
        file_format=mr_cfg.format,
    )
    if mr_cfg.enabled:
        logger.info(
            f"ManualReview: запись ручных решений активна (файл: {mr_cfg.file})"
        )

    # Проверка фото на сигарету/алкоголь (CLIP на CPU) для будущих лайков.
    photo_vice = PhotoViceDetector(config.photo_check)
    if config.photo_check.enabled:
        logger.info("PhotoVice: проверка фото на сигареты/алкоголь включена")

    collector = DvinchikCollector(
        clients, db, config,
        profile_service=profile_service,
        filter_service=filter_service,
        decision_service=decision_service,
        stats=stats,
        config_path=CONFIG_PATH,
        manual_review=manual_review,
        # Имена сессий ТОЛЬКО вошедших аккаунтов, параллельно clients.
        client_sessions=client_sessions,
        photo_vice=photo_vice,
    )
    collector.register()

    # Stage 7.5: ControlBot — панель управления режимом (вкл/выкл авто).
    # Слушает ВСЕ аккаунты из telegram.accounts: команды принимаются от
    # control.allowed_user_ids, ответ шлётся с того же аккаунта, что получил.
    if config.control.enabled:
        for client in clients:
            control_bot = ControlBot(
                client, config,
                collector=collector,
                db=db,
            )
            control_bot.register()
        logger.info("ControlBot: панель управления активна (слушает все аккаунты)")

    # Pipeline: parse → filter → deterministic scoring
    worker = DvinchikRawWorker(process=collector._process_message)
    collector.attach_worker(worker)
    collector.start()

    # Stage 9: Web UI (Flask) — запускается в daemon-потоке
    try:
        from web import create_app
        from web.config import WebConfig
        from web.db import SyncDB
        from web.photos import set_telegram_clients_with_sessions
        from web.actions import set_action_engine, set_collector

        web_cfg = WebConfig.from_env()
        sync_db = SyncDB(str(DB_PATH))
        flask_app = create_app(web_cfg)
        flask_app.config["SYNC_DB"] = sync_db
        flask_app.config["CONFIG_PATH"] = CONFIG_PATH

        # Устанавливаем TelegramClient-ы для скачивания фото (все аккаунты +
        # их session-имена). message_id в диалоге с Leo уникален для каждого
        # аккаунта, поэтому фото качается тем, кто реально получил сообщение.
        if clients:
            set_telegram_clients_with_sessions(clients, client_sessions)

        # Stage 7: web-кнопки лайк/дизлайк реально отправляют реакцию
        # в чат Leo через авто-аккаунт (чтобы лента двигалась дальше).
        set_action_engine(collector.auto_engine())
        # Stage 7.5/web: переключение режима на сайте меняет живой
        # AutoActionEngine сразу (не только config.yaml), SEMI_AUTO
        # начинает работать без перезапуска приложения.
        set_collector(collector)

        import threading
        flask_thread = threading.Thread(
            target=lambda: flask_app.run(
                host=web_cfg.host, port=web_cfg.port,
                debug=False, use_reloader=False,
            ),
            daemon=True,
        )
        flask_thread.start()
        logger.info(
            f"Web UI запущен: http://{web_cfg.host}:{web_cfg.port}"
        )
    except Exception as e:
        logger.warning(f"Web UI не запущен: {e}")

    # Stage 7 (SEMI_AUTO): обработка активной анкеты на авто-аккаунте
    auto_task = asyncio.create_task(
        collector.start_auto_stream()
    )

    # Модель CLIP грузится в фоне (~10–30 с), чтобы первая анкета не ждала.
    photo_vice_task = asyncio.create_task(photo_vice.preload())

    # W3: восстановление backlog'а
    recovery_task = asyncio.create_task(
        collector.recover_backlog()
    )

    # Сверка ленты с Telegram: досохраняет сообщения, пропущенные событиями
    # (обрывы прокси, рестарты, ручные ответы с телефона).
    history_sync_task = asyncio.create_task(
        collector.run_history_sync()
    )

    logger.info(
        f"Приложение запущено ({len(clients)} аккаунт(ов)) "
        f"в режиме {config.project.mode.value}. Ctrl+C для выхода."
    )

    try:
        await asyncio.gather(*(c.run_until_disconnected() for c in clients))
    except KeyboardInterrupt:
        pass
    finally:
        if not recovery_task.done():
            recovery_task.cancel()
        try:
            await recovery_task
        except asyncio.CancelledError:
            pass
        if not auto_task.done():
            auto_task.cancel()
        try:
            await auto_task
        except asyncio.CancelledError:
            pass
        history_sync_task.cancel()
        try:
            await history_sync_task
        except asyncio.CancelledError:
            pass
        await collector.stop()
        stats.print_summary()
        for c in clients:
            if c.is_connected():
                await c.disconnect()
        await db.close()
        logger.info("Приложение остановлено")


if __name__ == "__main__":
    if "--export-analysis" in sys.argv:
        asyncio.run(export_analysis())
    elif "--clear-db" in sys.argv:
        asyncio.run(clear_db())
    else:
        asyncio.run(main())
