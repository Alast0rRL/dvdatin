# Вывод ASCII-баннера и таблицы статуса при запуске приложения.

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table

from app.config import AppConfig

APP_NAME = "DvAI"
APP_VERSION = "0.6"


def print_banner(config: AppConfig, db_ok: bool, session_status: str) -> None:
    """Выводит баннер приложения и таблицу статуса.

    Args:
        config: Конфигурация приложения.
        db_ok: Статус подключения к БД.
        session_status: Статус Telegram-сессии.
    """
    banner_text = (
        "[bold cyan]"
        "  ____           _    ____ ___ _     _   \n"
        " |  _ \\  ___  __| |  |  _ \\_ _| |   | |  \n"
        " | | | |/ _ \\/ _` |  | | | | || |   | |  \n"
        " | |_| |  __/ (_| |  | |_| | || |___| |__\n"
        " |____/ \\___|\\__,_|  |____/___|_____|____|[/bold cyan]"
    )

    from rich.console import Console

    console = Console(force_terminal=True)
    console.print(Panel(banner_text, title=f"v{APP_VERSION}", expand=False))

    table = Table(show_header=False, border_style="dim")
    table.add_column("Параметр", style="bold")
    table.add_column("Значение")

    mode_color = {
        "OBSERVE": "green",
        "SEMI_AUTO": "yellow",
        "AUTO": "red",
    }.get(config.project.mode.value, "white")

    table.add_row("Mode", f"[{mode_color}]{config.project.mode.value}[/{mode_color}]")
    table.add_row("Database", "[green]OK[/green]" if db_ok else "[red]ERROR[/red]")
    table.add_row("Telegram", session_status)
    cities = ", ".join(config.filters.city_allowed) if config.filters.city_allowed else "—"
    table.add_row("City", cities)
    table.add_row("Age", f"{config.filters.age_min}-{config.filters.age_max}")

    console.print(table)
