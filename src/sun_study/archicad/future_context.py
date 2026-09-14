"""The future context, stood in the model: one slab per tier of each envelope.

``site.envelope`` works out what the controls allow on a lot, in metres. This
module is the two things it does not know: where the metres come from (the
bundle's cadastre and controls, projected through the run's frame) and
where they go (slabs on a layer of their own, through the add-on, on the
ground the contours give).

A layer of its own, ``LORIINI FUTURE``, rather than the context model's:
the shadow diagram's saved views say what stands (D78), and a future
neighbour is a question a view answers -- with, without -- not a fact the
model asserts. Switch the layer on for the SEARs envelope study and off for
the DA's existing-conditions set.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sun_study.archicad.connection import ArchicadConnection
from sun_study.archicad.context_model import (
    STOREY_M,
    _clear_previous,
    _contours,
    _datum_storey,
    _Ground,
    _on_the_floor_plan,
    _slabs_through_the_addon,
)
from sun_study.archicad.draw import ensure_layer
from sun_study.archicad.site_analysis import Frame, _clean_ring
from sun_study.site.arcgis import rings_of
from sun_study.site.envelope import (
    DEFAULT_FRONT_SETBACK_M,
    DEFAULT_REACH_M,
    DEFAULT_ZONES,
    Envelope,
    FutureReport,
    Lot,
    envelopes,
)
from sun_study.site.geo import Point, point_in_ring, ring_centroid
from sun_study.site.pipeline import SiteBundle

__all__ = [
    "DEFAULT_FRONT_SETBACK_M",
    "DEFAULT_REACH_M",
    "DEFAULT_ZONES",
    "ID_PREFIX",
    "LAYER",
    "FutureModelReport",
    "FutureOptions",
    "FutureReport",
    "future_envelopes",
    "lots_of",
    "model_future",
]

#: Where the envelopes stand. Not the context model's layer, so a view can
#: show the neighbourhood as it is or as the controls would have it.
LAYER = "LORIINI FUTURE"

#: What every slab's Element ID starts with, and what a rerun deletes by.
ID_PREFIX = "SA FUTURE"


@dataclass(frozen=True)
class FutureOptions:
    """What the envelopes are worked out with; the command line's flags."""

    reach_m: float = DEFAULT_REACH_M
    front_setback_m: float = DEFAULT_FRONT_SETBACK_M
    zones: tuple[str, ...] = DEFAULT_ZONES
    storey_m: float = STOREY_M


@dataclass(frozen=True)
class FutureModelReport:
    """What went into the model."""

    future: FutureReport
    slabs: int
    layer: str
    notes: tuple[str, ...] = ()

    def describe(self) -> str:
        lines = [self.future.describe(), f"  {self.slabs} slabs on {self.layer!r}"]
        lines.extend(f"    {note}" for note in self.notes)
        return "\n".join(lines)


class _Control:
    """A polygon-valued control at a point: the height, the ratio, the zone."""

    def __init__(self, collection: dict[str, Any], field: str) -> None:
        self.zones: list[tuple[tuple[float, float, float, float], list[list[Point]], Any]] = []
        for feature in collection.get("features", []):
            value = (feature.get("properties") or {}).get(field)
            if value is None or value == "":
                continue
            rings = rings_of(feature.get("geometry"))
            if not rings:
                continue
            xs = [x for ring in rings for x, _ in ring]
            ys = [y for ring in rings for _, y in ring]
            self.zones.append(((min(xs), min(ys), max(xs), max(ys)), rings, value))

    def at(self, lon: float, lat: float) -> Any:
        for (x0, y0, x1, y1), rings, value in self.zones:
            if x0 <= lon <= x1 and y0 <= lat <= y1:
                if any(point_in_ring(lon, lat, ring) for ring in rings):
                    return value
        return None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def lots_of(bundle: SiteBundle, frame: Frame) -> list[Lot]:
    """Every lot in the bundle's cadastre, projected, with its controls read
    at its centroid. Strata lots stacked on one footprint are one lot."""
    heights = _Control(bundle.height_of_building, "MAX_B_H")
    ratios = _Control(bundle.floor_space_ratio, "FSR")
    zoning = _Control(bundle.zoning, "SYM_CODE")
    seen: set[tuple[tuple[int, int], ...]] = set()
    lots: list[Lot] = []
    for feature in bundle.all_lots.get("features", []):
        name = str((feature.get("properties") or {}).get("lotidstring") or "").strip()
        for ring in rings_of(feature.get("geometry")):
            projected = _clean_ring(frame.ring(ring))
            if len(projected) < 3:
                continue
            signature = tuple(sorted((round(x / 0.05), round(y / 0.05)) for x, y in projected))
            if signature in seen:
                continue
            seen.add(signature)
            lon, lat = frame.unproject(*ring_centroid(projected))
            zone = zoning.at(lon, lat)
            lots.append(
                Lot(
                    identifier=name or f"LOT {len(lots) + 1}",
                    ring=tuple(projected),
                    zone=str(zone) if zone is not None else None,
                    height_m=_number(heights.at(lon, lat)),
                    fsr=_number(ratios.at(lon, lat)),
                )
            )
    return lots


def future_envelopes(
    bundle: SiteBundle, frame: Frame, options: FutureOptions | None = None
) -> FutureReport:
    """The envelopes the controls allow around the site, in the project frame."""
    options = options or FutureOptions()
    site_rings = [_clean_ring(frame.ring(ring)) for ring in bundle.site_rings]
    return envelopes(
        lots_of(bundle, frame),
        site_rings,
        reach_m=options.reach_m,
        front_setback_m=options.front_setback_m,
        storey_m=options.storey_m,
        zones=options.zones,
    )


def _identifier(envelope: Envelope, tier_index: int) -> str:
    tier = envelope.tiers[tier_index]
    binding = " FSR" if envelope.binding == "fsr" else ""
    return (
        f"{ID_PREFIX} {envelope.lot.identifier} {envelope.storeys} STOREY "
        f"{tier.bottom_m:g}-{tier.top_m:g} m{binding}"
    )


def model_future(
    connection: ArchicadConnection,
    bundle: SiteBundle,
    frame: Frame,
    *,
    options: FutureOptions | None = None,
    found: FutureReport | None = None,
    say: Callable[[str], None] | None = None,
) -> FutureModelReport:
    """The envelopes as slabs, one per tier, on ``LORIINI FUTURE``.

    ``found`` is the report already worked out for the site sheet, so the
    model and the sheet cannot disagree; left out, it is worked out here.
    """
    options = options or FutureOptions()
    _on_the_floor_plan(connection)
    layer = ensure_layer(connection, LAYER)
    notes: list[str] = []
    removed = _clear_previous(connection, prefixes=(ID_PREFIX,))
    if removed:
        notes.append(f"{removed} slabs from the last run removed first.")

    report = found if found is not None else future_envelopes(bundle, frame, options)
    contours = _contours(bundle, frame)
    ground = _Ground(contours) if contours else None
    if ground is None:
        notes.append("no contours in the bundle; the envelopes stand at level zero.")

    slabs: list[dict[str, Any]] = []
    identifiers: list[str] = []
    for envelope in report.envelopes:
        base = ground.at(ring_centroid(list(envelope.footprint))) if ground else 0.0
        for i, tier in enumerate(envelope.tiers):
            for ring in tier.rings:
                slabs.append(
                    {
                        "polygonCoordinates": [{"x": x, "y": y} for x, y in ring],
                        "level": base + tier.bottom_m,
                        "thickness": tier.top_m - tier.bottom_m,
                        "referencePlaneLocation": "Bottom",
                    }
                )
                identifiers.append(_identifier(envelope, i))

    made: list[dict[str, Any]] = []
    if slabs:
        own = _slabs_through_the_addon(
            connection, slabs, identifiers, layer.index, _datum_storey(connection)[0], notes
        )
        if own is None:
            notes.append(
                "the add-on has no CreateSlabs; the future context needs a Loriini build "
                "that does, and was not modelled."
            )
        else:
            made = own
    if say:
        say(f"  {len(made)} future slabs, {len(report.envelopes)} envelopes")
    return FutureModelReport(report, len(made), layer.name, tuple(notes))
