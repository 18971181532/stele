"""Terminal output: widths that count CJK as two cells, and number formatting."""

from __future__ import annotations

import sys
import unicodedata
from typing import Iterable, List, Sequence

GLYPHS = {"ascii": {"bar": "#", "dash": "-"}, "utf8": {"bar": "▏", "dash": "─"}}


def use_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def _utf8() -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return "utf" in encoding.lower()


def glyph(name: str) -> str:
    return GLYPHS["utf8" if _utf8() else "ascii"][name]


def display_width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def pad(text: str, width: int, align: str = "left") -> str:
    gap = max(0, width - display_width(text))
    if align == "right":
        return " " * gap + text
    return text + " " * gap


def table(headers: Sequence[str], rows: Iterable[Sequence[str]], aligns=None) -> str:
    rows = [list(row) for row in rows]
    aligns = list(aligns or ["left"] * len(headers))
    widths = [display_width(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], display_width(cell))
    lines = [
        "  ".join(pad(header, widths[i], aligns[i]) for i, header in enumerate(headers)).rstrip()
    ]
    lines.append("  ".join(glyph("dash") * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append(
            "  ".join(pad(cell, widths[i], aligns[i]) for i, cell in enumerate(row)).rstrip()
        )
    return "\n".join(lines)


def number(value: float) -> str:
    if value >= 1000:
        return f"{value:,.0f}"
    if value >= 10:
        return f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{value:.3g}"


def plural(count: int, singular: str, many: str = "") -> str:
    """``1 key``, ``2 keys``, ``2 leaves`` -- the count and the right word."""
    return f"{count:,} {singular if count == 1 else (many or singular + 's')}"


def seconds(value: float) -> str:
    if value < 1e-3:
        return f"{value * 1e6:.0f} µs"
    if value < 1:
        return f"{value * 1e3:.1f} ms"
    return f"{value:.2f} s"


def size(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value / (1024 * 1024):.2f} MiB"


def bar(fraction: float, width: int = 24) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    return glyph("bar") * filled + " " * (width - filled)


def wrap(text: str, width: int, indent: str = "") -> List[str]:
    words = text.split()
    lines: List[str] = []
    current = indent.rstrip()
    for word in words:
        candidate = f"{current} {word}" if current else indent + word
        if display_width(candidate) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = indent + word
    if current:
        lines.append(current)
    return lines
