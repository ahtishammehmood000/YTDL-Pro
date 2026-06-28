"""
failed_downloads.py - Failed Downloads feature for YTDL-Pro.

Mirrors the structure of channel_grabber.py: the module owns every piece
of logic for the Failed Downloads screen and exposes a single entry-point:

    run(cfg)   – called by menu.py, runs the full sub-menu loop.

Internal helpers are prefixed with ``_`` and are not part of the public API.

Sub-menu options
----------------
  1  Retry All         – re-download every URL in failed.txt
  2  Retry Selected    – choose individual URLs to re-download
  3  Delete Selected   – remove chosen URLs from failed.txt permanently
  4  Clear All         – empty failed.txt after confirmation
  5  Back              – return to the main menu

After every successful retry the URLs that were downloaded are removed from
failed.txt immediately.  If failed.txt becomes empty the main menu will no
longer display the red "pending" warning (that counter is read live from the
file each time the menu is drawn).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Optional

from rich import box
from rich.console import Console
from rich.rule import Rule
from rich.table import Table

from app_config import AppConfig
from downloader import (
    BASE_DIR,
    FAILED_FILE,
    HISTORY_FILE,
    Downloader,
    Logger,
)

console: Console = Console()
log: Logger = Logger()


# ===========================================================================
# Module-level I/O helpers
# (same style as menu.py so the UI is identical)
# ===========================================================================

def _prompt(label: str, default: str = "") -> str:
    """
    Display a styled prompt and return stripped user input.
    Falls back to *default* on empty input.

    Uses ``console.print`` for the label (so Rich markup is rendered) then
    calls bare ``input()`` for the response — exactly matching menu.py.
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


def _pause() -> None:
    """Wait for Enter before returning to the caller."""
    console.print("\n[dim]Press Enter to return to the main menu…[/]", end="  ")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    console.print()


# ===========================================================================
# failed.txt read / write
# ===========================================================================

def _load_failed() -> list[str]:
    """
    Read failed.txt and return a deduplicated, ordered list of URLs.

    Blank lines and lines that are all-whitespace are silently skipped.
    """
    if not FAILED_FILE.exists():
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for line in FAILED_FILE.read_text(encoding="utf-8").splitlines():
        u = line.strip()
        if u and u not in seen:
            urls.append(u)
            seen.add(u)
    return urls


def _write_failed(urls: list[str]) -> None:
    """
    Overwrite failed.txt with *urls* (one per line).

    Passing an empty list produces an empty file, which causes the main
    menu's failed-count check to display no warning.
    """
    try:
        FAILED_FILE.write_text(
            "\n".join(urls) + ("\n" if urls else ""),
            encoding="utf-8",
        )
    except OSError as exc:
        console.print(f"[bold red]✗  Could not update failed list:[/] {exc}")
        log.error("Failed-file write error: %s", exc)


# ===========================================================================
# Selection parser
# ===========================================================================

def _parse_selection(raw: str, max_idx: int) -> list[int]:
    """
    Convert a user input string into a sorted list of zero-based indices.

    Accepts any mix of spaces and commas as delimiters:
        "1"       → [0]
        "1 2"     → [0, 1]
        "1,2"     → [0, 1]
        "1, 2"    → [0, 1]

    Only values in the range [1, max_idx] are kept; anything outside is
    silently ignored.  Returns an empty list when no valid tokens are found.

    Parameters
    ----------
    raw:
        The raw string typed by the user.
    max_idx:
        The number of items currently displayed (1-based upper bound).
    """
    indices: set[int] = set()
    # Normalise: replace commas with spaces, then split on any whitespace.
    for token in raw.replace(",", " ").split():
        if token.isdigit():
            n = int(token)
            if 1 <= n <= max_idx:
                indices.add(n - 1)   # convert to 0-based
    return sorted(indices)


# ===========================================================================
# Post-retry cleanup
# ===========================================================================

def _remove_successful_from_failed(retried: list[str]) -> None:
    """
    After a retry run, remove from failed.txt every URL that was
    successfully downloaded (i.e. now appears in history.json).

    URLs whose retry also failed are left in place so the user can
    attempt them again later.

    Parameters
    ----------
    retried:
        The list of URLs that were submitted to the retry downloader.
    """
    # Build the set of URLs now recorded as successfully downloaded.
    downloaded: set[str] = set()
    if HISTORY_FILE.exists():
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                downloaded = {r.get("url", "") for r in data}
        except Exception:  # noqa: BLE001
            pass

    # Partition retried URLs into succeeded vs still-failed.
    succeeded: set[str] = {u for u in retried if u in downloaded}
    if not succeeded:
        return  # Nothing to clean up.

    # Reload the live failed list and remove only the succeeded ones.
    all_failed = _load_failed()
    remaining = [u for u in all_failed if u not in succeeded]
    _write_failed(remaining)

    log.info(
        "Failed downloads: removed %d successfully retried URL(s); "
        "%d remain in failed.txt",
        len(succeeded),
        len(remaining),
    )


# ===========================================================================
# Public sub-actions (also callable individually from tests / scripts)
# ===========================================================================

def retry_all(cfg: AppConfig) -> None:
    """
    Re-download every URL currently in failed.txt.

    Calls ``pick_folder`` from menu.py (imported lazily to avoid a circular
    import) so the user selects a destination before the retry begins,
    matching the behaviour of Single Video Download and Batch Download.

    Successfully downloaded URLs are removed from failed.txt immediately
    after ``Downloader.run()`` returns.
    """
    # Lazy import to avoid circular dependency (menu imports this module).
    from menu import pick_folder  # noqa: PLC0415

    urls = _load_failed()
    if not urls:
        return

    folder = pick_folder(cfg)
    log.info("Failed downloads: retrying all %d URL(s) → %s", len(urls), folder)

    try:
        downloader = Downloader(links_file=FAILED_FILE, videos_dir=folder)
        downloader.run()
        _remove_successful_from_failed(urls)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]✗  Retry failed:[/] {exc}")
        log.exception("Retry-all error: %s", exc)


def retry_selected(cfg: AppConfig, urls: list[str]) -> None:
    """
    Re-download a specific subset of failed URLs.

    Parameters
    ----------
    cfg:
        Application configuration (needed by ``pick_folder``).
    urls:
        The subset of URLs to retry (already resolved from the user's
        numeric selection by the caller).
    """
    from menu import pick_folder  # noqa: PLC0415

    folder = pick_folder(cfg)
    log.info(
        "Failed downloads: retrying selected %d URL(s) → %s", len(urls), folder
    )

    tmp_file: Optional[Path] = None
    try:
        fd, tmp_str = tempfile.mkstemp(
            prefix=".ytdl_retry_", suffix=".txt", dir=BASE_DIR
        )
        tmp_file = Path(tmp_str)
        with open(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(urls) + "\n")

        downloader = Downloader(links_file=tmp_file, videos_dir=folder)
        downloader.run()
        _remove_successful_from_failed(urls)

    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]✗  Retry failed:[/] {exc}")
        log.exception("Retry-selected error: %s", exc)
    finally:
        if tmp_file is not None:
            try:
                tmp_file.unlink(missing_ok=True)
            except OSError:
                pass


def delete_selected(urls: list[str], selected_indices: list[int]) -> list[str]:
    """
    Remove the URLs at *selected_indices* (0-based) from failed.txt.

    Parameters
    ----------
    urls:
        The current list of failed URLs (as returned by ``_load_failed``).
    selected_indices:
        Zero-based indices into *urls* to remove.

    Returns
    -------
    list[str]
        The remaining URLs (after deletion) so the caller can decide
        whether to continue showing the sub-menu.
    """
    indices_set = set(selected_indices)
    keep = [u for i, u in enumerate(urls) if i not in indices_set]
    _write_failed(keep)
    removed = len(urls) - len(keep)
    console.print(
        f"  [green]✔[/]  Deleted [cyan]{removed}[/] URL(s) from failed list."
    )
    log.info("Failed downloads: deleted %d URL(s)", removed)
    return keep


def clear_all(urls: list[str]) -> None:
    """
    Empty failed.txt after user confirmation.

    Parameters
    ----------
    urls:
        The current list of failed URLs (used only for the count in the
        confirmation prompt).
    """
    answer = _prompt(
        f"Clear all {len(urls)} failed URL(s)? [y/N]", default="n"
    ).lower()
    if answer in ("y", "yes"):
        _write_failed([])
        console.print("[green]✔  Failed downloads cleared.[/]")
        log.info("Failed downloads: cleared all %d URL(s)", len(urls))
    else:
        console.print("[dim]  Cancelled.[/]")


# ===========================================================================
# Main entry point
# ===========================================================================

def run(cfg: AppConfig) -> None:
    """
    Display the Failed Downloads sub-menu and handle user choices.

    This is the only symbol that menu.py needs to call:

        from failed_downloads import run
        run(cfg)

    The loop re-reads failed.txt at the top of every iteration so the
    table always reflects the current state (e.g. after a deletion).

    Parameters
    ----------
    cfg:
        Application configuration object passed through from the main menu.
        Required by retry actions so they can call ``pick_folder``.
    """
    while True:
        console.print()
        console.print(Rule("[bold cyan]Failed Downloads[/]"))
        console.print()

        # ── Load current failed URLs ─────────────────────────────────────────
        urls = _load_failed()

        if not urls:
            console.print("[bold green]✔  No failed downloads.[/]")
            _pause()
            return

        # ── Failed URLs table ────────────────────────────────────────────────
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

        # Validate the choice before dispatching.
        while True:
            choice = _prompt("Choose option [1–5]")
            if choice in ("1", "2", "3", "4", "5"):
                break
            console.print("[yellow]  Please enter a number between 1 and 5.[/]")

        # ── 1: Retry All ─────────────────────────────────────────────────────
        if choice == "1":
            retry_all(cfg)
            _pause()

        # ── 2: Retry Selected ────────────────────────────────────────────────
        elif choice == "2":
            console.print(
                "  Enter the number(s) to retry, separated by spaces or commas."
            )
            console.print("  [dim](e.g. 1  or  1,3  or  2 4)[/]")
            console.print()
            raw_sel = _prompt("URL number(s)")
            selected = _parse_selection(raw_sel, len(urls))
            if not selected:
                console.print("[yellow]  No valid selection – returning to menu.[/]")
            else:
                chosen = [urls[i] for i in selected]
                console.print(f"  Retrying [cyan]{len(chosen)}[/] URL(s)…")
                retry_selected(cfg, chosen)
            _pause()

        # ── 3: Delete Selected ───────────────────────────────────────────────
        elif choice == "3":
            console.print(
                "  Enter the number(s) to delete, separated by spaces or commas."
            )
            console.print("  [dim](e.g. 1  or  1,3  or  2 4)[/]")
            console.print()
            raw_sel = _prompt("URL number(s)")
            selected = _parse_selection(raw_sel, len(urls))
            if not selected:
                console.print("[yellow]  No valid selection – nothing deleted.[/]")
            else:
                remaining = delete_selected(urls, selected)
                if not remaining:
                    # List is now empty – no point staying in the sub-menu.
                    _pause()
                    return

        # ── 4: Clear All ─────────────────────────────────────────────────────
        elif choice == "4":
            console.print()
            clear_all(urls)
            # If the list was cleared, exit the sub-menu so the main menu
            # redraws without the red warning.
            if not _load_failed():
                _pause()
                return

        # ── 5: Back ──────────────────────────────────────────────────────────
        elif choice == "5":
            return
