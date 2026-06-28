"""
settings.py - Settings screen for YTDL-Pro.

Extracted from menu.py without modification.  All logic, prompts, options,
and config handling are identical to the original action_settings() function.

Public surface
--------------
run(cfg)
    Show the Settings sub-menu and handle all user input.
    Replaces the former action_settings(cfg) call in menu.py.
"""

from __future__ import annotations

from rich import box
from rich.console import Console
from rich.rule import Rule
from rich.table import Table

import file_browser
from app_config import DEFAULT_FOLDER, AppConfig
from downloader import FAILED_FILE, LINKS_FILE, Logger

console: Console = Console()
log: Logger = Logger()


def run(cfg: AppConfig) -> None:
    """
    Settings sub-menu (formerly ``action_settings`` in menu.py).

    Sub-options
    -----------
    1  Change download folder
    2  Change default .txt file
    3  Reset all settings to defaults
    4  Back to main menu
    """
    while True:
        console.print()
        console.print(Rule("[bold cyan]Settings[/]"))
        console.print()

        current_folder = cfg.last_download_folder or DEFAULT_FOLDER
        current_links = cfg.last_links_file or LINKS_FILE

        table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
        table.add_column("  #", style="bold cyan", justify="right", min_width=3)
        table.add_column("  Option", style="white")
        table.add_column("  Current", style="dim")

        table.add_row("1", "Change download folder", str(current_folder))
        table.add_row("2", "Change default .txt file", str(current_links))
        table.add_row("3", "Reset all settings to defaults", "")
        table.add_row("4", "Back to main menu", "")

        console.print(table)
        console.print()

        choice = _prompt("Choose option [1–4]")

        if choice == "1":
            # Per spec: Settings -> "Change download folder" launches the
            # file browser directly (not the Last-Used/Movies/Downloads
            # quick-pick menu used elsewhere).
            folder = file_browser.select_folder(start=cfg.last_download_folder)
            if folder is None:
                console.print("[yellow]  No folder selected — keeping the current one.[/]")
            else:
                folder.mkdir(parents=True, exist_ok=True)
                cfg.last_download_folder = folder
                cfg.save()
                console.print(f"[green]✔  Download folder updated to:[/] {folder}")

        elif choice == "2":
            # Per spec: Settings -> "Change default .txt file" launches the
            # file browser directly, in .txt-file-selection mode.
            current_default = cfg.last_links_file or LINKS_FILE
            console.print(f"  Current default: [cyan]{current_default}[/]")
            console.print()
            start_dir = current_default.parent if current_default.exists() else None
            new_path = file_browser.select_txt_file(start=start_dir)
            if new_path is None:
                console.print("[yellow]  No file selected — keeping the current default.[/]")
            else:
                cfg.last_links_file = new_path
                cfg.save()
                console.print(f"[green]✔  Default .txt file set to:[/] [cyan]{new_path}[/]")

        elif choice == "3":
            cfg.last_download_folder = None
            cfg.last_links_file = None
            cfg.ui_color = "cyan"
            cfg.save()
            console.print("[green]✔  All settings reset to defaults.[/]")

        elif choice == "4":
            break

        else:
            console.print("[yellow]  Enter 1, 2, 3, or 4.[/]")


# ---------------------------------------------------------------------------
# Internal helper (copied from menu.py so settings.py has no dependency on it)
# ---------------------------------------------------------------------------

def _prompt(label: str, default: str = "") -> str:
    """
    Display a styled prompt and return stripped user input.
    Falls back to *default* on empty input.
    """
    hint = f" [dim](default: {default})[/]" if default else ""
    console.print(f"[bold cyan]▶[/]  {label}{hint}", end="  ")
    try:
        raw = input()
    except (EOFError, KeyboardInterrupt):
        console.print()
        return default
    value = raw.strip()
    return value if value else default
