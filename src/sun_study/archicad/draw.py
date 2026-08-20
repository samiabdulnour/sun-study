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

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from sun_study.archicad.connection import ArchicadConnection, ArchicadError
from sun_study.archicad.layers import borrowed
from sun_study.archicad.read import ArchicadZone, disambiguated
from sun_study.rules.assessment import BuildingAssessment

__all__ = [
    "DEFAULT_BANDS",
    "DEFAULT_LAYER_NAME",
    "INDISTINGUISHABLE_RGB",
    "BandStyle",
    "DrawReport",
    "LayerState",
    "Pen",
    "band_for",
    "clear_layer",
    "draw_assessment",
    "ensure_layer",
    "hidden_layers",
    "indistinguishable_bands",
    "match_pens",
    "pen_table",
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
    rgb: tuple[int, int, int] = (0, 0, 0)
    """The colour this band *should* be, 0-255.

    Taken from the reference study's own legend, decoded during the Ladybug
    validation by integrating area per colour until the published table
    reproduced exactly. Kept alongside the pen index because a pen number is
    meaningless outside one project's pen table, while the colour is the thing
    everybody actually agreed on -- and it is what lets a pen be *chosen*
    rather than guessed.
    """


#: A starting point, not an answer. Pen indices mean whatever the project's pen
#: table says they mean, so these are almost certainly wrong for any given
#: office and are meant to be overridden. The run prints the mapping it used.
DEFAULT_BANDS: tuple[BandStyle, ...] = (
    BandStyle("0 hrs", 1e-9, fill_pen=91, rgb=(8, 48, 107)),
    BandStyle("0-1 hrs", 60.0, fill_pen=92, rgb=(43, 122, 191)),
    BandStyle("1-2 hrs", 120.0, fill_pen=93, rgb=(77, 182, 172)),
    BandStyle("2-3 hrs", 180.0, fill_pen=94, rgb=(230, 238, 156)),
    BandStyle("3-4 hrs", 240.0, fill_pen=95, rgb=(255, 213, 79)),
    BandStyle("4-5 hrs", 300.0, fill_pen=96, rgb=(255, 183, 77)),
    BandStyle("5+ hrs", float("inf"), fill_pen=97, rgb=(244, 81, 30)),
)


@dataclass(frozen=True)
class DrawReport:
    """What was drawn, and what could not be."""

    fills_drawn: int
    fills_removed: int
    legend_items: int
    layer: LayerState
    storeys: tuple[int, ...]
    """Which storeys carry fills. A plan on any other one shows nothing."""
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
            f"legend on layer index {self.layer.index}"
            + (
                f", replacing {self.fills_removed} from a previous run"
                if self.fills_removed
                else ""
            )
        ]
        # The two reasons a successful run looks like it did nothing.
        if self.layer.hidden:
            lines.append(
                "  THE LAYER IS HIDDEN, so none of this is on screen. Show it in "
                "Layer Settings, or in the layer combination you are working in."
            )
        if self.layer.locked:
            lines.append("  The layer is locked, so the fills cannot be selected or edited.")
        if self.storeys:
            shown = ", ".join(str(storey) for storey in self.storeys)
            lines.append(
                f"  drawn on storey index {shown} -- a plan on any other storey shows nothing"
            )
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


@dataclass(frozen=True)
class Pen:
    """One pen from the project's pen table."""

    index: int
    rgb: tuple[int, int, int]
    description: str = ""


def pen_table(connection: ArchicadConnection) -> tuple[Pen, ...]:
    """The pens of the pen table Archicad is using for the model.

    Three calls, for the same reason ``_find_layer`` takes two.
    ``GetAttributesByType`` enumerates but reports only id, index and name;
    ``GetPenTables`` carries the pens but *requires* ``attributeIds``, so it
    cannot be used to search -- calling it bare is a schema violation and
    Archicad rejects the whole command.

    The middle call asks only for ``isActiveForModel``. A project can hold
    several pen tables and each carries 255 pens, so pulling every pen of
    every table to find out which one governs the drawing is a lot of JSON for
    one boolean. ``fields`` exists precisely to avoid that, and the pens are
    fetched once the table is known.

    Colours come back as 0-1 floats and are scaled to 0-255 here, because
    every other colour in this project -- the reference legend, the band
    definitions -- is in 0-255 and mixing the two scales silently produces
    near-black.
    """
    attributes = connection.run_tapir("GetAttributesByType", {"attributeType": "PenTable"})
    listed = attributes.get("attributes") if isinstance(attributes, dict) else None
    identifiers = [
        str((attribute.get("attributeId") or {}).get("guid"))
        for attribute in (listed or [])
        if isinstance(attribute, dict) and (attribute.get("attributeId") or {}).get("guid")
    ]
    if not identifiers:
        raise ArchicadError(f"The project lists no pen tables: {attributes!r}")

    identifier = _active_pen_table(connection, identifiers)
    response = connection.run_tapir(
        "GetPenTables",
        {"attributeIds": [{"attributeId": {"guid": identifier}}], "fields": ["pens"]},
    )
    table = _first_pen_table(response)
    if table is None:
        raise ArchicadError(f"GetPenTables returned no usable pen table: {response!r}")

    pens: list[Pen] = []
    for pen in table.get("pens") or []:
        colour = (pen or {}).get("color") if isinstance(pen, dict) else None
        index = (pen or {}).get("index") if isinstance(pen, dict) else None
        if not isinstance(colour, dict) or not isinstance(index, (int, float)):
            continue
        pens.append(
            Pen(
                index=int(index),
                rgb=(
                    _channel(colour, "red"),
                    _channel(colour, "green"),
                    _channel(colour, "blue"),
                ),
                description=str(pen.get("description", "")),
            )
        )
    if not pens:
        raise ArchicadError(f"The active pen table carried no pens: {response!r}")
    return tuple(pens)


def _channel(colour: dict[str, Any], name: str) -> int:
    """One 0-1 colour channel as 0-255, clamped."""
    return max(0, min(255, round(255 * float(colour.get(name, 0.0)))))


def _active_pen_table(connection: ArchicadConnection, identifiers: Sequence[str]) -> str:
    """Which pen table is in effect for the model.

    Falls back to the first one listed. A project with one pen table -- the
    common case -- gives the same answer either way, and a build that will not
    report ``isActiveForModel`` is a worse reason to refuse to draw than
    picking the only table there is.
    """
    if len(identifiers) == 1:
        return identifiers[0]
    try:
        response = connection.run_tapir(
            "GetPenTables",
            {
                "attributeIds": [{"attributeId": {"guid": guid}} for guid in identifiers],
                "fields": ["isActiveForModel"],
            },
        )
    except ArchicadError:
        return identifiers[0]

    tables = response.get("penTables") if isinstance(response, dict) else None
    for position, entry in enumerate(tables or []):
        table = _unwrap_pen_table(entry)
        if table is not None and table.get("isActiveForModel"):
            guid = (table.get("attributeId") or {}).get("guid")
            return str(guid) if guid else identifiers[position]
    return identifiers[0]


def _unwrap_pen_table(entry: Any) -> dict[str, Any] | None:
    """One ``penTables`` entry, or ``None`` if it is an error item."""
    if not isinstance(entry, dict) or "error" in entry:
        return None
    inner = entry.get("penTableAttribute")
    return inner if isinstance(inner, dict) else entry


def _first_pen_table(response: Any) -> dict[str, Any] | None:
    tables = response.get("penTables") if isinstance(response, dict) else None
    if not isinstance(tables, list) or not tables:
        return None
    return _unwrap_pen_table(tables[0])


def _distance(left: tuple[int, int, int], right: tuple[int, int, int]) -> float:
    """How far apart two colours are.

    Plain Euclidean in RGB. A perceptual metric would rank near-misses better,
    but the job here is picking the obvious match out of a palette of a few
    hundred, and the distance is reported so a poor one is visible rather than
    silently accepted.
    """
    return math.dist(left, right)


#: Two fills closer than this in RGB read as the same colour on a printed
#: plan. Measured rather than chosen: the reference legend's tightest adjacent
#: pair -- 3-4 hrs and 4-5 hrs, rgb(255,213,79) against rgb(255,183,77) -- is
#: about 30 apart, so anything under that is closer than a distinction the
#: published study expects a reader to make.
INDISTINGUISHABLE_RGB = 30.0


def match_pens(
    bands: Sequence[BandStyle], pens: Sequence[Pen]
) -> tuple[tuple[BandStyle, ...], dict[str, float]]:
    """Re-point each band at the closest *unused* pen in the project's table.

    A pen index means nothing outside the pen table it came from, so a
    hard-coded default is guaranteed wrong somewhere. The colour, on the other
    hand, is the thing the reference study and this tool already agree on --
    so the colour is the input and the pen is derived.

    **The assignment is one-to-one, and that is the point.** Matching each
    band independently is the obvious implementation and it is wrong: on a real
    office pen table the 3-4 and 4-5 hour bands both landed on pen 124, because
    their reference colours are only 30 apart and the palette had one amber
    near both. Two bands sharing a pen makes the diagram unable to show where
    the four-hour line falls -- a plan that looks finished and answers the
    wrong question. So pens are claimed globally: every band-pen pairing is
    ranked by distance and the closest pairing wins, then that band and that
    pen are both out of the running.

    Greedy rather than optimal. With seven bands it is within a hair of the
    best possible assignment, and a person can follow "closest pairing first"
    when checking why a band got the pen it did.

    Returns the re-pointed bands and how far each had to reach, because a
    palette with no dark navy in it will still return *something* and the
    distance is the only sign that the answer is poor.
    """
    if not pens:
        return tuple(bands), {}

    # Ranked by distance, then by band and pen index so an exact tie -- two
    # identical pens in the table, which happens -- resolves the same way
    # every run rather than by dict ordering.
    pairings = sorted(
        (_distance(band.rgb, pen.rgb), position, pen.index, pen)
        for position, band in enumerate(bands)
        for pen in pens
    )

    chosen: dict[int, Pen] = {}
    claimed: set[int] = set()
    for _gap, position, _index, pen in pairings:
        if position in chosen or pen.index in claimed:
            continue
        chosen[position] = pen
        claimed.add(pen.index)
        if len(chosen) == len(bands):
            break

    matched: list[BandStyle] = []
    distances: dict[str, float] = {}
    for position, band in enumerate(bands):
        # Fewer pens than bands leaves the tail unassigned. Keeping the band's
        # own default is better than reusing a pen and hiding a boundary.
        assigned = chosen.get(position)
        if assigned is None:
            matched.append(band)
            continue
        matched.append(replace(band, fill_pen=assigned.index))
        distances[band.label] = _distance(band.rgb, assigned.rgb)
    return tuple(matched), distances


def indistinguishable_bands(
    bands: Sequence[BandStyle], pens: Sequence[Pen]
) -> tuple[tuple[str, str], ...]:
    """Pairs of bands whose assigned pens will look the same on the plan.

    Distinct pen indices are not the same as distinct colours. A palette can
    hold two ambers a hair apart, and a one-to-one assignment will happily use
    both -- leaving a diagram that is technically correct and unreadable at the
    boundary. This is the check that a reader could actually tell them apart.
    """
    colour = {pen.index: pen.rgb for pen in pens}
    close: list[tuple[str, str]] = []
    for position, band in enumerate(bands):
        for other in bands[position + 1 :]:
            left, right = colour.get(band.fill_pen), colour.get(other.fill_pen)
            if left is None or right is None:
                continue
            if _distance(left, right) < INDISTINGUISHABLE_RGB:
                close.append((band.label, other.label))
    return tuple(close)


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


@dataclass(frozen=True)
class LayerState:
    """A results layer, and whether anything drawn on it will be seen."""

    index: int
    identifier: str
    hidden: bool = False
    locked: bool = False

    @property
    def invisible(self) -> bool:
        return self.hidden or self.locked


def ensure_layer(connection: ArchicadConnection, name: str) -> LayerState:
    """Find or create the results layer, and report whether it is visible.

    Visibility matters more than it sounds. A freshly created layer is not
    necessarily shown by the layer combination that happens to be active, and
    a hidden layer makes a successful run look exactly like one that did
    nothing: the command reports fills drawn, and the drawing does not change.

    A layer created here is *shown*, and shown again on every later run. That
    is a deliberate exception to leaving existing attributes alone: visibility
    is not really a property of the layer but of the layer combination in
    force, and a layer this tool has just made is hidden in every combination
    the project already had. Measured on the reference project -- a fresh
    results layer came back ``isHidden=True`` immediately after being created
    with ``isHidden: False``, and the run drew a thousand fills nobody could
    see. Everything else about the layer is left as it is.
    """
    existing = _find_layer(connection, name)
    if existing is not None and not (existing.hidden or existing.locked):
        return existing
    if existing is not None:
        connection.run_tapir(
            "CreateLayers",
            {
                "layerDataArray": [{"name": name, "isHidden": False, "isLocked": False}],
                "overwriteExisting": True,
            },
        )
        shown = _find_layer(connection, name)
        return shown if shown is not None else existing

    connection.run_tapir(
        "CreateLayers",
        {
            "layerDataArray": [{"name": name, "isHidden": False, "isLocked": False}],
            "overwriteExisting": False,
        },
    )
    created = _find_layer(connection, name)
    if created is None:
        raise ArchicadError(
            f"Created layer {name!r} but Archicad does not list it. Drawing "
            f"cannot continue without a layer index to place fills on."
        )
    return created


def hidden_layers(connection: ArchicadConnection, names: Sequence[str]) -> list[str]:
    """Which of ``names`` are switched off in the project right now.

    The IFC translator exports what the current layer combination *shows*, so
    a hidden layer is simply absent from the export. That failure arrives far
    downstream and in the wrong words: the zones never reach the model, and
    the scene reports "apartment zone layers matched nothing", listing the
    layers that did export -- none of which is the one at fault. The reader is
    left comparing two lists to notice an absence.

    Asking Archicad first turns that into a sentence. A name this does not
    recognise is not reported here; that is a typo, and ``_require_matches``
    already says so against the export, where the available names are.
    """
    if not names:
        return []
    return [
        name
        for name in names
        if (state := _find_layer(connection, name)) is not None and state.hidden
    ]


def _find_layer(connection: ArchicadConnection, name: str) -> LayerState | None:
    """A layer by name, with its index, identifier and visibility.

    Two calls, because neither answers alone. ``GetAttributesByType``
    enumerates but reports only id, index and name; ``GetLayers`` carries
    ``isHidden`` and ``isLocked`` but *requires* ``attributeIds``, so it cannot
    be used to search -- calling it bare is a schema violation and Archicad
    rejects the whole command.
    """
    response = connection.run_tapir("GetAttributesByType", {"attributeType": "Layer"})
    attributes = response.get("attributes") if isinstance(response, dict) else None
    if not isinstance(attributes, list):
        raise ArchicadError(f"GetAttributesByType returned no layer list: {response!r}")

    wanted = name.casefold()
    for attribute in attributes:
        if not isinstance(attribute, dict):
            continue
        if str(attribute.get("name", "")).casefold() != wanted:
            continue
        index = attribute.get("index")
        identifier = (attribute.get("attributeId") or {}).get("guid")
        if not isinstance(index, (int, float)) or not identifier:
            continue
        return _layer_visibility(connection, int(index), str(identifier))
    return None


def _layer_visibility(connection: ArchicadConnection, index: int, identifier: str) -> LayerState:
    """Whether the layer is hidden or locked, defaulting to visible.

    A build that will not answer must not make the run stop: not knowing the
    visibility is a worse reason to fail than drawing onto a hidden layer.
    """
    try:
        response = connection.run_tapir(
            "GetLayers", {"attributeIds": [{"attributeId": {"guid": identifier}}]}
        )
    except ArchicadError:
        return LayerState(index, identifier)

    layers = response.get("layers") if isinstance(response, dict) else None
    first = layers[0] if isinstance(layers, list) and layers else None
    if not isinstance(first, dict) or "error" in first:
        return LayerState(index, identifier)

    attribute = first.get("layerAttribute") if "layerAttribute" in first else first
    if not isinstance(attribute, dict):
        return LayerState(index, identifier)
    return LayerState(
        index,
        identifier,
        hidden=bool(attribute.get("isHidden", False)),
        locked=bool(attribute.get("isLocked", False)),
    )


def clear_layer(connection: ArchicadConnection, layer_index: int) -> int:
    """Delete every Hatch, Text and PolyLine on the results layer. Returns the count.

    The count is **verified**, not assumed. ``DeleteElements`` answers
    ``{"success": true}`` whether or not it deleted anything, and there is one
    condition under which it deletes nothing at all: the elements are on a
    **hidden** layer. Since a layer this tool creates is hidden in every layer
    combination the project already had, that is the normal case rather than a
    rare one -- on the reference project a worksheet grew to 15,889 fills over
    three runs, each of which reported clearing the last.

    Callers reach this through ``ensure_layer``, which forces the layer
    visible first. What is left here is to check the result and say what
    really happened.

    Scoped to the two element types this module creates, so a person who put
    something else on the layer does not lose it without warning.
    """
    removed: list[dict[str, Any]] = []
    # ``PolyLine``, with a capital L: ``Polyline`` is rejected outright as an
    # invalid element type. The study's assessed-area outlines are polylines,
    # so a delete that missed them left every previous run's outlines on the
    # layer under the new ones.
    for element_type in ("Hatch", "Text", "PolyLine"):
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
        # A Fill inherits the Fill tool's current default, and on a real
        # project that default has "Show Area Text" on -- so every fill
        # arrives with its own square-metre figure printed across it, over
        # the apartment it is meant to be colouring.
        "showArea": False,
    }
    if zone.storey_index is not None:
        data["floorInd"] = zone.storey_index
    return data


#: Text height for everything the tool writes, in Archicad's own units for the
#: field. Set rather than inherited: a Text created without one takes the Text
#: tool's default, which on the reference project drew a legend label 1.27 m
#: tall in model space -- taller than the 1.5 m the rows were spaced at, so
#: every label sat on the one below it. An office's default is set for its own
#: drawings and has no reason to suit a legend beside a plan.
TEXT_HEIGHT = 1.0

#: What one line of ``TEXT_HEIGHT`` text occupies in model metres, measured:
#: 0.424 m for the whole bounding box, ascender and descender included. Used
#: to space rows, so the spacing follows the text rather than being guessed
#: alongside it.
TEXT_MODEL_HEIGHT_M = 0.43


def _legend(
    bands: Sequence[BandStyle],
    origin: tuple[float, float],
    layer_index: int,
    *,
    swatch_m: float = 1.0,
    spacing_m: float | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Swatches and labels for the key, top band first.

    Drawn in model space beside the plan, in metres, so it scales with the
    drawing rather than floating at a fixed paper size.

    The row spacing has to clear both the swatch and the label, and the label
    is the taller of the two at any scale where the text is not tiny. Spacing
    it at 1.5 m for a 1.0 m swatch looked generous and was not: the labels
    were 1.27 m tall, so consecutive rows overlapped and the key read as one
    smear of text. It is now the taller of the two plus half a line.
    """
    fills: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []
    x, top = origin
    step = spacing_m if spacing_m is not None else max(swatch_m, TEXT_MODEL_HEIGHT_M) * 1.5

    for row, band in enumerate(reversed(bands)):
        y = top - row * step
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
                # A swatch showing its own area is a legend annotated with
                # "1.00 m2" seven times. See _fill_for.
                "showArea": False,
            }
        )
        texts.append(
            {
                # Centred on the swatch rather than sitting on its baseline, so
                # a label reads against the colour it names.
                "coordinate": {
                    "x": x + swatch_m * 1.4,
                    "y": y + (swatch_m - TEXT_MODEL_HEIGHT_M) / 2.0,
                    "z": 0.0,
                },
                "text": band.label,
                "justification": "Left",
                "height": TEXT_HEIGHT,
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

    layer = ensure_layer(connection, layer_name)
    layer_index = layer.index
    removed = clear_layer(connection, layer_index)

    by_guid = {zone.guid: zone for zone in zones}
    # Zone names repeat. Reporting "RESI, RESI, RESI, RESI, RESI" names no
    # zone at all, so the ones that collide carry a GUID fragment.
    named = disambiguated({zone.guid: zone.label for zone in zones})
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
        label = named[zone.guid]
        if not zone.outline:
            no_outline.append(label)
            continue
        if zone.hole_count:
            with_holes.append(label)
        if zone.arc_count:
            with_arcs.append(label)
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
                "height": TEXT_HEIGHT,
            }
        )

    if fills or legend_fills:
        _create(connection, "CreateHatches", "hatchesData", fills + legend_fills)
    if legend_texts:
        # CreateTexts takes no layer, so the labels land on the Text tool's
        # default -- an office annotation layer -- and have to be moved onto
        # the study's own. Otherwise the legend is not part of what the next
        # run clears, and switching the study off leaves it behind.
        made = _create(connection, "CreateTexts", "textsData", legend_texts)
        moved = move_to_layer(connection, made, layer_index)
        if moved != len(made):
            raise ArchicadError(
                f"{len(made) - moved} of {len(made)} legend labels would not move onto "
                f"layer {layer_name!r}. They are still on the Text tool's default layer, "
                f"which is where a Text is created; that layer or this one is locked."
            )

    return DrawReport(
        fills_drawn=len(fills),
        fills_removed=removed,
        legend_items=len(legend_fills),
        layer=layer,
        storeys=tuple(sorted({int(f["floorInd"]) for f in fills if "floorInd" in f})),
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
) -> list[dict[str, Any]]:
    """Run a create command and fail on any per-element error.

    These commands report failures inside a successful response, one slot per
    input. A half-drawn diagram that reports success is worse than no diagram:
    the missing apartments look like apartments that were not assessed.

    Returns what was made, because a Text has to be moved after it is made.
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
    return [item for item in elements if isinstance(item, dict) and "elementId" in item]


def move_to_layer(
    connection: ArchicadConnection, elements: Sequence[dict[str, Any]], layer_index: int
) -> int:
    """Put created elements on the tool's own layer. Returns how many moved.

    ``CreateTexts`` takes no layer -- it takes a coordinate, a string, a
    height, a pen, a justification and an angle, and nothing else -- so a
    Text lands on whatever layer the Text tool defaults to. On the reference
    project that is ``05 | Dims/Notes.DA``, one of the office's own annotation
    layers, which means the study's legend arrives mixed in with the drawing
    set's dimensions and disappears from a run's own clean-up.

    Moving afterwards is the same route the facade skin's walls take, and it
    carries the same trap: ``SetDetailsOfElements`` answers success and
    changes nothing when the element sits on a hidden layer (D43). The result
    is therefore read back rather than believed.
    """
    if not elements:
        return 0

    # Where they landed, which is the Text tool's business and not the
    # caller's. A hidden layer refuses the move silently, so whatever they
    # arrived on is switched on for the duration and put back after.
    where = connection.run_tapir("GetDetailsOfElements", {"elements": list(elements)})
    rows = where.get("detailsOfElements") if isinstance(where, dict) else None
    landed = {
        row.get("layerIndex")
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict) and isinstance(row.get("layerIndex"), int)
    }
    if landed <= {layer_index}:
        # The Text tool already defaults to the study's layer, which happens on
        # a project the tool has been run on before. Nothing to move, and
        # nothing to unhide in order to move it.
        return len(elements)

    with borrowed(connection, [index for index in landed if isinstance(index, int)]):
        connection.run_tapir(
            "SetDetailsOfElements",
            {
                "elementsWithDetails": [
                    {"elementId": element["elementId"], "details": {"layerIndex": layer_index}}
                    for element in elements
                ]
            },
        )
        check = connection.run_tapir("GetDetailsOfElements", {"elements": list(elements)})

    moved = check.get("detailsOfElements") if isinstance(check, dict) else None
    if not isinstance(moved, list):
        return 0
    return sum(1 for row in moved if isinstance(row, dict) and row.get("layerIndex") == layer_index)
