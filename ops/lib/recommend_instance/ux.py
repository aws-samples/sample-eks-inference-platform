"""Terminal UX helpers: colour palettes, bars, formatting."""

from __future__ import annotations

import argparse
import os
import sys


# --------------------------------------------------------------------------- #
# Terminal UX helpers: colour, bars, formatting                               #
# --------------------------------------------------------------------------- #

class _AnsiPalette:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    GREEN  = "\033[32m"
    YELLOW = "\033[33m"
    RED    = "\033[31m"
    CYAN   = "\033[36m"


class _NoPalette:
    RESET = BOLD = DIM = GREEN = YELLOW = RED = CYAN = ""


def _palette(emit_colour: bool) -> type:
    """Return the active colour palette class."""
    return _AnsiPalette if emit_colour else _NoPalette


def _should_use_colour(args: argparse.Namespace) -> bool:
    """Honour --json, NO_COLOR, and TTY detection."""
    if args.json:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _bar(used: float, total: float, width: int = 24) -> str:
    """Render a Unicode horizontal bar. `used` and `total` are arbitrary floats."""
    if total <= 0:
        return "░" * width
    frac  = max(0.0, min(used / total, 1.0))
    fill  = int(round(frac * width))
    return "█" * fill + "░" * (width - fill)


def _fmt_monthly(price_hour: float) -> str:
    """Format approximate monthly cost (730 h/month) with thousand separators."""
    return f"${price_hour * 730:,.0f}"


def _ruler(width: int, palette: type) -> str:
    return f"{palette.DIM}{'═' * width}{palette.RESET}"
