"""Putting the drawn result on a sheet, without anybody placing a drawing.

``draw`` colours the floor plan. That is the analysis made visible, but it is
not yet a deliverable: a plan lives in the Project Map, and what gets issued is
a Layout with a Drawing on it at a stated scale. Doing that by hand is four
clicks per storey, and a study that has to be re-run after every massing change
is exactly where four clicks per storey stops being free.

The chain, all of it Tapir
--------------------------
Archicad will not let a Hatch be created into a chosen database -- ``CreateHatches``
draws into whatever is active -- so the fills cannot simply be made "in the
worksheet". The supported route is the navigator one, and it is better anyway
because the Drawing stays *linked*: re-run the study, the plan updates, and the
sheet updates with it.

===================================  =======  =====================================
``GetNavigatorItemTree``             1.1.7    Find the storeys, and the masters
``CloneProjectMapItemToViewMap``     1.1.7    Give each one a View to place
``CreateLayout``                     1.4.0    The sheet
``CreateDrawings``                   1.4.0    The plan on the sheet, at a scale
``GetLayoutSettings``                1.1.7    The sheet size, to arrange them in
===================================  =======  =====================================

Storeys are matched by ``prefix``, which for a Story item is the floor number
as a string -- the same number ``DrawReport.storeys`` reports. Matching by name
would break on any project whose storeys are not called what the tool guessed.

A Layout needs a master
-----------------------
``CreateLayout``'s published schema requires only ``layoutName``. Its
implementation refuses that: without ``masterLayoutName`` or
``masterNavigatorItemId`` it fails with ``APIERR_BADPARS`` (-2130313112),
"Either masterLayoutName or masterNavigatorItemId must be provided". So the
master is looked up rather than left out, in the Layout Book, where masters
come back as ``MasterLayoutItem``.

Which master is a judgement call and the run always says which one it made.
An office keeps dozens -- the reference project has 67 -- and they are not
interchangeable: a title block sized for A3 lands a 1:200 plan off the sheet.
Preference goes to one whose name states the scale being drawn at, which is
how practices name them ("A1 - VERTICAL 1:200"), then to the first in the
book. ``--master-layout`` settles it by hand.

What this deliberately does not do
----------------------------------
It does not touch an existing layout's contents, and it does not delete
anything. A sheet may carry a title block, notes and a revision history that
nobody wants regenerated; re-running adds a Drawing to a *new* layout rather
than rebuilding an old one. Cleaning up is a person's decision, and the run
says which layouts it made so there is something to clean up *by*.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from sun_study.archicad.connection import ArchicadConnection, ArchicadError

__all__ = [
    "DEFAULT_LAYOUT_SCALE",
    "DEFAULT_SHEET",
    "LAYOUT_MINIMUM_TAPIR_VERSION",
    "MM_PER_M",
    "LayoutReport",
    "LayoutSheet",
    "NavigatorItem",
    "choose_master",
    "layout_from_views",
    "layout_results",
    "layout_sheet",
    "master_layouts",
    "project_map",
    "sheet_positions",
    "storey_items",
    "view_extents",
]

#: ``CreateLayout`` and ``CreateDrawings`` both arrived here. Gated separately,
#: like drawing is, so wanting numbers does not require a sheet-capable add-on.
LAYOUT_MINIMUM_TAPIR_VERSION = (1, 4, 0)

#: 1:200 suits an apartment-building floor plate on A1. Tried at 1:500, which
#: fits more per sheet and was wrong: annotation is sized in millimetres *on
#: paper*, so shrinking the plan by two and a half leaves the labels exactly as
#: big and they swamp the drawing. Plan and labels have to shrink together and
#: only the plan can. Stated rather than inherited from the view, because a
#: diagram issued at "whatever scale the view happened to be on" is not a
#: drawing anybody can measure off.
DEFAULT_LAYOUT_SCALE = 200.0

#: What ``CreateDrawings`` takes in its ``scale`` field: a magnification, where
#: 1.0 is 100%. Not a scale denominator, which is what it looks like and what
#: an earlier version passed -- a Drawing asked for at "200" arrived at
#: 20000% and ran off the sheet.
DRAWING_MAGNIFICATION = 1.0


@dataclass(frozen=True)
class NavigatorItem:
    """One entry in a navigator tree."""

    identifier: str
    name: str
    kind: str
    """``StoryItem``, ``FolderItem``, ``LayoutItem`` and so on."""
    prefix: str
    """For a Story item, the floor number as a string. Empty otherwise."""

    @property
    def storey_index(self) -> int | None:
        """The floor number, when this is a storey and the prefix parses."""
        if self.kind != "StoryItem":
            return None
        try:
            return int(self.prefix)
        except ValueError:
            return None


@dataclass(frozen=True)
class LayoutSheet:
    """A layout's page, in millimetres, and the part of it that can be drawn on."""

    width_mm: float
    height_mm: float
    left_mm: float = 0.0
    top_mm: float = 0.0
    right_mm: float = 0.0
    bottom_mm: float = 0.0

    @property
    def usable(self) -> tuple[float, float, float, float]:
        """``(x, y, width, height)`` of the area inside the margins."""
        width = max(self.width_mm - self.left_mm - self.right_mm, 1.0)
        height = max(self.height_mm - self.top_mm - self.bottom_mm, 1.0)
        return self.left_mm, self.top_mm, width, height

    def grid(self, count: int) -> list[tuple[float, float]]:
        """``count`` positions, tiled across the usable area, row by row.

        Placed at cell centres. A row of drawings at a fixed spacing was the
        first implementation and it walks straight off the page: six storeys
        at 420 mm is 2.5 m of sheet, so five of the six plans were outside an
        A1 and the sheet looked empty. Nobody wants the tool's arrangement
        anyway -- they want their own -- but it has to start on the paper.
        """
        if count <= 0:
            return []
        x0, y0, width, height = self.usable
        columns = max(1, math.ceil(math.sqrt(count * width / height)))
        rows = max(1, math.ceil(count / columns))
        cell_w, cell_h = width / columns, height / rows
        return [
            (
                x0 + (index % columns + 0.5) * cell_w,
                y0 + (index // columns + 0.5) * cell_h,
            )
            for index in range(count)
        ]


#: Used when Archicad will not say how big the sheet is. A1 landscape, which
#: is the common case for a plan at 1:200, and the run says it assumed it.
DEFAULT_SHEET = LayoutSheet(width_mm=841.0, height_mm=594.0)


@dataclass(frozen=True)
class LayoutReport:
    """What was placed, and on what."""

    layout_name: str
    drawings_placed: int
    storeys: tuple[int, ...]
    scale: float
    missing_storeys: tuple[int, ...]
    """Storeys that carry fills but have no Project Map item to place."""

    database_id: str = ""
    """The layout's own database, which is what finishing it needs: measuring,
    straightening and tiling all happen inside it."""

    master_name: str = ""
    """The master layout the sheet was built on. Always reported: it decides
    the sheet size and the title block, and nothing else in the run says it."""

    sheet: LayoutSheet | None = None
    """The page the drawings were arranged on, once it is known."""

    sheet_assumed: bool = False
    """Whether that page is what Archicad said, or what this fell back to."""

    @property
    def complete(self) -> bool:
        return not self.missing_storeys

    def describe(self) -> str:
        lines = [
            f"placed {self.drawings_placed} drawings at 1:{self.scale:g} on layout "
            f"{self.layout_name!r}"
            + (f", on master {self.master_name!r}" if self.master_name else "")
        ]
        if self.storeys:
            shown = ", ".join(str(storey) for storey in self.storeys)
            lines.append(f"  storey index {shown}, one drawing each")
        if self.sheet is not None:
            size = f"{self.sheet.width_mm:g} x {self.sheet.height_mm:g} mm"
            lines.append(
                f"  arranged on a {size} sheet"
                + (", which Archicad would not state and was assumed" if self.sheet_assumed else "")
            )
        if self.missing_storeys:
            shown = ", ".join(str(storey) for storey in self.missing_storeys)
            lines.append(
                f"  no Project Map storey found for index {shown}, so those fills "
                f"are drawn but not on a sheet"
            )
        lines.append("  the drawings are linked, so re-running the study updates the sheet")
        return "\n".join(lines)


def layout_sheet(connection: ArchicadConnection, database_id: str) -> tuple[LayoutSheet, bool]:
    """One layout's page size and margins, and whether they had to be assumed.

    Not fatal when it cannot be read. The fills are already drawn by this
    point and a sheet with its drawings stacked in the wrong place beats no
    sheet at all, so a page that will not describe itself falls back to A1 and
    the run says which it used.
    """
    try:
        response = connection.run_tapir(
            "GetLayoutSettings", {"layoutDatabaseIds": [{"databaseId": {"guid": database_id}}]}
        )
    except ArchicadError:
        return DEFAULT_SHEET, True

    rows = response.get("layoutSettings") if isinstance(response, dict) else None
    first = rows[0] if isinstance(rows, list) and rows else None
    if not isinstance(first, dict):
        return DEFAULT_SHEET, True
    try:
        sheet = LayoutSheet(
            width_mm=float(first["horizontalSize"]),
            height_mm=float(first["verticalSize"]),
            left_mm=float(first.get("leftMargin", 0.0)),
            top_mm=float(first.get("topMargin", 0.0)),
            right_mm=float(first.get("rightMargin", 0.0)),
            bottom_mm=float(first.get("bottomMargin", 0.0)),
        )
    except (KeyError, TypeError, ValueError):
        return DEFAULT_SHEET, True
    if sheet.width_mm <= 0 or sheet.height_mm <= 0:
        return DEFAULT_SHEET, True
    return sheet, False


def master_layouts(connection: ArchicadConnection) -> tuple[NavigatorItem, ...]:
    """Every master layout in the Layout Book, in book order."""
    response = connection.run_tapir("GetNavigatorItemTree", {"navigatorMapId": "LayoutBook"})
    root = response.get("navigatorItemTree") if isinstance(response, dict) else None
    if not isinstance(root, dict):
        raise ArchicadError(f"GetNavigatorItemTree returned no Layout Book: {response!r}")
    return tuple(item for item in _walk(root) if item.kind == "MasterLayoutItem")


def choose_master(
    masters: Sequence[NavigatorItem], wanted: str | None, scale: float
) -> NavigatorItem:
    """Which master to build the sheet on, and it is never a silent choice.

    A named one is honoured or the run stops with the list, because a master
    named slightly wrong would otherwise fall back to an arbitrary sheet and
    the study would come out on a title block for something else.
    """
    if not masters:
        raise ArchicadError(
            "The project has no master layouts, and CreateLayout cannot make a "
            "Layout without one. Add a master in the Layout Book, or run "
            "without --sheet."
        )
    if wanted:
        tidy = " ".join(wanted.split()).casefold()
        for master in masters:
            if " ".join(master.name.split()).casefold() == tidy:
                return master
        available = "\n    ".join(master.name for master in masters)
        raise ArchicadError(
            f"No master layout is called {wanted!r}. The project has:\n    {available}"
        )
    marker = f"1:{scale:g}"
    for master in masters:
        if marker in master.name:
            return master
    # Nothing names this scale. The first in the book is a guess, and on a
    # real project it was "A4" -- a quarter of the page six storey plans need.
    # The run prints what it used; ``--master-layout`` settles it.
    return masters[0]


def project_map(connection: ArchicadConnection) -> tuple[NavigatorItem, ...]:
    """Every item in the Project Map, flattened.

    The tree nests arbitrarily -- storeys can sit inside folders -- and nothing
    here cares about the shape, only about finding storeys. Flattening keeps
    the recursion in one place.
    """
    response = connection.run_tapir("GetNavigatorItemTree", {"navigatorMapId": "ProjectMap"})
    root = response.get("navigatorItemTree") if isinstance(response, dict) else None
    if not isinstance(root, dict):
        raise ArchicadError(f"GetNavigatorItemTree returned no tree: {response!r}")
    return tuple(_walk(root))


def _walk(item: dict[str, Any], depth: int = 0) -> Iterator[NavigatorItem]:
    """Depth-first, with a bound. A cycle here would hang the run."""
    if depth > 32:
        return
    identifier = (item.get("navigatorItemId") or {}).get("guid")
    if identifier:
        yield NavigatorItem(
            identifier=str(identifier),
            name=str(item.get("name", "")),
            kind=str(item.get("type", "")),
            prefix=str(item.get("prefix", "")),
        )
    for wrapper in item.get("children") or []:
        child = (wrapper or {}).get("navigatorItem") if isinstance(wrapper, dict) else None
        if isinstance(child, dict):
            yield from _walk(child, depth + 1)


def storey_items(
    connection: ArchicadConnection, storeys: Sequence[int]
) -> tuple[dict[int, NavigatorItem], tuple[int, ...]]:
    """The Project Map storey for each floor index, and the ones not found.

    Matched on the floor number rather than the name. Storey names are a
    practice's own business -- "Level 08", "L08", "8" -- and a tool that
    guesses at them finds nothing on the first project that names them
    differently.
    """
    by_index = {
        item.storey_index: item for item in project_map(connection) if item.storey_index is not None
    }
    found = {storey: by_index[storey] for storey in storeys if storey in by_index}
    missing = tuple(storey for storey in storeys if storey not in by_index)
    return found, missing


def layout_from_views(
    connection: ArchicadConnection,
    views: Sequence[tuple[str, str]],
    *,
    layout_name: str,
    scale: float = DEFAULT_LAYOUT_SCALE,
    master_layout: str | None = None,
) -> LayoutReport:
    """Put one Drawing per given View onto a new Layout.

    ``views`` is ``(navigator id, drawing name)``, already made and already
    pinned to whatever layer combination the sheet is supposed to show. That
    is the difference between this and ``layout_results``: this places views
    somebody else has prepared, so a sheet titled 09:00 shows the 09:00 layer
    and nothing else.
    """
    connection.require_tapir_at_least(
        LAYOUT_MINIMUM_TAPIR_VERSION, because="CreateLayout and CreateDrawings"
    )
    if not views:
        return LayoutReport(layout_name, 0, (), scale, ())

    master = choose_master(master_layouts(connection), master_layout, scale)
    layout = _create_layout(connection, layout_name, master)
    sheet, assumed = layout_sheet(connection, layout)

    # The drawing's size on the sheet is its view's extent at the drawing's
    # scale: 77 m at 1:500 is 154 mm. Falling back to the page grid only where
    # a view will not say what it covers.
    # A view covering 53 m drawn at 1:200 is 0.266 m on the sheet. In metres,
    # because that is what a Drawing's position is measured in.
    extents = view_extents(connection, [identifier for identifier, _ in views])
    if extents:
        widest = max(width for width, _ in extents.values()) / scale
        tallest = max(height for _, height in extents.values()) / scale
        positions = sheet_positions(sheet, len(views), (widest, tallest))
    else:
        positions = [(x / MM_PER_M, y / MM_PER_M) for x, y in sheet.grid(len(views))]

    drawings = [
        {
            "navigatorItemId": {"guid": identifier},
            "layoutDatabaseId": {"guid": layout},
            "name": name,
            "position": {"x": x, "y": y},
            # 1.0, not the scale: this field is the Drawing's *magnification*,
            # and the scale belongs to the view. Passing 200 here put the
            # drawing on the sheet at 20000%.
            "scale": DRAWING_MAGNIFICATION,
        }
        for (identifier, name), (x, y) in zip(views, positions, strict=True)
    ]
    placed = connection.run_tapir("CreateDrawings", {"drawingsData": drawings})
    _check_each(placed, "elements", "CreateDrawings")

    return LayoutReport(
        layout_name=layout_name,
        drawings_placed=len(drawings),
        storeys=(),
        scale=scale,
        missing_storeys=(),
        database_id=layout,
        master_name=master.name,
        sheet=sheet,
        sheet_assumed=assumed,
    )


def layout_results(
    connection: ArchicadConnection,
    storeys: Sequence[int],
    *,
    layout_name: str,
    scale: float = DEFAULT_LAYOUT_SCALE,
    master_layout: str | None = None,
) -> LayoutReport:
    """Put one linked Drawing per storey onto a new Layout.

    ``storeys`` comes straight from ``DrawReport.storeys``, so the sheet shows
    exactly the plans that carry fills and no others.
    """
    connection.require_tapir_at_least(
        LAYOUT_MINIMUM_TAPIR_VERSION, because="CreateLayout and CreateDrawings"
    )
    if not storeys:
        return LayoutReport(layout_name, 0, (), scale, ())

    items, missing = storey_items(connection, storeys)
    if not items:
        return LayoutReport(layout_name, 0, (), scale, missing)

    ordered = sorted(items)
    views = _clone_to_view_map(connection, [items[storey] for storey in ordered])
    master = choose_master(master_layouts(connection), master_layout, scale)
    layout = _create_layout(connection, layout_name, master)

    # Tiled across the page, in millimetres on the sheet. Crude on purpose: a
    # person moves them where they want them, and the Drawings stay linked
    # wherever they end up. It only has to start on the paper.
    sheet, assumed = layout_sheet(connection, layout)
    positions = sheet.grid(len(ordered))
    drawings = [
        {
            "navigatorItemId": {"guid": view},
            "layoutDatabaseId": {"guid": layout},
            "name": f"Sun Study -- storey {storey}",
            "position": {"x": x, "y": y},
            "scale": scale,
        }
        for (storey, view), (x, y) in zip(zip(ordered, views, strict=True), positions, strict=True)
    ]
    placed = connection.run_tapir("CreateDrawings", {"drawingsData": drawings})
    _check_each(placed, "elements", "CreateDrawings")

    return LayoutReport(
        layout_name=layout_name,
        drawings_placed=len(drawings),
        storeys=tuple(ordered),
        scale=scale,
        missing_storeys=missing,
        master_name=master.name,
        sheet=sheet,
        sheet_assumed=assumed,
    )


def view_extents(
    connection: ArchicadConnection, views: Sequence[str]
) -> dict[str, tuple[float, float]]:
    """Each view's saved zoom, as the width and height it covers in metres.

    This is how a drawing's size on the sheet is known *before* it exists.
    Measuring the placed drawing would be better and is not possible: a layout
    created in this session cannot be read back from -- ``GetDetailsOfElements``
    answers with a per-element error even after ``ChangeWindow`` reports
    success -- the same materialisation limit that stops a freshly created
    worksheet being activated. Anything that needs the real bounds has to wait
    for a later session, and a sheet cannot.
    """
    if not views:
        return {}
    response = connection.run_tapir(
        "GetViewSettings",
        {"navigatorItemIds": [{"navigatorItemId": {"guid": guid}} for guid in views]},
    )
    rows = response.get("viewSettings") if isinstance(response, dict) else None
    if not isinstance(rows, list) or len(rows) != len(views):
        return {}

    extents: dict[str, tuple[float, float]] = {}
    for guid, row in zip(views, rows, strict=True):
        zoom = (row or {}).get("zoom") if isinstance(row, dict) else None
        if not isinstance(zoom, dict):
            continue
        try:
            width = float(zoom["xMax"]) - float(zoom["xMin"])
            height = float(zoom["yMax"]) - float(zoom["yMin"])
        except (KeyError, TypeError, ValueError):
            continue
        if width > 0 and height > 0:
            extents[guid] = (width, height)
    return extents


#: Layout coordinates are in **metres**, while ``GetLayoutSettings`` reports
#: the page size in millimetres. Mixing the two put a drawing meant for
#: x = 200 mm at x = 200 m -- a quarter of a kilometre off a sheet 0.841 m
#: wide, which is exactly how far the first sheets landed.
MM_PER_M = 1000.0


def sheet_positions(
    sheet: LayoutSheet,
    count: int,
    tile_m: tuple[float, float],
    gap_m: float = 0.012,
) -> list[tuple[float, float]]:
    """Centres for ``count`` drawings of a known size, tiled on the sheet.

    Everything here is in **metres**, including what comes back: that is the
    unit a Drawing's position is given in, even though the page it sits on is
    described in millimetres. ``sheet`` is taken in millimetres because that
    is how Archicad reports it, and converted once, here.

    Sized from the drawings rather than from the page, which is the fix for
    the first version: that divided the page into as many cells as there were
    drawings and placed one per cell, so six storey plans on an A1 were
    assigned cells 280 mm wide, laid out three across, and the second row ran
    off the bottom of the page.

    The block is centred, so a sheet with room to spare does not leave the
    drawings bunched into a corner with the title block empty beside them.
    """
    if count <= 0:
        return []
    left_mm, top_mm, wide_mm, high_mm = sheet.usable
    x0, y0 = left_mm / MM_PER_M, top_mm / MM_PER_M
    usable_w, usable_h = wide_mm / MM_PER_M, high_mm / MM_PER_M
    width, height = tile_m[0] + gap_m, tile_m[1] + gap_m
    columns = max(1, min(count, int(usable_w // width) or 1))
    rows = max(1, -(-count // columns))

    block_w, block_h = columns * width, rows * height
    left = x0 + max(0.0, (usable_w - block_w) / 2.0)
    top = y0 + max(0.0, (usable_h - block_h) / 2.0)

    return [
        (
            left + (index % columns + 0.5) * width,
            # Rows read downward from the top of the block.
            top + block_h - (index // columns + 0.5) * height,
        )
        for index in range(count)
    ]


def _clone_to_view_map(connection: ArchicadConnection, items: Sequence[NavigatorItem]) -> list[str]:
    """A View Map clone per storey, because a Drawing is placed from a View.

    Clones rather than independent views: a clone follows its Project Map
    source, so a storey renamed later stays consistent with the sheet.
    """
    response = connection.run_tapir(
        "CloneProjectMapItemToViewMap",
        {"viewsData": [{"navigatorItemId": {"guid": item.identifier}} for item in items]},
    )
    cloned = _check_each(response, "navigatorItems", "CloneProjectMapItemToViewMap")
    identifiers = [str((entry.get("navigatorItemId") or {}).get("guid", "")) for entry in cloned]
    if len(identifiers) != len(items) or not all(identifiers):
        raise ArchicadError(
            f"CloneProjectMapItemToViewMap returned {len(identifiers)} views for "
            f"{len(items)} storeys; the lists must be parallel."
        )
    return identifiers


def _create_layout(connection: ArchicadConnection, name: str, master: NavigatorItem) -> str:
    response = connection.run_tapir(
        "CreateLayout",
        {
            "layoutsData": [
                {
                    "layoutName": name,
                    "masterNavigatorItemId": {"guid": master.identifier},
                }
            ]
        },
    )
    databases = _check_each(response, "databases", "CreateLayout")
    first = databases[0] if databases else {}
    identifier = (first.get("databaseId") or {}).get("guid")
    if not identifier:
        raise ArchicadError(f"CreateLayout returned no database id: {response!r}")
    return str(identifier)


def _check_each(response: Any, key: str, command: str) -> list[dict[str, Any]]:
    """The per-item results, with any per-item error raised.

    These commands report failures inside a successful response, one slot per
    input -- the same shape ``draw._create`` guards against. A sheet missing
    one storey silently is worse than no sheet: the missing plan reads as a
    storey with no apartments.
    """
    items = response.get(key) if isinstance(response, dict) else None
    if not isinstance(items, list):
        raise ArchicadError(f"{command} returned no {key}: {response!r}")
    for entry in items:
        if isinstance(entry, dict) and "error" in entry:
            error = entry["error"] or {}
            raise ArchicadError(
                f"{command} failed for one item: "
                f"{error.get('message', 'no message')} (code {error.get('code')})"
            )
    return [entry for entry in items if isinstance(entry, dict)]
