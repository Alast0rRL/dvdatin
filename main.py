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
from database.database import Database
from services.filter_engine import FilterEngine
from services.filter_service import FilterService
from services.profile_service import ProfileService
from services.clip_service import CLIPService
from services.llm_service import LLMService
from services.remote_llm_client import RemoteLLMClient
from services.remote_clip_client import RemoteCLIPClient
from services.ai_scoring_service import AIScoringService
from services.decision_service import DecisionService
from services.review_service import ReviewService
from services.analytics_service import AnalyticsService
from services.review_export import EXPORTS_DIR, export_review_csv
from telegram.review_bot import ReviewBot
from telegram.control_bot import ControlBot
from telegram.client import authorize, create_client

CONFIG_PATH = Path("config/config.yaml")


async def export_review() -> None:
    """Экспортирует review dataset в CSV и завершает работу.

    Не требует Telegram-авторизации.
    """
    config = AppConfig.load(CONFIG_PATH)
    setup_logging(config)
    db = Database()
    await db.connect()
    try:
        path = await export_review_csv(db, EXPORTS_DIR)
        print(f"Экспорт завершён: {path}")
    except RuntimeError as e:
        logger.error(f"Экспорт не выполнен: {e}")
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

    ai_scoring_service = None
    if config.ai.enabled:
        if config.ai.backend == "remote":
            clip_service = RemoteCLIPClient(config.ai.clip, config.ai.remote)
            llm_service = RemoteLLMClient(config.ai.llm, config.ai.remote)
            logger.info(
                f"AI Scoring (remote): base_url={config.ai.remote.base_url}"
            )
        else:
            clip_service = CLIPService(config.ai.clip)
            llm_service = LLMService(config.ai.llm)
            logger.info("AI Scoring (local stubs)")
        ai_scoring_service = AIScoringService(db, config, clip_service, llm_service)

    # AI Decision Engine (Stage 5) — только OBSERVE, никаких Telegram-действий
    # Предпочтения пользователя (SKIP/LIKE) подгружаются из отдельного файла.
    preferences = load_preferences()
    if preferences.enabled:
        logger.info(
            "Предпочтения AI scoring загружены: "
            f"skip={len(preferences._prefs.skip)} like={len(preferences._prefs.like)}"
        )
    decision_service = None
    if config.ai.enabled and ai_scoring_service is not None:
        decision_service = DecisionService(
            db,
            config,
            profile_service,
            filter_service,
            ai_scoring_service,
            preferences=preferences,
        )

    # Human Review + Analytics (Stage 6) — только OBSERVE/REVIEW
    review_service = ReviewService(db, profile_service)
    analytics_service = AnalyticsService(db, config)
    review_bot = ReviewBot(
        clients[0], config,
        review_service=review_service,
        analytics_service=analytics_service,
    )
    review_bot.register()

    collector = DvinchikCollector(
        clients, db, config,
        profile_service=profile_service,
        filter_service=filter_service,
        ai_scoring_service=ai_scoring_service,
        decision_service=decision_service,
        stats=stats,
        config_path=CONFIG_PATH,
    )
    collector.register()

    # Stage 7.5: ControlBot — панель управления режимом (вкл/выкл авто).
    # Команды принимаются только от allowed_user_ids (см. config.control).
    if config.control.enabled:
        control_bot = ControlBot(
            clients[0], config,
            collector=collector,
            db=db,
        )
        control_bot.register()
        logger.info("ControlBot: панель управления активна")

    # Stage 6.2: pipeline (parse → filter → AI) выполняется в фоновом worker.
    # Хендлер только сохраняет RAW и ставит задание в очередь — AI/сетевой I/O
    # не блокирует обработку входящих Telegram-событий.
    worker = DvinchikRawWorker(process=collector._process_message)
    collector.attach_worker(worker)
    collector.start()

    # W3: восстановление backlog'а (RAW сохранён, но не обработан) запускаем
    # фоном — не блокируем старт Telegram. Recovery сам дросселируется
    # очередью (maxsize), worker обрабатывает параллельно.
    recovery_task = asyncio.get_event_loop().create_task(
        collector.recover_backlog()
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
        await collector.stop()
        stats.print_summary()
        if ai_scoring_service and config.ai.backend == "remote":
            clip = ai_scoring_service._clip
            llm = ai_scoring_service._llm
            if hasattr(clip, "close"):
                await clip.close()
            if hasattr(llm, "close"):
                await llm.close()
        for c in clients:
            if c.is_connected():
                await c.disconnect()
        await db.close()
        logger.info("Приложение остановлено")


if __name__ == "__main__":
    if "--export-review" in sys.argv:
        asyncio.run(export_review())
    else:
        asyncio.run(main())
