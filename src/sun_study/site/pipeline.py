"""The three bundles, and the runs that fetch them.

A bundle is everything one sheet needs, in longitude and latitude, saved as
JSON beside the run. Saved, because the fetch is the slow and fragile half:
a bundle on disk can be redrawn into a project in seconds, into another
project with a different origin, or after the public Overpass network has
gone away for the afternoon.

Every source is fetched in parallel and every one but the site itself fails
*soft*: a missing bus layer costs the sheet its bus stops and adds a line to
``warnings``, which the run prints. The site lots and the zoning are hard
failures for the context sheet, because a context analysis without a site or
without land use is not one.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from sun_study.site import nsw, osm, transport
from sun_study.site.arcgis import FeatureCollection, empty, rings_of
from sun_study.site.curate import keyword_category
from sun_study.site.geo import (
    Extent,
    Point,
    Ring,
    dissolve,
    extent_for,
    lonlat_to_mercator,
    lonlat_to_mga,
    mercator_to_lonlat,
    mga_zone,
    point_in_ring,
    ring_area,
    scale_factor,
)
from sun_study.site.geocode import Geocode, geocode
from sun_study.site.imagery import Aerial, aerial

__all__ = [
    "MAP_HEIGHT_MM",
    "MAP_WIDTH_MM",
    "SITE_SCALES",
    "ContextBundle",
    "Institution",
    "SiteBundle",
    "SummaryBundle",
    "describe_lots",
    "load_context",
    "load_site",
    "load_summary",
    "lots_area_m2",
    "run_context",
    "run_site",
    "run_summary",
    "save",
    "slug",
]

Log = Callable[[str], None]
T = TypeVar("T")

#: The map field of the office's A1 sheet: 841 mm less the title column.
MAP_WIDTH_MM = 690.0
MAP_HEIGHT_MM = 594.0

#: The site sheet's scale is the first of these the site fits at, with a
#: margin of street around it.
SITE_SCALES: tuple[int, ...] = (200, 250, 300, 400, 500, 600, 750)
SITE_MARGIN_M = 55.0


def slug(address: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", address.lower()).strip("-")


def _now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class Institution:
    """A named complex's own parcel, coloured by what the complex is."""

    category: str
    name: str
    rings: tuple[tuple[Point, ...], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "name": self.name,
            "rings": [[list(p) for p in ring] for ring in self.rings],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Institution:
        return cls(
            str(data["category"]),
            str(data["name"]),
            tuple(tuple((float(x), float(y)) for x, y in ring) for ring in data["rings"]),
        )


@dataclass
class ContextBundle:
    address: str
    matched: tuple[str, ...]
    centre: Point
    """Web Mercator."""
    extent: Extent
    scale: float
    site_lots: FeatureCollection
    all_lots: FeatureCollection = field(default_factory=empty)
    rail_corridors: FeatureCollection = field(default_factory=empty)
    zoning: FeatureCollection = field(default_factory=empty)
    height_of_building: FeatureCollection = field(default_factory=empty)
    heritage: FeatureCollection = field(default_factory=empty)
    roads: tuple[nsw.Road, ...] = ()
    rail_lines: nsw.RailLines = field(default_factory=nsw.RailLines)
    stations: tuple[nsw.NamedPoint, ...] = ()
    park_points: tuple[nsw.NamedPoint, ...] = ()
    park_areas: tuple[nsw.NamedArea, ...] = ()
    complexes: tuple[nsw.NamedPoint, ...] = ()
    institutions: tuple[Institution, ...] = ()
    bus: osm.BusData = field(default_factory=osm.BusData)
    isochrones: tuple[transport.Isochrone, ...] = ()
    aerial: Aerial | None = None
    warnings: tuple[str, ...] = ()
    generated_at: str = field(default_factory=_now)

    @property
    def centre_lonlat(self) -> tuple[float, float]:
        return mercator_to_lonlat(*self.centre)

    @property
    def mga_zone(self) -> int:
        return mga_zone(self.centre_lonlat[0])

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "context",
            "address": self.address,
            "matched": list(self.matched),
            "centre": list(self.centre),
            "extent": self.extent.as_dict(),
            "scale": self.scale,
            "site_lots": self.site_lots,
            "all_lots": self.all_lots,
            "rail_corridors": self.rail_corridors,
            "zoning": self.zoning,
            "height_of_building": self.height_of_building,
            "heritage": self.heritage,
            "roads": [r.as_dict() for r in self.roads],
            "rail_lines": self.rail_lines.as_dict(),
            "stations": [p.as_dict() for p in self.stations],
            "park_points": [p.as_dict() for p in self.park_points],
            "park_areas": [a.as_dict() for a in self.park_areas],
            "complexes": [p.as_dict() for p in self.complexes],
            "institutions": [i.as_dict() for i in self.institutions],
            "bus": self.bus.as_dict(),
            "isochrones": [i.as_dict() for i in self.isochrones],
            "aerial": self.aerial.as_dict() if self.aerial else None,
            "warnings": list(self.warnings),
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContextBundle:
        return cls(
            address=str(data["address"]),
            matched=tuple(str(m) for m in data.get("matched") or []),
            centre=(float(data["centre"][0]), float(data["centre"][1])),
            extent=Extent.from_dict(data["extent"]),
            scale=float(data["scale"]),
            site_lots=data.get("site_lots") or empty(),
            all_lots=data.get("all_lots") or empty(),
            rail_corridors=data.get("rail_corridors") or empty(),
            zoning=data.get("zoning") or empty(),
            height_of_building=data.get("height_of_building") or empty(),
            heritage=data.get("heritage") or empty(),
            roads=tuple(nsw.Road.from_dict(r) for r in data.get("roads") or []),
            rail_lines=nsw.RailLines.from_dict(data.get("rail_lines") or {}),
            stations=tuple(nsw.NamedPoint.from_dict(p) for p in data.get("stations") or []),
            park_points=tuple(nsw.NamedPoint.from_dict(p) for p in data.get("park_points") or []),
            park_areas=tuple(nsw.NamedArea.from_dict(a) for a in data.get("park_areas") or []),
            complexes=tuple(nsw.NamedPoint.from_dict(p) for p in data.get("complexes") or []),
            institutions=tuple(Institution.from_dict(i) for i in data.get("institutions") or []),
            bus=osm.BusData.from_dict(data.get("bus") or {}),
            isochrones=tuple(
                transport.Isochrone.from_dict(i) for i in data.get("isochrones") or []
            ),
            aerial=Aerial.from_dict(data["aerial"]) if data.get("aerial") else None,
            warnings=tuple(str(w) for w in data.get("warnings") or []),
            generated_at=str(data.get("generated_at") or _now()),
        )


@dataclass
class SiteBundle:
    address: str
    matched: tuple[str, ...]
    centre: Point
    extent: Extent
    scale: float
    site_lots: FeatureCollection
    site_rings: tuple[tuple[Point, ...], ...]
    """The site's outer boundary (or boundaries), lots dissolved, lon/lat."""
    all_lots: FeatureCollection = field(default_factory=empty)
    roads: tuple[nsw.Road, ...] = ()
    rail_lines: nsw.RailLines = field(default_factory=nsw.RailLines)
    zoning: FeatureCollection = field(default_factory=empty)
    contours: tuple[nsw.Contour, ...] = ()
    neighbours: tuple[nsw.Neighbour, ...] = ()
    furniture: osm.Furniture = field(default_factory=osm.Furniture)
    bus_stops: tuple[osm.Stop, ...] = ()
    aerial: Aerial | None = None
    warnings: tuple[str, ...] = ()
    generated_at: str = field(default_factory=_now)

    @property
    def centre_lonlat(self) -> tuple[float, float]:
        return mercator_to_lonlat(*self.centre)

    @property
    def mga_zone(self) -> int:
        return mga_zone(self.centre_lonlat[0])

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "site",
            "address": self.address,
            "matched": list(self.matched),
            "centre": list(self.centre),
            "extent": self.extent.as_dict(),
            "scale": self.scale,
            "site_lots": self.site_lots,
            "site_rings": [[list(p) for p in ring] for ring in self.site_rings],
            "all_lots": self.all_lots,
            "roads": [r.as_dict() for r in self.roads],
            "rail_lines": self.rail_lines.as_dict(),
            "zoning": self.zoning,
            "contours": [c.as_dict() for c in self.contours],
            "neighbours": [n.as_dict() for n in self.neighbours],
            "furniture": self.furniture.as_dict(),
            "bus_stops": [s.as_dict() for s in self.bus_stops],
            "aerial": self.aerial.as_dict() if self.aerial else None,
            "warnings": list(self.warnings),
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SiteBundle:
        return cls(
            address=str(data["address"]),
            matched=tuple(str(m) for m in data.get("matched") or []),
            centre=(float(data["centre"][0]), float(data["centre"][1])),
            extent=Extent.from_dict(data["extent"]),
            scale=float(data["scale"]),
            site_lots=data.get("site_lots") or empty(),
            site_rings=tuple(
                tuple((float(x), float(y)) for x, y in ring)
                for ring in data.get("site_rings") or []
            ),
            all_lots=data.get("all_lots") or empty(),
            roads=tuple(nsw.Road.from_dict(r) for r in data.get("roads") or []),
            rail_lines=nsw.RailLines.from_dict(data.get("rail_lines") or {}),
            zoning=data.get("zoning") or empty(),
            contours=tuple(nsw.Contour.from_dict(c) for c in data.get("contours") or []),
            neighbours=tuple(nsw.Neighbour.from_dict(n) for n in data.get("neighbours") or []),
            furniture=osm.Furniture.from_dict(data.get("furniture") or {}),
            bus_stops=tuple(osm.Stop.from_dict(s) for s in data.get("bus_stops") or []),
            aerial=Aerial.from_dict(data["aerial"]) if data.get("aerial") else None,
            warnings=tuple(str(w) for w in data.get("warnings") or []),
            generated_at=str(data.get("generated_at") or _now()),
        )


@dataclass
class SummaryBundle:
    address: str
    site_address: str
    lot_description: str
    site_area_m2: float | None
    controls: nsw.SiteControls
    generated_at: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "summary",
            "address": self.address,
            "site_address": self.site_address,
            "lot_description": self.lot_description,
            "site_area_m2": self.site_area_m2,
            "controls": self.controls.as_dict(),
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SummaryBundle:
        return cls(
            address=str(data["address"]),
            site_address=str(data.get("site_address") or data["address"]),
            lot_description=str(data.get("lot_description") or "-"),
            site_area_m2=float(data["site_area_m2"]) if data.get("site_area_m2") else None,
            controls=nsw.SiteControls.from_dict(data.get("controls") or {}),
            generated_at=str(data.get("generated_at") or _now()),
        )


# -- saving and loading -------------------------------------------------------


def save(bundle: ContextBundle | SiteBundle | SummaryBundle, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle.as_dict()), encoding="utf-8")
    return path


def _read(path: Path, kind: str) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("kind") != kind:
        raise ValueError(f"{path} is not a saved {kind} bundle")
    return data


def load_context(path: Path) -> ContextBundle:
    return ContextBundle.from_dict(_read(path, "context"))


def load_site(path: Path) -> SiteBundle:
    return SiteBundle.from_dict(_read(path, "site"))


def load_summary(path: Path) -> SummaryBundle:
    return SummaryBundle.from_dict(_read(path, "summary"))


# -- the runs ------------------------------------------------------------------


class _Soft:
    """Run fetches in parallel; a failure becomes a warning and a fallback."""

    def __init__(self, log: Log, workers: int = 8) -> None:
        self.log = log
        self.warnings: list[str] = []
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self._fallbacks: dict[Future[Any], tuple[str, Any]] = {}

    def submit(self, name: str, work: Callable[[], T], fallback: Any) -> Future[T]:
        """``fallback`` is what ``take`` answers if ``work`` raises. Typed loosely so an
        empty list reads as the empty list of whatever ``work`` returns."""
        future = self.pool.submit(work)
        self._fallbacks[future] = (name, fallback)
        return future

    def take(self, future: Future[T]) -> T:
        name, fallback = self._fallbacks[future]
        try:
            return future.result()
        except Exception as error:
            line = f"{name} failed: {error}"
            self.warnings.append(line)
            self.log(f"  WARN {line}")
            return fallback  # type: ignore[no-any-return]

    def close(self) -> None:
        self.pool.shutdown(wait=True)


def _geocode(address: str, log: Log) -> Geocode:
    log(f'geocoding "{address}"...')
    found = geocode(address)
    log(f"  matched: {'; '.join(found.matched)}")
    return found


def run_context(
    address: str,
    *,
    scale: float = 3000.0,
    map_width_mm: float = MAP_WIDTH_MM,
    map_height_mm: float = MAP_HEIGHT_MM,
    out_dir: Path | None = None,
    with_aerial: bool = False,
    aerial_px_width: int = 3200,
    log: Log | None = None,
) -> ContextBundle:
    """Everything the context analysis draws, for the sheet extent around the site."""
    say = log or (lambda _line: None)
    found = _geocode(address, say)
    site = nsw.site_lots(found.points)
    if not site["features"]:
        raise ValueError("No cadastral lots found for the site.")

    extent = extent_for(found.centre, map_width_mm, map_height_mm, scale)
    lon, lat = mercator_to_lonlat(*found.centre)
    say("fetching planning layers, cadastre, transport, points of interest, isochrones...")

    soft = _Soft(say)
    try:
        f_rail_corridors = soft.submit(
            "rail corridors", lambda: nsw.rail_corridors(extent), empty()
        )
        f_all_lots = soft.submit("cadastral lots", lambda: nsw.all_lots(extent), empty())
        f_zoning = soft.pool.submit(nsw.zoning, extent)
        f_hob = soft.submit("height of building", lambda: nsw.height_of_building(extent), empty())
        f_heritage = soft.submit("heritage", lambda: nsw.heritage(extent), empty())
        f_roads = soft.submit("roads", lambda: nsw.roads(extent), [])
        f_rail = soft.submit("rail lines", lambda: nsw.rail_lines(extent), nsw.RailLines())
        f_stations = soft.submit("train stations", lambda: nsw.train_stations(extent), [])
        f_parks = soft.submit("park points", lambda: nsw.park_points(extent), [])
        f_areas = soft.submit("named areas", lambda: nsw.named_areas(extent), [])
        f_complexes = soft.submit("complex points", lambda: nsw.complex_points(extent), [])
        f_bus = soft.submit(
            "bus/tram data (OSM)", lambda: osm.bus_data(extent, log=say), osm.BusData()
        )
        f_iso = soft.submit("isochrones", lambda: transport.isochrones(lon, lat), [])
        f_aerial: Future[Aerial | None] | None = None
        if with_aerial and out_dir is not None:
            f_aerial = soft.submit(
                "aerial imagery",
                lambda: aerial(extent, aerial_px_width, out_dir / "data"),
                None,
            )

        zoning = f_zoning.result()
        bus = soft.take(f_bus)
        key = transport.tfnsw_key()
        if key:
            stops = soft.take(
                soft.submit(
                    "bus stops (TfNSW)", lambda: transport.bus_stops_tfnsw(extent, key), None
                )
            )
            if stops is not None:
                bus = osm.BusData(tuple(stops), bus.routes, bus.tram_stops, bus.tram_routes)
                say(f"  bus stops from TfNSW: {len(stops)}")

        complexes = tuple(
            nsw.NamedPoint(p.name, p.lon, p.lat, keyword_category(p.name))
            for p in soft.take(f_complexes)
        )
        fillable = [p for p in complexes if p.category and p.category != "community"]
        institutions: list[Institution] = []
        if fillable:
            properties = soft.take(
                soft.submit(
                    "institution properties",
                    lambda: nsw.properties_at([(p.lon, p.lat) for p in fillable]),
                    empty(),
                )
            )
            # A multipoint query loses which point matched which polygon;
            # recover it by point-in-polygon so each parcel takes its
            # institution's category.
            for feature in properties["features"]:
                rings = rings_of(feature.get("geometry"))
                hit = next(
                    (p for p in fillable if any(point_in_ring(p.lon, p.lat, r) for r in rings)),
                    None,
                )
                if hit and hit.category:
                    institutions.append(
                        Institution(hit.category, hit.name, tuple(tuple(r) for r in rings))
                    )

        roads = soft.take(f_roads)
        stations = soft.take(f_stations)
        parks = soft.take(f_parks)
        areas = soft.take(f_areas)
        catchments = soft.take(f_iso)
        bundle = ContextBundle(
            address=address,
            matched=found.matched,
            centre=found.centre,
            extent=extent,
            scale=scale,
            site_lots=site,
            all_lots=soft.take(f_all_lots),
            rail_corridors=soft.take(f_rail_corridors),
            zoning=zoning,
            height_of_building=soft.take(f_hob),
            heritage=soft.take(f_heritage),
            roads=tuple(roads),
            rail_lines=soft.take(f_rail),
            stations=tuple(stations),
            park_points=tuple(parks),
            park_areas=tuple(areas),
            complexes=complexes,
            institutions=tuple(institutions),
            bus=bus,
            isochrones=tuple(catchments),
            aerial=soft.take(f_aerial) if f_aerial is not None else None,
        )
    finally:
        soft.close()
    bundle.warnings = tuple(soft.warnings)

    say(
        f"  lots:{len(site['features'])} zoning:{len(zoning['features'])} "
        f"roads:{len(bundle.roads)} "
        f"stations:{len(bundle.stations)} parks:{len(bundle.park_points)} "
        f"complexes:{len(complexes)} institutions:{len(institutions)} "
        f"bus stops:{len(bundle.bus.stops)} isochrones:{len(bundle.isochrones)}"
    )
    if out_dir is not None:
        say(f"  saved {save(bundle, out_dir / 'data' / 'context.json')}")
    return bundle


def _site_rings(site: FeatureCollection, zone: int) -> list[Ring]:
    """The site's outer boundary in lon/lat: the lots dissolved, when they can be."""
    lots = [ring for feature in site["features"] for ring in rings_of(feature.get("geometry"))]
    if len(lots) == 1:
        return [lots[0]]
    projected = [[lonlat_to_mga(lon, lat, zone) for lon, lat in ring] for ring in lots]
    merged = dissolve(projected, tolerance=0.02)
    if not merged:
        return lots
    # Back to lon/lat by nearest original vertex, since the projection is not
    # inverted here. Every vertex of a dissolved ring is a vertex of a lot.
    lookup: dict[tuple[int, int], Point] = {}
    for ring, source in zip(projected, lots, strict=True):
        for (e, n), lonlat in zip(ring, source, strict=True):
            lookup[(round(e / 0.02), round(n / 0.02))] = lonlat
    out: list[Ring] = []
    for ring in merged:
        out.append([lookup[(round(e / 0.02), round(n / 0.02))] for e, n in ring])
    return out


def run_site(
    address: str,
    *,
    scale: float | None = None,
    map_width_mm: float = MAP_WIDTH_MM,
    map_height_mm: float = MAP_HEIGHT_MM,
    out_dir: Path | None = None,
    with_aerial: bool = False,
    log: Log | None = None,
) -> SiteBundle:
    """The site-scale bundle: boundary, contours, neighbours, street furniture."""
    say = log or (lambda _line: None)
    found = _geocode(address, say)
    site = nsw.site_lots(found.points)
    if not site["features"]:
        raise ValueError("No cadastral lots found for the site.")

    lon0, _ = mercator_to_lonlat(*found.centre)
    zone = mga_zone(lon0)
    rings = _site_rings(site, zone)

    # Centre on the boundary's bounding box; pick the first scale it fits at.
    merc = [lonlat_to_mercator(lon, lat) for ring in rings for lon, lat in ring]
    xs, ys = [p[0] for p in merc], [p[1] for p in merc]
    centre = ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)
    _, lat = mercator_to_lonlat(*centre)
    k = scale_factor(lat)
    margin = SITE_MARGIN_M * k
    chosen = scale or 0.0
    if not chosen:
        for candidate in SITE_SCALES:
            half_w = map_width_mm / 1000.0 * candidate * k / 2.0
            half_h = map_height_mm / 1000.0 * candidate * k / 2.0
            if (max(xs) - min(xs)) / 2.0 + margin <= half_w and (
                max(ys) - min(ys)
            ) / 2.0 + margin <= half_h:
                chosen = float(candidate)
                break
        if not chosen:
            chosen = float(SITE_SCALES[-1])
    say(f"  site sheet scale 1:{chosen:g}")
    extent = extent_for(centre, map_width_mm, map_height_mm, chosen)

    say("fetching contours, lots, roads, neighbours, street furniture...")
    soft = _Soft(say)
    try:
        f_contours = soft.submit("contours", lambda: nsw.contours(extent), [])
        f_all_lots = soft.submit("lots", lambda: nsw.all_lots(extent), empty())
        f_roads = soft.submit("roads", lambda: nsw.roads(extent), [])
        f_rail = soft.submit("rail lines", lambda: nsw.rail_lines(extent), nsw.RailLines())
        f_zoning = soft.submit("zoning", lambda: nsw.zoning(extent), empty())
        f_neighbours = soft.submit(
            "neighbour addresses", lambda: nsw.neighbour_addresses(extent), []
        )
        f_furniture = soft.submit(
            "street furniture (OSM)", lambda: osm.furniture(extent, log=say), osm.Furniture()
        )
        f_aerial: Future[Aerial | None] | None = None
        if with_aerial and out_dir is not None:
            f_aerial = soft.submit(
                "aerial imagery", lambda: aerial(extent, 3600, out_dir / "data"), None
            )
        key = transport.tfnsw_key()
        f_stops = (
            soft.submit("bus stops (TfNSW)", lambda: transport.bus_stops_tfnsw(extent, key), [])
            if key
            else None
        )

        roads = soft.take(f_roads)
        contours = soft.take(f_contours)
        neighbours = soft.take(f_neighbours)
        stops = soft.take(f_stops) if f_stops is not None else []
        bundle = SiteBundle(
            address=address,
            matched=found.matched,
            centre=centre,
            extent=extent,
            scale=chosen,
            site_lots=site,
            site_rings=tuple(tuple(ring) for ring in rings),
            all_lots=soft.take(f_all_lots),
            roads=tuple(roads),
            rail_lines=soft.take(f_rail),
            zoning=soft.take(f_zoning),
            contours=tuple(contours),
            neighbours=tuple(neighbours),
            furniture=soft.take(f_furniture),
            bus_stops=tuple(stops),
            aerial=soft.take(f_aerial) if f_aerial is not None else None,
        )
    finally:
        soft.close()
    bundle.warnings = tuple(soft.warnings)

    say(
        f"  contours:{len(bundle.contours)} lots:{len(bundle.all_lots['features'])} "
        f"roads:{len(bundle.roads)} neighbours:{len(bundle.neighbours)} "
        f"trees:{len(bundle.furniture.trees)} buildings:{len(bundle.furniture.buildings)} "
        f"bus stops:{len(bundle.bus_stops)}"
    )
    if out_dir is not None:
        say(f"  saved {save(bundle, out_dir / 'data' / 'site.json')}")
    return bundle


def describe_lots(site: FeatureCollection) -> str:
    """``1//DP212120`` and ``2//DP212120`` -> ``LOT 1&2 - DP 212120``."""
    by_plan: dict[str, list[str]] = {}
    for feature in site["features"]:
        identifier = str(feature["properties"].get("lotidstring") or "")
        match = re.match(r"^([^/]+)/([^/]*)/(.+)$", identifier)
        if not match:
            continue
        plan = re.sub(r"^([A-Z]+)", r"\1 ", match.group(3))
        lot = match.group(1) + (f"/{match.group(2)}" if match.group(2) else "")
        by_plan.setdefault(plan, []).append(lot)
    if not by_plan:
        return "-"
    return "; ".join(f"LOT {'&'.join(lots)} - {plan}" for plan, lots in by_plan.items())


def lots_area_m2(site: FeatureCollection, zone: int) -> float | None:
    """Ground area of the lots on the MGA grid: outer rings less holes.

    This is the cadastral boundary, a GIS approximation; the survey figure
    is authoritative and usually a little different.
    """
    total = 0.0
    for feature in site["features"]:
        geometry = feature.get("geometry") or {}
        polygons: Sequence[Any]
        if geometry.get("type") == "Polygon":
            polygons = [geometry["coordinates"]]
        elif geometry.get("type") == "MultiPolygon":
            polygons = geometry["coordinates"]
        else:
            continue
        for polygon in polygons:
            for index, ring in enumerate(polygon):
                projected = [lonlat_to_mga(float(p[0]), float(p[1]), zone) for p in ring]
                total += (1 if index == 0 else -1) * abs(ring_area(projected))
    return total if total > 0 else None


def run_summary(
    address: str, *, out_dir: Path | None = None, log: Log | None = None
) -> SummaryBundle:
    """The development summary: no map, just the lots and the controls at the site."""
    say = log or (lambda _line: None)
    found = _geocode(address, say)
    site = nsw.site_lots(found.points)
    if not site["features"]:
        raise ValueError("No cadastral lots found for the site.")
    say("reading planning controls at the site...")
    controls = nsw.site_controls(found.centre)
    say(f"  {' - '.join(p for p in (controls.epi_name, controls.zone_code) if p) or 'no LEP'}")
    lon, _ = mercator_to_lonlat(*found.centre)
    bundle = SummaryBundle(
        address=address,
        site_address=found.matched[0] if found.matched else address,
        lot_description=describe_lots(site),
        site_area_m2=lots_area_m2(site, mga_zone(lon)),
        controls=controls,
    )
    if out_dir is not None:
        say(f"  saved {save(bundle, out_dir / 'data' / 'summary.json')}")
    return bundle
