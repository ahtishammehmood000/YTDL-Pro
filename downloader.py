"""
downloader.py - Core download engine for YTDL-Pro.

Handles reading URLs from links.txt, downloading best-quality video+audio
via yt-dlp, merging to MP4, tracking history, saving metadata, and logging.

Public surface (unchanged for backward compatibility)
-----------------------------------------------------
Constants / paths
    BASE_DIR, VIDEOS_DIR, LOGS_DIR, LINKS_FILE, HISTORY_FILE,
    METADATA_FILE, FAILED_FILE, LOG_FILE, MAX_RETRIES, RETRY_SLEEP

Classes
    Logger          – structured file + stderr logging
    Downloader      – full download pipeline orchestrator

Re-exported for callers that previously imported from this module
    MetadataRecord  – from metadata.py
    HistoryManager  – from metadata.py
    MetadataManager – from metadata.py
    ExcelMetadataManager – from metadata.py
    ProgressTracker – from ui.py

Metadata fields recorded per download
--------------------------------------
    file_number, filename, title, channel, channel_id, video_id,
    duration_seconds, duration_formatted, upload_date, upload_timestamp,
    resolution, fps, video_codec, audio_codec, filesize_bytes,
    filesize_formatted, description, hashtags, thumbnail_url, url,
    downloaded_at, download_status
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any, Optional

import yt_dlp
from rich.console import Console
from rich.table import Table

# ---------------------------------------------------------------------------
# Internal modules
# ---------------------------------------------------------------------------
from config import (
    BASE_DIR,
    FAILED_FILE,
    HISTORY_FILE,
    LINKS_FILE,
    LOG_FILE,
    LOGS_DIR,
    MAX_RETRIES,
    METADATA_FILE,
    RETRY_SLEEP,
    VIDEOS_DIR,
)
from metadata import (
    ExcelMetadataManager,
    HistoryManager,
    MetadataManager,
    MetadataRecord,
)
from ui import ProgressTracker

# ---------------------------------------------------------------------------
# Re-export everything that main.py (and any external callers) previously
# imported directly from downloader.py.  This keeps backward compatibility
# without any changes to main.py's import block.
# ---------------------------------------------------------------------------
__all__ = [
    # paths / constants
    "BASE_DIR",
    "VIDEOS_DIR",
    "LOGS_DIR",
    "LINKS_FILE",
    "HISTORY_FILE",
    "METADATA_FILE",
    "FAILED_FILE",
    "LOG_FILE",
    "MAX_RETRIES",
    "RETRY_SLEEP",
    # classes defined here
    "Logger",
    "Downloader",
    # re-exported from metadata.py
    "MetadataRecord",
    "HistoryManager",
    "MetadataManager",
    "ExcelMetadataManager",
    # re-exported from ui.py
    "ProgressTracker",
]

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
    5. Playlist entries each receive their own unique sequential number.
    6. On success  → build :class:`~metadata.MetadataRecord` with final
       on-disk metadata, update history + metadata.
    7. On failure  → append to *failed.txt* with reason.
    8. Gracefully handle SIGINT (Ctrl+C).

    Parameters
    ----------
    links_file:
        Path to the plain-text file containing one YouTube URL per line.
    videos_dir:
        Directory where MP4 files are saved.
    logger:
        Optional :class:`Logger` instance; a new one is created if omitted.
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
        self._metadata = MetadataManager(logger=self._log, videos_dir=self._videos_dir)
        self._excel_metadata = ExcelMetadataManager(logger=self._log, videos_dir=self._videos_dir)
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
        Read *links.txt*, drop comments, empty lines and duplicates, and
        return an ordered list of URLs not yet present in download history.
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

        Called once per individual video (including each playlist entry) so
        that every output file receives a unique sequential number.
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
            # Quality: best video merged with best audio, fallback to best
            # single stream.
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
        Download a single URL (or playlist) with up to ``MAX_RETRIES``
        attempts.

        For playlists, each entry is recorded separately with its own
        unique sequential file number so that filenames are never reused.

        Returns ``True`` on success, ``False`` on permanent failure.
        """
        output_stem = str(number)
        output_path = self._videos_dir / output_stem  # ext added by yt-dlp

        ydl_opts = self._build_ydl_opts(output_path, progress_tracker)

        last_error: str = "unknown error"

        for attempt in range(1, MAX_RETRIES + 1):
            if self._interrupted:
                return False

            self._log.info(
                "Attempt %d/%d for URL: %s", attempt, MAX_RETRIES, url
            )

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info: dict[str, Any] = ydl.extract_info(url, download=True)

                entries = info.get("entries")
                if entries:
                    # --------------------------------------------------------
                    # Playlist: every entry gets its own unique file number.
                    # The first entry reuses ``number`` (already allocated by
                    # the caller); subsequent entries claim fresh numbers.
                    # --------------------------------------------------------
                    entry_number = number
                    for idx, entry in enumerate(entries):
                        if not entry:
                            continue
                        if idx > 0:
                            # Allocate a new number for each additional entry.
                            entry_number = self._next_number()
                            console.print(
                                f"  [dim]Playlist entry {idx + 1} → "
                                f"[cyan]{entry_number}.mp4[/][/]"
                            )
                        entry_filename = f"{entry_number}.mp4"
                        entry_path = self._videos_dir / entry_filename
                        self._on_success(
                            original_url=url,
                            info=entry,
                            file_number=entry_number,
                            filename=entry_filename,
                            final_path=entry_path,
                        )
                else:
                    # Single video.
                    filename = f"{output_stem}.mp4"
                    final_path = self._videos_dir / filename
                    self._on_success(
                        original_url=url,
                        info=info,
                        file_number=number,
                        filename=filename,
                        final_path=final_path,
                    )

                return True

            except yt_dlp.utils.DownloadError as exc:
                last_error = str(exc)
                self._log.error(
                    "DownloadError on attempt %d/%d for %s: %s",
                    attempt,
                    MAX_RETRIES,
                    url,
                    last_error,
                )
                if attempt < MAX_RETRIES:
                    console.print(
                        f"  [yellow]⟳  Retry {attempt}/{MAX_RETRIES - 1} in "
                        f"{RETRY_SLEEP:.0f}s…[/]"
                    )
                    time.sleep(RETRY_SLEEP)
                else:
                    console.print(
                        f"  [bold red]✗  All retries exhausted for:[/] {url}"
                    )

            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                self._log.exception(
                    "Unexpected error on attempt %d/%d for %s: %s",
                    attempt,
                    MAX_RETRIES,
                    url,
                    last_error,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_SLEEP)

        # All attempts exhausted – record why.
        self._log.error(
            "FAILED after %d attempts for %s – reason: %s",
            MAX_RETRIES,
            url,
            last_error,
        )
        return False

    # ------------------------------------------------------------------
    # Post-download callbacks
    # ------------------------------------------------------------------

    def _on_success(
        self,
        *,
        original_url: str,
        info: dict[str, Any],
        file_number: int,
        filename: str,
        final_path: Optional[Path] = None,
    ) -> None:
        """
        Build a :class:`~metadata.MetadataRecord` from the yt-dlp info dict
        — using the finished on-disk file for accurate size/codec data —
        then persist it to both *history.json* and *metadata.txt*.

        Parameters
        ----------
        original_url:
            The URL submitted to the downloader (fallback when
            ``info["webpage_url"]`` is absent).
        info:
            Raw yt-dlp ``info_dict`` for a single video.
        file_number:
            Sequential output number.
        filename:
            Final MP4 filename (e.g. ``"3.mp4"``).
        final_path:
            Absolute path to the finished file; used to read exact size.
        """
        meta = MetadataRecord.from_info(
            info,
            file_number=file_number,
            filename=filename,
            url=original_url,
            final_path=final_path,
        )
        self._history.record(meta)
        self._metadata.append(meta)
        self._excel_metadata.append(meta)

        # Detailed success log line.
        self._log.info(
            "SUCCESS | #%d  %s | title=%r | channel=%r | video_id=%s"
            " | duration=%s | size=%s",
            meta.file_number,
            meta.filename,
            meta.title,
            meta.channel,
            meta.video_id or "N/A",
            meta.duration_formatted,
            meta.filesize_formatted,
        )

    def _on_failure(self, url: str, reason: str = "") -> None:
        """
        Append *url* to *failed.txt* and write a structured failure log
        entry that includes the reason for the failure.
        """
        self._log.error(
            "FAILED | url=%s%s",
            url,
            f" | reason={reason}" if reason else "",
        )
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

        # Summary table
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
        table.add_row(
            "✗  Failed",
            f"[red]{failed}[/]" if failed else "[green]0[/]",
        )
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
