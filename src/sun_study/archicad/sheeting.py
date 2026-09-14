"""How a run of drawings is dealt onto layouts: one sheet, two, or one each.

Every solar tool makes a series -- seven hours, sometimes nine -- and each
had its own idea of how many go on a sheet: the shadow diagrams four, the
sun views four with a knob, the apartment plans one, the communal plans all
of them. Same question, four answers, and the office asks it the same way
every time: *all on one sheet, split in two, or a sheet each*. So the
answer lives here, once, and the four builders ask it.

- ``one``: every drawing on one layout.
- ``two``: the first half on one layout and the rest on a second, laid out
  on the same grid, so seven is four and three with one cell empty rather
  than two by two beside a row of three. Nine is five and four.
- ``each``: a layout per drawing, at full size.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = [
    "DEFAULT_SHEET_MODE",
    "SHEETS_HELP",
    "SHEET_MODES",
    "cells_for",
    "parse_sheet_mode",
    "sheet_chunks",
    "sheet_title",
]

SHEET_MODES: tuple[str, ...] = ("one", "two", "each")

#: What the window and the reference sheets do: a morning and an afternoon.
DEFAULT_SHEET_MODE = "two"

SHEETS_HELP = (
    "How the drawings are dealt onto layouts: 'one' puts them all on one sheet, "
    "'two' splits them across two sheets on the same grid (seven is four and "
    "three), 'each' makes a sheet per drawing."
)

_ALIASES = {
    "1": "one",
    "one": "one",
    "single": "one",
    "all": "one",
    "2": "two",
    "two": "two",
    "half": "two",
    "halves": "two",
    "split": "two",
    "each": "each",
    "every": "each",
    "per": "each",
    "per-drawing": "each",
    "per drawing": "each",
    "separate": "each",
}


def parse_sheet_mode(text: str | None) -> str:
    """``one``, ``two`` or ``each``, from any of the ways people say them.

    ``None`` or blank is the default. Raises ``ValueError`` naming the three
    words, because the caller is an option parser and that is the message.
    """
    wanted = " ".join((text or "").split()).lower()
    if not wanted:
        return DEFAULT_SHEET_MODE
    mode = _ALIASES.get(wanted)
    if mode is None:
        raise ValueError(f"Sheets are one, two or each. {text!r} is none of those.")
    return mode


def sheet_chunks(count: int, mode: str) -> list[tuple[int, int]]:
    """``(start, stop)`` of each sheet's drawings, in order.

    ``two`` gives the first sheet the larger half, so the second is the one
    short: seven is ``(0, 4), (4, 7)``. Fewer than two drawings is one sheet
    whatever the mode says, because a second sheet with nothing on it is not
    a sheet.
    """
    if count <= 0:
        return []
    chosen = parse_sheet_mode(mode)
    if chosen == "each":
        return [(i, i + 1) for i in range(count)]
    if chosen == "two" and count >= 2:
        half = -(-count // 2)
        return [(0, half), (half, count)]
    return [(0, count)]


def cells_for(chunks: Sequence[tuple[int, int]]) -> int:
    """The grid every sheet of a set is laid out as: the fullest sheet's.

    A short second sheet takes the first cells of the same grid, at the same
    size, with the rest empty. Two sheets that are read against each other
    have to be laid out the same way or the reader compares sizes instead
    of shadows.
    """
    return max((stop - start for start, stop in chunks), default=0)


def sheet_title(labels: Sequence[str]) -> str:
    """What a sheet is called for the drawings on it: ``09:00-12:00``, or
    the one label when there is one."""
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    return f"{labels[0]}-{labels[-1]}"
