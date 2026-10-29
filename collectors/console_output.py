# Консольный вывод коллектора (Rich) и сериализация entities/reply_markup для RAW.
# Миксин DvinchikCollector (вынесен из collectors/dvinchik_collector.py без изменения логики).

from __future__ import annotations

import json

from rich.panel import Panel
from rich.table import Table

from collectors.leo_markers import console
from models.raw import MessageType


class ConsoleOutputMixin:
    """Печать сообщений/анкет/решений и сериализация разметки (миксин DvinchikCollector)."""

    def _serialize_entities(self, msg: object) -> str:
        """Сериализует entities сообщения в JSON."""
        if not msg.entities:
            return "[]"
        entities = []
        for e in msg.entities:
            entities.append({
                "type": type(e).__name__,
                "offset": e.offset,
                "length": e.length,
            })
        return json.dumps(entities, ensure_ascii=False)

    def _serialize_reply_markup(self, msg: object) -> str:
        """Сериализует кнопки сообщения (reply_markup) в JSON.

        Read-only разведка слоя действий: наличие inline-кнопок с callback_data
        на анкете говорит о том, как ставится LIKE (кнопка на сообщении).
        Никаких действий не вызывается. Ошибки/отсутствие разметки → "[]",
        чтобы RAW-save никогда не падал из-за незнакомой разметки.
        """
        markup = getattr(msg, "reply_markup", None)
        rows = getattr(markup, "rows", None) if markup is not None else None
        if not rows:
            return "[]"
        result: list[list[dict[str, str]]] = []
        for row in rows:
            buttons = getattr(row, "buttons", None) or []
            row_items = []
            for btn in buttons:
                item: dict[str, str] = {
                    "text": str(getattr(btn, "text", "") or ""),
                    "type": type(btn).__name__,
                }
                cb = getattr(btn, "callback_data", None)
                if cb:
                    if isinstance(cb, bytes):
                        item["callback_data"] = cb.decode("utf-8", errors="replace")
                    else:
                        item["callback_data"] = str(cb)
                url = getattr(btn, "url", None)
                if url:
                    item["url"] = str(url)
                row_items.append(item)
            if row_items:
                result.append(row_items)
        return json.dumps(result, ensure_ascii=False)

    def _print_message(
        self,
        chat_id: int,
        message_id: int,
        sender_name: str,
        sender_username: str,
        text: str,
        msg_date: str,
        media_type: str,
        msg_type: MessageType,
        buttons: str = "[]",
    ) -> None:
        """Красивый вывод нового сообщения в консоль."""
        is_dvinchik = (
            self._dvinchik_chat_id != 0
            and chat_id == self._dvinchik_chat_id
        )
        source = "[green]Дайвинчик[/green]" if is_dvinchik else f"chat={chat_id}"

        type_color = {
            MessageType.PROFILE: "yellow",
            MessageType.MATCH: "green",
            MessageType.MEDIA_ONLY: "cyan",
            MessageType.SERVICE: "dim",
            MessageType.UNKNOWN: "red",
        }.get(msg_type, "white")

        table = Table(show_header=False, border_style="dim", padding=(0, 1))
        table.add_column("Key", style="bold cyan", width=14)
        table.add_column("Value")
        table.add_row("Source", source)
        table.add_row("Message ID", str(message_id))
        table.add_row("Date", msg_date[:19])
        table.add_row("Type", f"[{type_color}]{msg_type.value}[/{type_color}]")
        table.add_row(
            "Sender",
            f"{sender_name} (@{sender_username})" if sender_username else sender_name,
        )
        if media_type:
            table.add_row("Media", media_type)
        if text:
            preview = text[:200] + ("..." if len(text) > 200 else "")
            table.add_row("Text", preview)
        if buttons and buttons != "[]":
            table.add_row("Buttons", self._render_buttons(buttons))

        console.print(Panel(table, title="[bold]NEW MESSAGE[/bold]", border_style="blue"))

    def _render_buttons(self, buttons_json: str) -> str:
        """Читаемое представление кнопок: 'текст(callback) | текст(url)'.

        Demo-формат для разведки: callback_data может быть любым байтовым
        payload, поэтому показываем как есть (полезно для реверса кнопки LIKE).
        """
        try:
            rows = json.loads(buttons_json)
        except (ValueError, TypeError):
            return buttons_json
        lines = []
        for row in rows or []:
            cells = []
            for btn in row or []:
                text = btn.get("text", "")
                cb = btn.get("callback_data")
                url = btn.get("url")
                if cb:
                    cells.append(f"{text} ({cb})")
                elif url:
                    cells.append(f"{text} [{url}]")
                else:
                    cells.append(text)
            lines.append(" | ".join(cells))
        return " / ".join(lines)

    def _print_profile_stored(self, profile: object, is_new: bool = True) -> None:
        """Красивый вывод сохранённого профиля (Stage 2 format)."""
        status_color = "green" if is_new else "cyan"
        status_label = "NEW" if is_new else "SEEN"
        title = f"[bold {status_color}]PROFILE[/]"

        table = Table(show_header=False, border_style="dim", padding=(0, 1))
        table.add_column("Key", style="bold yellow", width=18)
        table.add_column("Value")

        if hasattr(profile, "id") and profile.id:
            table.add_row("Profile ID", str(profile.id))
        if hasattr(profile, "name") and profile.name:
            table.add_row("Name", profile.name)
        if hasattr(profile, "age") and profile.age:
            table.add_row("Age", str(profile.age))
        if hasattr(profile, "normalized_city") and profile.normalized_city:
            table.add_row("City", profile.normalized_city)
        if hasattr(profile, "status"):
            table.add_row("Status", f"[{status_color}]{profile.status}[/{status_color}]")
        if hasattr(profile, "first_seen_at") and profile.first_seen_at:
            table.add_row("First seen", profile.first_seen_at[:19])
        if hasattr(profile, "last_seen_at") and profile.last_seen_at:
            table.add_row("Last seen", profile.last_seen_at[:19])
        if hasattr(profile, "source_message_id") and profile.source_message_id:
            table.add_row("Source message", str(profile.source_message_id))
        if hasattr(profile, "message_count") and profile.message_count:
            table.add_row("Messages", str(profile.message_count))

        console.print(Panel(table, title=title, border_style="yellow"))

    def _print_profile(self, profile: object) -> None:
        """Красивый вывод распарсенной анкеты (без сохранения)."""
        table = Table(show_header=False, border_style="dim", padding=(0, 1))
        table.add_column("Key", style="bold yellow", width=18)
        table.add_column("Value")

        if hasattr(profile, "name") and profile.name:
            table.add_row("Name", profile.name)
        if hasattr(profile, "age") and profile.age:
            table.add_row("Age", str(profile.age))
        if hasattr(profile, "raw_city") and profile.raw_city:
            table.add_row("Raw City", profile.raw_city)
        if hasattr(profile, "normalized_city") and profile.normalized_city:
            table.add_row("Normalized City", profile.normalized_city)
        if hasattr(profile, "filter_result"):
            color = "green" if profile.filter_result == "FILTER_MATCH" else "red"
            table.add_row("Filter", f"[{color}]{profile.filter_result}[/{color}]")
        if hasattr(profile, "description") and profile.description:
            desc = profile.description[:300]
            table.add_row("Description", desc)

        console.print(Panel(table, title="[bold yellow]PROFILE[/bold yellow]", border_style="yellow"))

    def _print_match(self, match: object) -> None:
        """Красивый вывод сообщения о матче."""
        table = Table(show_header=False, border_style="dim", padding=(0, 1))
        table.add_column("Key", style="bold green", width=18)
        table.add_column("Value")

        if hasattr(match, "name"):
            table.add_row("Name", match.name)
        if hasattr(match, "telegram_username"):
            table.add_row("Telegram", f"@{match.telegram_username}")
        if hasattr(match, "telegram_url"):
            table.add_row("URL", match.telegram_url)
        table.add_row("Action", "[yellow]NONE — OBSERVE MODE[/yellow]")

        console.print(Panel(table, title="[bold green]MATCH DETECTED[/bold green]", border_style="green"))

    def _print_filter_result(self, profile: object, result: object) -> None:
        """Красивый вывод результата фильтрации."""
        if result is None:
            return

        decision = result.decision if hasattr(result, "decision") else "UNKNOWN"
        reasons = result.reasons if hasattr(result, "reasons") else []

        color_map = {
            "PASS": "green",
            "REJECT": "red",
            "REVIEW": "yellow",
        }
        color = color_map.get(str(decision), "white")

        reason_icons = {
            "AGE_OK": "[green]✓[/green]",
            "CITY_OK": "[green]✓[/green]",
            "AGE_OUT_OF_RANGE": "[red]✗[/red]",
            "CITY_OUT_OF_RANGE": "[red]✗[/red]",
            "AGE_UNKNOWN": "[yellow]?[/yellow]",
            "CITY_UNKNOWN": "[yellow]?[/yellow]",
            "INSUFFICIENT_DATA": "[yellow]?[/yellow]",
        }

        table = Table(show_header=False, border_style="dim", padding=(0, 1))
        table.add_column("Key", style="bold cyan", width=18)
        table.add_column("Value")

        if hasattr(profile, "id"):
            table.add_row("Profile", f"#{profile.id}")
        if hasattr(profile, "name"):
            table.add_row("Name", profile.name)
        if hasattr(profile, "age") and profile.age:
            table.add_row("Age", str(profile.age))
        if hasattr(profile, "normalized_city") and profile.normalized_city:
            table.add_row("City", profile.normalized_city)

        table.add_row("Decision", f"[{color}]{decision}[/{color}]")

        reasons_text = []
        for r in reasons:
            icon = reason_icons.get(str(r), "?")
            reasons_text.append(f"  {icon} {r}")
        if reasons_text:
            table.add_row("Reasons", "\n".join(reasons_text))

        console.print(Panel(
            table,
            title=f"[bold {color}]FILTER RESULT[/]",
            border_style=color,
        ))

    def _print_ai_decision(self, profile: object, decision: object) -> None:
        """Красивый вывод решения Decision Engine (детерминированный scoring)."""
        color_map = {
            "LIKE": "green",
            "REVIEW": "yellow",
            "DISLIKE": "red",
        }
        color = color_map.get(decision.decision.value, "white")

        table = Table(show_header=False, border_style="dim", padding=(0, 1))
        table.add_column("Key", style="bold magenta", width=18)
        table.add_column("Value")

        if hasattr(profile, "id"):
            table.add_row("Profile", f"#{profile.id}")
        if hasattr(profile, "name"):
            table.add_row("Name", profile.name)

        table.add_row("Filter", "PASS")
        table.add_row("Score", f"{decision.combined_score:.2f}")
        table.add_row(
            "Decision",
            f"[bold {color}]{decision.decision.value}[/bold {color}]",
        )
        table.add_row(
            "Mode",
            self._mode_label,
        )

        if decision.reasons:
            reasons_text = "\n".join(f"  • {r}" for r in decision.reasons)
            table.add_row("Reasons", reasons_text)
        if decision.scoring_version:
            table.add_row("Version", decision.scoring_version)

        console.print(Panel(
            table,
            title=f"[bold {color}]DECISION[/]",
            border_style=color,
        ))
