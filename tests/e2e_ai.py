# REAL_E2E — Stage 5 AI Decision Engine end-to-end test against live gateway.
#
# Это НЕ unit-тест. Имя файла (без префикса test_) — pytest по умолчанию
# НЕ собирает и НЕ запускает этот файл в обычном прогоне.
#
# Запуск (вручную, с реальной сетью):
#   python tests/e2e_ai.py
#
# Проверяет полный путь:
#   Windows → AI Client (httpx) → FastAPI Gateway → Qwen3/CLIP → решение → SQLite
#
# Требует доступного Ubuntu AI Gateway (config/config.yaml → ai.remote.base_url).

from __future__ import annotations

import asyncio
import base64
import io
import os
import sys
import tempfile
from pathlib import Path

os.environ["PYTHONUTF8"] = "1"
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

_REAL_E2E = True
_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))


async def _gateway_ok(base_url: str) -> tuple[bool, dict]:
    """Проверяет доступность шлюза и его контракт."""
    import httpx

    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        health = await client.get("/health")
        models = await client.get("/v1/models")
        return (
            health.status_code == 200,
            {"health": health.json(), "models": models.json()},
        )


async def main() -> int:
    """Запускает E2E сценарий против реального шлюза."""
    from app.config import AppConfig
    from database.database import Database
    from services.remote_llm_client import RemoteLLMClient
    from services.remote_clip_client import RemoteCLIPClient
    from services.ai_scoring_service import AIScoringService
    from services.filter_engine import FilterEngine
    from services.filter_service import FilterService
    from services.profile_service import ProfileService
    from services.decision_service import DecisionService
    from models.decision import AIDecision

    print("=" * 72)
    print("REAL_E2E  |  STAGE 5 AI DECISION ENGINE  |  OBSERVE MODE")
    print("=" * 72)

    config_path = _BASE_DIR / "config" / "config.yaml"
    if not config_path.exists():
        print(f"ERROR: config {config_path} не найден")
        return 2

    config = AppConfig.load(config_path)
    base_url = config.ai.remote.base_url
    print(f"Gateway base_url: {base_url}")

    try:
        ok, info = await _gateway_ok(base_url)
    except Exception as e:
        print(f"Gateway НЕдоступен: {e}")
        print("Skipped: AI_UNAVAILABLE")
        return 1

    print(f"health: {info['health']}")
    print(f"models: {info['models']}")

    db = Database(path=Path(tempfile.mkdtemp()) / "e2e.db")
    await db.connect()

    try:
        prof_id = await db.insert_profile(
            name="Анна", age=19, raw_city="Санкт-Петербург",
            normalized_city="Санкт-Петербург",
            description="Люблю природу, прогулки по парку и фотографию. "
                        "Ищу интересного собеседника для долгих разговоров.",
            fingerprint="fp_e2e_anna",
            source_chat_id=1234060895, source_message_id=9001,
            first_seen_at="now", last_seen_at="now", status="NEW",
        )

        llm = RemoteLLMClient(config.ai.llm, config.ai.remote)
        clip = RemoteCLIPClient(config.ai.clip, config.ai.remote)
        ps = ProfileService(db)
        fs = FilterService(db, ps, FilterEngine(config))
        ai_svc = AIScoringService(db, config, clip, llm)
        decision_svc = DecisionService(db, config, ps, fs, ai_svc)

        # реальное 1x1 PNG для CLIP
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8A\n"
            "AQUBAScY42YAAAAASUVORK5CYII="
        )

        print("\n--- запуск Decision Engine ---")
        result = await decision_svc.evaluate(prof_id, image_data_list=[png])

        print("\n" + "=" * 72)
        print("AI DECISION")
        print("=" * 72)
        print(f"Profile:   #{result.profile_id} (Анна, 19, Санкт-Петербург)")
        print(f"Filter:    PASS")
        print(f"LLM:       {result.llm_score:.2f}" if result.llm_score is not None else "LLM:       N/A")
        print(f"CLIP:      {result.clip_score:.2f}" if result.clip_score is not None else "CLIP:      N/A")
        print(f"Combined:  {result.combined_score:.2f}")
        print(f"Confidence:{result.confidence:.2f}")
        print(f"Decision:  {result.decision}")
        print("Reasons:")
        for r in result.reasons:
            print(f"  - {r}")
        print(f"Scoring version: {result.scoring_version}")
        print("=" * 72)

        saved = await db.get_latest_ai_decision(prof_id)
        assert saved is not None, "Решение не сохранено в SQLite"
        assert result.decision in (AIDecision.LIKE, AIDecision.REVIEW, AIDecision.DISLIKE)
        print(f"SQLite: decision={saved['decision']}, combined={saved['combined_score']}")

        await llm.close()
        await clip.close()

        print("\nE2E OK")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
