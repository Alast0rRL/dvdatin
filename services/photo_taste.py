# PhotoTasteModel: «во вкусе ли владельца» по фото анкеты.
#
# Логистическая регрессия поверх CLIP-эмбеддингов (services/clip_encoder.py):
# эмбеддинги фото карточки усредняются и нормируются, score = sigmoid(w·x + b).
# Обучена на разметке владельца со страницы /rate (photo_ratings: LIKE=1,
# DISLIKE/VICE=0, SKIP исключён) — офлайн 2026-10-22: 1391 анкета, 168 ❤️,
# AUC 0.83 на отложенных анкетах. Веса — JSON на сервере
# (config.photo_taste.model_path, data/ — не в git: обучено на личных данных).
#
# Пока ТЕНЕВОЙ режим: вердикт пишется в photo_scores и на действия бота не
# влияет — его сравнивают с решениями владельца, прежде чем включать.
#
# Telegram-free: на вход готовые эмбеддинги.

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    import numpy as np

    from app.config import PhotoTasteConfig


@dataclass(frozen=True)
class TasteVerdict:
    """Вердикт модели вкуса: score 0..1 и зона LIKE / REVIEW / DISLIKE."""

    score: float
    verdict: str


class PhotoTasteModel:
    """Загружает веса из JSON и считает score по эмбеддингам фото."""

    def __init__(self, config: PhotoTasteConfig) -> None:
        self._cfg = config
        self._weights: list[float] | None = None
        self._bias = 0.0
        self._like_thr = 1.0
        self._dislike_thr = 0.0
        # Порог score для «верхних N% анкет» (из обучения: like_thresholds).
        self._like_by_share: dict[float, float] = {}
        self.version = ""
        self._failed = False
        self._mtime = 0.0
        self._checked_at = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled) and not self._failed and self.load()

    #: Как часто проверять, не переобучена ли модель (новый JSON на диске).
    RELOAD_CHECK_SEC = 30.0

    def _maybe_reload(self) -> None:
        """Переобучение пишет новый JSON — подхватываем без рестарта бота."""
        now = time.monotonic()
        if self._weights is None or now - self._checked_at < self.RELOAD_CHECK_SEC:
            return
        self._checked_at = now
        try:
            mtime = Path(self._cfg.model_path).stat().st_mtime
        except OSError:
            return
        if mtime != self._mtime:
            backup = (self._weights, self._bias, self._like_thr, self._dislike_thr,
                      self.version, self._mtime, self._like_by_share)
            old = self.version
            self._weights = None
            if self.load():
                logger.info(f"PhotoTaste: модель обновлена {old} → {self.version}")
            else:
                # Битый файл — остаёмся на прежней модели, попробуем позже.
                (self._weights, self._bias, self._like_thr, self._dislike_thr,
                 self.version, _, self._like_by_share) = backup
                self._mtime = mtime
                self._failed = False

    def load(self) -> bool:
        if self._weights is not None:
            return True
        if self._failed or not self._cfg.enabled:
            return False
        try:
            path = Path(self._cfg.model_path)
            self._mtime = path.stat().st_mtime
            data = json.loads(path.read_text(encoding="utf-8"))
            self._weights = [float(w) for w in data["weights"]]
            self._bias = float(data["bias"])
            self._like_thr = float(data["like_threshold"])
            self._dislike_thr = float(data["dislike_threshold"])
            self._like_by_share = {
                float(k): float(v) for k, v in (data.get("like_thresholds") or {}).items()
            }
            self.version = str(data.get("version", ""))
            logger.info(
                f"PhotoTaste: модель {self.version} загружена "
                f"(LIKE ≥ {self._like_thr:.3f}, DISLIKE ≤ {self._dislike_thr:.3f})"
            )
            return True
        except Exception as e:
            self._failed = True
            logger.warning(f"PhotoTaste: модель не загружена ({self._cfg.model_path}): {e!r}")
            return False

    def like_threshold(self, share: float) -> float | None:
        """Порог score, выше которого — верхние ``share`` анкет (ближайшая
        доля из обучения; нет таблицы — порог зоны LIKE)."""
        if not self.load():
            return None
        if not self._like_by_share:
            return self._like_thr
        nearest = min(self._like_by_share, key=lambda k: abs(k - share))
        return self._like_by_share[nearest]

    def score(self, emb: np.ndarray) -> TasteVerdict | None:
        """Score по эмбеддингам фото карточки (строка = фото)."""
        self._maybe_reload()
        if emb is None or len(emb) == 0 or not self.load():
            return None
        mean = emb.mean(axis=0)
        norm = float((mean @ mean) ** 0.5) or 1.0
        z = float(mean @ self._weights_array(len(mean))) / norm + self._bias
        s = 1.0 / (1.0 + math.exp(-z))
        if s >= self._like_thr:
            verdict = "LIKE"
        elif s <= self._dislike_thr:
            verdict = "DISLIKE"
        else:
            verdict = "REVIEW"
        return TasteVerdict(score=s, verdict=verdict)

    def _weights_array(self, dim: int):  # noqa: ANN202 — np.ndarray
        import numpy as np  # noqa: PLC0415

        w = np.asarray(self._weights, dtype=np.float32)
        if w.shape[0] != dim:
            msg = f"размер весов {w.shape[0]} ≠ размер эмбеддинга {dim}"
            raise ValueError(msg)
        return w


class PhotoShadowScorer:
    """Один прогон CLIP по фото карточки → вердикт вкуса + вероятности пороков.

    Для теневого режима: результат пишется в photo_scores и ни на что не влияет.
    Синхронный (CLIP на CPU) — коллектор зовёт его через asyncio.to_thread.
    """

    def __init__(self, encoder, taste: PhotoTasteModel, vice=None) -> None:  # noqa: ANN001
        self._encoder = encoder
        self._taste = taste
        self._vice = vice

    @property
    def enabled(self) -> bool:
        return self._taste.enabled and not self._encoder.failed

    def like_threshold(self, share: float) -> float | None:
        return self._taste.like_threshold(share)

    def score_images(self, images: list[bytes]) -> dict | None:
        emb, index = self._encoder.encode_images(images)
        if emb is None:
            return None
        verdict = self._taste.score(emb)
        if verdict is None:
            return None
        import numpy as np  # noqa: PLC0415

        mean = emb.mean(axis=0)
        mean = mean / (float(np.linalg.norm(mean)) or 1.0)
        out = {
            "score": verdict.score,
            "verdict": verdict.verdict,
            "n_photos": len(index),
            "model_version": self._taste.version,
            "smoke": None,
            "alcohol": None,
            # Отпечаток для переобучения (photo_embeddings): float32 LE.
            "embedding": mean.astype("<f4").tobytes(),
        }
        if self._vice is not None:
            try:
                probs = self._vice.probs_from_embeddings(emb)
                if probs:
                    out["smoke"] = max(p.get("smoke", 0.0) for p in probs)
                    out["alcohol"] = max(p.get("alcohol", 0.0) for p in probs)
            except Exception as e:
                logger.debug(f"PhotoShadow: вероятности пороков не посчитаны: {e!r}")
        return out
