"""
downloader.py - Core download engine for YTDL-Pro.

Handles reading URLs from links.txt, downloading best-quality video+audio
via yt-dlp, merging to MP4, tracking history, saving metadata, and logging.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yt_dlp
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

# ---------------------------------------------------------------------------
# Project-level paths (resolved relative to this file so the module is
# importable from any working directory).
# ---------------------------------------------------------------------------
BASE_DIR: Path = Path(__file__).parent.resolve()
VIDEOS_DIR: Path = BASE_DIR / "Videos"
LOGS_DIR: Path = BASE_DIR / "Logs"
LINKS_FILE: Path = BASE_DIR / "links.txt"
HISTORY_FILE: Path = BASE_DIR / "history.json"
METADATA_FILE: Path = BASE_DIR / "metadata.txt"
FAILED_FILE: Path = BASE_DIR / "failed.txt"
LOG_FILE: Path = LOGS_DIR / "latest.log"

MAX_RETRIES: int = 3
RETRY_SLEEP: float = 3.0  # seconds between retry attempts

console: Console = Console()


# ===========================================================================
# Logger
# ===========================================================================

class Logger:
    """
    Configures and exposes a named :class:`logging.Logger` that writes to
    both *Logs/latest.log* (DEBUG+) and the console (WARNING+).

    Only one instance is necessary per process; subsequent instantiations
    with the same *name* reuse the existing handlers.
    """

    def __init__(self, name: str = "ytdl_pro") -> None:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        self._logger: logging.Logger = logging.getLogger(name)
        if self._logger.handlers:
            # Already configured – avoid duplicate handlers on re-import.
            return

        self._logger.setLevel(logging.DEBUG)

        fmt = logging.Formatter(
            fmt="%(asctime)s [%(levelname)-8s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # File handler – always DEBUG level.
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8", mode="w")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        self._logger.addHandler(fh)

        # Stream handler – WARNING and above so the Rich UI stays clean.
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.WARNING)
        sh.setFormatter(fmt)
        self._logger.addHandler(sh)

    # ------------------------------------------------------------------
    # Convenience proxies so callers can do ``logger.info(...)`` directly.
    # ------------------------------------------------------------------

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._logger.debug(msg, *args, **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._logger.info(msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._logger.warning(msg, *args, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._logger.error(msg, *args, **kwargs)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._logger.exception(msg, *args, **kwargs)


# ===========================================================================
# HistoryManager
# ===========================================================================

class HistoryManager:
    """
    Persists a JSON log of every completed download to *history.json*.

    Schema (one entry per download)::

        {
          "url": "https://...",
          "title": "Video title",
          "filename": "3.mp4",
          "downloaded_at": "2024-06-01T12:00:00+00:00",
          "duration_seconds": 312,
          "filesize_bytes": 48234567
        }
    """

    def __init__(self, path: Path = HISTORY_FILE, logger: Optional[Logger] = None) -> None:
        self._path = path
        self._log = logger or Logger()
        self._records: list[dict[str, Any]] = self._load()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                return data
            self._log.warning("history.json has unexpected format; resetting.")
            return []
        except (json.JSONDecodeError, OSError) as exc:
            self._log.error("Cannot read history file: %s", exc)
            return []

    def _save(self) -> None:
        try:
            with self._path.open("w", encoding="utf-8") as fh:
                json.dump(self._records, fh, indent=2, ensure_ascii=False)
        except OSError as exc:
            self._log.error("Cannot write history file: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def downloaded_urls(self) -> set[str]:
        """Return the set of URLs that have already been downloaded."""
        return {r["url"] for r in self._records}

    def record(
        self,
        *,
        url: str,
        title: str,
        filename: str,
        duration_seconds: Optional[int] = None,
        filesize_bytes: Optional[int] = None,
    ) -> None:
        """Append a successful download entry and persist to disk."""
        entry: dict[str, Any] = {
            "url": url,
            "title": title,
            "filename": filename,
            "downloaded_at": datetime.now(tz=timezone.utc).isoformat(),
            "duration_seconds": duration_seconds,
            "filesize_bytes": filesize_bytes,
        }
        self._records.append(entry)
        self._save()
        self._log.debug("History updated: %s → %s", url, filename)

    def all_records(self) -> list[dict[str, Any]]:
        return list(self._records)


# ===========================================================================
# MetadataManager
# ===========================================================================

class MetadataManager:
    """
    Appends human-readable metadata blocks to *metadata.txt* after each
    successful download.

    Each block is separated by a line of dashes so the file stays readable
    even when opened in a plain-text editor.
    """

    _SEPARATOR: str = "-" * 72

    def __init__(self, path: Path = METADATA_FILE, logger: Optional[Logger] = None) -> None:
        self._path = path
        self._log = logger or Logger()

    def append(self, info: dict[str, Any], filename: str) -> None:
        """
        Write a metadata block for a completed download.

        Parameters
        ----------
        info:
            The ``info_dict`` returned by yt-dlp after a successful download.
        filename:
            The final MP4 filename (e.g. ``"3.mp4"``).
        """
        lines: list[str] = [
            self._SEPARATOR,
            f"File       : {filename}",
            f"Title      : {info.get('title', 'N/A')}",
            f"URL        : {info.get('webpage_url', info.get('url', 'N/A'))}",
            f"Uploader   : {info.get('uploader', 'N/A')}",
            f"Upload Date: {self._fmt_date(info.get('upload_date'))}",
            f"Duration   : {self._fmt_duration(info.get('duration'))}",
            f"Views      : {info.get('view_count', 'N/A')}",
            f"Likes      : {info.get('like_count', 'N/A')}",
            f"Resolution : {info.get('resolution', 'N/A')}",
            f"FPS        : {info.get('fps', 'N/A')}",
            f"Saved At   : {datetime.now(tz=timezone.utc).isoformat()}",
            "",
        ]
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(lines))
        except OSError as exc:
            self._log.error("Cannot write metadata file: %s", exc)

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_date(upload_date: Optional[str]) -> str:
        if not upload_date:
            return "N/A"
        try:
            return datetime.strptime(upload_date, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            return upload_date

    @staticmethod
    def _fmt_duration(seconds: Optional[int | float]) -> str:
        if seconds is None:
            return "N/A"
        seconds = int(seconds)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m:02d}m {s:02d}s"
        return f"{m}m {s:02d}s"


# ===========================================================================
# ProgressTracker
# ===========================================================================

class ProgressTracker:
    """
    Wraps a Rich :class:`~rich.progress.Progress` bar and translates yt-dlp
    progress hook dictionaries into Rich task updates.

    Usage::

        tracker = ProgressTracker()
        with tracker:
            ydl_opts["progress_hooks"] = [tracker.hook]
            # … run yt-dlp …
    """

    def __init__(self) -> None:
        self._progress: Progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(bar_width=None),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        )
        self._task_id: Optional[TaskID] = None
        self._current_filename: str = ""

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "ProgressTracker":
        self._progress.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        self._progress.__exit__(*args)

    # ------------------------------------------------------------------
    # yt-dlp progress hook
    # ------------------------------------------------------------------

    def hook(self, d: dict[str, Any]) -> None:
        """Called by yt-dlp with progress information."""
        status: str = d.get("status", "")

        if status == "downloading":
            filename = Path(d.get("filename", "")).name
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)

            if self._task_id is None or filename != self._current_filename:
                # New file started (happens for each stream before merge).
                if self._task_id is not None:
                    self._progress.remove_task(self._task_id)
                self._current_filename = filename
                self._task_id = self._progress.add_task(
                    description=f"[cyan]{filename}",
                    total=total if total else None,
                )

            self._progress.update(
                self._task_id,
                completed=downloaded,
                total=total if total else None,
            )

        elif status in ("finished", "error"):
            if self._task_id is not None:
                self._progress.remove_task(self._task_id)
                self._task_id = None
            self._current_filename = ""

    def set_description(self, text: str) -> None:
        """Update the description of the current active task."""
        if self._task_id is not None:
            self._progress.update(self._task_id, description=text)


# ===========================================================================
# Downloader
# ===========================================================================

class Downloader:
    """
    Orchestrates the full download pipeline:

    1. Read & deduplicate URLs from *links.txt*.
    2. Skip URLs already in *history.json*.
    3. For each URL, attempt up to ``MAX_RETRIES`` times via yt-dlp.
    4. Number output files sequentially, continuing from the highest
       existing number in *Videos/*.
    5. On success  → update history + metadata.
    6. On failure  → append to *failed.txt*.
    7. Gracefully handle SIGINT (Ctrl+C).

    Parameters
    ----------
    links_file:
        Path to the plain-text file containing one YouTube URL per line.
    videos_dir:
        Directory where MP4 files are saved.
    """

    def __init__(
        self,
        links_file: Path = LINKS_FILE,
        videos_dir: Path = VIDEOS_DIR,
        logger: Optional[Logger] = None,
    ) -> None:
        self._links_file = links_file
        self._videos_dir = videos_dir
        self._log = logger or Logger()
        self._history = HistoryManager(logger=self._log)
        self._metadata = MetadataManager(logger=self._log)
        self._interrupted: bool = False

        # Ensure output directories exist.
        self._videos_dir.mkdir(parents=True, exist_ok=True)
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        # Register graceful Ctrl+C handler.
        signal.signal(signal.SIGINT, self._handle_sigint)

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------

    def _handle_sigint(self, signum: int, frame: Any) -> None:  # noqa: ARG002
        console.print("\n[bold yellow]⚠  Interrupted by user. Finishing current item…[/]")
        self._log.warning("SIGINT received – stopping after current download.")
        self._interrupted = True

    # ------------------------------------------------------------------
    # URL loading & deduplication
    # ------------------------------------------------------------------

    def _load_urls(self) -> list[str]:
        """
        Read *links.txt*, drop empty lines and duplicates, and return an
        ordered list of URLs not yet present in download history.
        """
        if not self._links_file.exists():
            self._log.warning("links.txt not found at %s", self._links_file)
            console.print(f"[bold red]✗  links.txt not found:[/] {self._links_file}")
            return []

        seen: set[str] = set()
        urls: list[str] = []
        with self._links_file.open("r", encoding="utf-8") as fh:
            for raw in fh:
                url = raw.strip()
                if not url or url.startswith("#"):
                    continue
                if url in seen:
                    self._log.debug("Duplicate skipped: %s", url)
                    continue
                seen.add(url)
                urls.append(url)

        already_done = self._history.downloaded_urls
        pending = [u for u in urls if u not in already_done]
        skipped = len(urls) - len(pending)
        if skipped:
            console.print(
                f"[dim]↷  Skipping {skipped} already-downloaded URL(s).[/]"
            )
        self._log.info(
            "Loaded %d URL(s) from links.txt (%d pending, %d skipped).",
            len(urls),
            len(pending),
            skipped,
        )
        return pending

    # ------------------------------------------------------------------
    # Output file numbering
    # ------------------------------------------------------------------

    def _next_number(self) -> int:
        """
        Return the next integer to use for output filenames by scanning
        *Videos/* for files matching the pattern ``<N>.mp4``.
        """
        max_n: int = 0
        for entry in self._videos_dir.iterdir():
            if entry.suffix.lower() == ".mp4" and entry.stem.isdigit():
                max_n = max(max_n, int(entry.stem))
        return max_n + 1

    # ------------------------------------------------------------------
    # yt-dlp option builder
    # ------------------------------------------------------------------

    def _build_ydl_opts(
        self,
        output_path: Path,
        progress_tracker: ProgressTracker,
    ) -> dict[str, Any]:
        """
        Construct the yt-dlp options dictionary for a single download.

        * ``bestvideo+bestaudio/best`` → always highest quality.
        * ``ffmpeg`` postprocessor merges streams into MP4.
        * Cookies/auth left to the user's environment.
        """
        return {
            # Quality: best video merged with best audio, fallback to best single stream.
            "format": "bestvideo+bestaudio/best",
            # Output template: the caller already determined the final name.
            "outtmpl": str(output_path.with_suffix(".%(ext)s")),
            # Merge everything into MP4.
            "merge_output_format": "mp4",
            "postprocessors": [
                {
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "mp4",
                }
            ],
            # Retry configuration.
            "retries": MAX_RETRIES,
            "fragment_retries": MAX_RETRIES,
            # Progress.
            "progress_hooks": [progress_tracker.hook],
            # Suppress yt-dlp's own stdout chatter; we handle output via Rich.
            "quiet": True,
            "no_warnings": False,
            # Write thumbnail – useful for metadata; skip on error.
            "writethumbnail": False,
            # Support playlists (individual URLs work too).
            "noplaylist": False,
            # Allow continuation of partial downloads.
            "continuedl": True,
            # Use ffmpeg for post-processing.
            "prefer_ffmpeg": True,
            # Geo-bypass attempts.
            "geo_bypass": True,
        }

    # ------------------------------------------------------------------
    # Single URL download (with retry loop)
    # ------------------------------------------------------------------

    def _download_one(
        self,
        url: str,
        number: int,
        progress_tracker: ProgressTracker,
    ) -> bool:
        """
        Download a single URL (or playlist) with up to ``MAX_RETRIES`` attempts.

        Returns ``True`` on success, ``False`` on permanent failure.
        """
        output_stem = str(number)
        output_path = self._videos_dir / output_stem  # ext added by yt-dlp

        ydl_opts = self._build_ydl_opts(output_path, progress_tracker)

        for attempt in range(1, MAX_RETRIES + 1):
            if self._interrupted:
                return False

            self._log.info(
                "Attempt %d/%d for URL: %s", attempt, MAX_RETRIES, url
            )

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info: dict[str, Any] = ydl.extract_info(url, download=True)

                # yt-dlp may wrap playlist entries; unwrap if needed.
                entries = info.get("entries")
                if entries:
                    # Playlist: record each entry individually.
                    for entry in entries:
                        if entry:
                            self._on_success(url, entry, f"{output_stem}.mp4")
                else:
                    self._on_success(url, info, f"{output_stem}.mp4")

                return True

            except yt_dlp.utils.DownloadError as exc:
                self._log.error(
                    "DownloadError on attempt %d for %s: %s", attempt, url, exc
                )
                if attempt < MAX_RETRIES:
                    console.print(
                        f"  [yellow]⟳  Retry {attempt}/{MAX_RETRIES - 1} in "
                        f"{RETRY_SLEEP:.0f}s…[/]"
                    )
                    time.sleep(RETRY_SLEEP)
                else:
                    console.print(f"  [bold red]✗  All retries exhausted for:[/] {url}")

            except Exception as exc:  # noqa: BLE001
                self._log.exception("Unexpected error on attempt %d: %s", attempt, exc)
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_SLEEP)

        return False

    # ------------------------------------------------------------------
    # Post-download callbacks
    # ------------------------------------------------------------------

    def _on_success(
        self,
        url: str,
        info: dict[str, Any],
        filename: str,
    ) -> None:
        title: str = info.get("title", "Unknown Title")
        duration: Optional[int] = info.get("duration")
        filesize: Optional[int] = info.get("filesize") or info.get("filesize_approx")

        self._history.record(
            url=url,
            title=title,
            filename=filename,
            duration_seconds=duration,
            filesize_bytes=filesize,
        )
        self._metadata.append(info, filename)
        self._log.info("SUCCESS: %s → %s", url, filename)

    def _on_failure(self, url: str) -> None:
        self._log.error("FAILED: %s", url)
        try:
            with FAILED_FILE.open("a", encoding="utf-8") as fh:
                fh.write(url + "\n")
        except OSError as exc:
            self._log.error("Cannot write to failed.txt: %s", exc)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Execute the full download queue.

        Reads URLs from *links.txt*, downloads each one sequentially,
        and updates history, metadata, and failed logs accordingly.
        Handles Ctrl+C gracefully without corrupting output files.
        """
        console.rule("[bold cyan]YTDL-Pro[/]")
        urls = self._load_urls()

        if not urls:
            console.print("[bold yellow]No pending URLs to download.[/]")
            return

        console.print(f"[green]✔  {len(urls)} URL(s) queued for download.[/]\n")

        total = len(urls)
        success_count = 0
        fail_count = 0

        with ProgressTracker() as tracker:
            for idx, url in enumerate(urls, start=1):
                if self._interrupted:
                    console.print("\n[bold yellow]Download queue stopped by user.[/]")
                    break

                number = self._next_number()
                console.print(
                    f"[bold]({idx}/{total})[/] Downloading → "
                    f"[cyan]{number}.mp4[/]\n  [dim]{url}[/]"
                )
                self._log.info("Starting download %d/%d: %s", idx, total, url)

                ok = self._download_one(url, number, tracker)

                if ok:
                    success_count += 1
                    console.print(
                        f"  [bold green]✔  Saved as {number}.mp4[/]\n"
                    )
                else:
                    fail_count += 1
                    self._on_failure(url)
                    console.print()  # visual spacing

        # ------------------------------------------------------------------
        # Summary table
        # ------------------------------------------------------------------
        self._print_summary(success_count, fail_count, total)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _print_summary(
        self, success: int, failed: int, total: int
    ) -> None:
        console.rule()
        table = Table(title="Download Summary", show_header=False, box=None)
        table.add_column(style="bold", min_width=20)
        table.add_column()

        table.add_row("Total queued", str(total))
        table.add_row("✔  Succeeded", f"[green]{success}[/]")
        table.add_row("✗  Failed", f"[red]{failed}[/]" if failed else "[green]0[/]")
        table.add_row("Output directory", str(self._videos_dir))
        table.add_row("Log file", str(LOG_FILE))
        if failed:
            table.add_row("Failed URLs", str(FAILED_FILE))

        console.print(table)
        console.rule()

        self._log.info(
            "Run complete. Success: %d / %d. Failed: %d.",
            success,
            total,
            failed,
        )
