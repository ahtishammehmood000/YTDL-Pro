"""
channel_grabber.py - Channel Link Grabber for YTDL-Pro.

Extracted from menu.py.  The single public entry-point is :func:`run`,
which menu.py calls as::

    from channel_grabber import run
    run(cfg)

How URL collection works
------------------------
The original code used ``extract_info(url, download=False)`` and then
inspected ``info["entries"]``.  That approach had two successive bugs:

Bug 1 – generator exhaustion (fixed in previous revision):
    ``info["entries"]`` is a lazy generator.  Calling ``list()`` on it
    for a debug print drained it, leaving nothing for the real loop.

Bug 2 – only the *first page* of entries was collected (this revision):
    Even with the generator fix, ``extract_info`` returns the raw
    playlist dict whose ``entries`` value is the generator from
    ``YoutubeTabIE._entries()``.  That generator *does* handle YouTube's
    continuation tokens internally, but only when it is fully consumed
    inside yt-dlp's own ``__process_playlist`` machinery.  When we call
    ``list(info["entries"])`` ourselves we consume the generator but we
    bypass the per-entry ``process_ie_result`` step; shelf-level entries
    (which are ``url_result`` objects pointing at another tab page, not
    individual videos) never get followed.  As a result we only get the
    entries that happened to land on the first page of the initial
    response (typically 2–9 items).

Root cause confirmed by reading:
    - ``YoutubeDL.__process_playlist`` (exhausts generator, processes
      each entry through ``process_ie_result``)
    - ``process_ie_result`` with ``result_type == 'url'`` and
      ``extract_flat='in_playlist'`` (calls ``__forced_printings`` then
      returns early — this is where the CLI prints each URL)
    - ``YoutubeTabIE._entries`` (yields both individual video
      ``url_result``s AND shelf ``url_result``s that point to sub-pages)

Fix – replicate exactly what the CLI does:
    Subclass ``YoutubeDL`` and override ``to_stdout`` to capture each
    line it would print.  Set ``forceprint={'video': ['%(url)s']}`` and
    ``simulate=True`` so yt-dlp walks the full playlist, follows every
    continuation page, processes every entry through the normal pipeline,
    and for each video-level entry calls ``__forced_printings`` →
    ``_forceprint`` → ``to_stdout``.  This is precisely the code path
    that ``yt-dlp --flat-playlist --print url CHANNEL_URL`` exercises,
    so the count will always match ``yt-dlp --flat-playlist … | wc -l``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yt_dlp as _yt_dlp

from rich import box
from rich.console import Console
from rich.rule import Rule
from rich.table import Table

import file_browser
from app_config import AppConfig
from downloader import BASE_DIR, Logger

console: Console = Console()
log: Logger = Logger()


# ---------------------------------------------------------------------------
# Helpers
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
# yt-dlp subclass that captures every URL the CLI would print
# ---------------------------------------------------------------------------


class _CapturingYDL(_yt_dlp.YoutubeDL):
    """
    YoutubeDL subclass that intercepts ``to_stdout`` so we can collect
    every URL that ``forceprint`` would normally write to the terminal.

    This is the same code path as ``yt-dlp --flat-playlist --print url``.
    """

    def __init__(self, params: dict) -> None:
        self.captured_lines: list[str] = []
        super().__init__(params)

    def to_stdout(self, message: str, skip_eol: bool = False, quiet=None) -> None:  # type: ignore[override]
        line = message.strip()
        if line:
            self.captured_lines.append(line)


def _collect_urls(scan_url: str) -> list[str]:
    """
    Walk *scan_url* exactly as the CLI does and return a list of video URLs.
    """
    # ── Capturing run (full processing, exactly like the CLI) ─────────────
    capture_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "simulate": True,
        "ignoreerrors": True,
        # Tell yt-dlp to print %(url)s for every video-level entry it
        # processes.  _CapturingYDL.to_stdout() will intercept each call.
        "forceprint": {"video": ["%(url)s"]},
    }
    with _CapturingYDL(capture_opts) as ydl:
        ydl.extract_info(scan_url, download=False)
        urls = list(ydl.captured_lines)

    return urls


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------


def run(cfg: AppConfig) -> None:
    """
    Channel Link Grabber — full interactive flow.

    Steps
    -----
    1. Ask for a YouTube channel URL (any supported format).
    2. Ask which content type: Shorts Only / Videos Only / Both.
    3. Use yt-dlp (full pipeline, no download) to enumerate every URL,
       following all continuation pages exactly as the CLI does.
    4. Deduplicate URLs.
    5. Ask where to save the resulting links.txt.
    6. Write one URL per line and report statistics.
    """
    console.print()
    console.print(Rule("[bold cyan]Channel Link Grabber[/]"))
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
            "[yellow]  That doesn't look like a URL. Please include http:// or https://[/]"
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

    # ── Step 3: scan with yt-dlp ─────────────────────────────────────────────
    console.print()
    console.print(f"  [dim]Scanning channel for {content_label}…[/]")
    console.print("  [dim](This may take a while for large channels)[/]")
    console.print()

    raw_urls: list[str] = []

    try:
        for scan_url in scan_urls:
            count_before = len(raw_urls)
            console.print(f"  [dim]→ {scan_url}[/]")

            page_urls = _collect_urls(scan_url)

            raw_urls.extend(page_urls)

            found = len(raw_urls) - count_before
            console.print(
                f"  [green]✔[/]  Found [cyan]{found}[/] URL(s) in this pass."
            )

    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]✗  Scan error:[/] {exc}")
        log.exception("Channel grabber scan error: %s", exc)
        _pause()
        return

    if not raw_urls:
        console.print(
            "[yellow]  No URLs found. Check the channel URL and try again.[/]"
        )
        _pause()
        return

    # ── Step 4: deduplicate ──────────────────────────────────────────────────
    seen_set: set[str] = set()
    unique_urls: list[str] = []
    for u in raw_urls:
        if u not in seen_set:
            seen_set.add(u)
            unique_urls.append(u)

    duplicates_removed = len(raw_urls) - len(unique_urls)

    # ── Step 5: choose save location ─────────────────────────────────────────
    console.print()
    console.print(Rule("[bold cyan]Save links.txt[/]"))
    console.print()

    save_options: list[tuple[str, Optional[Path]]] = []

    if cfg.last_download_folder is not None:
        label = str(cfg.last_download_folder)
        if len(label) > 52:
            label = "…" + label[-51:]
        save_options.append(
            (f"Last Used Folder  ({label})", cfg.last_download_folder)
        )

    save_options.append(("Browse for folder…", None))
    save_options.append(("Enter custom path…", Path("__custom__")))

    so_table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
    so_table.add_column("  #", style="bold cyan", justify="right", min_width=3)
    so_table.add_column("  Option", style="white")
    for i, (lbl, _) in enumerate(save_options, start=1):
        so_table.add_row(str(i), lbl)
    console.print(so_table)
    console.print()

    n_so = len(save_options)
    while True:
        raw_so = _prompt(f"Choose save location [1–{n_so}]")
        if raw_so.isdigit() and 0 <= int(raw_so) - 1 < n_so:
            break
        console.print(f"[yellow]  Please enter a number between 1 and {n_so}.[/]")

    so_idx = int(raw_so) - 1
    _, so_path = save_options[so_idx]

    if so_path is None:
        browsed = file_browser.select_folder()
        if browsed is None:
            console.print(
                "[yellow]  No folder selected – using last used folder or project root.[/]"
            )
            so_path = cfg.last_download_folder or BASE_DIR
        else:
            so_path = browsed
    elif so_path == Path("__custom__"):
        while True:
            raw_path = _prompt("Enter folder path")
            if raw_path:
                so_path = Path(raw_path).expanduser().resolve()
                break
            console.print("[yellow]  Path cannot be empty.[/]")

    so_path.mkdir(parents=True, exist_ok=True)
    output_file: Path = so_path / "links.txt"

    # ── Step 6: write file ───────────────────────────────────────────────────
    try:
        with output_file.open("w", encoding="utf-8") as fh:
            for u in unique_urls:
                fh.write(u + "\n")
    except OSError as exc:
        console.print(f"[bold red]✗  Could not write file:[/] {exc}")
        log.error("Channel grabber write error: %s", exc)
        _pause()
        return

    # ── Summary ──────────────────────────────────────────────────────────────
    console.print()
    console.rule()
    summary = Table(title="Channel Grab Summary", show_header=False, box=None)
    summary.add_column(style="bold", min_width=22)
    summary.add_column()
    summary.add_row("Channel", channel_url)
    summary.add_row("Content type", content_label)
    summary.add_row("Total found", str(len(raw_urls)))
    summary.add_row("Duplicates removed", str(duplicates_removed))
    summary.add_row("Saved", f"[green]{len(unique_urls)}[/] URL(s)")
    summary.add_row("Output file", str(output_file))
    console.print(summary)
    console.rule()

    log.info(
        "Channel grabber complete. channel=%s type=%s total=%d dupes=%d saved=%d file=%s",
        channel_url,
        content_label,
        len(raw_urls),
        duplicates_removed,
        len(unique_urls),
        output_file,
    )

    _pause()
