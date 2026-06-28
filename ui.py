"""
ui.py - All terminal UI components for YTDL-Pro.

Centralises every piece of Rich-based terminal output that is shared between
the downloader and the CLI:

* :func:`print_banner`         – application header panel
* :class:`DependencyChecker`   – verifies yt-dlp / ffmpeg are on PATH
* :class:`Environment`         – creates dirs & seed files on first run
* :class:`ProgressTracker`     – Rich progress bar wired to yt-dlp hooks

Nothing in this module performs downloads or writes metadata; it is purely
presentation and environment setup.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from rich import box
from rich.align import Align
from rich.console import Console
from rich.panel import Panel
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
from rich.text import Text

from config import (
    APP_DESCRIPTION,
    APP_NAME,
    APP_VERSION,
    FAILED_FILE,
    HISTORY_FILE,
    LINKS_FILE,
    LOG_FILE,
    LOGS_DIR,
    METADATA_FILE,
    VIDEOS_DIR,
)

console: Console = Console()


# ===========================================================================
# Banner
# ===========================================================================


def print_banner() -> None:
    """Render the application banner using Rich."""
    title = Text(f" {APP_NAME} ", style="bold cyan")
    subtitle = Text(f"v{APP_VERSION} • {APP_DESCRIPTION}", style="dim")

    banner = Align.center(
        Panel(
            Align.center(subtitle),
            title=title,
            border_style="cyan",
            padding=(0, 4),
        )
    )
    console.print(banner)
    console.print()


# ===========================================================================
# DependencyChecker
# ===========================================================================


class DependencyChecker:
    """
    Verifies that required external tools are present on PATH before any
    download attempt. Reports missing tools clearly and exits early so the
    user gets a useful error message instead of a cryptic yt-dlp traceback.
    """

    _REQUIRED: dict[str, str] = {
        "yt-dlp": "pip install yt-dlp",
        "ffmpeg": "pkg install ffmpeg (Termux) | apt install ffmpeg (Debian)",
        "python3": "Built-in – should always be present",
    }

    def __init__(self) -> None:
        self._missing: list[str] = []

    def check(self) -> bool:
        """
        Check all required tools. Returns ``True`` if all are present.
        Populates ``self._missing`` with the names of absent tools.
        """
        self._missing = [
            name
            for name in self._REQUIRED
            if shutil.which(name) is None
        ]
        return len(self._missing) == 0

    def report(self) -> None:
        """Print a formatted table of missing dependencies to the console."""
        table = Table(
            title="[bold red]Missing Dependencies[/]",
            box=box.ROUNDED,
            border_style="red",
            show_lines=True,
        )
        table.add_column("Tool", style="bold yellow", min_width=12)
        table.add_column("Install command", style="cyan")

        for name in self._missing:
            table.add_row(name, self._REQUIRED.get(name, "See project README"))

        console.print(table)
        console.print(
            "\n[bold red]✗ Install the missing tools and try again.[/]\n"
        )

    def check_or_exit(self, logger: Optional[Any] = None) -> None:
        """Run :meth:`check`; call :meth:`report` and exit if anything is missing."""
        if not self.check():
            self.report()
            if logger is not None:
                logger.error("Missing dependencies: %s", ", ".join(self._missing))
            sys.exit(1)


# ===========================================================================
# Environment
# ===========================================================================


class Environment:
    """
    Ensures all project directories and seed files exist before the first run.

    Directories created : Videos/  Logs/
    Files created (empty if absent): links.txt  history.json  metadata.txt
                                      failed.txt
    """

    _DIRS: tuple[Path, ...] = (VIDEOS_DIR, LOGS_DIR)

    _SEED_FILES: dict[Path, str] = {
        LINKS_FILE: "# Add one YouTube URL per line.\n",
        HISTORY_FILE: "[]",
        METADATA_FILE: "",
        FAILED_FILE: "",
    }

    def bootstrap(self, logger: Optional[Any] = None) -> None:
        """Create missing directories and seed files silently."""
        for directory in self._DIRS:
            directory.mkdir(parents=True, exist_ok=True)
            if logger is not None:
                logger.debug("Directory ready: %s", directory)

        for file_path, seed_content in self._SEED_FILES.items():
            if not file_path.exists():
                try:
                    file_path.write_text(seed_content, encoding="utf-8")
                    if logger is not None:
                        logger.debug("Created seed file: %s", file_path)
                except OSError as exc:
                    if logger is not None:
                        logger.error("Cannot create %s: %s", file_path, exc)


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
# Formatting helpers (shared between ui and main)
# ===========================================================================


def fmt_duration(seconds: Optional[int | float]) -> str:
    """Format a duration in seconds to a human-readable string."""
    if seconds is None:
        return "—"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {sec:02d}s"
    return f"{m}m {sec:02d}s"


def fmt_size(size_bytes: Optional[int]) -> str:
    """Format a byte count to a human-readable string."""
    if size_bytes is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes //= 1024  # type: ignore[assignment]
    return f"{size_bytes:.1f} TB"


def fmt_timestamp(iso: Optional[str]) -> str:
    """Convert an ISO-8601 UTC timestamp to a local-time display string."""
    if not iso:
        return "—"
    try:
        from datetime import datetime  # noqa: PLC0415

        dt = datetime.fromisoformat(iso)
        local = dt.astimezone()
        return local.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso
