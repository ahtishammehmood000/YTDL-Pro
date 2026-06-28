"""
download_ui.py - Shared download UI sub-flows for YTDL-Pro.

Contains the two interactive helpers that are needed by both menu.py
(single-video, batch) and channel_downloader.py (channel download):

    pick_folder(cfg)          – destination-folder picker
    pick_quality(url)         – quality / resolution picker

Placing them here breaks the circular import that would arise if
channel_downloader.py tried to import from menu.py.

Import graph after this change
-------------------------------
    menu.py              → download_ui.py
    channel_downloader.py → download_ui.py

Neither menu.py nor channel_downloader.py imports the other.

Public surface
--------------
pick_folder(cfg)
    Show the Download Destination sub-menu and return the chosen Path.
    Saves the choice into cfg and persists it to config.json.

pick_quality(url_for_probe)
    Show the Download Quality sub-menu and return a quality string or None.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from rich import box
from rich.console import Console
from rich.rule import Rule
from rich.table import Table

import file_browser
from app_config import DEFAULT_FOLDER, PRESET_FOLDERS, AppConfig
from downloader import Downloader

console: Console = Console()


# ---------------------------------------------------------------------------
# Low-level input helpers (private to this module)
# ---------------------------------------------------------------------------


def _prompt(label: str, default: str = "") -> str:
    """Display a styled prompt and return stripped user input."""
    hint = f" [dim](default: {default})[/]" if default else ""
    console.print(f"[bold cyan]▶[/]  {label}{hint}", end="  ")
    try:
        raw = input()
    except (EOFError, KeyboardInterrupt):
        console.print()
        return default
    value = raw.strip()
    return value if value else default


# ---------------------------------------------------------------------------
# Folder picker
# ---------------------------------------------------------------------------


def pick_folder(cfg: AppConfig) -> Path:
    """
    Show the destination picker and return the chosen folder.

    The picker is **always** displayed so the user consciously selects where
    each batch lands.  The last-used folder (if any) appears as option 1 so
    it can be re-selected with a single keystroke.

    Menu layout
    -----------
    When a folder has been used before::

        1  Last Used  (/storage/emulated/0/Movies)
        2  Movies     (/storage/emulated/0/Movies)
        3  Downloads  (/storage/emulated/0/Download)
        4  Custom path…

    On first run (no stored folder)::

        1  Movies     (/storage/emulated/0/Movies)
        2  Downloads  (/storage/emulated/0/Download)
        3  Custom path…

    Choosing "Custom path…" launches the interactive terminal file
    browser (see file_browser.py) instead of a raw typed-path prompt.

    The chosen folder is saved into *cfg* and persisted to ``config.json``
    before this function returns.

    Returns
    -------
    Path
        Absolute path to the chosen (and created) directory.
    """
    console.print()
    console.print(Rule("[bold cyan]Download Destination[/]"))
    console.print()

    # Build option list.  "Last Used" is prepended when a folder is stored.
    options: list[tuple[str, Path | None]] = []

    if cfg.last_download_folder is not None:
        label = str(cfg.last_download_folder)
        if len(label) > 52:
            label = "…" + label[-51:]
        options.append((f"Last Used  ({label})", cfg.last_download_folder))

    for name, path in PRESET_FOLDERS.items():
        options.append((f"{name}  ({path})", path))

    options.append(("Custom path…", None))

    # Render the table.
    table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
    table.add_column("  #", style="bold cyan", justify="right", min_width=3)
    table.add_column("  Location", style="white")

    for i, (label, _) in enumerate(options, start=1):
        table.add_row(str(i), label)

    console.print(table)
    console.print()

    # Prompt until a valid choice is made.
    n = len(options)
    while True:
        raw = _prompt(f"Choose destination [1–{n}]")
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < n:
                _, chosen_path = options[idx]
                break
        console.print(f"[yellow]  Please enter a number between 1 and {n}.[/]")

    # Handle custom path entry via the interactive file browser (replaces
    # the old free-typed path prompt — see file_browser.py).
    if chosen_path is None:
        browsed = file_browser.select_folder()
        if browsed is None:
            # User backed out of the browser without picking anything;
            # fall back to the existing default rather than leaving the
            # caller with no folder at all.
            console.print("[yellow]  No folder selected — keeping the previous default.[/]")
            chosen_path = cfg.last_download_folder or DEFAULT_FOLDER
        else:
            chosen_path = browsed

    # Create the directory and persist the choice.
    chosen_path.mkdir(parents=True, exist_ok=True)
    cfg.last_download_folder = chosen_path
    cfg.save()

    console.print(
        f"\n[green]✔  Saving to:[/] [cyan]{chosen_path}[/]\n"
        f"   [dim](Change anytime via Settings → option 1)[/]\n"
    )
    return chosen_path


# ---------------------------------------------------------------------------
# Quality picker
# ---------------------------------------------------------------------------

_RESOLUTION_LABELS: dict[int, str] = {
    2160: "2160p (4K)",
    1440: "1440p (2K)",
    1080: "1080p (Full HD)",
    720:  "720p (HD)",
    480:  "480p (SD)",
    360:  "360p",
    240:  "240p",
    144:  "144p",
}


def pick_quality(url_for_probe: Optional[str] = None) -> Optional[str]:
    """
    Show the Download Quality step and return a quality string.

    Returns
    -------
    str or None
        ``None``       → use best available (no change to existing behaviour)
        ``"1080p"``    → specific resolution string understood by Downloader
    """
    console.print()
    console.print(Rule("[bold cyan]Download Quality[/]"))
    console.print()

    q_table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
    q_table.add_column("  #", style="bold cyan", justify="right", min_width=3)
    q_table.add_column("  Option", style="white")

    q_table.add_row("1", "Best Available  [dim](Recommended)[/]")
    q_table.add_row("2", "Choose Specific Quality")
    console.print(q_table)
    console.print()

    while True:
        choice = _prompt("Choose quality option [1–2]")
        if choice in ("1", "2"):
            break
        console.print("[yellow]  Please enter 1 or 2.[/]")

    if choice == "1":
        console.print("[dim]  Using best available quality.[/]\n")
        return None

    # ---- Specific quality path ----
    if url_for_probe is None:
        console.print(
            "[yellow]  Cannot fetch resolutions without a URL – "
            "defaulting to best available.[/]\n"
        )
        return None

    console.print("[dim]  Fetching available resolutions…[/]")
    heights = Downloader.fetch_resolutions(url_for_probe)

    if not heights:
        console.print(
            "[yellow]  Could not retrieve resolutions – "
            "defaulting to best available.[/]\n"
        )
        return None

    console.print()
    res_table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
    res_table.add_column("  #", style="bold cyan", justify="right", min_width=3)
    res_table.add_column("  Resolution", style="white")

    for i, h in enumerate(heights, start=1):
        label = _RESOLUTION_LABELS.get(h, f"{h}p")
        res_table.add_row(str(i), label)

    console.print(res_table)
    console.print()

    n = len(heights)
    while True:
        raw = _prompt(f"Choose resolution [1–{n}]")
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < n:
                chosen_height = heights[idx]
                chosen_label = _RESOLUTION_LABELS.get(chosen_height, f"{chosen_height}p")
                console.print(f"[dim]  Selected: {chosen_label}[/]\n")
                return f"{chosen_height}p"
        console.print(f"[yellow]  Please enter a number between 1 and {n}.[/]")
