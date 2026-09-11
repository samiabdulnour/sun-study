"""Coordinates and plane geometry for the site analysis.

Three frames, and the reason for each:

* **Longitude and latitude (WGS84 / GDA2020).** What every service answers
  in, and what a bundle is stored in, so a saved run can be redrawn into a
  project with a different origin.
* **Web Mercator (EPSG:3857).** What the ArcGIS services take an envelope in.
  A rectangle on the sheet is a rectangle here, stretched by ``1/cos(lat)``,
  which ``scale_factor`` undoes so a printed scale is true on the ground.
* **MGA2020.** The NSW survey grid: transverse Mercator on GRS80, one zone
  per six degrees of longitude, in metres. Geometry is projected to it before
  it is drawn, because a metre here is a metre on the ground to a few parts
  per ten thousand, and a survey lands on the same grid.

The transverse Mercator arithmetic is Redfearn's series, as published in the
GDA Technical Manual; it is accurate to a millimetre within a zone and needs
no library, which matters on a machine that cannot install one.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise

__all__ = [
    "Extent",
    "LonLat",
    "Point",
    "Ring",
    "clip_ring",
    "convex_hull",
    "dissolve",
    "extent_for",
    "lonlat_to_mercator",
    "lonlat_to_mga",
    "mercator_to_lonlat",
    "mga_to_lonlat",
    "mga_zone",
    "point_in_ring",
    "polyline_length",
    "ring_area",
    "ring_centroid",
    "scale_factor",
]

Point = tuple[float, float]
#: A snapped vertex, as ``dissolve`` keys it.
_Key = tuple[int, int]
LonLat = tuple[float, float]
Ring = list[Point]

#: Half the Web Mercator world width, in metres.
_R = 20037508.342789244


def lonlat_to_mercator(lon: float, lat: float) -> Point:
    return (
        lon * _R / 180.0,
        math.log(math.tan((90.0 + lat) * math.pi / 360.0)) / (math.pi / 180.0) * (_R / 180.0),
    )


def mercator_to_lonlat(x: float, y: float) -> LonLat:
    lon = x / _R * 180.0
    lat = math.atan(math.exp(y / _R * 180.0 * math.pi / 180.0)) * 360.0 / math.pi - 90.0
    return lon, lat


def scale_factor(lat: float) -> float:
    """Mercator metres per ground metre at this latitude."""
    return 1.0 / math.cos(math.radians(lat))


@dataclass(frozen=True)
class Extent:
    """An axis-aligned window in Web Mercator metres."""

    xmin: float
    ymin: float
    xmax: float
    ymax: float

    @property
    def width(self) -> float:
        return self.xmax - self.xmin

    @property
    def height(self) -> float:
        return self.ymax - self.ymin

    @property
    def centre(self) -> Point:
        return ((self.xmin + self.xmax) / 2.0, (self.ymin + self.ymax) / 2.0)

    def lonlat_bbox(self) -> tuple[float, float, float, float]:
        """``(south, west, north, east)`` -- the order Overpass wants."""
        west, south = mercator_to_lonlat(self.xmin, self.ymin)
        east, north = mercator_to_lonlat(self.xmax, self.ymax)
        return south, west, north, east

    def esri(self) -> dict[str, object]:
        return {
            "xmin": self.xmin,
            "ymin": self.ymin,
            "xmax": self.xmax,
            "ymax": self.ymax,
            "spatialReference": {"wkid": 3857},
        }

    def as_dict(self) -> dict[str, float]:
        return {"xmin": self.xmin, "ymin": self.ymin, "xmax": self.xmax, "ymax": self.ymax}

    @classmethod
    def from_dict(cls, data: dict[str, float]) -> Extent:
        return cls(
            float(data["xmin"]), float(data["ymin"]), float(data["xmax"]), float(data["ymax"])
        )


def extent_for(centre: Point, width_mm: float, height_mm: float, scale: float) -> Extent:
    """The window a map field of ``width_mm`` by ``height_mm`` at 1:``scale``
    covers, centred on ``centre``, with the mercator stretch put in so the
    printed scale is true on the ground."""
    _, lat = mercator_to_lonlat(*centre)
    k = scale_factor(lat)
    half_w = width_mm / 1000.0 * scale * k / 2.0
    half_h = height_mm / 1000.0 * scale * k / 2.0
    return Extent(centre[0] - half_w, centre[1] - half_h, centre[0] + half_w, centre[1] + half_h)


# -- MGA2020 -----------------------------------------------------------------

#: GRS80, the ellipsoid under both GDA94 and GDA2020.
_A = 6378137.0
_F = 1.0 / 298.257222101
_E2 = 2.0 * _F - _F * _F
_K0 = 0.9996
_FALSE_EASTING = 500000.0
_FALSE_NORTHING = 10000000.0


def mga_zone(lon: float) -> int:
    """The MGA zone this longitude falls in: 151 E is zone 56."""
    return int(lon // 6.0) + 31


def lonlat_to_mga(lon: float, lat: float, zone: int) -> Point:
    """Redfearn's transverse Mercator, as the GDA Technical Manual gives it.

    ``zone`` is passed rather than derived so a site straddling a zone edge
    is projected whole into one grid rather than split across two.
    """
    phi = math.radians(lat)
    omega = math.radians(lon - (zone * 6.0 - 183.0))
    e2 = _E2
    e4 = e2 * e2
    e6 = e4 * e2

    sin_phi = math.sin(phi)
    cos_phi = math.cos(phi)
    t = math.tan(phi)
    t2 = t * t
    t4 = t2 * t2
    t6 = t4 * t2

    # Meridian distance.
    a0 = 1.0 - e2 / 4.0 - 3.0 * e4 / 64.0 - 5.0 * e6 / 256.0
    a2 = 3.0 / 8.0 * (e2 + e4 / 4.0 + 15.0 * e6 / 128.0)
    a4 = 15.0 / 256.0 * (e4 + 3.0 * e6 / 4.0)
    a6 = 35.0 * e6 / 3072.0
    m = _A * (
        a0 * phi - a2 * math.sin(2.0 * phi) + a4 * math.sin(4.0 * phi) - a6 * math.sin(6.0 * phi)
    )

    denominator = 1.0 - e2 * sin_phi * sin_phi
    rho = _A * (1.0 - e2) / denominator**1.5
    nu = _A / math.sqrt(denominator)
    psi = nu / rho
    psi2 = psi * psi
    psi3 = psi2 * psi
    psi4 = psi3 * psi

    w2 = omega * omega
    c2 = cos_phi * cos_phi

    easting = (
        _K0
        * nu
        * omega
        * cos_phi
        * (
            1.0
            + w2 * c2 * (psi - t2) / 6.0
            + w2
            * w2
            * c2
            * c2
            * (4.0 * psi3 * (1.0 - 6.0 * t2) + psi2 * (1.0 + 8.0 * t2) - psi * 2.0 * t2 + t4)
            / 120.0
            + w2 * w2 * w2 * c2 * c2 * c2 * (61.0 - 479.0 * t2 + 179.0 * t4 - t6) / 5040.0
        )
    )
    northing = _K0 * (
        m
        + nu * sin_phi * w2 * cos_phi / 2.0
        + nu * sin_phi * w2 * w2 * cos_phi * c2 * (4.0 * psi2 + psi - t2) / 24.0
        + nu
        * sin_phi
        * w2
        * w2
        * w2
        * cos_phi
        * c2
        * c2
        * (
            8.0 * psi4 * (11.0 - 24.0 * t2)
            - 28.0 * psi3 * (1.0 - 6.0 * t2)
            + psi2 * (1.0 - 32.0 * t2)
            - psi * 2.0 * t2
            + t4
        )
        / 720.0
        + nu
        * sin_phi
        * w2
        * w2
        * w2
        * w2
        * cos_phi
        * c2
        * c2
        * c2
        * (1385.0 - 3111.0 * t2 + 543.0 * t4 - t6)
        / 40320.0
    )
    return easting + _FALSE_EASTING, northing + _FALSE_NORTHING


# -- plane geometry ------------------------------------------------------------


def mga_to_lonlat(east: float, north: float, zone: int) -> LonLat:
    """The inverse of ``lonlat_to_mga``, by iteration on the forward formula.

    Four Newton steps from a flat-earth first guess land within a tenth of a
    millimetre anywhere in a zone; the closed-form inverse is a page of
    series for the same answer.
    """
    lon0 = zone * 6.0 - 183.0
    lat = math.degrees((north - _FALSE_NORTHING) / (_K0 * _A))
    lon = lon0 + math.degrees((east - _FALSE_EASTING) / (_K0 * _A * math.cos(math.radians(lat))))
    for _ in range(6):
        e, n = lonlat_to_mga(lon, lat, zone)
        de, dn = east - e, north - n
        if abs(de) < 1e-5 and abs(dn) < 1e-5:
            break
        h = 1e-5
        e_lon, n_lon = lonlat_to_mga(lon + h, lat, zone)
        e_lat, n_lat = lonlat_to_mga(lon, lat + h, zone)
        j11, j21 = (e_lon - e) / h, (n_lon - n) / h
        j12, j22 = (e_lat - e) / h, (n_lat - n) / h
        det = j11 * j22 - j12 * j21
        lon += (j22 * de - j12 * dn) / det
        lat += (-j21 * de + j11 * dn) / det
    return lon, lat


def clip_ring(ring: Sequence[Point], xmin: float, ymin: float, xmax: float, ymax: float) -> Ring:
    """The part of a polygon inside an axis-aligned box (Sutherland-Hodgman).

    Empty when nothing of it is inside. A ring that crosses the box comes
    back with new vertices on the box's sides, which is what a block at the
    edge of a model needs: an outline that ends where the model does.
    """
    out = list(ring)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    # Each side: which coordinate it bounds, the bound, and its sign.
    for axis, bound, keep_below in (
        (0, xmin, False),
        (0, xmax, True),
        (1, ymin, False),
        (1, ymax, True),
    ):
        if not out:
            return []
        kept: list[Point] = []
        previous = out[-1]
        for point in out:
            here = _within(point, axis, bound, keep_below)
            there = _within(previous, axis, bound, keep_below)
            if here:
                if not there:
                    kept.append(_cross(previous, point, axis, bound))
                kept.append(point)
            elif there:
                kept.append(_cross(previous, point, axis, bound))
            previous = point
        out = kept
    return out if len(out) >= 3 else []


def _within(p: Point, axis: int, bound: float, keep_below: bool) -> bool:
    return p[axis] <= bound if keep_below else p[axis] >= bound


def _cross(a: Point, b: Point, axis: int, bound: float) -> Point:
    t = (bound - a[axis]) / (b[axis] - a[axis])
    if axis == 0:
        return (bound, a[1] + t * (b[1] - a[1]))
    return (a[0] + t * (b[0] - a[0]), bound)


def convex_hull(points: Iterable[Point]) -> Ring:
    """The convex hull, anticlockwise, by Andrew's monotone chain."""
    unique = sorted(set(points))
    if len(unique) < 3:
        return list(unique)

    def turn(o: Point, a: Point, b: Point) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[Point] = []
    for p in unique:
        while len(lower) >= 2 and turn(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[Point] = []
    for p in reversed(unique):
        while len(upper) >= 2 and turn(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def ring_area(ring: Sequence[Point]) -> float:
    """Signed shoelace area; positive anticlockwise. A closing point is optional."""
    if len(ring) < 3:
        return 0.0
    total = 0.0
    for (x1, y1), (x2, y2) in zip(ring, [*ring[1:], ring[0]], strict=True):
        total += x1 * y2 - x2 * y1
    return total / 2.0


def ring_centroid(ring: Sequence[Point]) -> Point:
    """Area centroid; the first point when the ring is degenerate."""
    area = ring_area(ring)
    if abs(area) < 1e-12 or len(ring) < 3:
        xs = [x for x, _ in ring]
        ys = [y for _, y in ring]
        return (sum(xs) / len(xs), sum(ys) / len(ys)) if ring else (0.0, 0.0)
    cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(ring, [*ring[1:], ring[0]], strict=True):
        cross = x1 * y2 - x2 * y1
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    return cx / (6.0 * area), cy / (6.0 * area)


def point_in_ring(x: float, y: float, ring: Sequence[Point]) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def polyline_length(points: Sequence[Point]) -> float:
    return sum(math.dist(a, b) for a, b in pairwise(points))


def dissolve(rings: Iterable[Sequence[Point]], tolerance: float = 0.02) -> list[Ring]:
    """Merge touching polygons into their outer boundaries.

    Cadastral lots that make one site share their common boundaries exactly,
    so the outline of the site is every edge that appears once. Edges are
    first split at any vertex of another ring lying on them -- one lot's long
    side is often two of its neighbour's -- then the edges seen twice are
    dropped, and what remains is chained into rings.

    Returns the rings found, largest first. Returns nothing at all when the
    remaining edges do not chain cleanly (three edges meeting at a point,
    say), because a guessed outline of a site is worse than the lots drawn
    separately; the caller falls back to those.
    """

    def key(point: Point) -> _Key:
        return (round(point[0] / tolerance), round(point[1] / tolerance))

    def coordinates(k: _Key) -> Point:
        return (k[0] * tolerance, k[1] * tolerance)

    cleaned: list[list[_Key]] = []
    for ring in rings:
        keys: list[_Key] = []
        for point in ring:
            k = key(point)
            if not keys or keys[-1] != k:
                keys.append(k)
        if len(keys) > 1 and keys[0] == keys[-1]:
            keys.pop()
        if len(keys) >= 3:
            cleaned.append(keys)
    if not cleaned:
        return []

    vertices = {k for keys in cleaned for k in keys}

    def between(a: _Key, b: _Key, v: _Key) -> float | None:
        """Position of ``v`` along ``ab`` in (0, 1) when it lies on the segment."""
        ax, ay = coordinates(a)
        bx, by = coordinates(b)
        vx, vy = coordinates(v)
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        if length2 == 0.0:
            return None
        t = ((vx - ax) * dx + (vy - ay) * dy) / length2
        if t <= 1e-9 or t >= 1.0 - 1e-9:
            return None
        px, py = ax + t * dx, ay + t * dy
        if math.hypot(px - vx, py - vy) > tolerance:
            return None
        return t

    counts: dict[frozenset[_Key], int] = {}
    for keys in cleaned:
        for a, b in zip(keys, [*keys[1:], keys[0]], strict=True):
            splits = sorted(
                (t, v) for v in vertices if v not in (a, b) and (t := between(a, b, v)) is not None
            )
            chain = [a, *(v for _, v in splits), b]
            for p, q in pairwise(chain):
                if p != q:
                    edge = frozenset((p, q))
                    counts[edge] = counts.get(edge, 0) + 1

    remaining = [edge for edge, count in counts.items() if count == 1]
    neighbours: dict[_Key, list[_Key]] = {}
    for edge in remaining:
        p, q = tuple(edge)
        neighbours.setdefault(p, []).append(q)
        neighbours.setdefault(q, []).append(p)
    if any(len(others) != 2 for others in neighbours.values()):
        return []

    found: list[Ring] = []
    unused = set(remaining)
    while unused:
        start_edge = next(iter(unused))
        start, current = tuple(start_edge)
        unused.discard(start_edge)
        ring_keys = [start, current]
        previous = start
        while current != start:
            a, b = neighbours[current]
            following = b if a == previous else a
            unused.discard(frozenset((current, following)))
            previous, current = current, following
            if current != start:
                ring_keys.append(current)
            if len(ring_keys) > len(remaining) + 1:
                return []
        found.append([coordinates(k) for k in ring_keys])
    found.sort(key=lambda ring: -abs(ring_area(ring)))
    return found
