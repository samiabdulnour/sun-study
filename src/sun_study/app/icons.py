"""The window's icons: a small picture beside every label.

Why the window has icons at all
-------------------------------

The window asks about sixty things across seven tabs, and the printer driver
it borrows from is the one settings dialog nobody enjoys and everybody
finishes. It works because each control carries a small picture of the thing
it changes -- a tray, a stapled corner, a folded sheet -- so the eye finds the
row before it reads the label. Four fields here begin with the word
"Communal" and are otherwise unrelated; a zone, a threshold, a height and a
grid tell them apart at a glance in a way the second word never will.

How a row gets its icon
-----------------------

By its own label, looked up in ``BY_LABEL`` below. The field helpers in
``window`` pass the label they were already given, so adding an icon to a
field is one line here and nothing at the call site -- and a field that moves
from one tab to another keeps its picture, because the picture belongs to the
thing and not to the tab.

A label with no entry gets no icon and reads exactly as it did before. That is
the intended behaviour for the rows where a drawing would be a lie: a layer
name is free text out of the project and there is nothing to draw.

Nothing here ever raises
------------------------

An icon is decoration. A missing PNG, a settings folder on a locked profile, a
Tk build that will not read the file -- none of those are worth refusing to
open the window for, so every failure answers ``None`` and the label renders
bare. This mirrors ``preferences``, where a settings file hand-edited into
nonsense costs one setting rather than the whole window.

The PNGs are drawn by ``scripts/make_icons.py`` and committed. Pillow is a
development dependency and is excluded from the bundle, because Tk reads PNG
by itself and the app only ever needs the finished files.
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from typing import Any

#: The size asked for when a caller does not say. Tab strips and row labels
#: both sit at 20: big enough that the sheet reads, small enough that a tab
#: does not grow to accommodate it.
DEFAULT_SIZE = 20

#: Sizes ``make_icons.py`` writes. Asking for anything else answers ``None``
#: rather than scaling a bitmap, which at these sizes looks broken.
SIZES = (16, 20, 24, 32)

#: Loaded images, by name and size.
#:
#: Tk does not own the images it is handed: a ``PhotoImage`` that Python
#: garbage-collects is a widget that silently loses its picture, which is the
#: oldest bug in Tkinter. Holding them here for the life of the process is
#: both the fix and the cache.
#:
#: An image belongs to the Tk interpreter that made it, and this cache outlives
#: any one of them, so an entry can go stale -- a second root, or a window
#: rebuilt after the first was destroyed, and the image is a name Tk no longer
#: knows. ``named`` checks an entry before handing it back rather than trusting
#: the dictionary, because the failure is a ``TclError`` at the moment a widget
#: is constructed, which is nowhere near where it would be understood.
_loaded: dict[tuple[str, int], tk.PhotoImage] = {}


def folder() -> Path:
    """Where the PNGs are.

    Two places, because the app runs two ways. Frozen, PyInstaller unpacks the
    data it was given into a temporary folder and names it on ``sys._MEIPASS``;
    from a checkout, the files sit in ``assets/icons`` at the top of the
    repository, four levels up from this module.
    """
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled is not None:
        return Path(bundled) / "assets" / "icons"
    return Path(__file__).resolve().parents[3] / "assets" / "icons"


def named(name: str, size: int = DEFAULT_SIZE) -> tk.PhotoImage | None:
    """One icon by its own name, or ``None`` if it cannot be had.

    ``None`` is a normal answer and every caller treats it as "no icon".
    """
    if size not in SIZES:
        return None

    key = (name, size)
    cached = _loaded.get(key)
    if cached is not None:
        try:
            # Touches the interpreter that owns it. A live image answers its
            # width; one whose root has gone raises, and is dropped and redrawn
            # against the root that is asking now.
            cached.width()
        except tk.TclError:
            del _loaded[key]
        else:
            return cached

    path = folder() / f"{name}-{size}.png"
    try:
        image = tk.PhotoImage(file=str(path))
    except (tk.TclError, RuntimeError, OSError):
        # TclError is the file that is not there or will not parse; RuntimeError
        # is a PhotoImage built before there is a Tk root to own it, which is a
        # programming error here but not one worth a blank window.
        return None

    _loaded[key] = image
    return image


def for_label(label: str, size: int = DEFAULT_SIZE) -> tk.PhotoImage | None:
    """The icon for a field, looked up by the label it already carries."""
    name = BY_LABEL.get(label)
    return named(name, size) if name else None


def options(label: str, size: int = DEFAULT_SIZE) -> dict[str, Any]:
    """Widget options that put this label's icon to the left of its text.

    Returned as a mapping to be splatted into the widget, so a label with no
    icon is constructed exactly as it was before rather than with an
    ``image=""`` that Tk has to be told to ignore.
    """
    image = for_label(label, size)
    if image is None:
        return {}
    return {"image": image, "compound": "left", "padding": (0, 0, 6, 0)}


def section_options(title: str, size: int = DEFAULT_SIZE) -> dict[str, Any]:
    """The same, for a notebook tab, which takes no padding.

    Keyed on the tab's title rather than on its position, so inserting a tab
    cannot shift every icon along by one.
    """
    name = BY_SECTION.get(title)
    if name is None:
        return {}
    image = named(name, size)
    return {} if image is None else {"image": image, "compound": "left"}


def button_options(name: str, size: int = 16) -> dict[str, Any]:
    """The same, for a button, which wants the smaller drawing.

    Run and Stop are the two icons in the set that are not pictures of
    anything -- a play triangle and a red square. They are verbs, and dressing
    a verb up as a drawing would claim it was a thing.
    """
    image = named(name, size)
    return {} if image is None else {"image": image, "compound": "left"}


def state_options(name: str, size: int = 16) -> dict[str, Any]:
    """Options for a label whose picture changes with what it is saying.

    The project line at the top of the window says one of five things -- no
    Archicad, reading, Tapir missing, unreadable, or the project's own tally --
    and the plug in front of it is seated or struck out to match.

    Both keys are always named, including when there is no icon: ``config``
    leaves alone what it is not given, so a label that had a plug and should
    not any more has to be told to drop it.
    """
    image = named(name, size) if name else None
    if image is None:
        return {"image": "", "compound": "none"}
    return {"image": image, "compound": "left"}


def study_options(label: str, size: int = DEFAULT_SIZE) -> dict[str, Any]:
    """The same, for a study's own tick.

    A ``ttk.Checkbutton`` draws its indicator to the left of everything, so
    the icon lands between the box and the words -- which is where the printer
    driver puts it too.
    """
    name = BY_STUDY.get(label)
    if name is None:
        return {}
    image = named(name, size)
    return {} if image is None else {"image": image, "compound": "left"}


#: The sections, in tab order. Keyed on the titles ``window`` defines, so a
#: renamed tab is a failed lookup and a bare tab rather than a wrong picture.
BY_SECTION: dict[str, str] = {
    "General": "general",
    "Site tools": "site",
    "Solar tools": "solar",
    "Model": "model",
    "Solar analysis": "analysis",
    "Shadow diagram": "shadow",
    "Sun views": "views",
}

#: The six studies, by the label on their own tick.
BY_STUDY: dict[str, str] = {
    "Facade (massing stage)": "facade",
    "Apartments": "apartments",
    "Communal open space": "communal",
    "Shadow diagram": "shadow",
    "Sun views": "views",
    "Site tools": "site",
}

#: Every field that has a picture worth drawing, by its own label.
#:
#: Shared entries are deliberate. The four prefixes are one question asked
#: four times and differ only in which study they file, so they wear one tag;
#: giving each a colour would cost the palette its meaning, where blue is the
#: interface and ochre is the sun and nothing else is either. The subsets are
#: the same folder four times over for the same reason.
BY_LABEL: dict[str, str] = {
    # General -- the project's own setup
    "Sheet master": "general",
    "Title block width (mm)": "title_block_width",
    "Sheets": "sheets",
    "Prefix — solar analysis": "prefix",
    "Prefix — shadow diagram": "prefix",
    "Prefix — sun views": "prefix",
    "Prefix — site analysis": "prefix",
    "Site group — context": "group",
    "Archicad wait (min)": "wait",
    "Measure from storey": "storey_datum",
    "...to storey": "storey_datum",
    "Days": "custom_day",
    "Custom day": "custom_day",
    "Year": "year",
    # Model -- what leaves Archicad
    "Export combination": "combination",
    "Also export": "also_export",
    "Neighbouring buildings": "neighbours",
    "Keep off drawings": "keep_off",
    # Solar analysis -- facade, apartments, communal
    "Facade layers": "facade_layers",
    "Skin cell (m)": "skin_cell",
    "Apartment zones": "zones",
    "Balcony zones": "balcony",
    "Living-room glazing": "glazing",
    "Plan times": "plan_times",
    "Time-of-day sheets filed under": "subset",
    "ADG sheets filed under": "subset",
    "Communal zones": "communal",
    "Communal window": "communal_window",
    "Communal threshold": "threshold",
    "Communal height": "height",
    "Communal grid": "grid",
    "Communal areas CSV": "table",
    # Shadow diagram
    "Published views": "folder",
    "Always there": "always_there",
    "Being tested": "being_tested",
    "Ground it lands on": "ground_plane",
    "Ignore ground below": "ground_cut",
    "Hours": "plan_times",
    "Fill Favorite": "fill_favorite",
    "Draw on storey": "storey_draw",
    "Shadow sheets filed under": "subset",
    # Sun views
    "Pen set": "pen_set",
    "Graphic override": "override",
    "Layers, from": "layers_from",
    "Scale": "scale",
    # Site tools
    "Address": "address",
    "Context Analysis sheet": "site_analysis",
    "Site Analysis sheet": "site_analysis",
    "Summary of Controls layout": "subset",
    "Future Context (what the controls allow next door)": "being_tested",
    "Context Model in 3D (terrain, blocks, neighbours)": "model",
    "Set the project location from the address": "address",
    "Context scale": "scale",
    "Site lands at": "site",
    "Model radius (m)": "radius",
    "Future reach (m)": "radius",
    "Street setback (m)": "setback",
    "Run folder": "run_folder",
}
