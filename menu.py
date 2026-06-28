"""
menu.py - Interactive terminal menu for YTDL-Pro.

Implements the full screen-driven menu that opens when the user runs
``python main.py`` with no arguments.  All existing CLI sub-commands
continue to work exactly as before; this module is only invoked when
there are no command-line arguments.

Menu structure
--------------
  1  Single Video Download   – paste a URL → choose folder → download
  2  Batch Download (.txt)   – pick a file → choose folder → download
  3  Failed Downloads        – manage and retry failed downloads
  4  Settings                – change download folder, reset config
  5  About                   – project info panel
  6  Channel Link Grabber    – collect all URLs from a channel
  7  Exit

The folder-picker sub-menu appears before every download (single or batch)
**unless** a folder is already stored in config.json, in which case it is
reused silently.  The user can always change it via Settings → option 1.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Optional

from rich import box
from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

import file_browser
from app_config import DEFAULT_FOLDER, PRESET_FOLDERS, AppConfig
from downloader import (
    FAILED_FILE,
    LINKS_FILE,
    BASE_DIR,
    Downloader,
    Logger,
)

console: Console = Console()
log: Logger = Logger()

# ---------------------------------------------------------------------------
# Application constants shared across menu and main.
# ---------------------------------------------------------------------------
APP_NAME: str = "YTDL-Pro"
APP_VERSION: str = "2.0"
APP_AUTHOR: str = "Ahtisham Mahmood"
APP_DESCRIPTION: str = "Professional YouTube Downloader for Termux"


# ===========================================================================
# Banner (shared with main.py via import)
# ===========================================================================


def print_banner(*, clear: bool = False) -> None:
    """
    Render the YTDL-Pro banner panel.

    Parameters
    ----------
    clear:
        When True, print blank lines before the banner to visually
        separate it from the previous menu screen.
    """
    if clear:
        console.print("\n" * 2)

    author_line = Text(f"By {APP_AUTHOR}", style="dim italic")
    version_line = Text(f"v{APP_VERSION}  •  {APP_DESCRIPTION}", style="dim")

    content = Align.center(
        Text.assemble(author_line, "\n", version_line)
    )

    banner = Align.center(
        Panel(
            content,
            title=Text(f"  {APP_NAME}  ", style="bold cyan"),
            border_style="cyan",
            padding=(0, 6),
        )
    )
    console.print(banner)
    console.print()


# ===========================================================================
# Low-level input helpers
# ===========================================================================


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


def _choose(prompt: str, choices: list[str]) -> str:
    """
    Display a numbered list of *choices* and return the chosen item.
    Re-prompts until a valid number is entered.
    """
    while True:
        raw = _prompt(prompt)
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(choices):
                return choices[idx]
        console.print("[yellow]  Please enter a number from the list above.[/]")


def _pause() -> None:
    """Wait for Enter before returning to the menu."""
    console.print("\n[dim]Press Enter to return to the main menu…[/]", end="  ")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    console.print()


def _is_valid_txt_file(path: Optional[Path]) -> bool:
    """
    Return True if *path* points at an existing, regular ``.txt`` file.

    Used to check a path already loaded from config.json (``last_links_file``)
    — unlike :func:`_validate_txt_file`, this takes a ``Path | None`` rather
    than raw user input, and never returns an error message.
    """
    return (
        path is not None
        and path.exists()
        and path.is_file()
        and path.suffix.lower() == ".txt"
    )


def _validate_txt_file(raw: str) -> tuple:
    """
    Validate that *raw* is a path to an existing, regular .txt file.

    Returns (Path, "") on success, or (None, reason_string) on failure.

    Checks performed (in order):
        1. Path is not empty.
        2. The resolved path exists on disk.
        3. It is a regular file, not a directory.
        4. Its suffix (case-insensitive) is .txt.
    """
    if not raw:
        return None, "Path cannot be empty."

    path = Path(raw).expanduser().resolve()

    if not path.exists():
        return None, f"File not found: {path}"

    if path.is_dir():
        return None, f"That is a directory, not a file: {path}"

    if path.suffix.lower() != ".txt":
        return None, f"Not a .txt file (got \'{path.suffix or 'no extension'}\' ): {path}"

    return path, ""


def _prompt_txt_file(prompt_label: str, default: Path) -> Path:
    """
    Prompt for a .txt file path, re-asking on every invalid input.

    Parameters
    ----------
    prompt_label:
        Label shown beside the prompt arrow.
    default:
        Used when the user presses Enter with no input.

    Returns
    -------
    Path
        A validated, resolved path to an existing .txt file.
    """
    while True:
        raw = _prompt(prompt_label, default=str(default))
        path, error = _validate_txt_file(raw)
        if path is not None:
            return path
        console.print(f"[bold red]  ✗  {error}[/]")
        console.print("[dim]     Enter a valid .txt file path, or press Enter to use the default.[/]")


# ===========================================================================
# Folder picker
# ===========================================================================


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


# ===========================================================================
# Quality picker
# ===========================================================================

_RESOLUTION_LABELS: dict[int, str] = {
    2160: "2160p (4K)",
    1440: "1440p (2K)",
    1080: "1080p (Full HD)",
    720: "720p (HD)",
    480: "480p (SD)",
    360: "360p",
    240: "240p",
    144: "144p",
}


def _pick_quality(url_for_probe: Optional[str] = None) -> Optional[str]:
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


# ===========================================================================
# Menu actions
# ===========================================================================


def action_single_video(cfg: AppConfig) -> None:
    """
    Menu item 1 – Single Video Download.

    Asks for a URL, resolves the download folder, writes the URL to a
    temporary file, and calls Downloader with that file + folder.
    The temporary file is removed after the download finishes.
    """
    console.print()
    console.print(Rule("[bold cyan]Single Video Download[/]"))
    console.print()

    url = _prompt("Paste the YouTube URL")
    if not url:
        console.print("[yellow]  No URL entered – returning to menu.[/]")
        _pause()
        return

    if not (url.startswith("http://") or url.startswith("https://")):
        console.print("[yellow]  That doesn't look like a URL.  Please include http:// or https://[/]")
        _pause()
        return

    quality = _pick_quality(url_for_probe=url)

    folder = pick_folder(cfg)

    # Write the single URL to a temporary file so Downloader can read it.
    tmp_file: Optional[Path] = None
    try:
        fd, tmp_str = tempfile.mkstemp(prefix=".ytdl_single_", suffix=".txt", dir=BASE_DIR)
        tmp_file = Path(tmp_str)
        with open(fd, "w", encoding="utf-8") as fh:
            fh.write(url + "\n")

        log.info("Single-video download: %s → %s", url, folder)
        downloader = Downloader(links_file=tmp_file, videos_dir=folder, quality=quality)
        downloader.run()

    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]✗  Download failed:[/] {exc}")
        log.exception("Single-video download error: %s", exc)
    finally:
        if tmp_file is not None:
            try:
                tmp_file.unlink(missing_ok=True)
            except OSError:
                pass

    _pause()


def action_batch_download(cfg: AppConfig) -> None:
    """
    Menu item 2 – Batch Download (.txt).

    Flow
    ----
    1. Use the saved ``last_links_file`` from config.json, if it is set
       and still points at a valid, existing ``.txt`` file.
    2. If it is missing, invalid, or has never been set, the user is
       required to pick one via the interactive file browser — there is
       **no** silent fallback to the project's internal ``links.txt``.
    3. Whatever file is selected (saved one re-confirmed, or freshly
       browsed) is immediately written to config.json as
       ``last_links_file`` so it survives restarts.
    4. Offer the destination folder picker.
    5. Run the downloader.
    """
    console.print()
    console.print(Rule("[bold cyan]Batch Download[/]"))
    console.print()

    links_path: Optional[Path] = cfg.last_links_file

    if not _is_valid_txt_file(links_path):
        if links_path is not None:
            console.print(f"[yellow]  Saved .txt file is missing or invalid:[/] {links_path}")
        console.print("  Select a .txt file containing your URLs.")
        console.print()

        browsed = file_browser.select_txt_file()
        if browsed is None:
            console.print("[yellow]  No file selected — returning to menu.[/]")
            _pause()
            return

        links_path = browsed
        cfg.last_links_file = links_path
        cfg.save()
        log.info("last_links_file saved: %s", links_path)
    else:
        console.print(f"  Current file: [cyan]{links_path}[/]")
        console.print("  [dim]Press Enter to use it, or type 'b' to browse for a different file.[/]")
        console.print()

        raw_choice = _prompt("Continue with this file? (Enter/b)")

        if raw_choice.strip().lower() == "b":
            browsed = file_browser.select_txt_file(start=links_path.parent)
            if browsed is None:
                console.print("[yellow]  No file selected — keeping the current file.[/]")
            else:
                links_path = browsed
                cfg.last_links_file = links_path
                cfg.save()
                log.info("last_links_file updated: %s", links_path)

    console.print(f"  [green]✔  Using:[/] [cyan]{links_path}[/]")
    console.print()

    # Quality selection – asked once, applied to every URL in the batch.
    # Probe the first URL in the file so we can show real resolutions.
    _probe_url: Optional[str] = None
    try:
        with links_path.open("r", encoding="utf-8") as _fh:
            for _line in _fh:
                _u = _line.strip()
                if _u and not _u.startswith("#"):
                    _probe_url = _u
                    break
    except OSError:
        pass

    quality = _pick_quality(url_for_probe=_probe_url)

    folder = pick_folder(cfg)

    log.info("Batch download: file=%s → folder=%s", links_path, folder)
    try:
        downloader = Downloader(links_file=links_path, videos_dir=folder, quality=quality)
        downloader.run()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]✗  Download failed:[/] {exc}")
        log.exception("Batch download error: %s", exc)

    _pause()


def _parse_selection(raw: str, max_idx: int) -> list[int]:
    """
    Parse a user selection string like ``"1 3"`` or ``"2,4"`` into a
    sorted, deduplicated list of zero-based indices.

    Only values in the range [1, max_idx] are accepted; anything outside
    that range is silently ignored.
    """
    indices: set[int] = set()
    for token in raw.replace(",", " ").split():
        if token.isdigit():
            n = int(token)
            if 1 <= n <= max_idx:
                indices.add(n - 1)
    return sorted(indices)


def _write_failed(urls: list[str]) -> None:
    """Overwrite failed.txt with *urls* (one per line). Creates or truncates."""
    import os as _os
    content = "\n".join(urls) + ("\n" if urls else "")
    try:
        with open(FAILED_FILE, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            _os.fsync(fh.fileno())
    except OSError as exc:
        console.print(f"[bold red]✗  Could not update failed list:[/] {exc}")
        log.error("Failed-file write error: %s", exc)


def action_failed() -> None:
    """
    Menu item 3 – Failed Downloads.

    Sub-options:
        1  Retry All         – retry every URL in failed.txt
        2  Retry Selected    – pick one or more URLs to retry
        3  Delete Selected   – remove chosen URLs from failed.txt
        4  Clear All         – empty failed.txt after confirmation
        5  Back              – return to main menu
    """
    while True:
        console.print()
        console.print(Rule("[bold cyan]Failed Downloads[/]"))
        console.print()

        # ── Load current failed URLs ─────────────────────────────────────────
        urls: list[str] = []
        if FAILED_FILE.exists():
            for line in FAILED_FILE.read_text(encoding="utf-8").splitlines():
                u = line.strip()
                if u:
                    urls.append(u)

        if not urls:
            console.print("[bold green]✔  No failed downloads.[/]")
            _pause()
            return

        # ── Show the failed URLs table ───────────────────────────────────────
        url_table = Table(box=box.SIMPLE, show_header=True, pad_edge=False)
        url_table.add_column("  #", style="bold cyan", justify="right", min_width=3)
        url_table.add_column("  URL", style="white")
        for i, u in enumerate(urls, start=1):
            url_table.add_row(str(i), u)
        console.print(url_table)
        console.print()

        # ── Action menu ──────────────────────────────────────────────────────
        action_table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
        action_table.add_column("  #", style="bold cyan", justify="right", min_width=3)
        action_table.add_column("  Option", style="white")
        action_table.add_row("1", "Retry All")
        action_table.add_row("2", "Retry Selected")
        action_table.add_row("3", "Delete Selected")
        action_table.add_row("4", "Clear All")
        action_table.add_row("5", "Back")
        console.print(action_table)
        console.print()

        while True:
            choice = _prompt("Choose option [1–5]")
            if choice in ("1", "2", "3", "4", "5"):
                break
            console.print("[yellow]  Please enter a number between 1 and 5.[/]")

        # ── 1: Retry All ─────────────────────────────────────────────────────
        if choice == "1":
            log.info("Failed downloads: retrying all %d URL(s)", len(urls))
            try:
                downloader = Downloader(links_file=FAILED_FILE, videos_dir=None)
                downloader.run()
            except Exception as exc:  # noqa: BLE001
                console.print(f"[bold red]✗  Retry failed:[/] {exc}")
                log.exception("Retry-all error: %s", exc)
            _pause()

        # ── 2: Retry Selected ────────────────────────────────────────────────
        elif choice == "2":
            console.print(
                "  Enter the number(s) to retry, separated by spaces or commas."
            )
            console.print(f"  [dim](e.g. 1  or  1,3  or  2 4)[/]")
            console.print()
            selected = _parse_selection(_prompt("URL number(s)"), len(urls))
            if not selected:
                console.print("[yellow]  No valid selection – returning to menu.[/]")
            else:
                chosen = [urls[i] for i in selected]
                console.print(
                    f"  Retrying [cyan]{len(chosen)}[/] URL(s)…"
                )
                tmp_file: Optional[Path] = None
                try:
                    fd, tmp_str = tempfile.mkstemp(
                        prefix=".ytdl_retry_", suffix=".txt", dir=BASE_DIR
                    )
                    tmp_file = Path(tmp_str)
                    with open(fd, "w", encoding="utf-8") as fh:
                        fh.write("\n".join(chosen) + "\n")
                    log.info(
                        "Failed downloads: retrying selected %d URL(s)", len(chosen)
                    )
                    downloader = Downloader(links_file=tmp_file, videos_dir=None)
                    downloader.run()
                except Exception as exc:  # noqa: BLE001
                    console.print(f"[bold red]✗  Retry failed:[/] {exc}")
                    log.exception("Retry-selected error: %s", exc)
                finally:
                    if tmp_file is not None:
                        try:
                            tmp_file.unlink(missing_ok=True)
                        except OSError:
                            pass
            _pause()

        # ── 3: Delete Selected ───────────────────────────────────────────────
        elif choice == "3":
            console.print(
                "  Enter the number(s) to delete, separated by spaces or commas."
            )
            console.print(f"  [dim](e.g. 1  or  1,3  or  2 4)[/]")
            console.print()
            selected = _parse_selection(_prompt("URL number(s)"), len(urls))
            if not selected:
                console.print("[yellow]  No valid selection – nothing deleted.[/]")
            else:
                keep = [u for i, u in enumerate(urls) if i not in selected]
                _write_failed(keep)
                removed = len(urls) - len(keep)
                console.print(
                    f"  [green]✔[/]  Deleted [cyan]{removed}[/] URL(s) from failed list."
                )
                log.info("Failed downloads: deleted %d URL(s)", removed)
                if not keep:
                    _pause()
                    return

        # ── 4: Clear All ─────────────────────────────────────────────────────
        elif choice == "4":
            console.print()
            answer = _prompt(
                f"Clear all {len(urls)} failed URL(s)? [y/N]", default="n"
            ).lower()
            if answer in ("y", "yes"):
                _write_failed([])
                console.print("[green]✔  Failed downloads cleared.[/]")
                log.info("Failed downloads: cleared all %d URL(s)", len(urls))
                _pause()
                return
            else:
                console.print("[dim]  Cancelled.[/]")

        # ── 5: Back ──────────────────────────────────────────────────────────
        elif choice == "5":
            return


def action_settings(cfg: AppConfig) -> None:
    """
    Menu item 5 – Settings.

    Sub-options:
        1  Change download folder
        2  Change default .txt file
        3  Reset all settings
        4  Back
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


def action_about() -> None:
    """
    Menu item 6 – About.
    """
    console.print()
    console.print(Rule("[bold cyan]About[/]"))
    console.print()

    lines = Text.assemble(
        Text(f"{APP_NAME}\n", style="bold cyan"),
        Text(f"Version {APP_VERSION}\n\n", style="dim"),
        Text(f"By {APP_AUTHOR}\n\n", style="white"),
        Text(f"{APP_DESCRIPTION}\n\n", style="dim"),
        Text("Features\n", style="bold white"),
        Text("  • Best-quality video + audio merged to MP4\n", style="dim"),
        Text("  • Sequential automatic file numbering\n", style="dim"),
        Text("  • Full metadata & history tracking\n", style="dim"),
        Text("  • Playlist support\n", style="dim"),
        Text("  • Retry on failure\n", style="dim"),
        Text("  • Interactive menu + classic CLI sub-commands\n", style="dim"),
    )

    console.print(
        Align.center(
            Panel(
                Align.center(lines),
                border_style="cyan",
                padding=(1, 6),
            )
        )
    )

    _pause()


def action_channel_grabber(cfg: AppConfig) -> None:
    """
    Menu item 7 – Channel Link Grabber.
    Delegates entirely to channel_grabber.run().
    """
    from channel_grabber import run
    run(cfg)


# ===========================================================================
# Main menu loop
# ===========================================================================


def _build_menu_table(cfg: AppConfig) -> Table:
    """Render the numbered menu as a Rich table."""
    failed_count = (
        sum(
            1
            for line in FAILED_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if FAILED_FILE.exists()
        else 0
    )

    failed_label = (
        f"[bold red]Failed Downloads  ({failed_count} pending)[/]"
        if failed_count
        else "Failed Downloads"
    )

    table = Table(
        box=box.ROUNDED,
        border_style="cyan",
        show_header=False,
        pad_edge=True,
        min_width=52,
    )
    table.add_column("  #", style="bold cyan", justify="right", min_width=4)
    table.add_column("  Menu Item", style="white", min_width=36)

    table.add_row("1", "Single Video Download")
    table.add_row("2", "Batch Download (.txt)")
    table.add_row("3", failed_label)
    table.add_row("4", "Settings")
    table.add_row("5", "About")
    table.add_row("6", "Channel Link Grabber")
    table.add_row("7", "Exit")

    return table


def run_menu() -> None:
    """
    Main interactive menu loop.

    Entered when ``python main.py`` is called with no arguments.
    Loops until the user chooses Exit (7) or presses Ctrl+C.
    """
    cfg = AppConfig.load()

    while True:
        # Clear screen cheaply by printing enough blank lines.
        console.print("\n" * 1)
        print_banner()

        table = _build_menu_table(cfg)
        console.print(Align.center(table))
        console.print()

        raw = _prompt("Choose option [1–7]")

        if raw == "1":
            action_single_video(cfg)

        elif raw == "2":
            action_batch_download(cfg)

        elif raw == "3":
            action_failed()

        elif raw == "4":
            action_settings(cfg)

        elif raw == "5":
            action_about()

        elif raw == "6":
            action_channel_grabber(cfg)

        elif raw == "7":
            console.print("\n[bold cyan]Goodbye![/]\n")
            sys.exit(0)

        else:
            console.print("[yellow]  Please enter a number between 1 and 7.[/]")
            _pause()
