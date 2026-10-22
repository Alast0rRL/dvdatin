# PhotoViceDetector: есть ли на фото анкеты сигарета/вейп/кальян или алкоголь.
#
# Правило владельца (2026-10-22): такое на фото → сразу 👎. Детектор —
# zero-shot CLIP (services/clip_encoder.py): фото сравнивается с фразами
# «порок» и с похожими безобидными сценами (кофе, телефон, газировка),
# вероятности считаются softmax'ом по всем фразам и суммируются по классу.
# Безобидные фразы обязательны: без них любой стакан или рука у лица = «порок».
#
# Пороги строгие (config.photo_check): офлайн на 2362 фото анкет уверенно
# ловятся только явные случаи (~4 из 5 срабатываний верные), остальное —
# тёмные фото, кола, кафе. Метки владельца «🚬/🍷 на фото» со страницы
# разметки (photo_ratings.rating = 'VICE') — данные для будущего дообучения.
#
# Telegram-free: принимает байты изображений или готовые эмбеддинги.

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

    from app.config import PhotoCheckConfig
    from services.clip_encoder import ClipEncoder

#: Классы и фразы. Пороги подобраны именно под этот набор — менять вместе.
VICE_PROMPTS: dict[str, list[str]] = {
    "smoke": [
        "a photo of a girl smoking a cigarette",
        "a girl holding a lit cigarette",
        "a girl vaping with an electronic cigarette",
        "a girl smoking a hookah",
    ],
    "alcohol": [
        "a girl holding a glass of wine",
        "a girl drinking beer from a bottle",
        "a girl holding a cocktail glass at a bar",
        "bottles of alcohol on a table",
        "a girl holding a shot of vodka",
    ],
    "neutral": [
        "a selfie of a girl",
        "a girl holding a phone taking a mirror selfie",
        "a girl drinking coffee",
        "a girl holding a cup of tea",
        "a girl drinking juice",
        "a girl eating food",
        "a girl holding a pen",
        "a portrait photo of a girl",
        "a girl with a can of soda",
        "a girl drinking water from a bottle",
    ],
}

#: Классы, которые ведут к скипу.
VICE_KINDS = ("smoke", "alcohol")


@dataclass(frozen=True)
class ViceHit:
    """Найденный «порок» на фото: класс, вероятность, номер фото в наборе."""

    kind: str
    prob: float
    photo_index: int

    def reason(self) -> str:
        """Код причины для ai_decisions (переводится в services/reason_labels)."""
        return f"PHOTO_VICE:{self.kind}:{self.prob:.3f}"


class PhotoViceDetector:
    """Zero-shot детектор поверх общего ClipEncoder."""

    def __init__(self, config: PhotoCheckConfig, encoder: ClipEncoder | None = None) -> None:
        self._cfg = config
        self._encoder = encoder
        self._text: np.ndarray | None = None
        self._labels: list[str] = []

    @property
    def enabled(self) -> bool:
        return (
            bool(self._cfg.enabled)
            and self._encoder is not None
            and not self._encoder.failed
        )

    @property
    def max_photos(self) -> int:
        return max(1, int(self._cfg.max_photos))

    def thresholds(self) -> dict[str, float]:
        return {"smoke": self._cfg.smoke_threshold, "alcohol": self._cfg.alcohol_threshold}

    def load(self) -> bool:
        """Загружает CLIP и эмбеддинги фраз (один раз)."""
        if self._text is not None:
            return True
        if self._encoder is None:
            return False
        labels: list[str] = []
        texts: list[str] = []
        for kind, phrases in VICE_PROMPTS.items():
            for phrase in phrases:
                labels.append(kind)
                texts.append(phrase)
        emb = self._encoder.encode_texts(texts)
        if emb is None:
            return False
        self._text, self._labels = emb, labels
        return True

    def probs_from_embeddings(self, emb: np.ndarray) -> list[dict[str, float]]:
        """Вероятности классов по готовым эмбеддингам фото (строка = фото)."""
        if not self.load():
            return []
        import numpy as np  # noqa: PLC0415

        logits = 100.0 * emb @ self._text.T
        logits -= logits.max(axis=1, keepdims=True)
        probs = np.exp(logits)
        probs /= probs.sum(axis=1, keepdims=True)
        labels = np.array(self._labels)
        return [
            {k: float(row[labels == k].sum()) for k in VICE_PROMPTS} for row in probs
        ]

    def class_probs(self, images: list[bytes]) -> list[dict[str, float]]:
        """Вероятности классов для каждого фото (битые фото → {})."""
        images = images[: self.max_photos]
        if self._encoder is None or not self.load():
            return []
        emb, index = self._encoder.encode_images(images)
        out: list[dict[str, float]] = [{} for _ in images]
        if emb is None:
            return out
        for probs, i in zip(self.probs_from_embeddings(emb), index):
            out[i] = probs
        return out

    def best_hit(self, probs_per_photo: list[dict[str, float]]) -> ViceHit | None:
        """Самый уверенный «порок» выше порога или None."""
        best: ViceHit | None = None
        thr = self.thresholds()
        for i, probs in enumerate(probs_per_photo):
            for kind in VICE_KINDS:
                p = probs.get(kind, 0.0)
                if p > thr[kind] and (best is None or p > best.prob):
                    best = ViceHit(kind=kind, prob=p, photo_index=i)
        return best

    def check(self, images: list[bytes]) -> ViceHit | None:
        """Первый уверенный «порок» на наборе фото или None."""
        if not self.enabled or not images:
            return None
        return self.best_hit(self.class_probs(images))

    async def check_async(self, images: list[bytes]) -> ViceHit | None:
        """То же, но в потоке — CLIP на CPU не блокирует event loop бота."""
        return await asyncio.to_thread(self.check, images)
