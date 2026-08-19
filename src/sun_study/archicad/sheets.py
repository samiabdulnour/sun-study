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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sun_study.archicad.connection import ArchicadConnection, ArchicadError
from sun_study.archicad.layout import LayoutSheet

__all__ = [
    "DrawingPlacement",
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

    @property
    def width(self) -> float:
        return self.x_max - self.x_min

    @property
    def height(self) -> float:
        return self.y_max - self.y_min

    @property
    def centre(self) -> tuple[float, float]:
        return ((self.x_min + self.x_max) / 2.0, (self.y_min + self.y_max) / 2.0)


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
                    angle_rad=float(details.get("angle", 0.0)),
                    x_min=float(bounds["xMin"]),
                    y_min=float(bounds["yMin"]),
                    x_max=float(bounds["xMax"]),
                    y_max=float(bounds["yMax"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return placements


def straighten_and_tile(
    connection: ArchicadConnection,
    layout_database_id: str,
    sheet: LayoutSheet,
    *,
    gap_m: float = 0.012,
    tolerance_rad: float = 1e-4,
) -> tuple[int, int]:
    """Turn every Drawing upright and lay them out. ``(straightened, moved)``.

    The sheet is described in millimetres and the drawings live in metres,
    which is the unit everything here works in.
    """
    placements = measure_drawings(connection, layout_database_id)
    if not placements:
        return 0, 0

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
        placements = measure_drawings(connection, layout_database_id)

    moves = _tile(placements, sheet, gap_m)
    if moves:
        connection.run_tapir("MoveElements", {"elementsWithMoveVectors": moves})
    return len(crooked), len(moves)


def _turn_by(angle_rad: float, origin: tuple[float, float]) -> dict[str, Any]:
    """A rotation of ``angle_rad`` about ``origin``, as two points on its arc.

    ``RotateElements`` takes no angle. It takes a centre and the two ends of an
    arc, and derives the angle from them -- so the caller has to build the arc.
    A unit radius keeps the numbers well away from the precision at which two
    points would read as the same one.
    """
    x, y = origin
    return {
        "origin": {"x": x, "y": y},
        "beginPoint": {"x": x + 1.0, "y": y},
        "endPoint": {"x": x + math.cos(angle_rad), "y": y + math.sin(angle_rad)},
    }


def _tile(
    placements: Sequence[DrawingPlacement], sheet: LayoutSheet, gap_m: float
) -> list[dict[str, Any]]:
    """Move vectors putting the drawings in a centred grid on the sheet."""
    if not placements:
        return []
    widest = max(p.width for p in placements) + gap_m
    tallest = max(p.height for p in placements) + gap_m

    left_mm, top_mm, wide_mm, high_mm = sheet.usable
    x0, y0 = left_mm / 1000.0, top_mm / 1000.0
    usable_w, usable_h = wide_mm / 1000.0, high_mm / 1000.0

    columns = max(1, min(len(placements), int(usable_w // widest) or 1))
    rows = max(1, -(-len(placements) // columns))
    block_w, block_h = columns * widest, rows * tallest
    left = x0 + max(0.0, (usable_w - block_w) / 2.0)
    bottom = y0 + max(0.0, (usable_h - block_h) / 2.0)

    moves: list[dict[str, Any]] = []
    for index, placement in enumerate(placements):
        column, row = index % columns, index // columns
        target_x = left + (column + 0.5) * widest
        # Rows read downward from the top of the block.
        target_y = bottom + block_h - (row + 0.5) * tallest
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
