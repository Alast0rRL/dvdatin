# ClipEncoder: один экземпляр CLIP ViT-L-14 (laion2b) на CPU для всех задач по
# фото — проверка сигарет/алкоголя (photo_vice) и модель вкуса (photo_taste).
# Модель ~1.7 ГБ: грузится лениво и один раз; вызовы сериализуются локом
# (torch на CPU и так занимает заданное число потоков).
#
# Telegram-free: на вход байты изображений. torch/open_clip импортируются
# лениво — без них энкодер недоступен (лог-предупреждение), бот работает.

from __future__ import annotations

import io
import threading
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    import numpy as np

#: Модель и веса — те же, на которых обучена модель вкуса и подобраны пороги.
CLIP_ARCH = "ViT-L-14"
CLIP_WEIGHTS = "laion2b_s32b_b82k"


class ClipEncoder:
    """Ленивая обёртка над CLIP: эмбеддинги фото и фраз (L2-нормированные)."""

    def __init__(self, threads: int = 4) -> None:
        self._threads = max(1, int(threads))
        self._lock = threading.Lock()
        self._loaded = False
        self._failed = False
        self._model = None
        self._preprocess = None
        self._tokenizer = None

    @property
    def failed(self) -> bool:
        return self._failed

    def load(self) -> bool:
        """Загружает модель (долго, ~10–30 с). Повторные вызовы — no-op."""
        if self._failed:
            return False
        with self._lock:
            if self._loaded:
                return True
            try:
                import open_clip  # noqa: PLC0415 — тяжёлый импорт только при включении
                import torch  # noqa: PLC0415

                torch.set_num_threads(self._threads)
                model, _, preprocess = open_clip.create_model_and_transforms(
                    CLIP_ARCH, pretrained=CLIP_WEIGHTS, device="cpu",
                )
                model.eval()
                self._model = model
                self._preprocess = preprocess
                self._tokenizer = open_clip.get_tokenizer(CLIP_ARCH)
                self._loaded = True
                logger.info(f"CLIP {CLIP_ARCH} загружен (CPU, {self._threads} потоков)")
                return True
            except Exception as e:
                self._failed = True
                logger.warning(f"CLIP недоступен — проверки фото выключены: {e!r}")
                return False

    def encode_texts(self, texts: list[str]) -> np.ndarray | None:
        if not self.load():
            return None
        import torch  # noqa: PLC0415

        with self._lock, torch.no_grad():
            emb = self._model.encode_text(self._tokenizer(texts)).float()
        return (emb / emb.norm(dim=-1, keepdim=True)).numpy()

    def encode_images(self, images: list[bytes]) -> tuple[np.ndarray | None, list[int]]:
        """Эмбеддинги читаемых фото + их индексы во входном списке."""
        if not images or not self.load():
            return None, []
        import torch  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415

        tensors, index = [], []
        for i, data in enumerate(images):
            try:
                img = Image.open(io.BytesIO(data)).convert("RGB")
                tensors.append(self._preprocess(img))
                index.append(i)
            except Exception as e:
                logger.debug(f"CLIP: фото #{i} не читается: {e!r}")
        if not tensors:
            return None, []
        with self._lock, torch.no_grad():
            emb = self._model.encode_image(torch.stack(tensors)).float()
        return (emb / emb.norm(dim=-1, keepdim=True)).numpy(), index
