"""
config.py - Central configuration for YTDL-Pro.

All project-level paths and tuneable constants live here so that every other
module imports from a single source of truth.  Nothing in this file has
side-effects: it only defines names.

Usage::

    from config import BASE_DIR, VIDEOS_DIR, MAX_RETRIES, RETRY_SLEEP

Path layout
-----------
<project root>/
    Videos/          – downloaded MP4 files
    Logs/
        latest.log   – runtime log (overwritten each run)
    links.txt        – one YouTube URL per line
    history.json     – JSON array of completed download records
    metadata.txt     – human-readable download log
    failed.txt       – URLs that failed after all retries
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Root directory
# ---------------------------------------------------------------------------

#: Absolute path of the project root (the directory that contains this file).
#: All other paths are resolved relative to this so the project is importable
#: from any working directory.
BASE_DIR: Path = Path(__file__).parent.resolve()

# ---------------------------------------------------------------------------
# Runtime directories
# ---------------------------------------------------------------------------

#: Directory where downloaded MP4 files are stored.
VIDEOS_DIR: Path = BASE_DIR / "Videos"

#: Directory where log files are written.
LOGS_DIR: Path = BASE_DIR / "Logs"

# ---------------------------------------------------------------------------
# Data files
# ---------------------------------------------------------------------------

#: Plain-text file containing one YouTube URL per line.
LINKS_FILE: Path = BASE_DIR / "links.txt"

#: JSON array of :class:`~metadata.MetadataRecord` dicts for every completed
#: download.
HISTORY_FILE: Path = BASE_DIR / "history.json"

#: Human-readable plain-text log appended after each successful download.
METADATA_FILE: Path = BASE_DIR / "metadata.txt"

#: Plain-text file; each line is a URL that failed all retry attempts.
FAILED_FILE: Path = BASE_DIR / "failed.txt"

# ---------------------------------------------------------------------------
# Log file
# ---------------------------------------------------------------------------

#: Active runtime log file (overwritten at the start of each run).
LOG_FILE: Path = LOGS_DIR / "latest.log"

# ---------------------------------------------------------------------------
# Download behaviour
# ---------------------------------------------------------------------------

#: Number of times the downloader will attempt a URL before giving up and
#: writing it to *failed.txt*.
MAX_RETRIES: int = 3

#: Seconds to wait between retry attempts.
RETRY_SLEEP: float = 3.0

# ---------------------------------------------------------------------------
# Application metadata
# ---------------------------------------------------------------------------

APP_NAME: str = "YTDL-Pro"
APP_VERSION: str = "1.0.0"
APP_DESCRIPTION: str = "Professional YouTube Downloader for Termux"
