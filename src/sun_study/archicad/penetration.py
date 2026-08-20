"""The study drawing the office actually issues, drawn onto the floor plan.

What it draws, per apartment, for one instant
---------------------------------------------
* the **sun patch** on the floor, as merged grid cells;
* the **outline** of the assessed area, so the patch is read against the flat
  it belongs to rather than against the whole plate;
* a **label on a leader**, carrying the lit area of the room and of the private
  open space at that instant, and the day's verdict.

That is the reference deliverable's own language, read off its drawings rather
than invented here. It replaces the earlier diagram -- one flat colour per
apartment for the whole day -- which answers "did this flat pass" and nothing
about where the sun actually was.

Why it goes on the floor plan
-----------------------------
Because the plan linework is already there. The worksheet series
(``archicad/series.py``) has no plan under it: it is a contact sheet of the
whole day, deliberately abstract. This is the opposite trade -- one instant,
in place, over the drawing everybody already recognises -- so one layer per
instant keeps them separable and lets a layer combination pick the moment.

The coordinate problem, and how it is checked
---------------------------------------------
The patch is computed in the *export's* world frame and drawn in Archicad's
*project* frame. Those differ by a rotation whenever the export is
north-aligned, so the patch is fitted onto the project with
``core.geometry.fit_plan_transform`` over one pair per apartment -- its
centroid as the export sees it against its outline's centroid as Archicad
does -- and the fit's residual is checked before anything is drawn. A patch
drawn through a bad transform lands on the wrong flat at the wrong angle and
looks entirely plausible, which is why the residual is a refusal and not a
warning.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
import numpy.typing as npt

from sun_study.archicad.connection import ArchicadConnection, ArchicadError
from sun_study.archicad.draw import (
    DRAWING_MINIMUM_TAPIR_VERSION,
    BandStyle,
    LayerState,
    clear_layer,
    ensure_layer,
    move_to_layer,
)
from sun_study.archicad.read import ArchicadZone
from sun_study.core.geometry import PlanTransform, fit_plan_transform
from sun_study.core.patches import merge_lit_cells, trace_lit_regions

FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]

__all__ = [
    "MAX_FIT_RESIDUAL_M",
    "OUTLINE_STYLE",
    "PATCH_STYLE",
    "CellGroup",
    "PenetrationReport",
    "PlanInstant",
    "box_centre",
    "draw_cell_groups",
    "draw_penetration",
    "fit_to_plan",
]

#: How far the fitted transform may be out before the drawing is refused.
#:
#: A patch is drawn at 250 mm resolution, so a fit good to a quarter of a cell
#: is beyond anything the drawing can express. Half a metre is generous by that
#: standard and still far below the width of the smallest room, which is the
#: scale at which a patch would start landing in the wrong one.
MAX_FIT_RESIDUAL_M = 0.5

#: The patch. The same amber the office's own studies use for sun on a surface.
PATCH_STYLE = BandStyle("in sun", float("inf"), fill_pen=124, rgb=(255, 213, 79))

#: The assessed area's outline. Green, as the reference drawings have it.
OUTLINE_STYLE = BandStyle("assessed", float("inf"), fill_pen=28, rgb=(0, 200, 0))


@dataclass(frozen=True)
class CellGroup:
    """One band of floor cells to draw as a single colour, with its figures.

    The area and share travel with the group because they belong in the
    legend, and a legend computed separately from the fills it labels is a
    legend that can disagree with them.
    """

    label: str
    mask: BoolArray
    style: BandStyle
    area_m2: float = 0.0
    share: float = 0.0


@dataclass(frozen=True)
class PlanInstant:
    """One moment of the day, ready to draw."""

    label: str
    """As it appears on the drawing, e.g. ``21 Jun 12:00``."""
    lit: BoolArray
    """Per floor cell, whether the sun reached it at this instant."""


@dataclass(frozen=True)
class PenetrationReport:
    """What was drawn, and what could not be."""

    instants: tuple[str, ...]
    layers: tuple[str, ...]
    patches: int
    outlines: int
    labels: int
    removed: int
    fit_residual_m: float
    fit_pairs: int
    unmatched: tuple[str, ...]
    storeys: tuple[int, ...]
    hidden_layers: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.unmatched and not self.hidden_layers

    def describe(self) -> str:
        lines = [
            f"drew {self.patches} patch fills, {self.outlines} assessed-area "
            f"outlines and {self.labels} labels for {len(self.instants)} instant(s)"
            + (f", replacing {self.removed} from a previous run" if self.removed else "")
        ]
        lines.append(f"  one layer per instant: {', '.join(self.layers)}")
        if self.storeys:
            shown = ", ".join(str(storey) for storey in sorted(self.storeys))
            lines.append(f"  on storey index {shown}")
        lines.append(
            f"  patch fitted onto the project frame from {self.fit_pairs} apartments, "
            f"residual {self.fit_residual_m * 1000:.0f} mm"
        )
        if self.fit_pairs < 3:
            lines.append(
                "    two pairs fit perfectly by construction, so that residual "
                "confirms nothing. Check one patch against its room by eye."
            )
        if self.unmatched:
            lines.append(
                f"  {len(self.unmatched)} assessed apartments had no zone to draw on: "
                + ", ".join(self.unmatched[:5])
            )
        if self.hidden_layers:
            lines.append(
                "  THE LAYER IS HIDDEN, so none of this is on screen: "
                + ", ".join(self.hidden_layers)
            )
        return "\n".join(lines)


def box_centre(points: FloatArray) -> list[float]:
    """The centre of a plan bounding box.

    Used on both sides of the fit rather than a mean of vertices or of cells.
    A mean is weighted by how the points happen to be distributed -- an
    outline with three vertices along one wall and one along the other pulls
    towards the crowded side, and a grid of floor cells does not -- so two
    means of the same room, taken differently, are not the same point. A box
    centre is a property of the extent alone, which is the thing both frames
    agree about.
    """
    flat = np.asarray(points, dtype=np.float64)[:, :2]
    return [
        float((flat[:, 0].min() + flat[:, 0].max()) / 2.0),
        float((flat[:, 1].min() + flat[:, 1].max()) / 2.0),
    ]


def fit_to_plan(
    export_extents: Mapping[str, FloatArray],
    zones: Mapping[str, ArchicadZone],
) -> PlanTransform:
    """Fit the export's frame onto the project's, from matched apartments.

    One pair per apartment: the centre of its extent as the export has it,
    against the centre of its Archicad outline. Both describe the same flat,
    so what is left after fitting is the transform being wrong rather than the
    building having moved.

    ``export_extents`` must describe the *dwelling* and nothing else. Passing
    the apartment's floor cells instead looks equivalent and is not: those
    include the balcony, which sits on one side of the flat and drags the
    centre with it by a different amount for every apartment. On the reference
    project that alone left 2.96 m of residual and the drawing was refused --
    correctly, but for a reason that had nothing to do with the model.
    """
    source: list[list[float]] = []
    target: list[list[float]] = []
    for apartment, zone in zones.items():
        extent = export_extents.get(apartment)
        if not zone.outline or extent is None or not len(extent):
            continue
        source.append(box_centre(extent))
        target.append(box_centre(np.array(zone.outline, dtype=np.float64)))

    if len(source) < 2:
        raise ArchicadError(
            f"Only {len(source)} apartment(s) could be paired between the export "
            f"and the project, and a plan transform needs two. Without it the "
            f"patch cannot be placed on the floor plan at all."
        )
    return fit_plan_transform(np.array(source), np.array(target))


def draw_penetration(
    connection: ArchicadConnection,
    *,
    instants: Sequence[PlanInstant],
    positions: FloatArray,
    parent_ids: Sequence[str],
    spacing_m: float,
    zone_by_apartment: dict[str, str],
    zones: Sequence[ArchicadZone],
    export_extents: Mapping[str, FloatArray],
    annotations: Mapping[str, Sequence[str]],
    layer_prefix: str,
    patch_style: BandStyle = PATCH_STYLE,
    outline_style: BandStyle = OUTLINE_STYLE,
    caption_height_mm: float = 2.5,
) -> PenetrationReport:
    """Draw the patch, the outline and the label for each instant.

    ``annotations`` gives the text lines for each apartment, already formatted
    -- what goes in them is a domain decision and belongs upstream, not here.
    ``export_extents`` gives each apartment's plan extent as the export has
    it, which is what the frames are fitted on. See ``fit_to_plan``.
    """
    connection.require_tapir_at_least(
        DRAWING_MINIMUM_TAPIR_VERSION, "CreateHatches, which draws the patch,"
    )
    if not instants:
        raise ArchicadError("No instants to draw.")

    by_guid = {zone.guid: zone for zone in zones}
    matched = {
        apartment: by_guid[guid] for apartment, guid in zone_by_apartment.items() if guid in by_guid
    }
    unmatched = tuple(sorted(set(zone_by_apartment) - set(matched)))
    transform = fit_to_plan(export_extents, matched)
    if transform.rmse_m > MAX_FIT_RESIDUAL_M:
        raise ArchicadError(
            f"The export and the project disagree about where the apartments "
            f"are: fitting one onto the other leaves {transform.rmse_m:.2f} m of "
            f"residual, over the {MAX_FIT_RESIDUAL_M:g} m limit. A patch drawn "
            f"through that lands on the wrong flat and still looks plausible. "
            f"The usual cause is a stale export, or apartments matched to the "
            f"wrong zones."
        )

    patches = outlines = labels = removed = 0
    layers: list[str] = []
    hidden: list[str] = []
    storeys: set[int] = set()

    for instant in instants:
        layer = ensure_layer(connection, f"{layer_prefix} {instant.label}")
        layers.append(f"{layer_prefix} {instant.label}")
        if layer.hidden:
            hidden.append(f"{layer_prefix} {instant.label}")
        removed += clear_layer(connection, layer.index)

        fills: list[dict[str, Any]] = []
        lines: list[dict[str, Any]] = []
        texts: list[dict[str, Any]] = []

        for apartment, zone in matched.items():
            mine = np.array([parent == apartment for parent in parent_ids])
            # An apartment with no floor cells still gets its outline and its
            # label: it is being assessed, and leaving it off the drawing
            # entirely would read as a flat nobody looked at rather than one
            # with no sun on it.
            if mine.any():
                here = positions[mine]
                shapes = _contours(here, instant.lit[mine], spacing_m)
                for shape in shapes:
                    fills.append(_patch_fill(shape, transform, patch_style, layer, zone))
                patches += len(shapes)

            if zone.outline:
                lines.append(_outline(zone, outline_style, layer))
                outlines += 1

            text = annotations.get(apartment)
            if text:
                texts.append(_label(zone, text, caption_height_mm, layer))
                labels += 1
            if zone.storey_index is not None:
                storeys.add(zone.storey_index)

        _create(connection, "CreateHatches", "hatchesData", fills)
        _create(connection, "CreatePolylines", "polylinesData", lines)
        _texts_on(connection, texts, layer)

    return PenetrationReport(
        instants=tuple(instant.label for instant in instants),
        layers=tuple(layers),
        patches=patches,
        outlines=outlines,
        labels=labels,
        removed=removed,
        fit_residual_m=transform.rmse_m,
        fit_pairs=len(matched),
        unmatched=unmatched,
        storeys=tuple(sorted(storeys)),
        hidden_layers=tuple(hidden),
    )


def draw_cell_groups(
    connection: ArchicadConnection,
    *,
    groups: Sequence[CellGroup],
    positions: FloatArray,
    parent_ids: Sequence[str],
    spacing_m: float,
    zone_by_apartment: dict[str, str],
    zones: Sequence[ArchicadZone],
    export_extents: Mapping[str, FloatArray],
    layer_name: str,
    title: str = "",
    caption_height_mm: float = 2.5,
    on_storey: int | None = None,
) -> PenetrationReport:
    """Draw floor cells grouped by band, one colour each, plus a legend.

    ``on_storey`` draws every cell on one storey instead of grouping them by
    apartment. That is what open ground needs: it belongs to no dwelling, and
    the storey it appears on is the one whose level it sits at.

    This is the whole-day picture -- how long the sun was on each piece of
    floor, banded -- where ``draw_penetration`` draws one instant. Both put
    fills on the storey the apartment is on, through the same fitted
    transform, so the two can be laid over each other.
    """
    connection.require_tapir_at_least(
        DRAWING_MINIMUM_TAPIR_VERSION, "CreateHatches, which draws the bands,"
    )
    if not groups:
        raise ArchicadError("No bands to draw.")

    by_guid = {zone.guid: zone for zone in zones}
    matched = {
        apartment: by_guid[guid] for apartment, guid in zone_by_apartment.items() if guid in by_guid
    }
    unmatched = tuple(sorted(set(zone_by_apartment) - set(matched)))
    transform = fit_to_plan(export_extents, matched)
    if transform.rmse_m > MAX_FIT_RESIDUAL_M:
        raise ArchicadError(
            f"The export and the project disagree about where the apartments "
            f"are: {transform.rmse_m:.2f} m of residual, over the "
            f"{MAX_FIT_RESIDUAL_M:g} m limit."
        )

    layer = ensure_layer(connection, layer_name)
    removed = clear_layer(connection, layer.index)

    fills: list[dict[str, Any]] = []
    storeys: set[int] = set()
    if on_storey is not None:
        anywhere = next(iter(matched.values()), None)
        for group in groups:
            here = positions[np.asarray(group.mask, dtype=bool)]
            if not len(here) or anywhere is None:
                continue
            flat = replace(anywhere, storey_index=on_storey)
            for shape in _contours(here, np.ones(len(here), dtype=bool), spacing_m):
                fills.append(_patch_fill(shape, transform, group.style, layer, flat))
            storeys.add(on_storey)

    for group in groups if on_storey is None else ():
        for apartment, zone in matched.items():
            mine = np.array([parent == apartment for parent in parent_ids]) & np.asarray(
                group.mask, dtype=bool
            )
            if not mine.any():
                continue
            here = positions[mine]
            for shape in _contours(here, np.ones(len(here), dtype=bool), spacing_m):
                fills.append(_patch_fill(shape, transform, group.style, layer, zone))
            if zone.storey_index is not None:
                storeys.add(zone.storey_index)

    texts: list[dict[str, Any]] = []
    legend_fills, legend_texts = _band_legend(
        groups, _legend_origin(matched.values()), layer, storeys, title, caption_height_mm
    )
    fills.extend(legend_fills)
    texts.extend(legend_texts)

    _create(connection, "CreateHatches", "hatchesData", fills)
    _texts_on(connection, texts, layer)

    return PenetrationReport(
        instants=(title or layer_name,),
        layers=(layer_name,),
        patches=len(fills) - len(legend_fills),
        outlines=0,
        labels=len(texts),
        removed=removed,
        fit_residual_m=transform.rmse_m,
        fit_pairs=len(matched),
        unmatched=unmatched,
        storeys=tuple(sorted(storeys)),
        hidden_layers=(layer_name,) if layer.hidden else (),
    )


def _legend_origin(zones: Any) -> tuple[float, float]:
    """Just clear of the apartments, so the key does not land on the plan."""
    outlines = [point for zone in zones for point in zone.outline]
    if not outlines:
        return (0.0, 0.0)
    return (max(x for x, _ in outlines) + 4.0, max(y for _, y in outlines))


def _band_legend(
    groups: Sequence[CellGroup],
    origin: tuple[float, float],
    layer: LayerState,
    storeys: Sequence[int] | set[int],
    title: str,
    height_mm: float,
    swatch_m: float = 1.2,
    step_m: float = 1.8,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Swatches and their figures, in the reference deliverable's own form.

    Every band carries its area and its share, because that is what the
    legend of a solar study is actually for -- the drawing shows where, the
    legend says how much. Drawn on **every** storey the fills landed on, so a
    plan of any one of them carries its own key rather than referring to a
    sheet somebody has to find.
    """
    x, y = origin
    fills: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []
    # A legend on every storey the fills reached, so each plan carries its own
    # key. Where nothing landed anywhere, one copy with no storey at all.
    on: list[int | None] = [int(storey) for storey in sorted(storeys)] or [None]
    for storey in on:
        top = y
        if title:
            entry: dict[str, Any] = {
                "coordinate": {"x": x, "y": top + step_m, "z": 0.0},
                "text": title,
                "height": height_mm * 1.4,
                "justification": "Left",
            }
            if storey is not None:
                entry["floorIndex"] = storey
            texts.append(entry)

        for row, group in enumerate(groups):
            bottom = top - row * step_m
            fill: dict[str, Any] = {
                "coordinates": [
                    {"x": x, "y": bottom},
                    {"x": x + swatch_m, "y": bottom},
                    {"x": x + swatch_m, "y": bottom + swatch_m},
                    {"x": x, "y": bottom + swatch_m},
                ],
                "layerIndex": layer.index,
                "fillPenIndex": group.style.fill_pen,
                "fillBackgroundPenIndex": group.style.background_pen,
                "contourPenIndex": group.style.fill_pen,
                "showArea": False,
            }
            label: dict[str, Any] = {
                "coordinate": {"x": x + swatch_m * 1.4, "y": bottom, "z": 0.0},
                "text": f"{group.label}   {group.area_m2:.1f} m²   {group.share:.1%}",
                "height": height_mm,
                "justification": "Left",
            }
            if storey is not None:
                fill["floorInd"] = storey
                label["floorIndex"] = storey
            fills.append(fill)
            texts.append(label)
    return fills, texts


def _contours(positions: FloatArray, lit: BoolArray, spacing_m: float) -> list[Any]:
    """The shapes to draw for one set of lit cells, as unclosed rings.

    One polygon per connected patch where that is safe, and the tiled
    rectangles where it is not. ``CreateHatches`` takes a single contour and
    no holes, so a patch with a hole in it can only be drawn as one shape by
    filling the hole -- which would claim sunlight on a piece of floor that
    never saw any. Those fall back to rectangles, which tile the same area
    exactly and simply need more of them.

    On the reference project this turns thousands of small fills into a
    handful of outlines, which is the difference between a drawing that can be
    edited and one that cannot.
    """
    regions = trace_lit_regions(positions, lit, spacing_m)
    if regions and not any(region.holes for region in regions):
        return [region.outer for region in regions]

    shapes: list[Any] = []
    for region in regions:
        if not region.holes:
            shapes.append(region.outer)
    if not shapes:
        return [rectangle.corners for rectangle in merge_lit_cells(positions, lit, spacing_m)]

    # Mixed: the solid patches as outlines, the holed ones as rectangles.
    holed = np.zeros(len(positions), dtype=bool)
    flat = np.asarray(positions, dtype=np.float64)[:, :2]
    for region in regions:
        if not region.holes:
            continue
        xs = [x for x, _ in region.outer]
        ys = [y for _, y in region.outer]
        inside = (
            (flat[:, 0] >= min(xs))
            & (flat[:, 0] <= max(xs))
            & (flat[:, 1] >= min(ys))
            & (flat[:, 1] <= max(ys))
        )
        holed |= inside & np.asarray(lit, dtype=bool)
    if holed.any():
        shapes.extend(
            rectangle.corners for rectangle in merge_lit_cells(positions, holed, spacing_m)
        )
    return shapes


def _patch_fill(
    ring: Sequence[tuple[float, float]],
    transform: PlanTransform,
    style: BandStyle,
    layer: LayerState,
    zone: ArchicadZone,
) -> dict[str, Any]:
    """One patch outline, moved into the project's own frame."""
    corners = transform.apply(np.array(ring, dtype=np.float64))
    data: dict[str, Any] = {
        "coordinates": [{"x": float(x), "y": float(y)} for x, y in corners],
        "layerIndex": layer.index,
        "fillPenIndex": style.fill_pen,
        "fillBackgroundPenIndex": style.background_pen,
        "contourPenIndex": style.fill_pen,
        # Explicitly off. A Fill inherits the Fill tool's current default, and
        # on a real project that default has "Show Area Text" on -- so every
        # patch cell arrives with its own square-metre figure printed across
        # it, which at 250 mm resolution is thousands of numbers over the plan.
        "showArea": False,
    }
    if zone.storey_index is not None:
        data["floorInd"] = zone.storey_index
    return data


def _outline(zone: ArchicadZone, style: BandStyle, layer: LayerState) -> dict[str, Any]:
    """The assessed area, drawn as a line rather than a fill.

    A fill would cover the plan under it; the whole point of drawing on the
    floor plan is that the plan stays readable through it.
    """
    data: dict[str, Any] = {
        "coordinates": [{"x": x, "y": y} for x, y in (*zone.outline, zone.outline[0])],
        "layerIndex": layer.index,
        "linePenIndex": style.fill_pen,
    }
    if zone.storey_index is not None:
        data["floorInd"] = zone.storey_index
    return data


def _label(
    zone: ArchicadZone, lines: Sequence[str], height_mm: float, layer: LayerState
) -> dict[str, Any]:
    """The annotation block, placed at the apartment's own centroid.

    Not on a leader: ``CreateLabels`` refuses an associative label on a Zone
    here (-2130312912, the Label tool's default is not a text-class label), and
    a leader whose end is computed from nothing but a centroid points somewhere
    arbitrary anyway. Text in the middle of the flat it describes is
    unambiguous, and moving it is one drag.
    """
    outline = np.array(zone.outline, dtype=np.float64)
    data: dict[str, Any] = {
        "coordinate": {
            "x": float(outline[:, 0].mean()),
            "y": float(outline[:, 1].mean()),
            "z": 0.0,
        },
        "text": "\n".join(lines),
        "height": height_mm,
        "justification": "Center",
    }
    if zone.storey_index is not None:
        data["floorIndex"] = zone.storey_index
    return data


def _create(
    connection: ArchicadConnection, command: str, key: str, data: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Run a create command in batches, failing on any per-element error.

    Returns what was made, because a Text has to be moved onto its layer
    afterwards -- ``CreateTexts`` accepts no layer at all.
    """
    made: list[dict[str, Any]] = []
    if not data:
        return made
    for start in range(0, len(data), 500):
        response = connection.run_tapir(command, {key: data[start : start + 500]})
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
        made.extend(entry for entry in elements if isinstance(entry, dict) and "elementId" in entry)
    return made


def _texts_on(
    connection: ArchicadConnection, data: list[dict[str, Any]], layer: LayerState
) -> None:
    """Create texts and put them on the study's layer, not the Text tool's.

    Without the move the labels land on whatever the Text tool defaults to --
    ``05 | Dims/Notes.DA`` on the reference project, an office annotation layer
    for the drawing set. That is wrong twice over: the study's annotation is
    mixed into somebody else's layer, and it is outside what the next run
    clears, so switching the study's layer off leaves the labels on the plan.
    """
    move_to_layer(connection, _create(connection, "CreateTexts", "textsData", data), layer.index)
