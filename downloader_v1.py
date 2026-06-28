"""
downloader.py - Core download engine for YTDL-Pro.

Handles reading URLs from links.txt, downloading best-quality video+audio
via yt-dlp, merging to MP4, tracking history, saving metadata, and logging.

Metadata system records the following fields for every download:
    file_number, filename, title, channel, channel_id, video_id,
    duration_seconds, duration_formatted, upload_date, upload_timestamp,
    resolution, fps, video_codec, audio_codec, filesize_bytes,
    filesize_formatted, description, hashtags, thumbnail_url, url,
    downloaded_at, download_status
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import tempfile
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
# MetadataRecord
# ===========================================================================

class MetadataRecord:
    """
    Canonical data structure for a single download's metadata.

    Constructed from a raw yt-dlp ``info_dict`` via :meth:`from_info`, then
    consumed by both :class:`HistoryManager` (JSON) and
    :class:`MetadataManager` (plain-text).

    Fields
    ------
    file_number : int
        Sequential output number (matches the stem of ``filename``).
    filename : str
        Final MP4 filename, e.g. ``"3.mp4"``.
    title : str
        Video title as reported by the platform.
    channel : str
        Channel / uploader display name.
    channel_id : str | None
        Platform channel identifier (e.g. YouTube channel ID).
    video_id : str | None
        Platform video identifier (e.g. YouTube video ID).
    duration_seconds : int | None
        Duration in whole seconds.
    duration_formatted : str
        Human-readable duration (``"1h 02m 33s"``).
    upload_date : str | None
        Upload date in ``YYYY-MM-DD`` format.
    upload_timestamp : int | None
        Unix timestamp of the upload, if available.
    resolution : str | None
        Video resolution string (e.g. ``"1920x1080"``).
    fps : float | None
        Frames per second.
    video_codec : str | None
        Video codec string (e.g. ``"avc1.640028"``).
    audio_codec : str | None
        Audio codec string (e.g. ``"mp4a.40.2"``).
    filesize_bytes : int | None
        File size in bytes (exact, sourced from the final file on disk when
        available, otherwise from yt-dlp's reported/estimated value).
    filesize_formatted : str
        Human-readable file size (``"45.2 MB"``).
    description : str | None
        Full, untruncated video description text.
    hashtags : list[str]
        Hashtags in original order (description first, then tags),
        deduplicated while preserving first-seen order.
    thumbnail_url : str | None
        URL of the best available thumbnail.
    url : str
        Original URL that was passed to yt-dlp.
    downloaded_at : str
        ISO-8601 UTC timestamp of when the download completed.
    download_status : str
        Always ``"success"`` when constructed via :meth:`from_info`.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        *,
        file_number: int,
        filename: str,
        title: str,
        channel: str,
        channel_id: Optional[str],
        video_id: Optional[str],
        duration_seconds: Optional[int],
        duration_formatted: str,
        upload_date: Optional[str],
        upload_timestamp: Optional[int],
        resolution: Optional[str],
        fps: Optional[float],
        video_codec: Optional[str],
        audio_codec: Optional[str],
        filesize_bytes: Optional[int],
        filesize_formatted: str,
        description: Optional[str],
        hashtags: list[str],
        thumbnail_url: Optional[str],
        url: str,
        downloaded_at: str,
        download_status: str,
    ) -> None:
        self.file_number = file_number
        self.filename = filename
        self.title = title
        self.channel = channel
        self.channel_id = channel_id
        self.video_id = video_id
        self.duration_seconds = duration_seconds
        self.duration_formatted = duration_formatted
        self.upload_date = upload_date
        self.upload_timestamp = upload_timestamp
        self.resolution = resolution
        self.fps = fps
        self.video_codec = video_codec
        self.audio_codec = audio_codec
        self.filesize_bytes = filesize_bytes
        self.filesize_formatted = filesize_formatted
        self.description = description
        self.hashtags = hashtags
        self.thumbnail_url = thumbnail_url
        self.url = url
        self.downloaded_at = downloaded_at
        self.download_status = download_status

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_info(
        cls,
        info: dict[str, Any],
        *,
        file_number: int,
        filename: str,
        url: str,
        final_path: Optional[Path] = None,
    ) -> "MetadataRecord":
        """
        Build a :class:`MetadataRecord` from a yt-dlp ``info_dict``.

        When ``final_path`` is supplied and the file exists on disk, exact
        values for filesize, resolution, and codec are taken directly from
        the finished file rather than from yt-dlp's pre-merge estimates.

        Parameters
        ----------
        info:
            The dict returned by ``YoutubeDL.extract_info()``.
        file_number:
            Sequential output number for this download.
        filename:
            Final MP4 filename (e.g. ``"3.mp4"``).
        url:
            The original URL submitted to the downloader.
        final_path:
            Absolute path to the finished MP4 file on disk (optional).
            Used to derive the exact file size after merging.
        """
        raw_duration: Optional[int | float] = info.get("duration")
        duration_seconds: Optional[int] = (
            int(raw_duration) if raw_duration is not None else None
        )

        raw_date: Optional[str] = info.get("upload_date")
        upload_date: Optional[str] = cls._parse_upload_date(raw_date)

        # Prefer explicit timestamp; fall back to deriving it from upload_date.
        upload_timestamp: Optional[int] = info.get("timestamp")
        if upload_timestamp is None and upload_date:
            try:
                dt = datetime.strptime(upload_date, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
                upload_timestamp = int(dt.timestamp())
            except ValueError:
                pass

        # ----------------------------------------------------------------
        # File size: prefer the real on-disk size of the merged file.
        # ----------------------------------------------------------------
        filesize: Optional[int] = None
        if final_path is not None and final_path.exists():
            try:
                filesize = final_path.stat().st_size
            except OSError:
                pass
        if filesize is None:
            filesize = info.get("filesize") or info.get("filesize_approx")

        # ----------------------------------------------------------------
        # Resolution: prefer the explicit field; derive from width×height
        # if absent.  For the merged output the top-level fields reflect
        # the best video stream selected.
        # ----------------------------------------------------------------
        resolution: Optional[str] = info.get("resolution")
        if not resolution:
            w, h = info.get("width"), info.get("height")
            if w and h:
                resolution = f"{w}x{h}"

        # ----------------------------------------------------------------
        # Codec strings may be composite (e.g. "vp9+opus"); keep as-is.
        # Normalise the literal string "none" that yt-dlp emits for absent
        # streams.
        # ----------------------------------------------------------------
        video_codec: Optional[str] = info.get("vcodec") or None
        audio_codec: Optional[str] = info.get("acodec") or None
        if video_codec == "none":
            video_codec = None
        if audio_codec == "none":
            audio_codec = None

        fps_raw = info.get("fps")
        fps: Optional[float] = float(fps_raw) if fps_raw is not None else None

        hashtags = cls._extract_hashtags(
            description=info.get("description", ""),
            tags=info.get("tags") or [],
        )

        thumbnail_url: Optional[str] = cls._best_thumbnail(info)

        return cls(
            file_number=file_number,
            filename=filename,
            title=info.get("title") or "Unknown Title",
            channel=info.get("uploader") or info.get("channel") or "Unknown Channel",
            channel_id=info.get("channel_id") or info.get("uploader_id"),
            video_id=info.get("id"),
            duration_seconds=duration_seconds,
            duration_formatted=cls._fmt_duration(duration_seconds),
            upload_date=upload_date,
            upload_timestamp=upload_timestamp,
            resolution=resolution,
            fps=fps,
            video_codec=video_codec,
            audio_codec=audio_codec,
            filesize_bytes=filesize,
            filesize_formatted=cls._fmt_size(filesize),
            description=info.get("description"),  # full, never truncated
            hashtags=hashtags,
            thumbnail_url=thumbnail_url,
            url=info.get("webpage_url") or url,
            downloaded_at=datetime.now(tz=timezone.utc).isoformat(),
            download_status="success",
        )

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict of all fields."""
        return {
            "file_number": self.file_number,
            "filename": self.filename,
            "title": self.title,
            "channel": self.channel,
            "channel_id": self.channel_id,
            "video_id": self.video_id,
            "duration_seconds": self.duration_seconds,
            "duration_formatted": self.duration_formatted,
            "upload_date": self.upload_date,
            "upload_timestamp": self.upload_timestamp,
            "resolution": self.resolution,
            "fps": self.fps,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "filesize_bytes": self.filesize_bytes,
            "filesize_formatted": self.filesize_formatted,
            "description": self.description,
            "hashtags": self.hashtags,
            "thumbnail_url": self.thumbnail_url,
            "url": self.url,
            "downloaded_at": self.downloaded_at,
            "download_status": self.download_status,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_upload_date(raw: Optional[str]) -> Optional[str]:
        """Convert yt-dlp's ``YYYYMMDD`` string to ``YYYY-MM-DD``."""
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            return raw

    @staticmethod
    def _fmt_duration(seconds: Optional[int]) -> str:
        if seconds is None:
            return "N/A"
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m:02d}m {s:02d}s"
        return f"{m}m {s:02d}s"

    @staticmethod
    def _fmt_size(size_bytes: Optional[int]) -> str:
        if size_bytes is None:
            return "N/A"
        value: float = float(size_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024:
                return f"{value:.2f} {unit}"
            value /= 1024
        return f"{value:.2f} PB"

    @staticmethod
    def _extract_hashtags(
        description: Optional[str],
        tags: list[str],
    ) -> list[str]:
        """
        Return a deduplicated list of hashtags preserving first-seen order.

        Hashtags are sourced first from inline ``#Tag`` patterns in the
        video description, then from the platform tags list.  Alphabetical
        sorting is intentionally avoided so the original order is kept.
        """
        seen: set[str] = set()
        result: list[str] = []

        def _add(tag: str) -> None:
            key = tag.lower()
            if key not in seen:
                seen.add(key)
                result.append(tag)

        # Mine inline hashtags from the description (preserves order).
        if description:
            for match in re.finditer(r"#(\w+)", description):
                _add(f"#{match.group(1)}")

        # Include explicit platform tags in their original order.
        for tag in tags:
            if tag:
                normalised = tag.strip()
                if normalised:
                    _add(
                        normalised
                        if normalised.startswith("#")
                        else f"#{normalised}"
                    )

        return result

    @staticmethod
    def _best_thumbnail(info: dict[str, Any]) -> Optional[str]:
        """
        Select the highest-resolution thumbnail URL from the info dict.

        yt-dlp provides a ``thumbnails`` list ordered by ascending
        preference; we pick the last entry (highest quality) or fall back
        to the top-level ``thumbnail`` key.
        """
        thumbnails: list[dict[str, Any]] = info.get("thumbnails") or []
        if thumbnails:
            with_url = [t for t in thumbnails if t.get("url")]
            if with_url:
                return with_url[-1]["url"]
        return info.get("thumbnail")


# ===========================================================================
# HistoryManager
# ===========================================================================

class HistoryManager:
    """
    Persists a structured JSON log of every completed download to
    *history.json*.

    Each record is a full :class:`MetadataRecord` serialised via
    :meth:`MetadataRecord.to_dict`, providing rich metadata for all
    downstream consumers (CLI ``history`` command, external scripts, etc.).

    The ``downloaded_urls`` property is used by :class:`Downloader` to skip
    URLs that have already been processed in previous runs.

    Writes are performed atomically: the JSON payload is written to a
    sibling temporary file first, then renamed over the target.  This
    guarantees that *history.json* is never left in a partially-written
    (corrupt) state if the process crashes mid-write.
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
        """
        Write records to disk atomically.

        The payload is serialised to a temporary file in the same directory
        as *history.json*, then ``os.replace()`` renames it into place.
        Because ``os.replace()`` is atomic on POSIX (and near-atomic on
        Windows via the same-volume rename guarantee), the target file is
        always either the old version or the new version — never a partial
        write.
        """
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)

        try:
            # Use a temp file in the same directory to ensure same-filesystem
            # rename (required for atomic replace on most OS/fs combinations).
            fd, tmp_path_str = tempfile.mkstemp(
                dir=parent, prefix=".history_tmp_", suffix=".json"
            )
            tmp_path = Path(tmp_path_str)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._records, fh, indent=2, ensure_ascii=False)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, self._path)
            except Exception:
                # Clean up the orphaned temp file on any error.
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
        except OSError as exc:
            self._log.error("Cannot write history file: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def downloaded_urls(self) -> set[str]:
        """Return the set of URLs that have already been downloaded."""
        return {r["url"] for r in self._records}

    def record(self, meta: MetadataRecord) -> None:
        """
        Append the full metadata record for a successful download and
        persist the updated list to disk atomically.

        Parameters
        ----------
        meta:
            Populated :class:`MetadataRecord` instance.
        """
        self._records.append(meta.to_dict())
        self._save()
        self._log.debug(
            "History updated: %s → %s (video_id=%s)",
            meta.url,
            meta.filename,
            meta.video_id,
        )

    def all_records(self) -> list[dict[str, Any]]:
        """Return a shallow copy of all history records."""
        return list(self._records)


# ===========================================================================
# MetadataManager
# ===========================================================================

class MetadataManager:
    """
    Appends a structured, human-readable metadata block to *metadata.txt*
    after each successful download.

    The block uses clearly labelled sections enclosed between separator
    rules, making the file readable in any plain-text viewer while remaining
    trivially parseable by scripts.  Descriptions are stored in full — never
    truncated.
    """

    _OUTER_SEP: str = "=" * 72
    _INNER_SEP: str = "-" * 72

    def __init__(self, path: Path = METADATA_FILE, logger: Optional[Logger] = None) -> None:
        self._path = path
        self._log = logger or Logger()

    def append(self, meta: MetadataRecord) -> None:
        """
        Write a structured metadata block for a completed download.

        Parameters
        ----------
        meta:
            Populated :class:`MetadataRecord` instance.
        """
        hashtag_str = (
            "  ".join(meta.hashtags) if meta.hashtags else "N/A"
        )
        description_block = self._format_description(meta.description)

        lines: list[str] = [
            self._OUTER_SEP,
            "  YTDL-Pro  |  Download Record",
            self._OUTER_SEP,
            "",
            # ── General Information ─────────────────────────────────────
            "  General Information",
            self._INNER_SEP,
            f"  File Number      : {meta.file_number}",
            f"  Filename         : {meta.filename}",
            f"  Original URL     : {meta.url}",
            "",
            # ── Video Information ────────────────────────────────────────
            "  Video Information",
            self._INNER_SEP,
            f"  Title            : {meta.title}",
            f"  Channel          : {meta.channel}",
            f"  Channel ID       : {meta.channel_id or 'N/A'}",
            f"  Video ID         : {meta.video_id or 'N/A'}",
            f"  Upload Date      : {meta.upload_date or 'N/A'}",
            f"  Upload Timestamp : {meta.upload_timestamp if meta.upload_timestamp is not None else 'N/A'}",
            f"  Thumbnail URL    : {meta.thumbnail_url or 'N/A'}",
            "",
            # ── Technical Information ────────────────────────────────────
            "  Technical Information",
            self._INNER_SEP,
            f"  Duration         : {meta.duration_formatted}",
            f"  Duration (sec)   : {meta.duration_seconds if meta.duration_seconds is not None else 'N/A'}",
            f"  Resolution       : {meta.resolution or 'N/A'}",
            f"  FPS              : {meta.fps if meta.fps is not None else 'N/A'}",
            f"  Video Codec      : {meta.video_codec or 'N/A'}",
            f"  Audio Codec      : {meta.audio_codec or 'N/A'}",
            f"  File Size        : {meta.filesize_formatted}",
            f"  File Size (bytes): {meta.filesize_bytes if meta.filesize_bytes is not None else 'N/A'}",
            "",
            # ── Hashtags ─────────────────────────────────────────────────
            "  Hashtags",
            self._INNER_SEP,
            f"  {hashtag_str}",
            "",
            # ── Description ──────────────────────────────────────────────
            "  Description",
            self._INNER_SEP,
            description_block,
            "",
            # ── Download Information ──────────────────────────────────────
            "  Download Information",
            self._INNER_SEP,
            f"  Downloaded At    : {meta.downloaded_at}",
            f"  Download Status  : {meta.download_status}",
            "",
            self._OUTER_SEP,
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
    def _format_description(description: Optional[str]) -> str:
        """
        Indent every line of the description for visual separation inside
        the block.  Returns the full text — no truncation is applied.
        """
        if not description:
            return "  (no description)"
        text = description.strip()
        # Indent each line consistently; preserve blank lines as-is.
        return "\n".join(
            f"  {line}" if line.strip() else ""
            for line in text.splitlines()
        )


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
    5. Playlist entries each receive their own unique sequential number.
    6. On success  → build :class:`MetadataRecord` with final on-disk
       metadata, update history + metadata.
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
        Build a :class:`MetadataRecord` from the yt-dlp info dict — using
        the finished on-disk file for accurate size/codec data — then
        persist it to both *history.json* and *metadata.txt*.

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
