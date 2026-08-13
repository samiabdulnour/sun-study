"""Drawing the result onto the floor plan as coloured fills and a legend.

A number in a schedule is the record; a coloured plan is what gets looked at.
This draws the second from the first, natively, so the diagram is Archicad
elements on an Archicad layer rather than an image pasted onto a sheet -- it
prints at any scale, sits under the office pen table, and can be edited.

One fill per apartment, from the Zone's own outline, on the Zone's own storey,
coloured by the band its sunlight hours fall in. Plus a legend, because a
coloured plan with no key is a decoration.

Why pens and not colours
------------------------
``CreateHatches`` takes pen *indices*, not RGB. That is a constraint worth
welcoming: a practice runs a pen table (``00 FA Pens`` in the reference
office), and a diagram drawn from it stays consistent with every other drawing
on the sheet. A palette imported from an analysis tool would not. So the band
to pen mapping is configuration, it is echoed on every run, and the default is
a guess that is *meant* to be replaced.

Re-running replaces
-------------------
Everything is drawn on one dedicated layer and the previous run's contents are
deleted first. Without that, a second run silently doubles up: the new fills
land exactly on the old ones, the plan looks unchanged, and the stale colours
underneath are the ones that print if the top layer is ever hidden.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sun_study.archicad.connection import ArchicadConnection, ArchicadError
from sun_study.archicad.read import ArchicadZone
from sun_study.rules.assessment import BuildingAssessment

__all__ = [
    "DEFAULT_BANDS",
    "DEFAULT_LAYER_NAME",
    "BandStyle",
    "DrawReport",
    "band_for",
    "clear_layer",
    "draw_assessment",
    "ensure_layer",
]

#: Everything this tool draws goes here, and nothing else does. That is what
#: makes deleting the previous run safe, and what lets the whole diagram be
#: switched off in a layer combination.
DEFAULT_LAYER_NAME = "Sun Study.Results"

#: The add-on version ``CreateHatches`` arrived in. Higher than the rest of
#: this package needs, so drawing is gated separately rather than raising the
#: floor for people who only want numbers.
DRAWING_MINIMUM_TAPIR_VERSION = (1, 5, 7)


@dataclass(frozen=True)
class BandStyle:
    """One legend entry: a range of hours, a label, and how to draw it."""

    label: str
    upper_minutes: float
    """Exclusive upper bound. ``inf`` for the open-ended top band."""
    fill_pen: int
    background_pen: int = 19
    contour_pen: int = 1


#: A starting point, not an answer. Pen indices mean whatever the project's pen
#: table says they mean, so these are almost certainly wrong for any given
#: office and are meant to be overridden. The run prints the mapping it used.
DEFAULT_BANDS: tuple[BandStyle, ...] = (
    BandStyle("0 hrs", 1e-9, fill_pen=91),
    BandStyle("0-1 hrs", 60.0, fill_pen=92),
    BandStyle("1-2 hrs", 120.0, fill_pen=93),
    BandStyle("2-3 hrs", 180.0, fill_pen=94),
    BandStyle("3-4 hrs", 240.0, fill_pen=95),
    BandStyle("4-5 hrs", 300.0, fill_pen=96),
    BandStyle("5+ hrs", float("inf"), fill_pen=97),
)


@dataclass(frozen=True)
class DrawReport:
    """What was drawn, and what could not be."""

    fills_drawn: int
    fills_removed: int
    legend_items: int
    layer_index: int
    zones_without_outline: tuple[str, ...]
    zones_with_holes: tuple[str, ...]
    zones_with_arcs: tuple[str, ...]
    unmatched: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not (self.zones_without_outline or self.unmatched)

    def describe(self) -> str:
        lines = [
            f"drew {self.fills_drawn} apartment fills and a {self.legend_items} item "
            f"legend on layer index {self.layer_index}"
            + (
                f", replacing {self.fills_removed} from a previous run"
                if self.fills_removed
                else ""
            )
        ]
        if self.unmatched:
            lines.append(
                f"  {len(self.unmatched)} assessed apartments had no zone to draw: "
                + ", ".join(self.unmatched[:5])
            )
        if self.zones_without_outline:
            lines.append(
                f"  {len(self.zones_without_outline)} zones reported no outline and "
                f"were skipped: " + ", ".join(self.zones_without_outline[:5])
            )
        if self.zones_with_holes:
            lines.append(
                f"  {len(self.zones_with_holes)} zones have holes -- a lift core or "
                f"light well -- and were drawn solid over them: "
                + ", ".join(self.zones_with_holes[:5])
            )
        if self.zones_with_arcs:
            lines.append(
                f"  {len(self.zones_with_arcs)} zones have curved edges, drawn as "
                f"straight segments between their nodes"
            )
        return "\n".join(lines)


def band_for(minutes: float, bands: Sequence[BandStyle]) -> BandStyle:
    """The band a duration falls in.

    Bands are half-open and tested in order, so the boundary belongs to the
    band above it: exactly 120 minutes is "2-3 hrs", not "1-2 hrs". That
    matches how the ADG threshold reads -- *at least* two hours -- and
    disagreeing with it by one band at exactly the threshold is the worst
    possible place to disagree.
    """
    for band in bands:
        if minutes < band.upper_minutes:
            return band
    return bands[-1]


def ensure_layer(connection: ArchicadConnection, name: str) -> int:
    """Create the results layer if it is missing, and return its index.

    ``CreateHatches`` wants a numeric layer index while ``CreateLayers`` deals
    in names and attribute ids, so the layer list is read back either way.
    Existing layers are never overwritten: the layer may carry settings
    somebody chose, and this tool has no business resetting them.
    """
    existing = _layer_index(connection, name)
    if existing is not None:
        return existing

    connection.run_tapir(
        "CreateLayers",
        {"layerDataArray": [{"name": name}], "overwriteExisting": False},
    )
    created = _layer_index(connection, name)
    if created is None:
        raise ArchicadError(
            f"Created layer {name!r} but Archicad does not list it. Drawing "
            f"cannot continue without a layer index to place fills on."
        )
    return created


def _layer_index(connection: ArchicadConnection, name: str) -> int | None:
    response = connection.run_tapir("GetLayers")
    layers = response.get("layers") if isinstance(response, dict) else None
    if not isinstance(layers, list):
        raise ArchicadError(f"GetLayers returned no layer list: {response!r}")

    wanted = name.casefold()
    for layer in layers:
        attribute = (layer or {}).get("layerAttribute") if isinstance(layer, dict) else None
        if not isinstance(attribute, dict):
            attribute = layer if isinstance(layer, dict) else None
        if isinstance(attribute, dict) and str(attribute.get("name", "")).casefold() == wanted:
            index = attribute.get("index")
            if isinstance(index, (int, float)):
                return int(index)
    return None


def clear_layer(connection: ArchicadConnection, layer_index: int) -> int:
    """Delete every Hatch and Text on the results layer. Returns the count.

    Scoped to the two element types this module creates, so a person who put
    something else on the layer does not lose it without warning.
    """
    removed: list[dict[str, Any]] = []
    for element_type in ("Hatch", "Text"):
        found = connection.run_tapir("GetElementsByType", {"elementType": element_type})
        elements = found.get("elements") if isinstance(found, dict) else None
        if not elements:
            continue

        details = connection.run_tapir("GetDetailsOfElements", {"elements": elements})
        rows = details.get("detailsOfElements") if isinstance(details, dict) else None
        if not isinstance(rows, list) or len(rows) != len(elements):
            raise ArchicadError(
                f"GetDetailsOfElements returned a list of a different length to the "
                f"{element_type} list, so the previous run's elements cannot be "
                f"identified safely and will not be deleted."
            )
        removed.extend(
            element
            for element, row in zip(elements, rows, strict=True)
            if isinstance(row, dict) and row.get("layerIndex") == layer_index
        )

    if removed:
        connection.run_tapir("DeleteElements", {"elements": removed})
    return len(removed)


def _fill_for(zone: ArchicadZone, band: BandStyle, layer_index: int) -> dict[str, Any]:
    data: dict[str, Any] = {
        "coordinates": [{"x": x, "y": y} for x, y in zone.outline],
        "layerIndex": layer_index,
        "fillPenIndex": band.fill_pen,
        "fillBackgroundPenIndex": band.background_pen,
        "contourPenIndex": band.contour_pen,
    }
    if zone.storey_index is not None:
        data["floorInd"] = zone.storey_index
    return data


def _legend(
    bands: Sequence[BandStyle],
    origin: tuple[float, float],
    layer_index: int,
    *,
    swatch_m: float = 1.0,
    spacing_m: float = 1.5,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Swatches and labels for the key, top band first.

    Drawn in model space beside the plan, in metres, so it scales with the
    drawing rather than floating at a fixed paper size.
    """
    fills: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []
    x, top = origin

    for row, band in enumerate(reversed(bands)):
        y = top - row * spacing_m
        fills.append(
            {
                "coordinates": [
                    {"x": x, "y": y},
                    {"x": x + swatch_m, "y": y},
                    {"x": x + swatch_m, "y": y + swatch_m},
                    {"x": x, "y": y + swatch_m},
                ],
                "layerIndex": layer_index,
                "fillPenIndex": band.fill_pen,
                "fillBackgroundPenIndex": band.background_pen,
                "contourPenIndex": band.contour_pen,
            }
        )
        texts.append(
            {
                "coordinate": {"x": x + swatch_m * 1.4, "y": y, "z": 0.0},
                "text": band.label,
                "justification": "Left",
            }
        )
    return fills, texts


def draw_assessment(
    connection: ArchicadConnection,
    assessment: BuildingAssessment,
    zones: Sequence[ArchicadZone],
    *,
    zone_by_apartment: dict[str, str],
    bands: Sequence[BandStyle] = DEFAULT_BANDS,
    layer_name: str = DEFAULT_LAYER_NAME,
    legend_origin: tuple[float, float] | None = None,
    title: str | None = None,
) -> DrawReport:
    """Draw the assessment as coloured fills on the floor plan, plus a legend.

    ``zone_by_apartment`` maps each apartment's identifier to the Archicad
    element GUID it was matched to -- the same join the property write-back
    uses, passed in rather than repeated so the picture and the schedule can
    never disagree about which apartment is which.
    """
    connection.require_tapir_at_least(
        DRAWING_MINIMUM_TAPIR_VERSION,
        "CreateHatches, which draws the result fills,",
    )

    layer_index = ensure_layer(connection, layer_name)
    removed = clear_layer(connection, layer_index)

    by_guid = {zone.guid: zone for zone in zones}
    fills: list[dict[str, Any]] = []
    no_outline: list[str] = []
    with_holes: list[str] = []
    with_arcs: list[str] = []
    unmatched: list[str] = []

    for apartment in assessment.apartments:
        guid = zone_by_apartment.get(apartment.apartment_id)
        zone = by_guid.get(guid) if guid else None
        if zone is None:
            unmatched.append(apartment.apartment_name or apartment.apartment_id)
            continue
        if not zone.outline:
            no_outline.append(zone.label)
            continue
        if zone.hole_count:
            with_holes.append(zone.label)
        if zone.arc_count:
            with_arcs.append(zone.label)
        fills.append(_fill_for(zone, band_for(apartment.governing_minutes, bands), layer_index))

    legend_fills, legend_texts = _legend(bands, legend_origin or _legend_origin(zones), layer_index)
    if title:
        legend_texts.append(
            {
                "coordinate": {
                    "x": (legend_origin or _legend_origin(zones))[0],
                    "y": (legend_origin or _legend_origin(zones))[1] + 2.5,
                    "z": 0.0,
                },
                "text": title,
                "justification": "Left",
            }
        )

    if fills or legend_fills:
        _create(connection, "CreateHatches", "hatchesData", fills + legend_fills)
    if legend_texts:
        _create(connection, "CreateTexts", "textsData", legend_texts)

    return DrawReport(
        fills_drawn=len(fills),
        fills_removed=removed,
        legend_items=len(legend_fills),
        layer_index=layer_index,
        zones_without_outline=tuple(no_outline),
        zones_with_holes=tuple(with_holes),
        zones_with_arcs=tuple(with_arcs),
        unmatched=tuple(unmatched),
    )


def _legend_origin(zones: Sequence[ArchicadZone]) -> tuple[float, float]:
    """Just clear of the zones, so the key does not land on the plan.

    Beats a fixed coordinate, which would sit in the middle of one project and
    a kilometre off the sheet in the next.
    """
    corners = [point for zone in zones for point in zone.outline]
    if not corners:
        return (0.0, 0.0)
    right = max(x for x, _ in corners)
    top = max(y for _, y in corners)
    return (right + 5.0, top)


def _create(
    connection: ArchicadConnection, command: str, key: str, data: list[dict[str, Any]]
) -> None:
    """Run a create command and fail on any per-element error.

    These commands report failures inside a successful response, one slot per
    input. A half-drawn diagram that reports success is worse than no diagram:
    the missing apartments look like apartments that were not assessed.
    """
    response = connection.run_tapir(command, {key: data})
    elements = response.get("elements") if isinstance(response, dict) else None
    if not isinstance(elements, list):
        raise ArchicadError(f"{command} returned no element list: {response!r}")

    problems = [
        f"{(item.get('error') or {}).get('message', 'unknown error')}"
        for item in elements
        if isinstance(item, dict) and "error" in item
    ]
    if problems:
        raise ArchicadError(
            f"{command} failed for {len(problems)} of {len(data)} elements:\n  "
            + "\n  ".join(sorted(set(problems))[:5])
        )
