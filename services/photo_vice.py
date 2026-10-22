# PhotoViceDetector: есть ли на фото анкеты сигарета/вейп/кальян или алкоголь.
#
# Правило владельца (2026-10-22): такое на фото → сразу 👎. Детектор —
# zero-shot CLIP ViT-L-14 (laion2b): фото сравнивается с фразами «порок» и с
# похожими безобидными сценами (кофе, телефон, газировка), вероятности
# считаются softmax'ом по всем фразам и суммируются по классу. Безобидные
# фразы обязательны: без них любой стакан или рука у лица = «порок».
#
# Пороги строгие (config.photo_check): офлайн на 2362 фото анкет уверенно
# ловятся только явные случаи (~4 из 5 срабатываний верные), остальное —
# тёмные фото, кола, кафе. Метки владельца «🚬/🍷 на фото» со страницы
# разметки (photo_ratings.rating = 'VICE') — данные для будущего дообучения.
#
# Telegram-free: принимает байты изображений. torch/open_clip импортируются
# лениво — без них детектор просто выключен (лог-предупреждение), бот работает.

from __future__ import annotations

import asyncio
import io
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from app.config import PhotoCheckConfig

#: Модель и веса CLIP (те же, на которых подбирались пороги).
CLIP_ARCH = "ViT-L-14"
CLIP_WEIGHTS = "laion2b_s32b_b82k"

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
    """Ленивая обёртка над CLIP: грузится один раз, считает в отдельном потоке."""

    def __init__(self, config: PhotoCheckConfig) -> None:
        self._cfg = config
        self._lock = threading.Lock()
        self._loaded = False
        self._failed = False
        self._model = None
        self._preprocess = None
        self._text = None      # np.ndarray [n_prompts, dim]
        self._labels: list[str] = []

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled) and not self._failed

    @property
    def max_photos(self) -> int:
        return max(1, int(self._cfg.max_photos))

    def thresholds(self) -> dict[str, float]:
        return {"smoke": self._cfg.smoke_threshold, "alcohol": self._cfg.alcohol_threshold}

    # ── Загрузка ──────────────────────────────────────────────────────

    def load(self) -> bool:
        """Загружает модель (долго, ~10–30 с). Повторные вызовы — no-op."""
        if not self._cfg.enabled or self._failed:
            return False
        with self._lock:
            if self._loaded:
                return True
            try:
                import open_clip  # noqa: PLC0415 — тяжёлый импорт только при включении
                import torch  # noqa: PLC0415

                torch.set_num_threads(max(1, int(self._cfg.threads)))
                model, _, preprocess = open_clip.create_model_and_transforms(
                    CLIP_ARCH, pretrained=CLIP_WEIGHTS, device="cpu",
                )
                model.eval()
                tokenizer = open_clip.get_tokenizer(CLIP_ARCH)
                labels: list[str] = []
                texts: list[str] = []
                for kind, phrases in VICE_PROMPTS.items():
                    for phrase in phrases:
                        labels.append(kind)
                        texts.append(phrase)
                with torch.no_grad():
                    emb = model.encode_text(tokenizer(texts)).float()
                    emb = emb / emb.norm(dim=-1, keepdim=True)
                self._model = model
                self._preprocess = preprocess
                self._text = emb.numpy()
                self._labels = labels
                self._loaded = True
                logger.info(f"PhotoVice: CLIP {CLIP_ARCH} загружен (CPU)")
                return True
            except Exception as e:
                self._failed = True
                logger.warning(
                    f"PhotoVice: проверка фото выключена — не удалось загрузить CLIP: {e!r}"
                )
                return False

    # ── Проверка ──────────────────────────────────────────────────────

    def class_probs(self, images: list[bytes]) -> list[dict[str, float]]:
        """Вероятности классов для каждого фото (битые фото пропускаются как {})."""
        if not self.load():
            return []
        import numpy as np  # noqa: PLC0415
        import torch  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415

        tensors, index = [], []
        for i, data in enumerate(images[: self.max_photos]):
            try:
                img = Image.open(io.BytesIO(data)).convert("RGB")
                tensors.append(self._preprocess(img))
                index.append(i)
            except Exception as e:
                logger.debug(f"PhotoVice: фото #{i} не читается: {e!r}")
        out: list[dict[str, float]] = [{} for _ in images[: self.max_photos]]
        if not tensors:
            return out
        with torch.no_grad():
            emb = self._model.encode_image(torch.stack(tensors)).float()
            emb = (emb / emb.norm(dim=-1, keepdim=True)).numpy()
        logits = 100.0 * emb @ self._text.T
        logits -= logits.max(axis=1, keepdims=True)
        probs = np.exp(logits)
        probs /= probs.sum(axis=1, keepdims=True)
        labels = np.array(self._labels)
        for row, i in enumerate(index):
            out[i] = {k: float(probs[row, labels == k].sum()) for k in VICE_PROMPTS}
        return out

    def check(self, images: list[bytes]) -> ViceHit | None:
        """Первый уверенный «порок» на наборе фото или None."""
        if not self.enabled or not images:
            return None
        best: ViceHit | None = None
        thr = self.thresholds()
        for i, probs in enumerate(self.class_probs(images)):
            for kind in VICE_KINDS:
                p = probs.get(kind, 0.0)
                if p > thr[kind] and (best is None or p > best.prob):
                    best = ViceHit(kind=kind, prob=p, photo_index=i)
        return best

    async def check_async(self, images: list[bytes]) -> ViceHit | None:
        """То же, но в потоке — CLIP на CPU не блокирует event loop бота."""
        return await asyncio.to_thread(self.check, images)

    async def preload(self) -> None:
        """Фоновая загрузка модели при старте, чтобы первая анкета не ждала."""
        if self._cfg.enabled:
            await asyncio.to_thread(self.load)
