"""
file_browser.py - Reusable interactive terminal file/folder browser for YTDL-Pro.

Replaces manual path typing with a lightweight, Explorer-style navigator
optimised for Termux/Android shared storage, while still allowing a
typed "Custom Path" as a fallback for fast/deep navigation.

Public surface
---------------
select_folder(start: Path | None = None) -> Path | None
    Browse and choose a directory.  Used for the download destination
    picker (replaces the old free-typed "Custom path…" prompt).

select_txt_file(start: Path | None = None) -> Path | None
    Browse and choose a ``.txt`` file.  Folders are always shown and
    navigable; only ``.txt`` files are listed and selectable.  Used for
    the batch-download links file and Settings → "default .txt file".

Both functions return ``None`` if the user cancels (backs out past the
root without picking anything), so callers can fall back to whatever
they were doing before (e.g. keep the previously configured path).

Design notes
------------
- Pure Python + Rich only — no new third-party dependencies beyond what
  YTDL-Pro already uses.
- Visual style (cyan/green accents, ``Rule``, ``Table`` with
  ``box.SIMPLE``) intentionally mirrors menu.py so the browser feels
  like a native part of the existing UI rather than a bolted-on tool.
- A single internal engine (``_browse``) drives both folder mode and
  file mode to avoid duplicating navigation logic — mode just changes
  what gets listed and what "select" means.
- This module does NOT import anything from downloader.py, metadata.py,
  config.py or app_config.py, and nothing in those modules needs to
  import this one either except where the caller chooses to use it.
  That keeps it a standalone, drop-in utility other features can reuse
  later (e.g. picking a thumbnail folder, an export directory, etc.).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from rich import box
from rich.console import Console
from rich.rule import Rule
from rich.table import Table

console: Console = Console()

# ---------------------------------------------------------------------------
# Starting location.
#
# /storage/emulated/0 is the Android shared-storage root visible in the
# Files app once `termux-setup-storage` has been run.  If it doesn't exist
# (e.g. running on desktop Linux during development), fall back to the
# user's home directory so the browser still works.
# ---------------------------------------------------------------------------
ANDROID_STORAGE_ROOT: Path = Path("/storage/emulated/0")
DEFAULT_START: Path = ANDROID_STORAGE_ROOT if ANDROID_STORAGE_ROOT.exists() else Path.home()


# ===========================================================================
# Internal helpers
# ===========================================================================


def _list_subdirs(current: Path) -> list[Path]:
    """
    Return visible subdirectories of *current*, alphabetically sorted.

    Hidden directories (name starts with ".") are excluded.  Entries that
    can't be stat'ed (permission errors, broken mounts, etc.) are silently
    skipped rather than crashing the browser.
    """
    dirs: list[Path] = []
    try:
        for entry in current.iterdir():
            try:
                if entry.is_dir() and not entry.name.startswith("."):
                    dirs.append(entry)
            except OSError:
                continue
    except OSError:
        return []
    return sorted(dirs, key=lambda p: p.name.lower())


def _list_txt_files(current: Path) -> list[Path]:
    """
    Return visible ``.txt`` files directly inside *current*, sorted
    alphabetically.  Hidden files (name starts with ".") are excluded.
    """
    files: list[Path] = []
    try:
        for entry in current.iterdir():
            try:
                if (
                    entry.is_file()
                    and not entry.name.startswith(".")
                    and entry.suffix.lower() == ".txt"
                ):
                    files.append(entry)
            except OSError:
                continue
    except OSError:
        return []
    return sorted(files, key=lambda p: p.name.lower())


def _prompt(label: str) -> str:
    """
    Display a styled prompt and return stripped user input.
    Mirrors menu.py's ``_prompt`` so behaviour (Ctrl+C / EOF handling)
    is identical throughout the app.
    """
    console.print(f"[bold cyan]▶[/]  {label}", end="  ")
    try:
        raw = input()
    except (EOFError, KeyboardInterrupt):
        console.print()
        return ""
    return raw.strip()


def _prompt_custom_path(*, want_file: bool) -> Optional[Path]:
    """
    Ask the user to type a path directly.

    Validates that the path exists and is the right kind (directory, or
    a ``.txt`` file when *want_file* is True).  Re-prompts on invalid
    input; an empty entry cancels back to the browser.

    Returns
    -------
    Path | None
        The validated, resolved path, or None if the user backed out.
    """
    console.print()
    console.print(Rule("[bold cyan]Custom Path[/]"))
    console.print("[dim]  Leave empty to cancel and return to the browser.[/]")
    console.print()

    while True:
        raw = _prompt("Enter full path")
        if not raw:
            return None

        path = Path(raw).expanduser().resolve()

        if not path.exists():
            console.print(f"[bold red]  ✗  Path not found:[/] {path}")
            continue

        if want_file:
            if path.is_dir():
                console.print(f"[bold red]  ✗  That is a directory, not a file:[/] {path}")
                continue
            if path.suffix.lower() != ".txt":
                console.print(
                    f"[bold red]  ✗  Not a .txt file (got "
                    f"'{path.suffix or 'no extension'}'):[/] {path}"
                )
                continue
        else:
            if not path.is_dir():
                console.print(f"[bold red]  ✗  Not a directory:[/] {path}")
                continue

        return path


# ===========================================================================
# Core browsing engine
# ===========================================================================


def _browse(start: Path, *, mode: str) -> Optional[Path]:
    """
    Shared navigation loop used by both ``select_folder`` and
    ``select_txt_file``.

    Parameters
    ----------
    start:
        Directory to open first.
    mode:
        ``"folder"`` — list directories only; "select" returns the
        current directory.
        ``"file"`` — list directories (navigable) and ``*.txt`` files
        (selectable); "select" returns a chosen file.

    Returns
    -------
    Path | None
        The chosen folder/file, or None if the user cancelled out of
        the browser entirely (backed out past the starting directory).
    """
    current = start if start.is_dir() else DEFAULT_START
    at_root = current  # the directory the browser opened on; backing out from here cancels.

    while True:
        subdirs = _list_subdirs(current)
        txt_files: list[Path] = _list_txt_files(current) if mode == "file" else []

        console.print()
        console.print(Rule("[bold cyan]File Browser[/]" if mode == "folder" else "[bold cyan]Select .txt File[/]"))
        console.print()
        console.print(f"[dim]Current:[/]  [cyan]{current}[/]")
        console.print()

        # ------------------------------------------------------------
        # Build the full list of selectable entries first (pure data,
        # no printing yet).  Each entry is (label, action).
        # action is one of:
        #   ("dir", Path)   -> descend into that directory
        #   ("file", Path)  -> select that file (file mode only)
        #   "up"            -> go to parent / cancel if already at root
        #   "confirm"       -> select current directory (folder mode)
        #   "custom"        -> prompt for a typed path
        #
        # section_breaks maps an entry index (0-based, into `entries`)
        # to a header line that should be printed directly above the
        # row for that entry — used only in file mode to separate
        # "Folders" from "TXT Files".
        # ------------------------------------------------------------
        entries: list[tuple[str, object]] = []
        section_breaks: dict[int, str] = {}

        if mode == "file" and subdirs:
            section_breaks[len(entries)] = "Folders"
        for d in subdirs:
            entries.append((f"{d.name}/", ("dir", d)))

        if mode == "file" and txt_files:
            section_breaks[len(entries)] = "TXT Files"
        for f in txt_files:
            entries.append((f.name, ("file", f)))

        up_label = "..  (parent folder)" if current != at_root else "..  (cancel)"
        entries.append((up_label, "up"))

        if mode == "folder":
            entries.append(("✓ Use This Folder", "confirm"))

        entries.append(("Custom Path…", "custom"))

        # ------------------------------------------------------------
        # Render: one single table, section headers as plain rows
        # inserted between the numbered entries.
        # ------------------------------------------------------------
        table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
        table.add_column("  #", style="bold cyan", justify="right", min_width=3)
        table.add_column("  Name", style="white")

        for i, (label, action) in enumerate(entries):
            if i in section_breaks:
                table.add_row("", f"[bold white]{section_breaks[i]}[/]")
            display = f"[bold green]{label}[/]" if action == "confirm" else label
            table.add_row(str(i + 1), display)

        console.print(table)
        console.print()

        n = len(entries)
        raw = _prompt(f"Choose [1–{n}]")

        if not raw.isdigit():
            console.print(f"[yellow]  Please enter a number between 1 and {n}.[/]")
            continue

        idx = int(raw) - 1
        if not (0 <= idx < n):
            console.print(f"[yellow]  Please enter a number between 1 and {n}.[/]")
            continue

        _, action = entries[idx]

        if action == "up":
            if current == at_root:
                return None
            current = current.parent
            continue

        if action == "confirm":
            return current

        if action == "custom":
            chosen = _prompt_custom_path(want_file=(mode == "file"))
            if chosen is not None:
                return chosen
            continue

        if isinstance(action, tuple):
            kind, target = action
            if kind == "dir":
                current = target
                continue
            if kind == "file":
                return target

        # Should never reach here, but stay safe rather than crash.
        console.print("[yellow]  Unrecognised option, please try again.[/]")


# ===========================================================================
# Public API
# ===========================================================================


def select_folder(start: Optional[Path] = None) -> Optional[Path]:
    """
    Launch the interactive browser in folder-selection mode.

    Parameters
    ----------
    start:
        Directory to open first.  Defaults to the Android shared
        storage root (``/storage/emulated/0``), falling back to the
        user's home directory if that path doesn't exist on this
        system.

    Returns
    -------
    Path | None
        The chosen directory (not yet created on disk — callers should
        ``mkdir(parents=True, exist_ok=True)`` as before), or None if
        the user cancelled.
    """
    root = (start or DEFAULT_START).expanduser().resolve()
    return _browse(root, mode="folder")


def select_txt_file(start: Optional[Path] = None) -> Optional[Path]:
    """
    Launch the interactive browser in ``.txt``-file-selection mode.

    Parameters
    ----------
    start:
        Directory to open first.  Defaults to the Android shared
        storage root, same fallback behaviour as :func:`select_folder`.

    Returns
    -------
    Path | None
        The chosen ``.txt`` file, or None if the user cancelled.
    """
    root = (start or DEFAULT_START).expanduser().resolve()
    return _browse(root, mode="file")
