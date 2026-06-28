"""
about.py - About screen for YTDL-Pro.

Extracted from menu.py without modification.  The panel layout, text,
styles, and pause behaviour are identical to the original action_about()
function.

Public surface
--------------
run()
    Render the About panel and wait for the user to press Enter.
    Replaces the former action_about() call in menu.py.
"""

from __future__ import annotations

from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

# These constants are the same values declared in menu.py.
APP_NAME: str = "YTDL-Pro"
APP_VERSION: str = "2.0"
APP_AUTHOR: str = "Ahtisham Mahmood"
APP_DESCRIPTION: str = "Professional YouTube Downloader for Termux"

console: Console = Console()


def run() -> None:
    """
    About screen (formerly ``action_about`` in menu.py).

    Renders a centred panel with application name, version, author, and
    feature list, then waits for the user to press Enter.
    """
    console.print()
    console.print(Rule("[bold cyan]About[/]"))
    console.print()

    lines = Text.assemble(
        Text(f"{APP_NAME}\n", style="bold cyan"),
        Text(f"Version {APP_VERSION}\n\n", style="dim"),
        Text(f"By {APP_AUTHOR}\n\n", style="white"),
        Text(f"{APP_DESCRIPTION}\n\n", style="dim"),
        Text("Features\n", style="bold white"),
        Text("  • Best-quality video + audio merged to MP4\n", style="dim"),
        Text("  • Sequential automatic file numbering\n", style="dim"),
        Text("  • Full metadata & history tracking\n", style="dim"),
        Text("  • Playlist support\n", style="dim"),
        Text("  • Retry on failure\n", style="dim"),
        Text("  • Interactive menu + classic CLI sub-commands\n", style="dim"),
    )

    console.print(
        Align.center(
            Panel(
                Align.center(lines),
                border_style="cyan",
                padding=(1, 6),
            )
        )
    )

    _pause()


# ---------------------------------------------------------------------------
# Internal helper (copied from menu.py so about.py has no dependency on it)
# ---------------------------------------------------------------------------

def _pause() -> None:
    """Wait for Enter before returning to the menu."""
    console.print("\n[dim]Press Enter to return to the main menu…[/]", end="  ")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    console.print()
