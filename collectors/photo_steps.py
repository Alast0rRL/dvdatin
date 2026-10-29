# Фото-шаги коллектора: фото карточки, теневой расчёт вкуса, PHOTO_TASTE/PHOTO_VICE, досчёт (backfill).
# Миксин DvinchikCollector (вынесен из collectors/dvinchik_collector.py без изменения логики).

from __future__ import annotations

import asyncio

from loguru import logger


class PhotoStepsMixin:
    """Фото карточек и фото-модели (миксин DvinchikCollector)."""

    @staticmethod
    def _is_card_video(msg: object) -> bool:
        """Видео анкеты (в Telegram — документ video/*): берём его превью-кадр."""
        if getattr(msg, "video", None):
            return True
        doc = getattr(msg, "document", None)
        return bool(doc) and str(getattr(doc, "mime_type", "") or "").startswith("video/")

    @classmethod
    def _has_card_media(cls, msg: object) -> bool:
        return bool(getattr(msg, "photo", None)) or cls._is_card_video(msg)

    async def _card_photos(self, msg: object, limit: int) -> list[bytes]:
        """Байты фото карточки (см. ``_card_photo_items``)."""
        return [data for _mid, data in await self._card_photo_items(msg, limit)]

    async def _card_photo_items(
        self, msg: object, limit: int,
    ) -> list[tuple[int, bytes]]:
        """Фото карточки ``[(message_id, байты)]``: само сообщение + остальной
        альбом (grouped_id).

        Альбом Leo приходит пачкой сообщений с соседними id, поэтому соседи
        берутся одним запросом. Видео-анкета (прод 2026-10-23: ~1 из 9
        карточек) — берётся превью-кадр видео (``thumb=-1``), иначе модель
        вкуса молчала и решало старое правило. Ошибки → то, что удалось скачать.
        """
        msgs = [msg]
        grouped_id = getattr(msg, "grouped_id", None)
        client = getattr(msg, "client", None)
        if grouped_id and client is not None:
            try:
                chat = await msg.get_input_chat()
                around = await client.get_messages(
                    chat, ids=list(range(msg.id - 9, msg.id + 10)),
                )
                album = [
                    m for m in around
                    if m is not None and getattr(m, "grouped_id", None) == grouped_id
                ]
                if album:
                    msgs = sorted(album, key=lambda m: m.id)
            except Exception as e:
                logger.debug(f"PhotoVice: альбом не прочитан: {e!r}")
        media = [m for m in msgs if self._has_card_media(m)][:limit]
        results = await asyncio.gather(
            *(
                m.download_media(file=bytes, thumb=-1)
                if self._is_card_video(m) and not getattr(m, "photo", None)
                else m.download_media(file=bytes)
                for m in media
            ),
            return_exceptions=True,
        )
        return [
            (m.id, bytes(r)) for m, r in zip(media, results)
            if isinstance(r, (bytes, bytearray)) and r
        ]

    async def _backfill_photo_bytes(self, photos: list[dict]) -> list[bytes]:
        """Байты фото для досчёта (см. ``_backfill_photo_items``)."""
        got = await self._backfill_photo_items(photos)
        return [got[ph["message_id"]] for ph in photos if ph["message_id"] in got]

    async def _backfill_photo_items(self, photos: list[dict]) -> dict[int, bytes]:
        """``{message_id: байты}``: фото из кэша media/; нет в кэше — скачиваем
        аккаунтом-получателем и кладём в кэш (тогда и на /rate они откроются
        сразу)."""
        from web.photos import _cache_filename  # noqa: PLC0415 — общая схема имён кэша

        out: dict[int, bytes] = {}
        missing: list[dict] = []
        for ph in photos:
            path = _cache_filename(ph["profile_id"], ph["message_id"], ph["account_session"])
            if path.exists():
                out[ph["message_id"]] = path.read_bytes()
            else:
                missing.append(ph)
        if not missing:
            return out
        client, _ = self._clients_for_session(missing[0]["account_session"])
        if client is None:
            return out
        try:
            msgs = await client.get_messages(
                missing[0]["chat_id"], ids=[ph["message_id"] for ph in missing],
            )
        except Exception as e:
            logger.debug(f"PhotoBackfill: get_messages: {e!r}")
            return out
        for ph, m in zip(missing, msgs or []):
            if m is None or not getattr(m, "photo", None):
                continue
            try:
                data = await m.download_media(file=bytes)
            except Exception as e:
                logger.debug(f"PhotoBackfill: download {ph['message_id']}: {e!r}")
                continue
            if not data:
                continue
            path = _cache_filename(ph["profile_id"], ph["message_id"], ph["account_session"])
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            except OSError:
                pass
            out[ph["message_id"]] = data
        return out

    async def run_photo_backfill(self) -> None:
        """Фоновый досчёт: анкеты без отпечатка фото → вердикт + отпечаток.

        Сначала НЕоценённые на /rate (для очереди «модель сомневается»), потом
        оценённые (для переобучения). Медленно и по одной, чтобы не мешать
        ленте: CLIP на CPU ~1.5 с на фото.
        """
        shadow = self._photo_shadow
        cfg = self._config.photo_taste
        if shadow is None or not cfg.enabled or not cfg.backfill:
            return
        await asyncio.sleep(60)  # даём боту подняться и загрузить CLIP
        done = 0
        while True:
            try:
                if not shadow.enabled:
                    await asyncio.sleep(self.PHOTO_BACKFILL_IDLE_SEC)
                    continue
                batch = await self._db.get_photo_backfill_batch(
                    limit=10, max_photos=max(1, int(cfg.max_photos)),
                )
                if not batch:
                    if done:
                        logger.info(f"PhotoBackfill: досчитано {done} анкет, жду новых")
                        done = 0
                    await asyncio.sleep(self.PHOTO_BACKFILL_IDLE_SEC)
                    continue
                for item in batch:
                    pid = item["profile_id"]
                    photos = [dict(ph, profile_id=pid) for ph in item["photos"]]
                    got = await self._backfill_photo_items(photos)
                    have = [ph for ph in photos if ph["message_id"] in got]
                    images = [got[ph["message_id"]] for ph in have]
                    # Вердикт — по первым max_photos фото (как живой расчёт),
                    # отпечатки — по всем фото, что видны на /rate.
                    first_ids = {
                        ph["message_id"] for ph in photos[: max(1, int(cfg.max_photos))]
                    }
                    score_first = sum(1 for ph in have if ph["message_id"] in first_ids)
                    result = (
                        await asyncio.to_thread(shadow.score_images, images, score_first)
                        if images and score_first else None
                    )
                    item_embs = (result or {}).get("item_embeddings") or []
                    by_id = {ph["message_id"]: e for ph, e in zip(have, item_embs)}
                    # Нет фото / не читается — NULL, чтобы не крутиться на нём.
                    await self._db.save_photo_item_embeddings(
                        pid, photos[0]["account_session"],
                        [(ph["message_id"], by_id.get(ph["message_id"])) for ph in photos],
                        (result or {}).get("model_version", ""),
                    )
                    if not result:
                        # Отметка «не получилось», чтобы не крутиться на ней.
                        await self._db.save_photo_embedding(pid, None, 0, "")
                    else:
                        await self._db.save_photo_embedding(
                            pid, result["embedding"], result["n_photos"],
                            result["model_version"],
                        )
                        first = photos[0]
                        await self._db.save_photo_score(
                            profile_id=pid,
                            account_session=first["account_session"],
                            chat_id=first["chat_id"],
                            telegram_message_id=first["message_id"],
                            score=result["score"],
                            verdict=result["verdict"],
                            smoke_prob=result.get("smoke"),
                            alcohol_prob=result.get("alcohol"),
                            n_photos=result["n_photos"],
                            model_version=result["model_version"],
                        )
                        done += 1
                        if done % 50 == 0:
                            logger.info(f"PhotoBackfill: досчитано {done} анкет")
                    await asyncio.sleep(max(0.0, float(cfg.backfill_pause_sec)))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"PhotoBackfill: {e!r}")
                await asyncio.sleep(60)

    def _remember_photo_result(self, key: tuple[str, int, int], result: dict) -> None:
        cache = getattr(self, "_photo_shadow_results", None)
        if cache is None:
            cache = self._photo_shadow_results = {}
        cache[key] = result
        while len(cache) > self.PHOTO_RESULTS_KEEP:
            cache.pop(next(iter(cache)))

    async def _photo_result_for(self, key: tuple[str, int, int] | None) -> dict | None:
        """Результат теневого расчёта карточки: готовый или дождаться идущего."""
        if key is None:
            return None
        ready = getattr(self, "_photo_shadow_results", {}).get(key)
        if ready is not None:
            return ready
        task = getattr(self, "_photo_shadow_pending", {}).get(key)
        if task is None:
            return None
        try:
            return await asyncio.wait_for(
                asyncio.shield(task), timeout=self.PHOTO_SHADOW_WAIT_SEC,
            )
        except Exception as e:
            logger.debug(f"PhotoShadow: результат карточки не дождались: {e!r}")
            return None

    async def _apply_photo_taste(
        self, decision: object, card_key: tuple[str, int, int],
    ) -> object:
        """Безопасная обёртка: любая ошибка → решение без изменений (лента не
        встаёт из-за фото-модели — прод-баг 2026-10-23)."""
        try:
            return await self._apply_photo_taste_impl(decision, card_key)
        except Exception as e:
            logger.error(f"PhotoTaste: сбой решения по фото, оставляю прежнее: {e!r}")
            return decision

    async def _apply_photo_taste_impl(
        self, decision: object, card_key: tuple[str, int, int],
    ) -> object:
        """Режим decide: LIKE/DISLIKE по модели вкуса (результат теневого расчёта
        этой карточки). Текстовые запреты и сильный плюс главнее; нет фото,
        CLIP недоступен или не дождались — решение не меняется (старое правило).
        """
        cfg = self._config.photo_taste
        shadow = self._photo_shadow
        if (
            not getattr(cfg, "decides", False)
            or shadow is None
            or self._decision_service is None
            or not self._decision_service.photo_taste_applies(decision)
        ):
            return decision
        result = await self._photo_result_for(card_key)
        if not result:
            # Нет вердикта по этой карточке (видео без кадра, не успели) — у
            # повторно показанной девушки есть отпечаток фото прошлого показа.
            try:
                # Отпечатки отдельных фото (если есть), иначе — анкеты целиком.
                blobs = await self._db.get_photo_item_embeddings(
                    decision.profile_id, limit=max(1, int(cfg.max_photos)),
                )
                if not blobs:
                    blob = await self._db.get_photo_embedding(decision.profile_id)
                    blobs = [blob] if blob else []
                result = shadow.score_embeddings(blobs) if blobs else None
            except Exception as e:
                logger.debug(f"PhotoTaste: отпечаток прошлого показа не прочитан: {e!r}")
                result = None
            if result:
                logger.info(
                    f"PhotoTaste: profile #{decision.profile_id} — по фото прошлого показа"
                )
        if not result:
            logger.info(
                f"PhotoTaste: profile #{decision.profile_id} — вердикта модели нет "
                "(нет фото / не успели), решает старое правило"
            )
            return decision
        try:
            threshold = shadow.like_threshold(float(cfg.like_share))
        except Exception as e:
            logger.warning(f"PhotoTaste: порог модели не получен: {e!r}")
            return decision
        if threshold is None:
            return decision
        try:
            return await self._decision_service.apply_photo_taste(
                decision, float(result["score"]), float(result["score"]) >= threshold,
                str(result.get("model_version") or ""),
            )
        except Exception as e:
            logger.error(f"PhotoTaste: не удалось записать решение: {e!r}")
            return decision

    async def _apply_photo_vice(
        self,
        decision: object,
        msg: object,
        card_key: tuple[str, int, int] | None = None,
    ) -> object:
        """Безопасная обёртка: любая ошибка → решение без изменений."""
        try:
            return await self._apply_photo_vice_impl(decision, msg, card_key)
        except Exception as e:
            logger.error(f"PhotoVice: сбой проверки фото, оставляю прежнее: {e!r}")
            return decision

    async def _apply_photo_vice_impl(
        self,
        decision: object,
        msg: object,
        card_key: tuple[str, int, int] | None = None,
    ) -> object:
        """LIKE → DISLIKE, если на фото карточки уверенно найдены сигарета/алкоголь.

        Если по карточке уже идёт теневой расчёт (он считает и вероятности
        пороков), берём его результат — CLIP на CPU ~1.5 с на фото, двойной
        прогон задерживал бы реакцию. Иначе считаем сами.
        Любая ошибка (нет torch, сеть, битое фото) — решение не меняется:
        проверка — дополнительный фильтр, бот без неё работает как раньше.
        """
        detector = self._photo_vice
        if (
            detector is None
            or not getattr(detector, "enabled", False)
            or msg is None
            or self._decision_service is None
        ):
            return decision
        try:
            hit = None
            shadow_result = await self._photo_result_for(card_key)
            if shadow_result and shadow_result.get("smoke") is not None:
                hit = detector.best_hit([{
                    "smoke": shadow_result.get("smoke") or 0.0,
                    "alcohol": shadow_result.get("alcohol") or 0.0,
                }])
            else:
                images = await self._card_photos(msg, detector.max_photos)
                hit = await detector.check_async(images)
        except Exception as e:
            logger.error(f"PhotoVice: проверка фото не удалась: {e!r}")
            return decision
        if hit is None:
            logger.debug(f"PhotoVice: profile #{decision.profile_id} — чисто")
            return decision
        try:
            return await self._decision_service.apply_photo_vice(decision, hit.reason())
        except Exception as e:
            logger.error(f"PhotoVice: не удалось записать решение: {e!r}")
            return decision

    def _schedule_photo_shadow(
        self,
        msg: object,
        profile_id: int,
        account_session: str,
        chat_id: int,
        message_id: int,
    ) -> None:
        """Ставит фоновый расчёт теневого вердикта по фото карточки.

        Не ждём: CLIP на CPU занимает секунды, а реакция Leo должна уйти
        быстро. Очередь ограничена photo_taste.max_pending — при быстром
        листании лишние карточки пропускаются (в лог DEBUG).
        """
        shadow = self._photo_shadow
        if shadow is None or msg is None or not self._has_card_media(msg):
            return
        try:
            if not shadow.enabled:
                return
        except Exception:
            return
        cap = max(1, int(self._config.photo_taste.max_pending))
        if len(self._photo_shadow_pending) >= cap:
            logger.debug(f"PhotoShadow: очередь полна, карточка {message_id} пропущена")
            return
        key = (account_session or "", chat_id, message_id)
        if key in self._photo_shadow_pending:
            return
        task = asyncio.create_task(
            self._photo_shadow_run(msg, profile_id, account_session, chat_id, message_id)
        )
        self._photo_shadow_pending[key] = task
        task.add_done_callback(lambda _t, k=key: self._photo_shadow_pending.pop(k, None))

    async def _photo_shadow_run(
        self,
        msg: object,
        profile_id: int,
        account_session: str,
        chat_id: int,
        message_id: int,
    ) -> dict | None:
        """Фото карточки → CLIP → вердикт вкуса → photo_scores. Ошибки — в лог.

        Возвращает результат (score/verdict/smoke/alcohol) — его переиспользует
        проверка сигарет/алкоголя для этой же карточки.
        """
        try:
            limit = max(1, int(self._config.photo_taste.max_photos))
            items = await self._card_photo_items(msg, limit)
            if not items:
                return None
            images = [data for _mid, data in items]
            result = await asyncio.to_thread(self._photo_shadow.score_images, images)
            if not result:
                return None
            if result.get("embedding"):
                await self._db.save_photo_embedding(
                    profile_id, result["embedding"], result["n_photos"],
                    result["model_version"],
                )
            item_embs = result.get("item_embeddings") or []
            if item_embs:
                await self._db.save_photo_item_embeddings(
                    profile_id, account_session or "",
                    [(mid, e) for (mid, _d), e in zip(items, item_embs)],
                    result["model_version"],
                )
            await self._db.save_photo_score(
                profile_id=profile_id,
                account_session=account_session or "",
                chat_id=chat_id,
                telegram_message_id=message_id,
                score=result["score"],
                verdict=result["verdict"],
                smoke_prob=result.get("smoke"),
                alcohol_prob=result.get("alcohol"),
                n_photos=result["n_photos"],
                model_version=result["model_version"],
            )
            logger.info(
                f"PhotoShadow: profile #{profile_id} → {result['verdict']} "
                f"(score {result['score']:.2f}, фото {result['n_photos']})"
            )
            self._remember_photo_result(
                (account_session or "", chat_id, message_id), result,
            )
            return result
        except Exception as e:
            logger.warning(f"PhotoShadow: profile #{profile_id}: {e!r}")
            return None
