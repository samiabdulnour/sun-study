"""Straightening and tiling the Drawings after they are on the sheet.

Why this is a second pass
------------------------
Neither the angle nor the true size of a Drawing can be chosen when it is
created. ``CreateDrawings`` takes a position, a name and a magnification, and
nothing else: the angle comes from the Drawing tool's own default -- on this
project 279.9 degrees, which is the project's north -- and the size follows
from the view's scale and extent, so it is only knowable once the drawing
exists.

Both are fixable afterwards, with ``RotateElements`` and ``MoveElements``. The
obstacle was reading the drawing back at all: a layout created in the same
session answers ``GetDetailsOfElements`` with a per-element error, which is
what made the first attempt at this impossible, and what took Archicad down
with it.

**Saving the project is what materialises the layout.** After ``SaveProject``
the same read answers normally. So the order is: create, save, measure,
straighten, measure again, move. Twice-measured because rotating changes the
bounding box, and the second measurement is the one the tiling needs.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sun_study.archicad.connection import ArchicadConnection, ArchicadError
from sun_study.archicad.layout import LayoutSheet, Tiling, tile_positions

__all__ = [
    "DrawingPlacement",
    "SheetReport",
    "TableRow",
    "draw_table",
    "measure_drawings",
    "straighten_and_tile",
]


@dataclass(frozen=True)
class DrawingPlacement:
    """One Drawing as it currently sits on a sheet, in metres."""

    element: dict[str, Any]
    angle_rad: float
    x_min: float
    y_min: float
    x_max: float
    y_max: float

    ratio: float = 1.0
    """The Drawing's magnification, which is how big it is *on the page*.

    Its scale cannot be changed from here -- ``SetDetailsOfElements`` accepts
    a ``drawingScale``, answers ``{"success": true}`` and leaves it exactly as
    it was, which is D43 again. ``ratio`` does change, and is therefore the
    only handle there is on a drawing that will not fit its sheet.
    """

    @property
    def width(self) -> float:
        return self.x_max - self.x_min

    @property
    def height(self) -> float:
        return self.y_max - self.y_min

    @property
    def centre(self) -> tuple[float, float]:
        return ((self.x_min + self.x_max) / 2.0, (self.y_min + self.y_max) / 2.0)


def _frame_angle(details: Mapping[str, Any]) -> float:
    """How far the Drawing's frame is off upright, in radians, within +-45 deg.

    Taken from the ``clipPolygon`` -- the frame's own corners -- and **not**
    from the ``angle`` field beside it, which is the whole point. ``angle`` is
    writable and answers with whatever was last written to it: a run that set
    it to zero read zero back and reported six drawings straightened, while
    every frame on the sheet still stood at 279.9 degrees. The corners are the
    only account of where the drawing actually is.

    Reduced modulo a quarter turn, because a rectangle at 90 degrees is
    upright, and then signed so that rotating by its negative is the shorter
    way round.
    """
    polygon = details.get("clipPolygon")
    if not isinstance(polygon, list) or len(polygon) < 3:
        return float(details.get("angle", 0.0) or 0.0)
    try:
        first, second = polygon[0], polygon[1]
        edge = math.atan2(
            float(second["y"]) - float(first["y"]), float(second["x"]) - float(first["x"])
        )
    except (KeyError, TypeError, ValueError):
        return float(details.get("angle", 0.0) or 0.0)
    quarter = math.pi / 2.0
    off = edge % quarter
    return off - quarter if off > quarter / 2.0 else off


def _turn_by(angle_rad: float, origin: tuple[float, float]) -> dict[str, Any]:
    """A rotation of ``angle_rad`` about ``origin``, as two points on its arc.

    ``RotateElements`` takes no angle. It takes a centre and the two ends of an
    arc, and derives the angle from them -- so the caller has to build the arc.
    A unit radius keeps the numbers well away from the precision at which two
    points would read as the same one.

    Worth the arithmetic: this is what actually turns a Drawing. Writing
    ``angle`` through ``SetDetailsOfElements`` is shorter, reports success and
    reads back, and leaves the drawing exactly where it was.
    """
    x, y = origin
    return {
        "origin": {"x": x, "y": y},
        "beginPoint": {"x": x + 1.0, "y": y},
        "endPoint": {"x": x + math.cos(angle_rad), "y": y + math.sin(angle_rad)},
    }


def measure_drawings(
    connection: ArchicadConnection, layout_database_id: str
) -> list[DrawingPlacement]:
    """Every Drawing on one layout, with its angle and bounds.

    Requires the project to have been saved since the layout was made: an
    unsaved layout answers with a per-element error instead.
    """
    connection.run_tapir(
        "ChangeWindow", {"databaseId": {"guid": layout_database_id}, "windowType": "Layout"}
    )
    found = connection.run_tapir("GetElementsByType", {"elementType": "Drawing"})
    elements = found.get("elements") if isinstance(found, dict) else None
    if not isinstance(elements, list) or not elements:
        return []

    response = connection.run_tapir("GetDetailsOfElements", {"elements": elements})
    rows = response.get("detailsOfElements") if isinstance(response, dict) else None
    if not isinstance(rows, list) or len(rows) != len(elements):
        raise ArchicadError(
            "The layout would not describe its drawings. It has probably not "
            "been saved since it was created, which is what makes a layout "
            "readable at all."
        )

    placements: list[DrawingPlacement] = []
    for element, row in zip(elements, rows, strict=True):
        details = (row or {}).get("details") or {}
        bounds = details.get("bounds") or {}
        try:
            placements.append(
                DrawingPlacement(
                    element=element,
                    angle_rad=_frame_angle(details),
                    x_min=float(bounds["xMin"]),
                    y_min=float(bounds["yMin"]),
                    x_max=float(bounds["xMax"]),
                    y_max=float(bounds["yMax"]),
                    ratio=float(details.get("ratio", 1.0)) or 1.0,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return placements


@dataclass(frozen=True)
class SheetReport:
    """What the second pass over a sheet did."""

    straightened: int
    moved: int
    shrunk_to: float = 1.0
    """The magnification the drawings had to be reduced to, 1.0 for none.

    Always reported. A drawing brought down to 47% of the size its scale asks
    for is no longer at that scale, and a run that did that quietly would be
    handing somebody a sheet whose title block lies about it.
    """

    def __iter__(self) -> Iterator[int]:
        """``turned, moved = straighten_and_tile(...)`` still works."""
        return iter((self.straightened, self.moved))

    def describe(self) -> str:
        said = f"straightened {self.straightened}, tiled {self.moved}"
        if self.shrunk_to < 1.0:
            per_cent = f"{self.shrunk_to * 100:.1f}".rstrip("0").rstrip(".")
            said += (
                f", and set every drawing to {per_cent}% to fit the page -- so they "
                f"are no longer at the scale the run asked for"
            )
        if self.shrunk_to < 0.25:
            said += (
                ". A quarter or less is not a page too small, it is the wrong "
                "scale for this sheet: raise the scale or use a bigger master"
            )
        return said


def straighten_and_tile(
    connection: ArchicadConnection,
    layout_database_id: str,
    sheet: LayoutSheet,
    *,
    gap_m: float = 0.012,
    tolerance_rad: float = 1e-4,
) -> SheetReport:
    """Turn every Drawing upright, size it to the page, and lay them out.

    The sheet is described in millimetres and the drawings live in metres,
    which is the unit everything here works in.

    A Drawing's angle is one of the few things ``SetDetailsOfElements`` will
    change directly, through ``DrawingSettings``, so the angle is *set* rather
    than rotated away. ``RotateElements`` did work and had to be handed a
    centre and two points on an arc to imply the angle, which is a lot of
    arithmetic to express "north up".

    Fitting is the other half, and it is not optional: a drawing made from a
    3D view came out 1,429 mm wide on an 841 mm page, and no arrangement of
    one drawing makes that fit. The shrink is applied to **every** drawing on
    the sheet by the same factor, even the ones that would have fitted. Three
    diagrams of the same building at three times of day are read against each
    other, and two of them at one size beside a third at another is a picture
    of nothing.
    """
    placements = measure_drawings(connection, layout_database_id)
    if not placements:
        return SheetReport(0, 0)

    crooked = [p for p in placements if abs(p.angle_rad) > tolerance_rad]
    if crooked:
        connection.run_tapir(
            "RotateElements",
            {
                "elementsWithRotations": [
                    {
                        "elementId": placement.element["elementId"],
                        "rotation": _turn_by(-placement.angle_rad, placement.centre),
                    }
                    for placement in crooked
                ]
            },
        )
        # Rotating moves the bounding box, and the tiling needs the new one.
        # Also the only proof it happened: the response says nothing.
        placements = measure_drawings(connection, layout_database_id)
        still = [p for p in placements if abs(p.angle_rad) > tolerance_rad]
        if still:
            raise ArchicadError(
                f"{len(still)} of {len(crooked)} drawings would not straighten. "
                f"They are on a locked layout, or the layout has not been saved "
                f"since it was made."
            )

    magnification, tiling = _fit_to_page(connection, placements, sheet, gap_m)

    # The arrangement comes from the same tiling that chose the magnification,
    # and the sheet is deliberately not re-measured to check it. A drawing's
    # bounds do not follow its magnification until Archicad regenerates it, so
    # a read here would answer with the size from before the change and put
    # the grid back where it was.
    moves = _moves_onto(placements, tiling.positions)
    if moves:
        connection.run_tapir("MoveElements", {"elementsWithMoveVectors": moves})
    return SheetReport(len(crooked), len(moves), magnification or 1.0)


def _fit_to_page(
    connection: ArchicadConnection,
    placements: Sequence[DrawingPlacement],
    sheet: LayoutSheet,
    gap_m: float,
) -> tuple[float | None, Tiling]:
    """Set every Drawing to the one magnification that fits the sheet.

    Returns what they were set to -- ``None`` when nothing was changed -- and
    the arrangement that decision was made from, which is the arrangement the
    drawings then have to be moved into. The two come back together because
    they are one answer: the magnification is what makes the grid fit, and the
    grid is what the magnification was chosen for.

    **A Drawing's bounds do not follow its magnification**, and everything
    here is shaped by that. Setting a drawing to 46.9% stores 46.9%, and it
    reads back, while the bounds go on reporting the size it had before --
    until Archicad regenerates the drawing, which happens when somebody opens
    the layout. Changing it and reading it back therefore proves nothing, and
    a re-read after the change is worse than none: it is D40 with the answer
    arriving from a database that has not caught up.

    What *is* dependable is that a drawing's bounds and its magnification
    agree until this function touches them, because Archicad drew them
    together. So the size at full magnification is bounds over magnification,
    and the answer is written once, from that, without reading back.

    Three states, one rule each. At full size, fit it. Above full size it is
    the old fault -- the scale denominator written into the magnification
    field, eighteen storey plans standing at 200x and 187 metres across -- and
    bounds over magnification is exactly the repair. Below full size it has
    been fitted already, and is left alone: nothing readable distinguishes a
    drawing that has regenerated from one that has not, and shrinking on that
    evidence went 46.9%, 22.1%, and on down by half every run.

    The cost of the last rule is that a sheet does not re-fit itself when the
    building outgrows it. The run says what magnification it left, which is
    the handle: delete the drawings and let the next run place them fresh.

    Uniform across the sheet, including drawings that would have fitted on
    their own. Three diagrams of one building at three times of day are read
    against each other, and two at one size beside a third at another is a
    picture of nothing.
    """
    if any(placement.ratio < 1.0 for placement in placements):
        # Already fitted. The bounds may or may not have caught up with it, so
        # they are used as they are: whichever they report, they are the same
        # multiple of the truth for every drawing, and a grid is a set of
        # proportions.
        return None, tile_positions(
            sheet,
            len(placements),
            (max(p.width for p in placements), max(p.height for p in placements)),
            gap_m,
        )

    tiling = tile_positions(
        sheet,
        len(placements),
        (
            max(p.width / p.ratio for p in placements),
            max(p.height / p.ratio for p in placements),
        ),
        gap_m,
    )
    if tiling.fits and all(placement.ratio == 1.0 for placement in placements):
        return None, tiling

    connection.run_tapir(
        "SetDetailsOfElements",
        {
            "elementsWithDetails": [
                {
                    "elementId": placement.element["elementId"],
                    "details": {"typeSpecificDetails": {"ratio": tiling.fit}},
                }
                for placement in placements
            ]
        },
    )
    return tiling.fit, tiling


def _moves_onto(
    placements: Sequence[DrawingPlacement], positions: Sequence[tuple[float, float]]
) -> list[dict[str, Any]]:
    """Move vectors taking each drawing from where it is to where it belongs.

    The positions are ``tile_positions``', shared with the placement pass, so
    that where a drawing is first put and where it is later moved cannot
    disagree.
    """
    if not placements:
        return []
    moves: list[dict[str, Any]] = []
    for placement, (target_x, target_y) in zip(placements, positions, strict=True):
        centre_x, centre_y = placement.centre
        moves.append(
            {
                "elementId": placement.element["elementId"],
                "moveVector": {
                    "x": target_x - centre_x,
                    "y": target_y - centre_y,
                    "z": 0.0,
                },
            }
        )
    return moves


@dataclass(frozen=True)
class TableRow:
    """One line of the figures table: a swatch, a label and its numbers."""

    label: str
    area_m2: float
    share: float
    fill_pen: int | None = None
    background_pen: int = 19


def draw_table(
    connection: ArchicadConnection,
    layout_database_id: str,
    *,
    title: str,
    rows: Sequence[TableRow],
    height_mm: float = 2.2,
    swatch_m: float = 0.006,
    step_m: float = 0.009,
) -> int:
    """Put the figures table on the sheet itself. Returns the rows drawn.

    On the **layout**, not on the plan. Drawn into the model it has to be
    repeated on every storey, so a six-storey study carries six copies of one
    table and each one sits somewhere in the building; on the sheet there is
    one, beside the drawings, where a reader looks for it.

    This works only because the project has been saved since the layout was
    made -- an unsaved layout cannot be activated, and creation follows the
    current database. Everything is in metres, as on any layout.
    """
    if not rows:
        return 0

    connection.run_tapir(
        "ChangeWindow", {"databaseId": {"guid": layout_database_id}, "windowType": "Layout"}
    )
    # The sheet is regenerated with its layout, so anything here is this
    # tool's own from an earlier pass over the same sheet.
    for kind in ("Text", "Hatch"):
        found = connection.run_tapir("GetElementsByType", {"elementType": kind})
        elements = found.get("elements") if isinstance(found, dict) else None
        if isinstance(elements, list) and elements:
            connection.run_tapir("DeleteElements", {"elements": elements})

    placements = measure_drawings(connection, layout_database_id)
    if placements:
        left = min(p.x_min for p in placements)
        top = min(p.y_min for p in placements) - step_m * 2
    else:
        left, top = 0.02, 0.20

    fills: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = [
        {
            "coordinate": {"x": left, "y": top + step_m * 1.6, "z": 0.0},
            "text": title,
            "height": height_mm * 1.3,
            "justification": "Left",
        }
    ]
    for index, row in enumerate(rows):
        bottom = top - index * step_m
        if row.fill_pen is not None:
            fills.append(
                {
                    "coordinates": [
                        {"x": left, "y": bottom},
                        {"x": left + swatch_m, "y": bottom},
                        {"x": left + swatch_m, "y": bottom + swatch_m},
                        {"x": left, "y": bottom + swatch_m},
                    ],
                    "fillPenIndex": row.fill_pen,
                    "fillBackgroundPenIndex": row.background_pen,
                    "contourPenIndex": row.fill_pen,
                    "showArea": False,
                }
            )
        texts.append(
            {
                "coordinate": {"x": left + swatch_m * 1.6, "y": bottom, "z": 0.0},
                "text": f"{row.label:<10} {row.area_m2:9.1f} m²   {row.share:6.1%}",
                "height": height_mm,
                "justification": "Left",
            }
        )

    if fills:
        connection.run_tapir("CreateHatches", {"hatchesData": fills})
    connection.run_tapir("CreateTexts", {"textsData": texts})
    return len(rows)
