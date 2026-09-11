"""Sun eye views: the 3D window aimed along the sun, one view per hour.

The office's solar penetration diagram is made by hand like this: aim the 3D
window along the sun for one hour, save a view, make a 3D Document from it,
repeat for every hour from 9am to 3pm, then place the documents on a layout.
Aimed by hand, the views are a few tenths of a degree off; aimed here they are
exact, and the sun on each is Archicad's own, computed from the date.

Two halves, and both are needed. The **projection** is the add-on's: Tapir has
no command for it, which is the whole reason ``archicad-addon/`` exists. Every
other setting on the view -- layers, the yellow-glazing override, the
renovation filter, the 3D style, the scale -- is Tapir's ``SetViewSettings``,
exactly as for the shadow diagrams.

Which frame the bearing is in
-----------------------------
A sun bearing is a *true* bearing. Archicad's 3D window works in the
*project's* frame, which on the reference project is turned 41 degrees. The
add-on takes the project-frame bearing, so the turn is made here, once, from
the north angle the project itself reports. Getting this wrong does not fail:
it draws a complete, plausible diagram of the building lit from the wrong
side. ``docs/addon.md`` records the measurement that settled the convention.

What a view and a document keep
-------------------------------
Measured on a live Archicad 26: a view saved from the aimed 3D window carries
its own projection and its own sun date, and a 3D Document made with
``CreateDocumentFrom3D`` carries the projection it was made with. So seven
views made in a row are seven hours, not seven copies of the last one.

Neither can be deleted or re-aimed through the API. A view or document that
already exists under a run's name is kept as it is and reported, which is
what makes a second run safe: it fills gaps rather than piling up copies.
"""

from __future__ import annotations

import collections
import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from sun_study.archicad import naming
from sun_study.archicad.connection import ArchicadConnection, ArchicadError
from sun_study.archicad.layout import (
    MM_PER_M,
    LayoutReport,
    LayoutSheet,
    _drawings_by_name,
    _walk,
    layout_from_views,
    layout_sheet,
)
from sun_study.archicad.read import GeoLocation, layer_names, zones
from sun_study.archicad.views import (
    ModelSource,
    StoreyView,
    _by_name_under,
    _check,
    ensure_layer_combination,
    ensure_view_folder,
    three_d_sources,
    views_for_sources,
)
from sun_study.core.solar import solar_position

__all__ = [
    "DOCUMENT_SCALE",
    "PER_SHEET",
    "TITLE_BLOCK_MM",
    "SunEye",
    "SunEyeSettings",
    "aim",
    "make_sun_eye_documents",
    "make_sun_eye_sheets",
    "make_sun_eye_views",
    "planned_renovation_filter",
    "sheet_cells_for",
    "sheet_groups",
    "sheet_positions_for",
    "sun_eye_layer_combination",
    "sun_eyes",
]

#: The scale the office's own sun view 3D Documents carry. A view of the 3D
#: window is at 1:1 and means nothing on a sheet; a document is a drawing.
#: 1:200 was tried on the way, when the drawings were still 59 mm stamps and
#: the scale looked like the reason; with each clipped to its cell the
#: practice asked for 1:500 back, which shows the street around the building.
DOCUMENT_SCALE = 500.0

#: How many hours share a sheet. Four puts 9am to noon on one and the
#: afternoon on the next, which reads as a morning and an afternoon.
PER_SHEET = 4

#: Between a drawing's clip frame and the edge of its cell, in millimetres.
#: Room for the drawing title Archicad puts under each.
CELL_GAP_MM = 20.0

#: The strip down the right of the practice's masters that the title block
#: occupies. Archicad reports a layout's margins and nothing about its master,
#: so a drawing tiled to the page edge runs under the title block; measured
#: at about 100 mm on the `DA B1 - VERTICAL` masters.
TITLE_BLOCK_MM = 100.0

#: The name a run gives its things, after the tool's prefix.
WORD = "Sun Eye"


@dataclass(frozen=True)
class SunEye:
    """Where the sun is at one instant, in both frames."""

    when: dt.datetime
    """Timezone-aware local time."""

    true_bearing_deg: float
    """Clockwise from true north: what an architect reads off a site plan."""

    project_bearing_deg: float
    """Clockwise from the project's +Y axis: what the add-on takes."""

    altitude_deg: float

    @property
    def label(self) -> str:
        """``21 Jun 09:00`` -- the instant, as a human names it."""
        return self.when.strftime("%d %b %H:%M").lstrip("0")

    @property
    def stamp(self) -> str:
        """``0900`` -- the instant, as a reference ID can carry it."""
        return self.when.strftime("%H%M")

    def sun_for_archicad(self) -> dict[str, Any]:
        """The date the add-on hands Archicad to compute its own sun from.

        A date rather than angles, on purpose: Archicad then works the sun out
        from the project's own georeferencing, and a disagreement with this
        tool's astronomy becomes visible on the drawing instead of being
        hidden by writing our answer into both sides.
        """
        return {
            "year": self.when.year,
            "month": self.when.month,
            "day": self.when.day,
            "hour": self.when.hour,
            "minute": self.when.minute,
            "second": 0,
            "summerTime": bool(self.when.dst()),
        }


def sun_eyes(
    geo: GeoLocation,
    *,
    date: dt.date,
    hours: Sequence[int],
    timezone: str,
) -> list[SunEye]:
    """The sun at each whole hour, in the project's frame as well as true.

    Hours when the sun is below the horizon are left out rather than drawn:
    a sun eye view from under the ground is not a diagram of anything.
    """
    zone = ZoneInfo(timezone)
    times = [dt.datetime.combine(date, dt.time(hour), tzinfo=zone) for hour in hours]
    if not times:
        return []
    position = solar_position(times, geo.latitude_deg, geo.longitude_deg)

    # Archicad reports north as the angle of its +Y axis; a bearing is
    # measured from that axis. Same arithmetic as the shadow drawings' frame
    # turn, kept in one line so the two cannot drift apart.
    project_plus_y = (270.0 + math.degrees(geo.north_radians)) % 360.0

    found: list[SunEye] = []
    for when, bearing, altitude in zip(
        times, position.azimuth_deg, position.elevation_deg, strict=True
    ):
        if altitude <= 0.0:
            continue
        found.append(
            SunEye(
                when=when,
                true_bearing_deg=float(bearing) % 360.0,
                project_bearing_deg=(float(bearing) - project_plus_y) % 360.0,
                altitude_deg=float(altitude),
            )
        )
    return found


@dataclass(frozen=True)
class SunEyeSettings:
    """What a sun eye view is pinned to, beyond its projection.

    Read off the office's own Solar Penetration Diagrams and corrected by the
    practice. The override is the one that paints the glazing; the filter is
    the project's planned state; the layer combination hides the zones, which
    are bodies in 3D and would otherwise sit inside the glazing the diagram is
    meant to show through.
    """

    layer_combination: str
    graphic_override: str = "Sun Eye Views"
    renovation_filter_guid: str | None = None
    model_view_options: str | None = "DA General Arrangement"
    pen_set: str | None = None
    d3_style: str | None = "OpenGL Shading with Contours with Shadows"
    document_scale: float = DOCUMENT_SCALE

    def for_view(self, *, of_document: bool) -> dict[str, Any]:
        """Tapir's ``viewSettings`` for a view of the 3D window or of a document.

        A 3D style belongs to a view of the 3D window and not to a document,
        which has its own drawing settings; the office's documents carry none
        and Tapir would refuse one.
        """
        settings: dict[str, Any] = {
            "layerCombination": self.layer_combination,
            "graphicOverrideCombination": self.graphic_override,
            "rotation": 0,
            "structureDisplay": "EntireStructure",
        }
        if self.renovation_filter_guid:
            settings["renovationFilterGuid"] = {"guid": self.renovation_filter_guid}
        if self.model_view_options:
            settings["modelViewOptions"] = self.model_view_options
        if self.pen_set:
            settings["penSetName"] = self.pen_set
        if of_document:
            settings["drawingScale"] = int(self.document_scale)
        else:
            settings["drawingScale"] = 1
            if self.d3_style:
                settings["d3styleName"] = self.d3_style
        return settings


def sun_eye_layer_combination(
    connection: ArchicadConnection,
    *,
    base: str,
    name: str | None = None,
) -> tuple[str, list[str]]:
    """``base`` with every layer that carries a zone hidden.

    Measured from the zones rather than listed by name: on the reference
    project thirteen layers carry zones, and only four of them are called
    ``Zone``. Returns the combination's name and the layers it hid.
    """
    names = layer_names(connection)
    counted = collections.Counter(
        names.get(zone.layer_index, "")
        for zone in zones(connection)
        if zone.layer_index is not None
    )
    hidden = sorted(layer for layer in counted if layer)
    combination = ensure_layer_combination(
        connection, name or naming.named(f"{WORD} Views"), show=[], hide=hidden, base=base
    )
    return combination, hidden


def planned_renovation_filter(connection: ArchicadConnection) -> str | None:
    """The renovation filter most of the project's views carry.

    Tapir can carry a filter's GUID onto a view but cannot name one, so the
    planned state is found by looking at what the project already shows: on
    the reference project 749 of 1,043 views share one filter, and it is the
    one the office's own solar penetration documents use. ``None`` when the
    View Map is empty.
    """
    response = connection.run_tapir("GetNavigatorItemTree", {"navigatorMapId": "PublicViewMap"})
    root = response.get("navigatorItemTree") if isinstance(response, dict) else None
    if not isinstance(root, dict):
        return None

    identifiers: list[dict[str, Any]] = []

    def walk(node: dict[str, Any], depth: int = 0) -> None:
        if depth > 32:
            return
        if node.get("type") not in (None, "FolderItem") and node.get("navigatorItemId"):
            identifiers.append(node["navigatorItemId"])
        for wrapper in node.get("children") or []:
            child = (wrapper or {}).get("navigatorItem") if isinstance(wrapper, dict) else None
            walk(child if isinstance(child, dict) else wrapper, depth + 1)

    walk(root.get("rootItem", root))

    counted: collections.Counter[str] = collections.Counter()
    for start in range(0, len(identifiers), 200):
        chunk = identifiers[start : start + 200]
        settings = connection.run_tapir(
            "GetViewSettings", {"navigatorItemIds": [{"navigatorItemId": i} for i in chunk]}
        )
        for entry in settings.get("viewSettings", []) if isinstance(settings, dict) else []:
            guid = ((entry or {}).get("renovationFilterGuid") or {}).get("guid")
            if guid:
                counted[str(guid)] += 1
    return counted.most_common(1)[0][0] if counted else None


def aim(connection: ArchicadConnection, eye: SunEye) -> None:
    """Point the 3D window along the sun, and set its sun to the same instant."""
    response = connection.run_loriini(
        "SetProjection",
        {
            "viewAzimuth": eye.project_bearing_deg,
            "viewAltitude": eye.altitude_deg,
            "sun": eye.sun_for_archicad(),
        },
    )
    if not isinstance(response, dict) or not response.get("success"):
        raise ArchicadError(f"SetProjection for {eye.label} answered {response!r}")


def _view_name(eye: SunEye, *, of_document: bool) -> str:
    kind = f"{WORD} Document" if of_document else WORD
    return naming.named(f"{kind} {eye.label}")


def make_sun_eye_views(
    connection: ArchicadConnection,
    eyes: Sequence[SunEye],
    *,
    settings: SunEyeSettings,
    folder: str | None = None,
) -> list[tuple[SunEye, StoreyView, bool]]:
    """One view of the 3D window per instant, aimed and pinned.

    Aim, then save, one instant at a time: the view takes the projection the
    window has at the moment it is made. Returns each view with whether it was
    already there, because an existing view keeps the aim it was saved with.
    """
    if not eyes:
        return []
    home = folder or naming.named(f"{WORD} Views")
    live = next((s for s in three_d_sources(connection) if s.kind == "AxonometryItem"), None)
    if live is None:
        raise ArchicadError("The Project Map has no 3D window item to save a view of.")

    already = _by_name_under(connection, "PublicViewMap", home)
    parent = ensure_view_folder(connection, home)

    made: list[tuple[SunEye, StoreyView, bool]] = []
    for eye in eyes:
        name = _view_name(eye, of_document=False)
        reused = name in already
        if not reused:
            aim(connection, eye)
            response = connection.run_tapir(
                "CreateViewsInViewMap",
                {
                    "viewsData": [
                        {
                            "navigatorItemId": {"guid": live.identifier},
                            "name": name,
                            "parentNavigatorItemId": {"guid": parent},
                        }
                    ]
                },
            )
            created = _check(response, "CreateViewsInViewMap")
            identifier = str((created[0].get("navigatorItemId") or {}).get("guid", ""))
            if not identifier:
                raise ArchicadError(f"CreateViewsInViewMap returned no id for {name!r}")
            already[name] = identifier
        made.append((eye, StoreyView(0, name, already[name]), reused))

    _check(
        connection.run_tapir(
            "SetViewSettings",
            {
                "navigatorItemIdsWithViewSettings": [
                    {
                        "navigatorItemId": {"guid": view.navigator_id},
                        "viewSettings": settings.for_view(of_document=False),
                    }
                    for _, view, _ in made
                ]
            },
        ),
        "SetViewSettings",
    )
    return made


def make_sun_eye_documents(
    connection: ArchicadConnection,
    eyes: Sequence[SunEye],
    *,
    settings: SunEyeSettings,
    folder: str | None = None,
) -> list[tuple[SunEye, StoreyView, bool]]:
    """One 3D Document per instant, each with its own projection, and a view of each.

    The document is what goes on a sheet: a drawing with its own settings,
    at a scale, rather than the live window. A document that already exists
    under the run's name is kept, since nothing can re-aim or delete it.
    """
    if not eyes:
        return []
    home = folder or naming.named(f"{WORD} Documents")
    existing = {s.name: s for s in three_d_sources(connection) if s.kind == "DocumentFrom3DItem"}

    sources: list[tuple[ModelSource, str]] = []
    reused: list[bool] = []
    for eye in eyes:
        name = naming.named(f"{WORD} {eye.label}")
        if name in existing:
            sources.append((existing[name], _view_name(eye, of_document=True)))
            reused.append(True)
            continue
        response = connection.run_loriini(
            "CreateDocumentFrom3D",
            {
                "name": name,
                "referenceId": f"SE{eye.stamp}",
                "viewAzimuth": eye.project_bearing_deg,
                "viewAltitude": eye.altitude_deg,
                "sun": eye.sun_for_archicad(),
            },
        )
        identifier = (
            str(((response or {}).get("databaseId") or {}).get("guid", ""))
            if isinstance(response, dict)
            else ""
        )
        if not identifier:
            raise ArchicadError(f"CreateDocumentFrom3D for {eye.label} answered {response!r}")
        reused.append(False)
        sources.append((_document_item(connection, name), _view_name(eye, of_document=True)))

    views = views_for_sources(
        connection,
        sources,
        combination=settings.layer_combination,
        folder=home,
        drawing_scale=settings.document_scale,
    )
    _check(
        connection.run_tapir(
            "SetViewSettings",
            {
                "navigatorItemIdsWithViewSettings": [
                    {
                        "navigatorItemId": {"guid": view.navigator_id},
                        "viewSettings": settings.for_view(of_document=True),
                    }
                    for view in views
                ]
            },
        ),
        "SetViewSettings",
    )
    return list(zip(eyes, views, reused, strict=True))


def sheet_groups(
    made: Sequence[tuple[SunEye, StoreyView, bool]],
    *,
    per_sheet: int = PER_SHEET,
    stem: str | None = None,
) -> list[tuple[str, list[tuple[str, str]]]]:
    """The documents split across sheets, each sheet named by the hours on it.

    ``(layout name, [(navigator id, drawing name), ...])`` per sheet, in the
    shape ``layout_from_views`` takes. Seven documents in fours is a morning
    sheet and an afternoon sheet -- ``09:00-12:00`` and ``13:00-15:00`` --
    rather than seven stamps on one.
    """
    if per_sheet <= 0:
        raise ValueError(f"per_sheet must be positive, not {per_sheet}")
    stem = stem or naming.named(f"{WORD} Views")
    groups: list[tuple[str, list[tuple[str, str]]]] = []
    for start in range(0, len(made), per_sheet):
        chunk = made[start : start + per_sheet]
        first, last = chunk[0][0].when, chunk[-1][0].when
        name = f"{stem} {first:%H:%M}-{last:%H:%M}" if len(made) > per_sheet else stem
        groups.append((name, [(view.navigator_id, view.name) for _, view, _ in chunk]))
    return groups


def sheet_cells_for(
    sheet: LayoutSheet,
    count: int,
    *,
    title_block_mm: float = TITLE_BLOCK_MM,
    gap_mm: float = CELL_GAP_MM,
) -> list[tuple[float, float, float, float]]:
    """Equal cells on the page, as ``(xMin, yMin, xMax, yMax)`` in metres.

    The page less the title block strip is cut into equal cells, as many
    across as wastes the fewest cells and then keeps them nearest square:
    four drawings are two by two, not three and one. Layout coordinates run
    upward from the bottom-left corner, so the first cell is put at the top
    by counting down from the page height rather than up from zero. Each cell
    is shrunk by ``gap_mm`` on every side, which is where the drawing's title
    goes.
    """
    if count <= 0:
        return []
    left, top, width, height = sheet.usable
    width = max(width - title_block_mm, 1.0)

    def cost(columns: int) -> tuple[int, float]:
        rows = -(-count // columns)
        return (columns * rows - count, abs(math.log((width / columns) / (height / rows))))

    columns = min(range(1, count + 1), key=cost)
    rows = -(-count // columns)
    cell_w, cell_h = width / columns, height / rows
    cells: list[tuple[float, float, float, float]] = []
    for index in range(count):
        x0 = left + (index % columns) * cell_w
        y1 = top + height - (index // columns) * cell_h
        cells.append(
            (
                (x0 + gap_mm) / MM_PER_M,
                (y1 - cell_h + gap_mm) / MM_PER_M,
                (x0 + cell_w - gap_mm) / MM_PER_M,
                (y1 - gap_mm) / MM_PER_M,
            )
        )
    return cells


def sheet_positions_for(
    sheet: LayoutSheet, count: int, *, title_block_mm: float = TITLE_BLOCK_MM
) -> list[tuple[float, float]]:
    """The centres of ``sheet_cells_for``, in metres."""
    cells = sheet_cells_for(sheet, count, title_block_mm=title_block_mm, gap_mm=0.0)
    return [((x0 + x1) / 2.0, (y0 + y1) / 2.0) for x0, y0, x1, y1 in cells]


def arrange_drawings(
    connection: ArchicadConnection,
    layout_database_id: str,
    placements: Sequence[tuple[str, tuple[float, float, float, float]]],
) -> int:
    """Put each drawing's origin at the centre of a cell and clip it to the cell.

    ``placements`` is ``(drawing element guid, (xMin, yMin, xMax, yMax))`` in
    metres on the layout. Returns how many the add-on arranged.

    The origin rather than the centre of the content, and a clip rather than a
    free frame, because of what a drawing of a 3D Document is: the whole site
    model, projected. Freed, it swamps the sheet; its origin is the projected
    model origin, which on a site modelled around it is the building. The
    placeholder frame Tapir leaves is centred there too, only 59 mm wide --
    "almost there, just expand it", as the practice put it.
    """
    if not placements:
        return 0
    response = connection.run_loriini(
        "ArrangeDrawings",
        {
            "layoutDatabaseId": {"guid": layout_database_id},
            "drawings": [
                {
                    "guid": guid,
                    "x": (x0 + x1) / 2.0,
                    "y": (y0 + y1) / 2.0,
                    "frame": {"xMin": x0, "yMin": y0, "xMax": x1, "yMax": y1},
                }
                for guid, (x0, y0, x1, y1) in placements
            ],
            "anchor": "origin",
            "frameRelativeToOrigin": False,
            "autoUpdate": True,
        },
    )
    if not isinstance(response, dict) or not response.get("success"):
        raise ArchicadError(f"ArrangeDrawings answered {response!r}")
    failures = response.get("failures") or []
    if failures:
        raise ArchicadError(
            f"ArrangeDrawings could not change {len(failures)} drawings: {failures}"
        )
    return int(response.get("arranged", 0))


def make_sun_eye_sheets(
    connection: ArchicadConnection,
    made: Sequence[tuple[SunEye, StoreyView, bool]],
    *,
    scale: float,
    per_sheet: int = PER_SHEET,
    master_layout: str | None = None,
    title_block_mm: float = TITLE_BLOCK_MM,
) -> tuple[list[LayoutReport], list[str]]:
    """The documents on sheets, and any earlier sun eye sheet removed.

    A layout *can* be deleted, unlike a view, and a sheet whose name no longer
    matches its hours -- the single sheet a first run made, say -- is worse
    than none: it stays in the Layout Book beside the current ones, looking
    equally current. So every sun eye sheet not about to be remade is removed
    first, and the names of those removed are returned for the run to say.

    Placing is Tapir's and arranging is the add-on's. ``CreateDrawings`` puts
    each drawing down clipped to a placeholder frame and anchored by a corner,
    and Tapir can change nothing about it afterwards; ``ArrangeDrawings`` then
    frees the frame, anchors each by its centre and puts it at the centre of
    its cell. Without the add-on the sheet is still made, and said to be the
    rough one.
    """
    groups = sheet_groups(made, per_sheet=per_sheet)
    wanted = {name for name, _ in groups}
    stem = naming.named(f"{WORD} Views")

    removed: list[str] = []
    response = connection.run_tapir("GetNavigatorItemTree", {"navigatorMapId": "LayoutBook"})
    root = response.get("navigatorItemTree") if isinstance(response, dict) else None
    if isinstance(root, dict):
        stale = [
            item
            for item in _walk(root)
            if item.kind == "LayoutItem" and item.name.startswith(stem) and item.name not in wanted
        ]
        if stale:
            connection.run_tapir(
                "DeleteNavigatorItems",
                {
                    "navigatorItemIds": [
                        {"navigatorItemId": {"guid": item.identifier}} for item in stale
                    ]
                },
            )
            removed = [item.name for item in stale]

    reports: list[LayoutReport] = []
    for name, views in groups:
        report = layout_from_views(
            connection, views, layout_name=name, scale=scale, master_layout=master_layout
        )
        reports.append(report)
        if not report.database_id:
            continue
        sheet, _ = layout_sheet(connection, report.database_id)
        placed = _drawings_by_name(connection, report.database_id)
        cells = sheet_cells_for(sheet, len(views), title_block_mm=title_block_mm)
        placements = [
            (str(placed[drawing_name]["elementId"]["guid"]), cell)
            for (_, drawing_name), cell in zip(views, cells, strict=True)
            if drawing_name in placed
        ]
        arrange_drawings(connection, report.database_id, placements)
    return reports, removed


def _document_item(connection: ArchicadConnection, name: str) -> ModelSource:
    """The Project Map item of a document just created, found by its name.

    The add-on answers with the database's own id and the navigator carries a
    different one, so the item is looked up rather than derived.
    """
    for source in three_d_sources(connection):
        if source.kind == "DocumentFrom3DItem" and source.name == name:
            return source
    raise ArchicadError(f"The 3D Document {name!r} was created but is not in the Project Map.")
