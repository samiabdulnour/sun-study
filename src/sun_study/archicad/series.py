"""Drawing the nine-to-three series into a Worksheet, one tile per instant.

What this makes
---------------
A single sheet of small plans: the floor of every assessed apartment in pale
grey, the sun patch on it in colour, and the clock time under each. Twenty-five
tiles across a page is the drawing the office's own reference deliverable makes
one page at a time, and it is the thing a duration cannot show -- 106 minutes
says nothing about *which* 106, or how far into the room the sun reached.

Why a Worksheet, and what that costs
------------------------------------
[D33](../../docs/decisions.md) records the measurement that makes this possible
at all: ``ChangeWindow`` with a ``databaseId`` moves the *current database*, and
element creation follows it, so fills can be made inside a Worksheet after all.
Two consequences shape everything here:

* **The worksheet has to exist already.** One created in the same session
  cannot be activated -- Archicad refuses with ``-2130313110``. So this finds a
  worksheet by name and fails with the list of names if it is not there, rather
  than making one and drawing into a database nobody can open.
* **The tool owns the worksheet.** ``CreateTexts`` takes no layer, so a caption
  lands on whatever layer is current and no layer-scoped delete can find it
  again. Rather than leave a run's captions behind for the next run to draw
  over, the whole database is cleared first. That is safe only because the
  worksheet is a dedicated output, and the run says so before doing it.

Coordinates
-----------
Everything is drawn in the *export's* coordinates, shifted so the building sits
at the worksheet origin. Nothing here is overlaid on the model, so the absolute
position is free -- which is what lets the whole tile be self-consistent
without fitting a transform between Archicad's project frame and the IFC world
frame. The floor outline comes from the same grid as the patch, merged with
every cell lit, so the two can never disagree about where the floor is.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

from sun_study.archicad.connection import ArchicadConnection, ArchicadError, activate
from sun_study.archicad.draw import DRAWING_MINIMUM_TAPIR_VERSION, BandStyle, ensure_layer
from sun_study.archicad.layout import _walk
from sun_study.archicad.read import layer_names
from sun_study.core.patches import Rectangle, merge_lit_cells

FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]

__all__ = [
    "DEFAULT_GUTTER_M",
    "FLOOR_STYLE",
    "SUNLIT_STYLE",
    "PatchRow",
    "SeriesReport",
    "WorksheetTarget",
    "clear_database",
    "database_of",
    "draw_patch_series",
    "ensure_model_database",
    "find_worksheet",
    "restore_after",
]

#: Between tiles, in metres of model space. A tile is a floor plate, so the gap
#: has to read as a gap at 1:200 without wasting half the sheet.
DEFAULT_GUTTER_M = 6.0

#: The floor, under everything. Pale, because it is context: the eye should
#: find the patch, not the plate it lies on.
FLOOR_STYLE = BandStyle("floor", float("inf"), fill_pen=19, rgb=(228, 228, 228))

#: The patch itself. The same amber the office's own studies use for sun on a
#: surface, and the one this tool already draws for its 3-4 hour band.
SUNLIT_STYLE = BandStyle("in sun", float("inf"), fill_pen=124, rgb=(255, 213, 79))


@dataclass(frozen=True)
class WorksheetTarget:
    """A worksheet that exists and can be drawn into."""

    name: str
    navigator_id: str
    database_id: str


@dataclass(frozen=True)
class PatchRow:
    """One storey of the series: its label and which floor cells are on it.

    A worksheet has no storeys, so six levels of apartments drawn at their own
    coordinates land on top of each other and the tile becomes a composite of
    the whole building -- which is not a floor plan of anything. Splitting the
    cells into rows puts one level per row of tiles, which is what a study
    sheet is: level down the side, time across the top.
    """

    label: str
    mask: BoolArray


@dataclass(frozen=True)
class SeriesReport:
    """What was drawn, and where."""

    worksheet: str
    tiles: int
    fills: int
    captions: int
    cleared: int
    left_behind: int
    layer_index: int
    first_time: str
    last_time: str
    lit_area_m2: tuple[float, ...]
    """Lit floor area at each drawn instant, so the picture can be checked
    against a number without opening Archicad."""

    def describe(self) -> str:
        lines = [
            f"drew {self.tiles} tiles ({self.first_time} to {self.last_time}) "
            f"into worksheet {self.worksheet!r}: {self.fills} fills, "
            f"{self.captions} captions, on layer index {self.layer_index}"
        ]
        if self.cleared:
            lines.append(f"  cleared {self.cleared} elements the previous run left there")
        if self.left_behind:
            lines.append(
                f"  WARNING: {self.left_behind} elements could not be deleted and are "
                f"still there, under this run's. Archicad refuses to delete from a "
                f"hidden layer and reports success anyway."
            )
        if self.lit_area_m2:
            peak = max(self.lit_area_m2)
            lines.append(
                f"  lit floor area runs {self.lit_area_m2[0]:.1f} to "
                f"{self.lit_area_m2[-1]:.1f} m2, peaking at {peak:.1f} m2"
            )
        lines.append(
            "  the worksheet is regenerated in full on every run, so anything "
            "drawn into it by hand will not survive"
        )
        return "\n".join(lines)


def find_worksheet(connection: ArchicadConnection, name: str) -> WorksheetTarget:
    """The worksheet with this name, or an error naming the ones that exist.

    Matched case- and whitespace-insensitively, because a worksheet name typed
    into a command line will not match ``Solar Penetration Outlines`` byte for
    byte. A worksheet's *ID* would be the better key, but the navigator tree
    does not carry it -- only the name and the type.
    """
    response = connection.run_tapir("GetNavigatorItemTree", {"navigatorMapId": "ProjectMap"})
    root = response.get("navigatorItemTree") if isinstance(response, dict) else None
    if not isinstance(root, dict):
        raise ArchicadError(f"GetNavigatorItemTree returned no tree: {response!r}")

    found = list(_worksheets(root))
    wanted = " ".join(name.split()).casefold()
    for item_name, identifier in found:
        if " ".join(item_name.split()).casefold() == wanted:
            return WorksheetTarget(
                name=item_name,
                navigator_id=identifier,
                database_id=database_of(connection, identifier),
            )

    available = "\n    ".join(sorted(item for item, _ in found)) or "(none at all)"
    raise ArchicadError(
        f"No worksheet is called {name!r}. The project has:\n    {available}\n"
        f"  Create it in Archicad first -- a worksheet made through the API "
        f"cannot be opened in the same session, so this cannot make it for you."
    )


def _worksheets(node: dict[str, Any], depth: int = 0) -> list[tuple[str, str]]:
    """Every worksheet in a navigator tree, as ``(name, navigator id)``."""
    if depth > 32:
        return []
    found: list[tuple[str, str]] = []
    if node.get("type") == "WorksheetDrawingItem":
        identifier = (node.get("navigatorItemId") or {}).get("guid")
        if identifier:
            found.append((str(node.get("name", "")), str(identifier)))
    for wrapper in node.get("children") or []:
        child = (wrapper or {}).get("navigatorItem") if isinstance(wrapper, dict) else None
        if isinstance(child, dict):
            found.extend(_worksheets(child, depth + 1))
    return found


def database_of(connection: ArchicadConnection, navigator_id: str) -> str:
    response = connection.run_tapir(
        "GetDatabaseIdFromNavigatorItemId",
        {"navigatorItemIds": [{"navigatorItemId": {"guid": navigator_id}}]},
    )
    databases = response.get("databases") if isinstance(response, dict) else None
    if not isinstance(databases, list) or not databases:
        raise ArchicadError(f"GetDatabaseIdFromNavigatorItemId returned nothing: {response!r}")
    identifier = (databases[0].get("databaseId") or {}).get("guid")
    if not identifier:
        raise ArchicadError(f"No database id for navigator item {navigator_id}: {response!r}")
    return str(identifier)


def clear_database(connection: ArchicadConnection) -> tuple[int, int]:
    """Empty the *current* database. Returns ``(deleted, left behind)``.

    Wholesale rather than by layer because ``CreateTexts`` accepts no layer, so
    a caption cannot be found again by one. This is only ever pointed at a
    worksheet the tool has been given as its own.

    Two things make this harder than it reads, both measured:

    * **A hidden layer cannot be deleted from.** ``DeleteElements`` answers
      ``{"success": true}`` and removes nothing. Since a layer this tool
      creates is hidden in every layer combination the project already had,
      that is the ordinary case: a worksheet on the reference project reached
      15,889 fills over three runs, each reporting that it had cleared the
      last. So every layer holding something here is forced visible first.
    * **The count has to be checked.** Nothing in the response distinguishes
      "deleted 6,396" from "deleted none", so the only honest number comes
      from counting again afterwards.
    """
    doomed: list[dict[str, Any]] = []
    for element_type in ("Hatch", "Text", "PolyLine"):
        response = connection.run_tapir("GetElementsByType", {"elementType": element_type})
        elements = response.get("elements") if isinstance(response, dict) else None
        if isinstance(elements, list):
            doomed.extend(element for element in elements if isinstance(element, dict))
    if not doomed:
        return 0, 0

    _unhide_layers_of(connection, doomed)
    connection.run_tapir("DeleteElements", {"elements": doomed})

    left = 0
    for element_type in ("Hatch", "Text", "PolyLine"):
        response = connection.run_tapir("GetElementsByType", {"elementType": element_type})
        elements = response.get("elements") if isinstance(response, dict) else None
        if isinstance(elements, list):
            left += len(elements)
    return len(doomed) - left, left


def _unhide_layers_of(connection: ArchicadConnection, elements: Sequence[dict[str, Any]]) -> None:
    """Show every layer these elements sit on, so they can be deleted.

    Best effort: a build that will not describe its elements is a worse reason
    to stop than a delete that may not take, and the caller checks the outcome
    either way.
    """
    try:
        response = connection.run_tapir("GetDetailsOfElements", {"elements": list(elements)})
    except ArchicadError:
        return
    rows = response.get("detailsOfElements") if isinstance(response, dict) else None
    if not isinstance(rows, list):
        return

    indices = {row.get("layerIndex") for row in rows if isinstance(row, dict)}
    names = layer_names(connection)
    for index in indices:
        name = names.get(index) if isinstance(index, int) else None
        if name:
            with suppress(ArchicadError):
                ensure_layer(connection, name)


def draw_patch_series(
    connection: ArchicadConnection,
    *,
    worksheet: WorksheetTarget,
    positions: FloatArray,
    sunlit: BoolArray,
    times: Sequence[str],
    spacing_m: float,
    layer_name: str,
    rows: Sequence[PatchRow] | None = None,
    floor_style: BandStyle = FLOOR_STYLE,
    sunlit_style: BandStyle = SUNLIT_STYLE,
    gutter_m: float = DEFAULT_GUTTER_M,
    caption_height_mm: float = 3.5,
) -> SeriesReport:
    """Draw one tile per instant into the worksheet, and restore nothing.

    ``positions`` and ``sunlit`` are the floor grid and its per-instant mask
    straight off ``InstantSeries``; ``times`` are the captions, already
    formatted, and become the columns. ``rows`` splits the cells by storey and
    becomes the rows; without it everything is drawn as one row, which is only
    right for a single-storey building.

    The caller should try to put a floor plan back afterwards -- see
    ``restore_after`` -- but on AC26 that mostly fails silently, and the export
    is unaffected either way.
    """
    connection.require_tapir_at_least(
        DRAWING_MINIMUM_TAPIR_VERSION, "CreateHatches, which draws the patch,"
    )
    if len(times) == 0:
        raise ArchicadError("No instants to draw.")

    layer = ensure_layer(connection, layer_name)
    cleared, left_behind = clear_database(connection)

    bands = list(rows) if rows else [PatchRow("", np.ones(len(positions), dtype=bool))]

    # One tile size for the whole sheet, from the whole grid, so that tiles
    # line up into columns even where one storey is smaller than another.
    origin_x = float(positions[:, 0].min())
    origin_y = float(positions[:, 1].min())
    width = float(positions[:, 0].max()) - origin_x + gutter_m
    height = float(positions[:, 1].max()) - origin_y + gutter_m

    fills: list[dict[str, Any]] = []
    captions: list[dict[str, Any]] = []
    lit_area: list[float] = []

    for row_index, band in enumerate(bands):
        here = positions[band.mask]
        if not len(here):
            continue
        # Merged once per storey: every cell of it, lit. Drawn under each tile
        # so the patch is read against the floor it lies on.
        footprint = merge_lit_cells(here, np.ones(len(here), dtype=bool), spacing_m)
        lit_here = sunlit[band.mask]

        # Rows run downward, the way a study sheet reads: level down the side,
        # time across the top.
        dy = -row_index * height - origin_y
        if band.label:
            captions.append(
                {
                    "coordinate": {
                        "x": -gutter_m * 2.5,
                        "y": dy + origin_y + height * 0.5,
                        "z": 0.0,
                    },
                    "text": band.label,
                    "height": caption_height_mm * 1.2,
                    "justification": "Right",
                }
            )

        for column, caption in enumerate(times):
            dx = column * width - origin_x

            for rectangle in footprint:
                fills.append(_hatch(rectangle, dx, dy, floor_style, layer.index))

            patch = merge_lit_cells(here, lit_here[:, column], spacing_m)
            if row_index == 0:
                lit_area.append(sum(rectangle.area_m2 for rectangle in patch))
            else:
                lit_area[column] += sum(rectangle.area_m2 for rectangle in patch)
            for rectangle in patch:
                fills.append(_hatch(rectangle, dx, dy, sunlit_style, layer.index))

            if row_index == 0:
                captions.append(
                    {
                        "coordinate": {
                            "x": dx + origin_x,
                            "y": gutter_m * 0.6,
                            "z": 0.0,
                        },
                        "text": caption,
                        "height": caption_height_mm,
                        "justification": "Left",
                    }
                )

    _create(connection, "CreateHatches", "hatchesData", fills)
    _create(connection, "CreateTexts", "textsData", captions)

    return SeriesReport(
        worksheet=worksheet.name,
        tiles=len(times) * len(bands),
        fills=len(fills),
        captions=len(captions),
        cleared=cleared,
        left_behind=left_behind,
        layer_index=layer.index,
        first_time=times[0],
        last_time=times[-1],
        lit_area_m2=tuple(lit_area),
    )


def ensure_model_database(connection: ArchicadConnection) -> str | None:
    """Make a floor plan current if something else is. Returns what it left.

    Not cosmetic, which is what the first version of this assumed. Almost
    every read in this tool goes through ``GetElementsByType``, and that is
    scoped to the **current database** -- so with a worksheet current the
    project has no Zones in it, no walls, and no windows. A run started that
    way reports zero apartments, or pairs none, and blames the layer filter.

    A previous run leaves exactly that state behind: ``draw_patch_series``
    activates the worksheet and AC26 will not switch back
    (see ``restore_after``), so the *next* run inherits it. The window on
    screen stays where it is either way; only the database moves.

    Cheap to call repeatedly, which is what makes it usable as a precondition
    rather than something remembered at a few chosen points. When the tool
    moved the database itself and moved it to a floor plan, that is already
    the answer and no round trip is needed. The slow path is for a connection
    that has not moved anything -- a fresh process inheriting whatever the
    last run left -- which is exactly the case the two reads exist for.
    """
    here = connection.database
    if here is not None and here.is_model:
        return None

    current = connection.run_tapir("GetCurrentWindowType", {})
    where = current.get("currentWindowType") if isinstance(current, dict) else None

    # The window is not the test. ``ChangeWindow`` moves the *database* even
    # when the visible window stays put, so a run can be looking at a floor
    # plan while every read goes to a layout -- and a layout holds no walls,
    # so the project reads as empty and the scene filter gets the blame. Ask
    # what is actually visible to a read instead.
    walls = connection.run_tapir("GetElementsByType", {"elementType": "Wall"})
    found = walls.get("elements") if isinstance(walls, dict) else None
    if where == "FloorPlan" and isinstance(found, list) and found:
        # Nothing to move, but the walls just proved where we are. Say so, or
        # the connection goes on reporting that it does not know -- and the
        # layer guard, which trusts that answer, stays switched off.
        connection.note_model_database()
        return None

    response = connection.run_tapir("GetNavigatorItemTree", {"navigatorMapId": "ProjectMap"})
    root = response.get("navigatorItemTree") if isinstance(response, dict) else None
    if not isinstance(root, dict):
        return None
    storeys = [item for item in _walk(root) if item.storey_index is not None]
    if not storeys:
        return None

    lowest = min(storeys, key=lambda item: item.storey_index or 0)
    activate(connection, database_of(connection, lowest.identifier), "FloorPlan")
    return str(where)


def restore_after(connection: ArchicadConnection, storey_database_id: str) -> bool:
    """Try to put a floor plan back in front. True if Archicad actually moved.

    On AC26 it usually does not, and it says it did. Every documented shape --
    ``windowType`` alone, ``windowType`` with a ``storyIndex``, and a floor
    plan's own ``databaseId`` -- returns ``{"success": true}`` and leaves the
    worksheet on screen. The ``navigatorItemId`` form, which might work, needs
    Archicad 27.

    This is **not** cosmetic, which an earlier version of this docstring
    claimed on the strength of one export that happened to come out whole. The
    export follows the *window*, not the current database: with a worksheet in
    front the same project exports as 5.8 kB carrying an ``IfcSite``, an
    ``IfcBuilding`` and nothing else, while element reads keep answering
    normally from the model database. The next run then fails in the scene
    filter, blaming a layer name that was right.

    So the caller says so plainly. Nobody can clear it except a person
    clicking a storey in the Project Map.
    """
    activate(connection, storey_database_id, "FloorPlan")
    current = connection.run_tapir("GetCurrentWindowType", {})
    return bool(isinstance(current, dict) and current.get("currentWindowType") == "FloorPlan")


def _hatch(
    rectangle: Rectangle, dx: float, dy: float, style: BandStyle, layer_index: int
) -> dict[str, Any]:
    """One rectangle as a hatch, shifted into its tile.

    No ``floorInd``: a worksheet has no storeys, and Tapir's own issue tracker
    records a floor index silently destroying elements in a database that has
    none.
    """
    return {
        "coordinates": [{"x": x + dx, "y": y + dy} for x, y in rectangle.corners],
        "layerIndex": layer_index,
        "fillPenIndex": style.fill_pen,
        "fillBackgroundPenIndex": style.background_pen,
        "contourPenIndex": style.fill_pen,
        # Explicitly off. A Fill inherits the Fill tool's current default, and
        # on a real project that default has "Show Area Text" on -- so every
        # patch cell arrives with its own square-metre figure printed across
        # it, which at 250 mm resolution is thousands of numbers over the plan.
        "showArea": False,
    }


def _create(
    connection: ArchicadConnection, command: str, key: str, data: list[dict[str, Any]]
) -> None:
    """Run a create command in batches, failing on any per-element error.

    Batched because a series is thousands of fills and one request carrying all
    of them is a JSON payload Archicad has to parse in one go.
    """
    if not data:
        return
    batch = 500
    for start in range(0, len(data), batch):
        response = connection.run_tapir(command, {key: data[start : start + batch]})
        elements = response.get("elements") if isinstance(response, dict) else None
        if not isinstance(elements, list):
            raise ArchicadError(f"{command} returned no element list: {response!r}")
        for entry in elements:
            if isinstance(entry, dict) and "error" in entry:
                error = entry["error"] or {}
                raise ArchicadError(
                    f"{command} failed for one item: "
                    f"{error.get('message', 'no message')} (code {error.get('code')})"
                )
