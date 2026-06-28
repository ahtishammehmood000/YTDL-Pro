"""
app_config.py - Persistent user configuration for YTDL-Pro.

Reads and writes ``config.json`` in the project root.  The module exposes a
single :class:`AppConfig` class; call :meth:`AppConfig.load` to get the
current config and :meth:`AppConfig.save` to persist changes.

Stored keys
-----------
last_download_folder : str | null
    Absolute path of the directory chosen the last time the user was asked
    where to save videos.  ``null`` means "never chosen yet".
last_links_file : str | null
    Absolute path of the ``.txt`` file used in the last batch download.
    ``null`` means default ``links.txt`` was used.
ui_color : str
    Primary accent colour name for Rich markup (default ``"cyan"``).

The file is written atomically (temp-then-rename) so a crash mid-write
never corrupts the stored config.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Config file location – sits next to this module in the project root.
# ---------------------------------------------------------------------------
_CONFIG_FILE: Path = Path(__file__).parent.resolve() / "config.json"

# ---------------------------------------------------------------------------
# Android shared storage root.
# On every Android device with Termux, /storage/emulated/0 is the primary
# shared storage volume (visible in the Files app and accessible after the
# user has granted Termux storage permission via `termux-setup-storage`).
#
# Path.home() in Termux resolves to /data/data/com.termux/files/home which
# is private app storage — NOT visible in the Files app or from other apps.
# ---------------------------------------------------------------------------
_ANDROID_STORAGE: Path = Path("/storage/emulated/0")

# ---------------------------------------------------------------------------
# Known download folder presets.
# These are offered in the folder-picker menu so the user doesn't have to
# type a path for the two most common destinations.
#
# Both point to Android shared storage so downloaded files are immediately
# visible in the Android Files app, Gallery, and other media apps.
# ---------------------------------------------------------------------------
PRESET_FOLDERS: dict[str, Path] = {
    "Movies":    _ANDROID_STORAGE / "Movies",
    "Downloads": _ANDROID_STORAGE / "Download",   # Android uses "Download" (no 's')
}

# Default folder used when the user has never chosen one.
# Falls back to the local Videos/ dir so the app works even without storage
# permission (e.g. on desktop Linux or before termux-setup-storage is run).
DEFAULT_FOLDER: Path = Path(__file__).parent.resolve() / "Videos"


class AppConfig:
    """
    In-memory representation of ``config.json``.

    Usage::

        cfg = AppConfig.load()
        cfg.last_download_folder = "/sdcard/Movies"
        cfg.save()

    Attributes
    ----------
    last_download_folder : Path | None
        Where videos were saved on the last run.
    last_links_file : Path | None
        Which ``.txt`` file was used for the last batch download.
    ui_color : str
        Rich markup colour used for accents (default ``"cyan"``).
    """

    def __init__(
        self,
        *,
        last_download_folder: Optional[Path] = None,
        last_links_file: Optional[Path] = None,
        ui_color: str = "cyan",
    ) -> None:
        self.last_download_folder: Optional[Path] = last_download_folder
        self.last_links_file: Optional[Path] = last_links_file
        self.ui_color: str = ui_color

    # ------------------------------------------------------------------
    # Convenience property: the folder to use right now
    # ------------------------------------------------------------------

    @property
    def effective_folder(self) -> Path:
        """
        Return the last chosen folder, or the default ``Videos/`` directory
        if the user has never selected one.
        """
        return self.last_download_folder or DEFAULT_FOLDER

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_download_folder": (
                str(self.last_download_folder)
                if self.last_download_folder
                else None
            ),
            "last_links_file": (
                str(self.last_links_file)
                if self.last_links_file
                else None
            ),
            "ui_color": self.ui_color,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        folder_raw = data.get("last_download_folder")
        links_raw = data.get("last_links_file")
        return cls(
            last_download_folder=Path(folder_raw) if folder_raw else None,
            last_links_file=Path(links_raw) if links_raw else None,
            ui_color=data.get("ui_color", "cyan"),
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @classmethod
    def load(cls) -> "AppConfig":
        """
        Load config from ``config.json``.  Returns defaults silently if the
        file is absent or malformed.
        """
        if not _CONFIG_FILE.exists():
            return cls()
        try:
            with _CONFIG_FILE.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return cls.from_dict(data)
        except (json.JSONDecodeError, OSError, TypeError):
            pass
        return cls()

    def save(self) -> None:
        """
        Persist the current config to ``config.json`` atomically.
        """
        parent = _CONFIG_FILE.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            fd, tmp_str = tempfile.mkstemp(
                dir=parent, prefix=".config_tmp_", suffix=".json"
            )
            tmp = Path(tmp_str)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, _CONFIG_FILE)
            except Exception:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
        except OSError:
            pass  # Non-fatal – menu still works, settings just won't persist.
