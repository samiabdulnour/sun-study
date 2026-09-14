"""What the controls would let a neighbour build: the future context.

A site analysis shows what stands next door today. What a design has to
live beside is what the planning controls allow there tomorrow: a lot zoned
for flats under a 25 m height control is a nine-storey neighbour whether or
not the cottage on it has been sold yet. This module works that envelope
out from the register and the Apartment Design Guide, lot by lot, so it can
be drawn on the site sheet and stood in the model for the shadow diagrams
and the sun views to cast against.

The envelope is the lot, set back, stepped, and capped:

- **Set back.** The ADG (Part 3F, *Visual privacy*) gives the separation a
  building keeps from its side and rear boundaries, by height: 6 m up to
  12 m (four storeys), 9 m up to 25 m (five to eight), 12 m above that
  (nine and more). Those are half the separation between habitable rooms
  and balconies, which is what a boundary shared with a neighbour has to
  provide for. The street setback is a council's, in its DCP, and is a
  setting here with a stated default.
- **Stepped.** The three bands are three tiers: the lower tier at 6 m in,
  the middle at 9 m, the top at 12 m, each as high as the band's storeys
  or the height control, whichever comes first. The step is on a storey
  line, because the ADG names the bands in storeys as well as metres and a
  fourth storey that ends at 12.4 m is still a fourth storey.
- **Capped.** The LEP's height-of-building control caps the height. Its
  floor-space ratio caps the floor area: where the stepped envelope holds
  more gross floor area than the ratio allows, storeys come off the top
  until it does not, and the envelope says the ratio was what bound it.

Which lots: those the reach takes in, zoned where a residential flat
building or shop-top housing is permitted, with a height control. A lot
zoned for houses is left as it is; its future is a house.

Pure geometry in project metres, so a test can measure it without a
register or a licence. The caller projects the lots and reads the controls;
this module does not know where the metres came from.

The inset is a true erosion -- every point at least its edge's setback from
that edge -- traced with the shadow tool's own edge tracer, so a battleaxe
keeps its head, an L keeps its arm, and a re-entrant corner gets the arc a
setback really leaves there. A lot the setbacks meet in the middle of is
left with nothing, as it would be. Amalgamation is not attempted: two
small lots that would be developed together are two small envelopes here,
or none.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

from sun_study.core.edges import refined_rings, signed_area
from sun_study.site.geo import Point, point_in_ring, ring_area, ring_centroid

__all__ = [
    "ADG_SEPARATION",
    "DEFAULT_FRONT_SETBACK_M",
    "DEFAULT_REACH_M",
    "DEFAULT_ZONES",
    "Envelope",
    "FutureReport",
    "Lot",
    "SeparationBand",
    "Tier",
    "envelope_of",
    "envelopes",
    "inset",
    "street_edges",
]


@dataclass(frozen=True)
class SeparationBand:
    """One row of the ADG's separation table, Part 3F design guidance."""

    top_m: float | None
    """The band's top as the ADG states it in metres; ``None`` for the last."""
    storeys_to: int | None
    """The band's top as the ADG states it in storeys, which is what the
    tiers step on: a fourth storey at 3.1 m ends at 12.4 m and is still in
    the first band, not 0.4 m into the second."""
    storeys: str
    """How the ADG names the band: ``up to 4 storeys``."""
    between_m: float
    """Habitable room to habitable room, between buildings."""
    boundary_m: float
    """Half of that, to a side or rear boundary."""


#: ADG Part 3F-1, design guidance: "Minimum required separation distances
#: from buildings to the side and rear boundaries are as follows". The
#: between-building figures are the ones the boundary halves.
ADG_SEPARATION: tuple[SeparationBand, ...] = (
    SeparationBand(12.0, 4, "up to 4 storeys", 12.0, 6.0),
    SeparationBand(25.0, 8, "5 to 8 storeys", 18.0, 9.0),
    SeparationBand(None, None, "9 storeys and over", 24.0, 12.0),
)

#: A council's street setback, from its DCP, which the register does not
#: carry. Six metres is the common figure for a residential flat building in
#: a Sydney DCP; the option exists because it is a council's to set.
DEFAULT_FRONT_SETBACK_M = 6.0

#: How far from the site's boundary a lot is still the site's context.
#: A hundred metres is the lots across the street and the two behind, which
#: is what casts a shadow on the site or takes one from it.
DEFAULT_REACH_M = 100.0

#: The zones a residential flat building or shop-top housing is permitted in
#: under the Standard Instrument: medium and high density residential, the
#: centres and mixed use, and the general residential zone. Low density
#: (R2, R5) and rural are left out: their future is a house.
DEFAULT_ZONES: tuple[str, ...] = ("R1", "R3", "R4", "B1", "B2", "B3", "B4", "E1", "E2", "MU1")

#: The floor-to-floor a storey is counted at; the context model's figure.
STOREY_M = 3.1

#: A tier smaller than this is no building; the envelope stops under it.
LEAST_TIER_M2 = 20.0

#: A lot edge whose midpoint is this close to another lot is shared with it.
SHARED_EDGE_M = 0.3

#: The lattice the erosion is traced on, and how close to the true setback
#: line its vertices are bisected. Half a metre keeps a turned lot's corners
#: within half a metre of square; the vertices themselves land within a
#: centimetre.
CELL_M = 0.5
TOLERANCE_M = 0.01


@dataclass(frozen=True)
class Lot:
    """One cadastral lot, in project metres, with the controls read at it."""

    identifier: str
    ring: tuple[Point, ...]
    zone: str | None
    height_m: float | None
    fsr: float | None

    @property
    def area_m2(self) -> float:
        return abs(ring_area(list(self.ring)))


@dataclass(frozen=True)
class Tier:
    """One step of the envelope: what stands between two heights.

    Usually one ring; two or more where the setbacks cut a lot in two, as
    they do a dumbbell.
    """

    rings: tuple[tuple[Point, ...], ...]
    bottom_m: float
    top_m: float
    boundary_m: float
    """The side and rear setback this tier stands at."""

    @property
    def area_m2(self) -> float:
        return sum(abs(ring_area(list(ring))) for ring in self.rings)


@dataclass(frozen=True)
class Envelope:
    """What the controls allow on one lot."""

    lot: Lot
    tiers: tuple[Tier, ...]
    storeys: int
    height_m: float
    gfa_m2: float
    """Gross floor area of the envelope as modelled, storey by storey."""
    allowed_gfa_m2: float | None
    """What the floor-space ratio allows on the lot, if there is one."""
    binding: str
    """``height`` when the height control set the top, ``fsr`` when the
    floor-space ratio took storeys off it."""
    street_edges: int

    @property
    def footprint(self) -> tuple[Point, ...]:
        """The largest ring of the lowest tier: where the caption goes."""
        return max(self.tiers[0].rings, key=lambda ring: abs(ring_area(list(ring))))

    @property
    def caption(self) -> str:
        """What the site sheet writes on it: ``FUTURE 6 STOREY / 20 m / FSR 2:1``."""
        lines = [f"FUTURE {self.storeys} STOREY", f"{self.height_m:g} m"]
        if self.lot.fsr:
            lines.append(f"FSR {self.lot.fsr:g}:1" + (" BINDS" if self.binding == "fsr" else ""))
        return "\n".join(lines)


@dataclass(frozen=True)
class FutureReport:
    """What was worked out, and what was passed over and why."""

    envelopes: tuple[Envelope, ...]
    beyond_reach: int
    on_site: int
    other_zones: int
    no_height: int
    too_small: int

    def describe(self) -> str:
        lines = [
            f"  future context: {len(self.envelopes)} envelopes, "
            f"{sum(1 for e in self.envelopes if e.binding == 'fsr')} capped by the FSR"
        ]
        passed = [
            f"{count} {reason}"
            for count, reason in (
                (self.on_site, "on the site"),
                (self.other_zones, "zoned for houses or otherwise"),
                (self.no_height, "without a height control"),
                (self.too_small, "too small for a building once set back"),
            )
            if count
        ]
        if passed:
            lines.append("    left out: " + ", ".join(passed))
        return "\n".join(lines)


# -- geometry ------------------------------------------------------------------------


def _anticlockwise(ring: Sequence[Point]) -> list[Point]:
    points: list[Point] = [(float(p[0]), float(p[1])) for p in ring]
    if len(points) > 1 and math.dist(points[0], points[-1]) <= 1e-9:
        points.pop()
    if ring_area(points) < 0:
        points.reverse()
    return points


def _segment_distance(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    if l2 == 0.0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def _edges(ring: Sequence[Point]) -> list[tuple[Point, Point]]:
    return [(ring[i], ring[(i + 1) % len(ring)]) for i in range(len(ring))]


def _ring_distance(ring: Sequence[Point], other: Sequence[Point]) -> float:
    """Least distance between two rings' boundaries."""
    best = math.inf
    for p in ring:
        for a, b in _edges(other):
            best = min(best, _segment_distance(p, a, b))
    for p in other:
        for a, b in _edges(ring):
            best = min(best, _segment_distance(p, a, b))
    return best


def street_edges(ring: Sequence[Point], others: Iterable[Sequence[Point]]) -> list[bool]:
    """Which edges of a lot face the street: the ones no other lot shares.

    A lot's boundary is either another lot's or the road reserve's (or a
    park's, or the railway's, which set back the same way). An edge whose
    midpoint lies on another lot's boundary is shared; the rest front the
    street. Nothing about the roads themselves is needed, which matters
    because the cadastre is complete and the road centrelines are not.
    """
    edges = _edges(list(ring))
    nearby = list(others)
    result: list[bool] = []
    for a, b in edges:
        mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        shared = any(
            _segment_distance(mid, c, d) <= SHARED_EDGE_M
            for other in nearby
            for c, d in _edges(list(other))
        )
        result.append(not shared)
    return result


def inset(
    ring: Sequence[Point],
    setbacks: Sequence[float],
    *,
    cell_m: float = CELL_M,
    tolerance_m: float = TOLERANCE_M,
) -> list[list[Point]]:
    """The lot set back from each of its edges by that edge's distance.

    A true erosion: a point stays when it is inside the lot and at least the
    setback from every edge, measured to the edge itself and not to its
    line, so a short edge sets back only what is near it. The outline is
    traced on a lattice by the shadow tool's edge tracer, whose vertices
    are bisected onto the setback line and whose corners are recovered.

    Returns the outlines, anticlockwise, without their closing point:
    usually one, several where the setbacks cut the lot, none where they
    meet in the middle.
    """
    points = _anticlockwise(ring)
    if len(points) < 3 or len(setbacks) != len(points):
        return []
    corners = np.asarray(points, dtype=np.float64)
    distances = np.asarray(setbacks, dtype=np.float64)
    starts = corners
    ends = np.roll(corners, -1, axis=0)
    spans = ends - starts
    lengths = np.einsum("ij,ij->i", spans, spans)
    lengths[lengths == 0.0] = 1.0

    def kept(sample: np.ndarray) -> np.ndarray:
        p = np.asarray(sample, dtype=np.float64)[:, :2]
        inside = np.zeros(len(p), dtype=bool)
        for (ax, ay), (bx, by) in zip(starts, ends, strict=True):
            if by == ay:
                continue
            crosses = (ay > p[:, 1]) != (by > p[:, 1])
            at_x = ax + (p[:, 1] - ay) * (bx - ax) / (by - ay)
            inside ^= crosses & (p[:, 0] < at_x)
        # Distance to each edge as a segment, all edges at once.
        rel = p[:, None, :] - starts[None, :, :]
        t = np.clip(np.einsum("pej,ej->pe", rel, spans) / lengths[None, :], 0.0, 1.0)
        nearest = starts[None, :, :] + t[:, :, None] * spans[None, :, :]
        far_enough = np.linalg.norm(p[:, None, :] - nearest, axis=2) >= distances[None, :]
        return np.asarray(inside & far_enough.all(axis=1))

    low = corners.min(axis=0) - cell_m
    high = corners.max(axis=0) + cell_m
    xs = np.arange(low[0], high[0] + cell_m, cell_m)
    ys = np.arange(low[1], high[1] + cell_m, cell_m)
    if len(xs) < 2 or len(ys) < 2:
        return []
    grid_x, grid_y = np.meshgrid(xs, ys, indexing="ij")
    lattice = np.column_stack([grid_x.ravel(), grid_y.ravel(), np.zeros(grid_x.size)])
    mask = kept(lattice)
    if not mask.any():
        return []
    traced = refined_rings(
        lattice, mask, (len(xs), len(ys)), shaded_at=kept, tolerance_m=tolerance_m
    )
    outlines: list[list[Point]] = []
    for found in traced:
        if signed_area(found) <= 0:
            continue  # an erosion has no holes; a clockwise ring is noise
        cleaned = _anticlockwise([(float(x), float(y)) for x, y in found])
        if len(cleaned) >= 3:
            outlines.append(cleaned)
    return outlines


# -- the envelopes -------------------------------------------------------------------


def _permitted(zone: str | None, zones: Sequence[str]) -> bool:
    code = (zone or "").upper().strip()
    return bool(code) and any(code == z.upper() or code.startswith(z.upper()) for z in zones)


def _stepped(
    lot: Lot,
    faces_street: Sequence[bool],
    height_m: float,
    front_setback_m: float,
    storey_m: float,
) -> list[Tier]:
    tiers: list[Tier] = []
    bottom = 0.0
    for band in ADG_SEPARATION:
        if bottom >= height_m:
            break
        top = height_m
        if band.storeys_to is not None:
            top = min(band.storeys_to * storey_m, height_m)
        setbacks = [front_setback_m if street else band.boundary_m for street in faces_street]
        rings = [
            tuple(ring)
            for ring in inset(lot.ring, setbacks)
            if abs(ring_area(ring)) >= LEAST_TIER_M2
        ]
        if not rings:
            break
        tiers.append(Tier(tuple(rings), bottom, top, band.boundary_m))
        bottom = top
    return tiers


def _gfa(tiers: Sequence[Tier], storeys: int, storey_m: float) -> float:
    """Floor area storey by storey: each storey stands on the tier it starts in."""
    total = 0.0
    for n in range(storeys):
        floor = n * storey_m
        for tier in tiers:
            if tier.bottom_m <= floor < tier.top_m:
                total += tier.area_m2
                break
    return total


def _capped(tiers: Sequence[Tier], height_m: float) -> tuple[Tier, ...]:
    kept: list[Tier] = []
    for tier in tiers:
        if tier.bottom_m >= height_m:
            break
        kept.append(Tier(tier.rings, tier.bottom_m, min(tier.top_m, height_m), tier.boundary_m))
    return tuple(kept)


def envelope_of(
    lot: Lot,
    neighbours: Iterable[Sequence[Point]],
    *,
    front_setback_m: float = DEFAULT_FRONT_SETBACK_M,
    storey_m: float = STOREY_M,
) -> Envelope | None:
    """The envelope on one lot, or ``None`` when nothing fits once set back."""
    if lot.height_m is None or lot.height_m <= 0 or len(lot.ring) < 3:
        return None
    ring = tuple(_anticlockwise(lot.ring))
    control_m = lot.height_m
    lot = Lot(lot.identifier, ring, lot.zone, control_m, lot.fsr)
    faces_street = street_edges(ring, neighbours)
    storeys = int(control_m / storey_m)
    if storeys < 1:
        return None
    height_m = storeys * storey_m
    tiers = _stepped(lot, faces_street, height_m, front_setback_m, storey_m)
    if not tiers:
        return None
    # The tiers reach as high as they can; the storeys are then counted
    # against the floor-space ratio and the top ones come off if it binds.
    reachable = tiers[-1].top_m
    storeys = min(storeys, round(reachable / storey_m))
    if storeys < 1:
        return None
    allowed = lot.fsr * lot.area_m2 if lot.fsr else None
    binding = "height"
    if allowed is not None:
        while storeys > 1 and _gfa(tiers, storeys, storey_m) > allowed:
            storeys -= 1
            binding = "fsr"
    height_m = storeys * storey_m
    return Envelope(
        lot=lot,
        tiers=_capped(tiers, height_m),
        storeys=storeys,
        height_m=height_m,
        gfa_m2=_gfa(tiers, storeys, storey_m),
        allowed_gfa_m2=allowed,
        binding=binding,
        street_edges=sum(faces_street),
    )


def envelopes(
    lots: Sequence[Lot],
    site_rings: Sequence[Sequence[Point]],
    *,
    reach_m: float = DEFAULT_REACH_M,
    front_setback_m: float = DEFAULT_FRONT_SETBACK_M,
    storey_m: float = STOREY_M,
    zones: Sequence[str] = DEFAULT_ZONES,
) -> FutureReport:
    """The envelopes on every lot within reach of the site that the controls
    would let a flat building stand on. The site's own lots are left out:
    their future is the proposal."""
    sites = [_anticlockwise(ring) for ring in site_rings if len(ring) >= 3]
    rings = [_anticlockwise(lot.ring) for lot in lots]
    made: list[Envelope] = []
    beyond = on_site = other = no_height = too_small = 0
    for i, lot in enumerate(lots):
        ring = rings[i]
        if len(ring) < 3:
            continue
        centre = ring_centroid(ring)
        if any(point_in_ring(centre[0], centre[1], site) for site in sites):
            on_site += 1
            continue
        if sites and min(_ring_distance(ring, site) for site in sites) > reach_m:
            beyond += 1
            continue
        if not _permitted(lot.zone, zones):
            other += 1
            continue
        if lot.height_m is None or lot.height_m <= 0:
            no_height += 1
            continue
        nearby = [
            other_ring
            for j, other_ring in enumerate(rings)
            if j != i and len(other_ring) >= 3 and _boxes_touch(ring, other_ring)
        ]
        built = envelope_of(lot, nearby, front_setback_m=front_setback_m, storey_m=storey_m)
        if built is None:
            too_small += 1
            continue
        made.append(built)
    return FutureReport(tuple(made), beyond, on_site, other, no_height, too_small)


def _boxes_touch(a: Sequence[Point], b: Sequence[Point], slack: float = 1.0) -> bool:
    ax0, ax1 = min(p[0] for p in a) - slack, max(p[0] for p in a) + slack
    ay0, ay1 = min(p[1] for p in a) - slack, max(p[1] for p in a) + slack
    return not (
        max(p[0] for p in b) < ax0
        or min(p[0] for p in b) > ax1
        or max(p[1] for p in b) < ay0
        or min(p[1] for p in b) > ay1
    )
