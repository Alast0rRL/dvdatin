# ReviewBot: Telegram UI для Human Review (Stage 6).
# Единственный слой Stage 6, который импортирует Telethon.
#
# Зависимости (только вниз):
#   Telegram UI → ReviewService / AnalyticsService → Database
#
# ПРАВИЛА:
# - Только OBSERVE/REVIEW. НИКАКИХ Telegram-действий с профилями
#   (нет LIKE/DISLIKE/swipe/сообщений).
# - Callback data содержит только идентификаторы (review:action:<ai_decision_id>).
# - Ошибки Telegram UI не должны ломать Database/ReviewService/Analytics.

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from telethon import Button, events

if TYPE_CHECKING:
    from telethon import TelegramClient

    from app.config import AppConfig
    from services.analytics_service import AnalyticsService
    from services.review_service import ReviewItem, ReviewService

_SEP = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

_CALLBACK_PREFIX = "review:"
_SORT_PREFIX = "disagreement:sort:"


class ReviewBot:
    """Регистрирует команды и inline-кнопки Human Review."""

    def __init__(
        self,
        client: TelegramClient,
        config: AppConfig,
        review_service: ReviewService,
        analytics_service: AnalyticsService,
    ) -> None:
        self._client = client
        self._config = config
        self._review = review_service
        self._analytics = analytics_service

    def register(self) -> None:
        """Регистрирует обработчики команд и callback-запросов."""
        self._client.add_event_handler(
            self._cmd_review, events.NewMessage(pattern=r"/review\s*$"),
        )
        self._client.add_event_handler(
            self._cmd_profile, events.NewMessage(pattern=r"/profile\s+(\d+)"),
        )
        self._client.add_event_handler(
            self._cmd_stats, events.NewMessage(pattern=r"/stats\s*$"),
        )
        self._client.add_event_handler(
            self._cmd_ai_stats, events.NewMessage(pattern=r"/ai_stats\s*$"),
        )
        self._client.add_event_handler(
            self._cmd_disagreements, events.NewMessage(pattern=r"/disagreements"),
        )
        self._client.add_event_handler(self._on_callback, events.CallbackQuery())
        logger.info("ReviewBot registered: /review /profile /stats /ai_stats /disagreements")

    # ── Команды ──────────────────────────────────────────────────────

    async def _cmd_review(self, event: events.NewMessage.Event) -> None:
        try:
            item = await self._review.get_next()
            if item is None:
                await event.respond("Нет pending AI-решений для рецензии.")
                return
            await event.respond(self._render_review(item), buttons=self._review_buttons(item.ai_decision_id))
        except Exception as e:
            logger.error(f"Review UI error (/review): {e}")
            await event.respond("Ошибка при загрузке рецензии.")

    async def _cmd_profile(self, event: events.NewMessage.Event) -> None:
        try:
            match = event.pattern_match
            profile_id = int(match.group(1))
            text = await self._render_profile(profile_id)
            await event.respond(text)
        except Exception as e:
            logger.error(f"Review UI error (/profile): {e}")
            await event.respond("Ошибка при загрузке профиля.")

    async def _cmd_stats(self, event: events.NewMessage.Event) -> None:
        try:
            text = await self._render_stats()
            await event.respond(text)
        except Exception as e:
            logger.error(f"Review UI error (/stats): {e}")
            await event.respond("Ошибка при загрузке статистики.")

    async def _cmd_ai_stats(self, event: events.NewMessage.Event) -> None:
        try:
            text = await self._render_ai_stats()
            await event.respond(text)
        except Exception as e:
            logger.error(f"Review UI error (/ai_stats): {e}")
            await event.respond("Ошибка при загрузке AI-статистики.")

    async def _cmd_disagreements(self, event: events.NewMessage.Event) -> None:
        sort = "newest"
        match = event.pattern_match
        if match and match.group(1):
            sort = match.group(1).strip().lower()
        await self._send_disagreements(event, sort)

    async def _send_disagreements(
        self, event: events.NewMessage.Event, sort: str,
    ) -> None:
        try:
            text = await self._render_disagreements(sort)
            buttons = [
                [Button.inline("NEWEST", data=b"disagreement:sort:newest"),
                 Button.inline("SCORE", data=b"disagreement:sort:score"),
                 Button.inline("CONFIDENCE", data=b"disagreement:sort:confidence")],
            ]
            await event.respond(text, buttons=buttons)
        except Exception as e:
            logger.error(f"Review UI error (/disagreements): {e}")
            await event.respond("Ошибка при загрузке расхождений.")

    # ── Callback (inline кнопки) ─────────────────────────────────────

    async def _on_callback(self, event: events.CallbackQuery.Event) -> None:
        raw = event.data.decode("utf-8", errors="replace")
        try:
            if raw.startswith(_CALLBACK_PREFIX):
                handler, ai_id = self._parse_review_callback(raw)
                if handler == "next":
                    item = await self._review.get_next()
                    if item is None:
                        await event.edit("Нет pending AI-решений для рецензии.")
                        return
                    await event.edit(
                        self._render_review(item),
                        buttons=self._review_buttons(item.ai_decision_id),
                    )
                    return
                await self._handle_review_decision(event, handler, int(ai_id))
                return

            if raw.startswith(_SORT_PREFIX):
                sort = raw.split(":", 2)[2]
                await self._refresh_disagreements(event, sort)
                return
        except Exception as e:
            logger.error(f"Review UI callback error: {e}")
            try:
                await event.answer("Ошибка обработки", alert=True)
            except Exception:
                pass

    async def _handle_review_decision(
        self,
        event: events.CallbackQuery.Event,
        handler: str,
        ai_decision_id: int,
    ) -> None:
        decision_map = {"approve": "APPROVE", "reject": "REJECT", "skip": "SKIP"}
        decision = decision_map[handler]

        profile_id = await self._review.resolve_ai_decision(ai_decision_id)
        if profile_id is None:
            await event.answer("AI-решение не найдено", alert=True)
            return

        try:
            result = await self._review.save_decision(
                profile_id, ai_decision_id, decision,
            )
        except ValueError as e:
            await event.answer(str(e), alert=True)
            return

        text = (
            f"Human decision saved.\n"
            f"Agreement: {result.agreement}\n\n"
            f"[ NEXT ]"
        )
        await event.edit(
            text,
            buttons=[[Button.inline("NEXT", data=b"review:next")]],
        )

    @staticmethod
    def _parse_review_callback(raw: str) -> tuple[str, str]:
        """Разбирает review:action:<ai_decision_id> или review:next."""
        parts = raw.split(":")
        action = parts[1] if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else ""
        return action, arg

    async def _refresh_disagreements(
        self, event: events.CallbackQuery.Event, sort: str,
    ) -> None:
        text = await self._render_disagreements(sort)
        buttons = [
            [Button.inline("NEWEST", data=b"disagreement:sort:newest"),
             Button.inline("SCORE", data=b"disagreement:sort:score"),
             Button.inline("CONFIDENCE", data=b"disagreement:sort:confidence")],
        ]
        await event.edit(text, buttons=buttons)

    # ── Рендер ───────────────────────────────────────────────────────

    def _review_buttons(self, ai_decision_id: int) -> list:
        return [
            [Button.inline("APPROVE", data=f"review:approve:{ai_decision_id}".encode()),
             Button.inline("REJECT", data=f"review:reject:{ai_decision_id}".encode()),
             Button.inline("SKIP", data=f"review:skip:{ai_decision_id}".encode())],
        ]

    @staticmethod
    def _render_review(item: ReviewItem) -> str:
        p = item.profile
        lines = [
            _SEP,
            "AI REVIEW",
            _SEP,
            "",
            f"Profile #{p.id}",
            "",
            f"{p.name}, {p.age}",
            p.normalized_city or (p.raw_city or "—"),
        ]
        if p.description:
            lines += ["", "Description:", p.description[:200]]
        lines += ["", _SEP, "", "FILTER", "", item.filter_decision or "—", "",
                  _SEP, "", "AI SCORING", "",
                  f"Combined:   {item.combined_score:.3f}",
                  f"Confidence: {item.confidence:.2f}", "",
                  _SEP, "", "AI DECISION:", "", item.ai_decision, ""]
        if item.reasons:
            lines += ["Reasons:"]
            for r in item.reasons:
                lines.append(f"  + {r}")
        lines += ["", _SEP, "",
                  f"Scoring: {item.scoring_version}", "",
                  _SEP]
        return "\n".join(lines)

    async def _render_profile(self, profile_id: int) -> str:
        from models.profile import ProfileStatus

        details = await self._review.get_review_details(profile_id)
        if details is None:
            return f"Профиль #{profile_id} не найден."

        profile = details["profile"]
        lines = [f"Profile #{profile['id']}", "",
                 f"{profile.get('name','')}, {profile.get('age','')}",
                 profile.get("normalized_city") or profile.get("raw_city", "") or "—",
                 "Status: " + profile.get("status", ProfileStatus.NEW.value)]

        latest_filter = details["latest_filter"]
        lines += ["", _SEP, "FILTER"]
        lines.append(
            latest_filter["decision"] if latest_filter else "—"
        )

        ai = details["latest_ai"]
        lines += ["", _SEP, "LATEST AI DECISION"]
        if ai:
            lines += [f"Decision: {ai['decision']}",
                      f"Score:    {ai['combined_score']:.3f}",
                      f"Confidence: {ai['confidence']:.2f}",
                      f"Version:  {ai.get('scoring_version','')}"]
        else:
            lines.append("—")

        human = details["latest_human"]
        lines += ["", _SEP, "LATEST HUMAN DECISION"]
        if human:
            lines += [f"Human:    {human['decision']}",
                      f"Agreement: {human['agreement']}"]
        else:
            lines.append("—")

        history = details["history"]
        lines += ["", _SEP, "HISTORY"]
        if history:
            for h in history:
                lines.append(
                    f"#{h['ai_decision_id']} → {h['decision']} "
                    f"({h['agreement']}) @ {h['created_at'][:19]}"
                )
        else:
            lines.append("—")

        lines += ["", _SEP]
        return "\n".join(lines)

    async def _render_stats(self) -> str:
        overview = await self._analytics.get_overview()
        agreement = await self._analytics.get_agreement_stats()
        pending = await self._analytics.get_pending_count()

        ai = overview["ai"]["counts"]
        human = overview["human"]
        rate = agreement["agreement_rate"]
        rate_txt = f"{(rate*100):.0f}%" if rate is not None else "null"

        lines = [
            _SEP, "STATS", _SEP, "",
            f"Profiles: {overview['profiles']}", "",
            "Filter:", "",
            f"  PASS      {overview['filter']['PASS']}",
            f"  REVIEW    {overview['filter']['REVIEW']}",
            f"  REJECT    {overview['filter']['REJECT']}", "",
            "AI:", "",
            f"  LIKE      {ai.get('LIKE',0)}",
            f"  REVIEW    {ai.get('REVIEW',0)}",
            f"  DISLIKE   {ai.get('DISLIKE',0)}", "",
            "Human:", "",
            f"  APPROVE   {human.get('APPROVE',0)}",
            f"  REJECT    {human.get('REJECT',0)}",
            f"  SKIP      {human.get('SKIP',0)}", "",
            _SEP, "AI/Human:", "",
            f"  Agreement:   {agreement['agreement']}",
            f"  Disagreement:{agreement['disagreement']}",
            f"  Unresolved:  {agreement['unresolved']}",
            f"  Rate:        {rate_txt}",
            f"  Pending:     {pending}",
            "",
            _SEP,
        ]
        return "\n".join(lines)

    async def _render_ai_stats(self) -> str:
        stats = await self._analytics.get_ai_stats()
        avg = stats["average"]
        c = stats["counts"]
        lines = [
            _SEP, "AI STATS", _SEP, "",
            f"Total AI decisions: {stats['total']}", "",
            f"LIKE:    {c.get('LIKE',0)}",
            f"REVIEW:  {c.get('REVIEW',0)}",
            f"DISLIKE: {c.get('DISLIKE',0)}", "",
            "Average:", "",
            f"  Combined:     {_fmt(avg['combined_score'])}",
            f"  Confidence:   {_fmt(avg['confidence'])}", "",
            f"AI decisions reviewed: {stats['reviewed']}",
            f"AI decisions pending:  {stats['pending']}", "",
            _SEP,
        ]
        return "\n".join(lines)

    async def _render_disagreements(self, sort: str) -> str:
        items = await self._analytics.get_disagreements(sort=sort)
        lines = [
            _SEP, "DISAGREEMENTS", _SEP,
            f"Sort: {sort.upper()}", "",
        ]
        if not items:
            lines.append("Нет расхождений (Human=REJECT).")
        for it in items:
            lines.append(
                f"#{it['profile_id']}\n"
                f"AI: {it['ai_decision']}\n"
                f"Score: {it['combined_score']:.2f}\n"
                f"Confidence: {it['confidence']:.2f}\n"
                f"Human: {it['human_decision']}\n"
                f"Reviewed: {it['reviewed_at'][:19]}\n"
            )
        lines.append(_SEP)
        return "\n".join(lines)


def _fmt(value: float | None) -> str:
    """Форматирует число или прочерк."""
    if value is None:
        return "—"
    return f"{value:.3f}"
