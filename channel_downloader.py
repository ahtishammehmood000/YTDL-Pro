"""
channel_downloader.py - Channel Downloader for YTDL-Pro.

Downloads an entire YouTube channel directly — no links.txt is created
or required at any point.

Workflow
--------
1. Ask for the channel URL.
2. Ask which content type: Shorts Only / Videos Only / Shorts + Videos.
3. Use channel_grabber._collect_urls() to enumerate every video URL,
   following all continuation pages exactly as the CLI does.
4. Ask for quality (reuses the existing quality selector).
5. Ask for destination folder (reuses the existing folder picker).
6. Pass the collected URLs directly to Downloader via a NamedTemporaryFile.
   The temporary file is deleted automatically after the download finishes.

Public surface
--------------
run(cfg)
    Full interactive flow.  Called from menu.py as::

        from channel_downloader import run
        run(cfg)
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from rich import box
from rich.console import Console
from rich.rule import Rule
from rich.table import Table

from app_config import AppConfig
from channel_grabber import _collect_urls          # reuse scan logic exactly
from downloader import BASE_DIR, Downloader, Logger
from download_ui import pick_folder, pick_quality as _pick_quality

console: Console = Console()
log: Logger = Logger()


# ---------------------------------------------------------------------------
# Helpers (same style as every other module in this project)
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


def _pause() -> None:
    """Wait for Enter before returning to the caller."""
    console.print("\n[dim]Press Enter to return to the main menu…[/]", end="  ")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    console.print()


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------


def run(cfg: AppConfig) -> None:
    """
    Channel Downloader — full interactive flow.

    Steps
    -----
    1. Prompt for a YouTube channel URL.
    2. Prompt for content type (Shorts / Videos / Both).
    3. Scan the channel with yt-dlp (no download, no links.txt written).
    4. Prompt for quality via the existing quality selector.
    5. Prompt for destination folder via the existing folder picker.
    6. Download every collected URL with the existing Downloader.
    """
    console.print()
    console.print(Rule("[bold cyan]Channel Downloader[/]"))
    console.print()

    # ── Step 1: channel URL ──────────────────────────────────────────────────
    console.print("  Supported formats: [dim]@username  /channel/UC…  /c/name[/]")
    console.print()
    channel_url = _prompt("Paste the YouTube channel URL")
    if not channel_url:
        console.print("[yellow]  No URL entered – returning to menu.[/]")
        _pause()
        return
    if not (channel_url.startswith("http://") or channel_url.startswith("https://")):
        console.print(
            "[yellow]  That doesn't look like a URL. "
            "Please include http:// or https://[/]"
        )
        _pause()
        return

    # ── Step 2: content type ─────────────────────────────────────────────────
    console.print()
    console.print(Rule("[bold cyan]Content Type[/]"))
    console.print()

    ct_table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
    ct_table.add_column("  #", style="bold cyan", justify="right", min_width=3)
    ct_table.add_column("  Option", style="white")
    ct_table.add_row("1", "Shorts Only")
    ct_table.add_row("2", "Videos Only")
    ct_table.add_row("3", "Shorts + Videos")
    console.print(ct_table)
    console.print()

    while True:
        ct_choice = _prompt("Choose content type [1–3]")
        if ct_choice in ("1", "2", "3"):
            break
        console.print("[yellow]  Please enter 1, 2, or 3.[/]")

    base = channel_url.rstrip("/")
    if ct_choice == "1":
        scan_urls: list[str] = [f"{base}/shorts"]
    elif ct_choice == "2":
        scan_urls = [f"{base}/videos"]
    else:
        scan_urls = [f"{base}/shorts", f"{base}/videos"]

    content_label = {"1": "Shorts", "2": "Videos", "3": "Shorts + Videos"}[ct_choice]

    # ── Step 3: scan with yt-dlp (no download, no file written) ─────────────
    console.print()
    console.print(f"  [dim]Scanning channel for {content_label}…[/]")
    console.print("  [dim](This may take a while for large channels)[/]")
    console.print()

    raw_urls: list[str] = []

    try:
        for scan_url in scan_urls:
            count_before = len(raw_urls)
            console.print(f"  [dim]→ {scan_url}[/]")

            page_urls = _collect_urls(scan_url)   # from channel_grabber
            raw_urls.extend(page_urls)

            found = len(raw_urls) - count_before
            console.print(
                f"  [green]✔[/]  Found [cyan]{found}[/] URL(s) in this pass."
            )

    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]✗  Scan error:[/] {exc}")
        log.exception("Channel downloader scan error: %s", exc)
        _pause()
        return

    if not raw_urls:
        console.print(
            "[yellow]  No URLs found. Check the channel URL and try again.[/]"
        )
        _pause()
        return

    # ── Deduplicate (preserve order) ─────────────────────────────────────────
    seen: set[str] = set()
    unique_urls: list[str] = []
    for u in raw_urls:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)

    duplicates_removed = len(raw_urls) - len(unique_urls)

    console.print()
    console.print(
        f"  [bold green]Scan complete.[/]  "
        f"Found [cyan]{len(unique_urls)}[/] unique URL(s)"
        + (f"  [dim]({duplicates_removed} duplicates removed)[/]" if duplicates_removed else "")
        + "."
    )

    # ── Step 4: quality ──────────────────────────────────────────────────────
    # pick_quality and pick_folder are imported from download_ui so we reuse
    # them without duplication.  No import from menu.py is needed here.
    quality = _pick_quality(url_for_probe=unique_urls[0] if unique_urls else None)

    # ── Step 5: destination folder ───────────────────────────────────────────
    folder = pick_folder(cfg)

    # ── Step 6: download via Downloader, no links.txt written ────────────────
    # Downloader reads from a file, so we give it an in-memory temp file.
    # The file is created in BASE_DIR (same pattern as the single-video and
    # retry flows) and deleted in the finally block regardless of outcome.
    tmp_file: Path | None = None
    try:
        fd, tmp_str = tempfile.mkstemp(
            prefix=".ytdl_channel_", suffix=".txt", dir=BASE_DIR
        )
        tmp_file = Path(tmp_str)
        with open(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(unique_urls) + "\n")

        log.info(
            "Channel download start: channel=%s type=%s urls=%d folder=%s quality=%s",
            channel_url, content_label, len(unique_urls), folder, quality or "best",
        )

        downloader = Downloader(links_file=tmp_file, videos_dir=folder, quality=quality)
        downloader.run()

    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]✗  Download failed:[/] {exc}")
        log.exception("Channel downloader error: %s", exc)

    finally:
        if tmp_file is not None:
            try:
                tmp_file.unlink(missing_ok=True)
            except OSError:
                pass

    log.info(
        "Channel download complete: channel=%s type=%s",
        channel_url, content_label,
    )

    _pause()
