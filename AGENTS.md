# AGENTS.md

## Commands

```bash
# Run the app
python main.py

# Run all tests (231 total, no linter/formatter configured)
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/test_ai.py -v
```

## Critical Architecture Rules

- **RAW-first invariant**: raw Telegram messages are ALWAYS saved to SQLite BEFORE any parsing/classification. Never change the order in `collectors/dvinchik_collector.py`.
- **FilterEngine is Telegram-free**: `services/filter_engine.py` takes only `Profile + AppConfig`. Never import Telethon types there.
- **AI services are Telegram-free**: `services/clip_service.py`, `services/llm_service.py`, `services/ai_scoring_service.py`, `services/decision_service.py` never import Telethon. Work only with bytes, Profile, and Config.
- **Remote AI clients are Telegram-free**: `services/remote_llm_client.py`, `services/remote_clip_client.py` never import Telethon.
- **AI scoring gated by PASS**: In the collector, AI scoring only runs when `FilterDecision == PASS`. REJECT/REVIEW → skip AI.
- **AI errors must not break Collector**: All AI calls are wrapped in try/except. RAW messages are never lost.
- **`filters/` package is empty** — a reserved placeholder. Actual filter logic lives in `services/filter_engine.py` and `services/filter_service.py`. Do not confuse the two.
- **`config/config.yaml` is gitignored** (contains API keys, phone, proxy creds). Only `config/config.example.yaml` is committed. Never commit real secrets.

## Config Format (nested)

```yaml
filters:
  age:
    min: 18
    max: 19
  city:
    allowed:
      - "Санкт-Петербург"
```

`FiltersConfig` exposes compat fields `age_min`, `age_max`, `city_allowed` via `model_post_init` — both forms work internally.

## Code Conventions

- **Python 3.12+** — `StrEnum`, `X | Y` union syntax, lowercase `list[str]` generics.
- **`from __future__ import annotations`** in every module.
- **Type hints on all function signatures** including return types.
- **Private attributes** use underscore prefix: `self._client`, `self._db`.
- **Docstrings and comments in Russian**, code identifiers in English.
- **`TYPE_CHECKING` guard** for heavy imports (avoids circular deps).
- **Pydantic v2** models — use `field_validator`, not `validator`.
- **Loguru** for logging (not stdlib logging). Rich for console output.
- **httpx** for async HTTP in remote AI clients.

## Testing Patterns

- **No pytest-asyncio** — tests wrap async with `asyncio.get_event_loop().run_until_complete()`.
- **Class-based test organization** (not function-level).
- **Factory helpers** per test file: `make_config()`, `make_profile()`, `make_parsed()`, `make_event()`.
- **Temp DB fixtures**: `tmp_path` creates a fresh SQLite per test.
- **Mocks**: `unittest.mock.AsyncMock` / `MagicMock` for Telegram client and DB.
- Current counts: test_parser (41), test_collector (18), test_profile (19), test_audit (29), test_filter (25), test_ai (54), test_ai_scoring (10), test_decision (20), test_human_review (29), test_analytics (20), test_review_ui (5) → 315 total. Reset the exact counts from the real file when editing them; the summary here is indicative.

## Gotchas

- **Dual FilterResult types**: `models/raw.py` has `FilterResult` (FILTER_MATCH/FILTER_NOT_MATCH) used by the parser. `models/filter.py` has `FilterResult` (PASS/REJECT/REVIEW) used by the filter engine. They serve different layers.
- **`FilterResult` in `models/raw.py`** uses Pydantic `StrEnum` named `FilterResult` — do not confuse with `models/filter.py`'s `FilterResult` dataclass.
- **SQLite WAL mode** + foreign keys explicitly enabled via PRAGMA. Schema is idempotent (`CREATE TABLE IF NOT EXISTS`).
- **Windows-specific**: `run.bat` sets `chcp 65001`, `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8`. `main.py` also forces UTF-8 on stdout/stderr.
- **Banner version** (`banner.py`) is synced to `"0.6"`.
- **Chat ID 1234060895** is the Dayvinchik bot default, hardcoded in `config/config.example.yaml`.
- **AI backend selection**: `ai.backend` = `local` (stubs) or `remote` (Ubuntu AI Server). Remote clients use httpx, not requests.
- **Remote CLIP contract**: server expects multipart field **`files`** (not `images`) and returns **`clip_score`**/`images_analyzed`/`images_failed`/`status` (not `aesthetic_score`). `remote_clip_client.py` maps `clip_score`→`CLIPScore.aesthetic_score`.
- **Remote endpoint** (`config.yaml` → `ai.remote.base_url`): `http://144.31.139.206:8000` — прокладка с SSH reverse tunnel на Ubuntu AI Server (Ollama qwen3:8b + CLIP на GPU). Это приватный endpoint — не коммитить в example как реальный рабочий (в example — пример).
- **`proxy/`** contains vendored xray-core binaries and real VLESS config — do not commit credential changes.
- **Human decisions append-only**: `human_decisions` history is never UPDATE/DELETE. `UNIQUE(ai_decision_id)` — one review per AI evaluation. Latest human decision by `created_at`.
- **`get_profile_by_id` returns dict** (not `Profile` object) — in `_render_profile` use `profile['id']`, not `profile.id`.
- **DB thread leak in tests**: any test that opens a `Database` must close it (fixture teardown). A leaked aiosqlite thread prevents the pytest process from exiting (hang).
- **Agreement rate** = AGREEMENT/(AGREEMENT+DISAGREEMENT); SKIP excluded from denominator; `null` (not 0%) when denominator = 0. Call it "AI/Human Agreement Rate", never "AI accuracy".
- **Stage 6 Telegram-free layers**: `ReviewService`, `AnalyticsService`, `review_export.py` never import Telethon. Only `telegram/review_bot.py` consumes Telethon.

## Project Stage

Currently at **Stage 6 complete** (Human Review & Analytics). See `PROJECT.md` for roadmap. DecisionService operates in OBSERVE mode only — it computes LIKE/REVIEW/DISLIKE but never performs Telegram actions; ReviewBot saves human decisions (APPROVE/REJECT/SKIP) but never performs Telegram actions either. Do not implement automatic likes/swipes/dialog manager until explicitly instructed.

## Empty Placeholder Packages

`dialogs/`, `utils/`, `prompts/`, `managers/`, `filters/` — all contain only `__init__.py`. Reserved for future stages.
