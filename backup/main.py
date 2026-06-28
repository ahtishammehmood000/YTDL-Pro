"""
main.py - Entry point for YTDL-Pro.

Provides a Rich-rendered CLI with four sub-commands:

    download   Run the full download queue from links.txt
    history    Display download history from history.json
    failed     Show / retry failed URLs from failed.txt
    info       Print project paths and environment diagnostics

Usage (Termux / any POSIX shell):

    python main.py download
    python main.py history
    python main.py failed
    python main.py failed --retry
    python main.py info
    python main.py --help
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

# ---------------------------------------------------------------------------
# downloader.py lives beside main.py; import its public surface only.
# ---------------------------------------------------------------------------
from downloader import (
    BASE_DIR,
    FAILED_FILE,
    HISTORY_FILE,
    LINKS_FILE,
    LOG_FILE,
    LOGS_DIR,
    METADATA_FILE,
    VIDEOS_DIR,
    Downloader,
    HistoryManager,
    Logger,
)

# ---------------------------------------------------------------------------
# Module-level singletons.
# ---------------------------------------------------------------------------
console: Console = Console()
log: Logger = Logger()

# Application metadata.
APP_NAME: str = "YTDL-Pro"
APP_VERSION: str = "1.0.0"
APP_DESCRIPTION: str = "Professional YouTube Downloader for Termux"


# ===========================================================================
# Banner
# ===========================================================================

def _print_banner() -> None:
    """Render the application banner using Rich."""
    title = Text(f"  {APP_NAME}  ", style="bold cyan")
    subtitle = Text(f"v{APP_VERSION}  •  {APP_DESCRIPTION}", style="dim")
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
# Dependency checker
# ===========================================================================

class DependencyChecker:
    """
    Verifies that required external tools are present on PATH before any
    download attempt.  Reports missing tools clearly and exits early so the
    user gets a useful error message instead of a cryptic yt-dlp traceback.
    """

    _REQUIRED: dict[str, str] = {
        "yt-dlp": "pip install yt-dlp",
        "ffmpeg": "pkg install ffmpeg  (Termux)  |  apt install ffmpeg  (Debian)",
        "python3": "Built-in – should always be present",
    }

    def __init__(self) -> None:
        self._missing: list[str] = []

    def check(self) -> bool:
        """
        Check all required tools.  Returns ``True`` if all are present.
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
            "\n[bold red]✗  Install the missing tools and try again.[/]\n"
        )
        log.error("Missing dependencies: %s", ", ".join(self._missing))

    def check_or_exit(self) -> None:
        """Run :meth:`check`; call :meth:`report` and exit if anything is missing."""
        if not self.check():
            self.report()
            sys.exit(1)


# ===========================================================================
# Environment bootstrapper
# ===========================================================================

class Environment:
    """
    Ensures all project directories and seed files exist before the first run.

    Directories created:  Videos/  Logs/
    Files created (empty if absent):  links.txt  history.json  metadata.txt
                                       failed.txt
    """

    _DIRS: tuple[Path, ...] = (VIDEOS_DIR, LOGS_DIR)
    _SEED_FILES: dict[Path, str] = {
        LINKS_FILE: "# Add one YouTube URL per line.\n",
        HISTORY_FILE: "[]",
        METADATA_FILE: "",
        FAILED_FILE: "",
    }

    def bootstrap(self) -> None:
        """Create missing directories and seed files silently."""
        for directory in self._DIRS:
            directory.mkdir(parents=True, exist_ok=True)
            log.debug("Directory ready: %s", directory)

        for file_path, seed_content in self._SEED_FILES.items():
            if not file_path.exists():
                try:
                    file_path.write_text(seed_content, encoding="utf-8")
                    log.debug("Created seed file: %s", file_path)
                except OSError as exc:
                    log.error("Cannot create %s: %s", file_path, exc)


# ===========================================================================
# Command handlers
# ===========================================================================

class CommandDownload:
    """
    Executes the ``download`` sub-command.

    1. Checks dependencies.
    2. Bootstraps the environment.
    3. Delegates to :class:`~downloader.Downloader`.
    """

    def run(self) -> int:
        """
        Perform the download run.

        Returns
        -------
        int
            Exit code: 0 on success, 1 on fatal error.
        """
        _print_banner()

        checker = DependencyChecker()
        checker.check_or_exit()

        Environment().bootstrap()

        if not LINKS_FILE.exists() or not LINKS_FILE.read_text(encoding="utf-8").strip():
            console.print(
                f"[bold yellow]⚠  links.txt is empty.[/]\n"
                f"   Add YouTube URLs (one per line) to:\n"
                f"   [cyan]{LINKS_FILE}[/]\n"
            )
            log.warning("links.txt is empty; nothing to download.")
            return 0

        log.info("Starting download run.")
        downloader = Downloader()
        downloader.run()
        log.info("Download run finished.")
        return 0


class CommandHistory:
    """
    Executes the ``history`` sub-command.

    Reads *history.json* via :class:`~downloader.HistoryManager` and renders
    an interactive Rich table.  The most recent entries are shown last so the
    newest download is always visible without scrolling.
    """

    def run(self, limit: Optional[int] = None) -> int:
        """
        Display download history.

        Parameters
        ----------
        limit:
            If given, show only the *N* most recent entries.

        Returns
        -------
        int
            Always 0.
        """
        _print_banner()
        history = HistoryManager()
        records = history.all_records()

        if not records:
            console.print("[bold yellow]No download history found.[/]")
            console.print(
                f"   History is stored in: [cyan]{HISTORY_FILE}[/]\n"
            )
            return 0

        # Apply optional limit (most recent N).
        display = records[-limit:] if limit else records

        table = Table(
            title=f"[bold cyan]Download History[/]  "
                  f"[dim]({len(display)} of {len(records)} entries)[/]",
            box=box.ROUNDED,
            border_style="cyan",
            show_lines=True,
            expand=True,
        )
        table.add_column("#", style="dim", justify="right", min_width=3)
        table.add_column("File", style="bold green", min_width=8)
        table.add_column("Title", style="white", min_width=30, overflow="fold")
        table.add_column("Duration", justify="right", min_width=10)
        table.add_column("Size", justify="right", min_width=10)
        table.add_column("Downloaded At", min_width=20)

        for idx, rec in enumerate(display, start=1):
            table.add_row(
                str(idx),
                rec.get("filename", "N/A"),
                rec.get("title", "N/A"),
                self._fmt_duration(rec.get("duration_seconds")),
                self._fmt_size(rec.get("filesize_bytes")),
                self._fmt_ts(rec.get("downloaded_at")),
            )

        console.print(table)
        console.print()
        log.info("History displayed: %d records.", len(display))
        return 0

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_duration(seconds: Optional[int | float]) -> str:
        if seconds is None:
            return "—"
        s = int(seconds)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h}h {m:02d}m {sec:02d}s"
        return f"{m}m {sec:02d}s"

    @staticmethod
    def _fmt_size(size_bytes: Optional[int]) -> str:
        if size_bytes is None:
            return "—"
        for unit in ("B", "KB", "MB", "GB"):
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes //= 1024  # type: ignore[assignment]
        return f"{size_bytes:.1f} TB"

    @staticmethod
    def _fmt_ts(iso: Optional[str]) -> str:
        if not iso:
            return "—"
        try:
            dt = datetime.fromisoformat(iso)
            local = dt.astimezone()  # Convert UTC → local timezone.
            return local.strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return iso


class CommandFailed:
    """
    Executes the ``failed`` sub-command.

    Without ``--retry``: renders a table of all URLs in *failed.txt*.
    With ``--retry``:    moves the contents of *failed.txt* into *links.txt*
                        (appending, preserving existing entries), clears
                        *failed.txt*, then delegates to
                        :class:`~downloader.Downloader` to re-attempt them.
    """

    def run(self, retry: bool = False) -> int:
        """
        Show or retry failed downloads.

        Parameters
        ----------
        retry:
            When ``True``, re-queue failed URLs and start a new download run.

        Returns
        -------
        int
            Exit code: 0 on success, 1 on setup error.
        """
        _print_banner()

        urls = self._load_failed_urls()

        if not urls:
            console.print("[bold green]✔  No failed URLs.[/]")
            console.print(
                f"   Failed URLs are logged to: [cyan]{FAILED_FILE}[/]\n"
            )
            return 0

        # Always show the table first.
        self._print_table(urls)

        if not retry:
            return 0

        # ------------------------------------------------------------------
        # Retry mode: append to links.txt and clear failed.txt.
        # ------------------------------------------------------------------
        console.print(
            f"\n[bold cyan]↻  Re-queuing {len(urls)} failed URL(s) for retry…[/]\n"
        )
        log.info("Retry requested for %d failed URL(s).", len(urls))

        try:
            with LINKS_FILE.open("a", encoding="utf-8") as fh:
                fh.write("\n# --- Retried failed downloads ---\n")
                for url in urls:
                    fh.write(url + "\n")

            # Clear failed.txt so we don't re-retry on the next run.
            FAILED_FILE.write_text("", encoding="utf-8")
            log.info("Failed URLs moved to links.txt; failed.txt cleared.")

        except OSError as exc:
            console.print(f"[bold red]✗  Cannot update files: {exc}[/]")
            log.error("Retry setup failed: %s", exc)
            return 1

        checker = DependencyChecker()
        checker.check_or_exit()

        downloader = Downloader()
        downloader.run()
        return 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_failed_urls() -> list[str]:
        if not FAILED_FILE.exists():
            return []
        urls: list[str] = []
        seen: set[str] = set()
        for raw in FAILED_FILE.read_text(encoding="utf-8").splitlines():
            url = raw.strip()
            if url and url not in seen:
                urls.append(url)
                seen.add(url)
        return urls

    @staticmethod
    def _print_table(urls: list[str]) -> None:
        table = Table(
            title=f"[bold red]Failed Downloads[/]  "
                  f"[dim]({len(urls)} URL(s))[/]",
            box=box.ROUNDED,
            border_style="red",
            show_lines=True,
            expand=True,
        )
        table.add_column("#", style="dim", justify="right", min_width=3)
        table.add_column("URL", style="yellow", overflow="fold")

        for idx, url in enumerate(urls, start=1):
            table.add_row(str(idx), url)

        console.print(table)
        console.print(
            "\n[dim]Run [bold]python main.py failed --retry[/] to re-attempt.[/]\n"
        )


class CommandInfo:
    """
    Executes the ``info`` sub-command.

    Prints a diagnostic panel showing:
    - Python version
    - yt-dlp version
    - ffmpeg version
    - All project paths with existence indicators
    - Download statistics from history.json
    """

    def run(self) -> int:
        """
        Print project environment and path diagnostics.

        Returns
        -------
        int
            Always 0.
        """
        _print_banner()

        # ------------------------------------------------------------------
        # Tool versions
        # ------------------------------------------------------------------
        tool_table = Table(
            title="[bold cyan]Environment[/]",
            box=box.ROUNDED,
            border_style="cyan",
            show_lines=True,
        )
        tool_table.add_column("Component", style="bold", min_width=16)
        tool_table.add_column("Version / Path", style="green")

        tool_table.add_row(
            "Python",
            f"{sys.version.split()[0]}  ({sys.executable})",
        )
        tool_table.add_row("yt-dlp", self._tool_version("yt-dlp", "--version"))
        tool_table.add_row("ffmpeg", self._tool_version("ffmpeg", "-version", first_line=True))
        tool_table.add_row("Project root", str(BASE_DIR))

        console.print(tool_table)
        console.print()

        # ------------------------------------------------------------------
        # Project paths
        # ------------------------------------------------------------------
        path_table = Table(
            title="[bold cyan]Project Paths[/]",
            box=box.ROUNDED,
            border_style="cyan",
            show_lines=True,
        )
        path_table.add_column("Name", style="bold", min_width=18)
        path_table.add_column("Path", style="dim", overflow="fold")
        path_table.add_column("Status", justify="center", min_width=10)

        path_entries: list[tuple[str, Path]] = [
            ("Videos dir", VIDEOS_DIR),
            ("Logs dir", LOGS_DIR),
            ("links.txt", LINKS_FILE),
            ("history.json", HISTORY_FILE),
            ("metadata.txt", METADATA_FILE),
            ("failed.txt", FAILED_FILE),
            ("latest.log", LOG_FILE),
        ]

        for label, path in path_entries:
            exists = path.exists()
            status = "[green]✔  exists[/]" if exists else "[red]✗  missing[/]"
            path_table.add_row(label, str(path), status)

        console.print(path_table)
        console.print()

        # ------------------------------------------------------------------
        # Quick stats
        # ------------------------------------------------------------------
        self._print_stats()

        return 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tool_version(
        tool: str,
        flag: str,
        *,
        first_line: bool = False,
    ) -> str:
        """
        Run ``tool flag`` and return its stdout, or ``not found`` if absent.
        """
        if shutil.which(tool) is None:
            return "[bold red]not found[/]"
        try:
            result = subprocess.run(
                [tool, flag],
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = (result.stdout or result.stderr).strip()
            if first_line:
                output = output.splitlines()[0] if output else ""
            return output or "—"
        except (subprocess.TimeoutExpired, OSError):
            return "[yellow]timeout / error[/]"

    @staticmethod
    def _print_stats() -> None:
        history = HistoryManager()
        records = history.all_records()

        # Count MP4 files in Videos/.
        mp4_count = sum(
            1
            for f in VIDEOS_DIR.iterdir()
            if VIDEOS_DIR.exists() and f.suffix.lower() == ".mp4"
        ) if VIDEOS_DIR.exists() else 0

        failed_count = (
            sum(
                1
                for line in FAILED_FILE.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            if FAILED_FILE.exists()
            else 0
        )

        stats_table = Table(
            title="[bold cyan]Statistics[/]",
            box=box.ROUNDED,
            border_style="cyan",
            show_lines=True,
        )
        stats_table.add_column("Metric", style="bold", min_width=24)
        stats_table.add_column("Value", justify="right", style="green")

        stats_table.add_row("Downloads in history", str(len(records)))
        stats_table.add_row("MP4 files in Videos/", str(mp4_count))
        stats_table.add_row("Failed URLs pending", str(failed_count))

        if records:
            last = records[-1]
            stats_table.add_row("Last downloaded title", last.get("title", "—"))
            stats_table.add_row("Last downloaded at", last.get("downloaded_at", "—"))

        console.print(stats_table)
        console.print()


# ===========================================================================
# CLI argument parser
# ===========================================================================

class CLI:
    """
    Builds and owns the :mod:`argparse` parser for YTDL-Pro.

    Sub-commands
    ------------
    download
        Run the download queue.
    history [-n N]
        Show download history (optionally limited to the last N entries).
    failed [--retry]
        Show failed URLs; optionally re-queue and retry them.
    info
        Print environment diagnostics.
    """

    def __init__(self) -> None:
        self._parser = self._build_parser()

    # ------------------------------------------------------------------
    # Parser construction
    # ------------------------------------------------------------------

    def _build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog="ytdl-pro",
            description=f"{APP_NAME} {APP_VERSION} – {APP_DESCRIPTION}",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=self._epilog(),
        )
        parser.add_argument(
            "--version",
            action="version",
            version=f"{APP_NAME} {APP_VERSION}",
        )

        sub = parser.add_subparsers(dest="command", metavar="<command>")
        sub.required = True

        # download
        sub.add_parser(
            "download",
            help="Run the download queue from links.txt",
            description="Read links.txt and download all pending YouTube URLs.",
        )

        # history
        hist_p = sub.add_parser(
            "history",
            help="Display download history",
            description="Show completed downloads recorded in history.json.",
        )
        hist_p.add_argument(
            "-n",
            "--limit",
            metavar="N",
            type=int,
            default=None,
            help="Show only the N most recent entries (default: all)",
        )

        # failed
        fail_p = sub.add_parser(
            "failed",
            help="Show (and optionally retry) failed downloads",
            description="List URLs in failed.txt.  Use --retry to re-queue them.",
        )
        fail_p.add_argument(
            "--retry",
            action="store_true",
            default=False,
            help="Re-queue failed URLs into links.txt and start downloading",
        )

        # info
        sub.add_parser(
            "info",
            help="Show environment diagnostics and project paths",
            description="Print tool versions, file paths, and download statistics.",
        )

        return parser

    @staticmethod
    def _epilog() -> str:
        return (
            "Examples:\n"
            "  python main.py download\n"
            "  python main.py history\n"
            "  python main.py history -n 10\n"
            "  python main.py failed\n"
            "  python main.py failed --retry\n"
            "  python main.py info\n"
        )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def parse_and_run(self) -> int:
        """
        Parse ``sys.argv``, dispatch to the appropriate command handler,
        and return an exit code.

        Returns
        -------
        int
            0 on success, non-zero on error.
        """
        args = self._parser.parse_args()
        log.info("Command invoked: %s", args.command)

        if args.command == "download":
            return CommandDownload().run()

        if args.command == "history":
            return CommandHistory().run(limit=args.limit)

        if args.command == "failed":
            return CommandFailed().run(retry=args.retry)

        if args.command == "info":
            return CommandInfo().run()

        # Unreachable due to sub.required = True, but satisfies type checkers.
        self._parser.print_help()
        return 1


# ===========================================================================
# Entry point
# ===========================================================================

def main() -> None:
    """
    YTDL-Pro entry point.

    Instantiates :class:`CLI`, runs the selected sub-command, and exits with
    the returned status code.  Any unhandled exception is caught here so the
    user always sees a clean Rich-formatted error rather than a raw traceback.
    """
    try:
        cli = CLI()
        exit_code = cli.parse_and_run()
    except KeyboardInterrupt:
        console.print("\n[bold yellow]⚠  Aborted.[/]")
        log.warning("Process interrupted by user at CLI level.")
        exit_code = 130  # Conventional SIGINT exit code.
    except Exception as exc:  # noqa: BLE001
        console.print(f"\n[bold red]✗  Unexpected error:[/] {exc}")
        log.exception("Unhandled exception in main(): %s", exc)
        exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
