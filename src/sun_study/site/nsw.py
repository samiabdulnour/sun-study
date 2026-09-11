"""NSW government layers: cadastre, ePlanning, topography and elevation.

All free, no key, all ArcGIS REST. The layer numbers are the services' own
and are recorded beside each function because a renumbered layer answers
with the wrong data rather than an error.

| need                        | service                                          |
|-----------------------------|--------------------------------------------------|
| lot boundary, rail corridor | ``NSW_Land_Parcel_Property_Theme`` 8, 7, 12      |
| zoning, HOB, FSR, heritage  | ``EPI_Primary_Planning_Layers`` 2, 5, 1, 0, 4    |
| roads, railways             | ``NSW_Transport_Theme`` 5, 7                     |
| parks, complexes, stations  | ``NSW_Features_of_Interest_Category`` 2, 3, 9;   |
|                             | ``NSW_FOI_Transport_Facilities`` 1               |
| contours                    | ``NSW_Elevation_and_Depth_Theme`` 2              |
| address points              | ``NSW_Geocoded_Addressing_Theme`` 1              |
"""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from sun_study.site.arcgis import (
    FeatureCollection,
    empty,
    paths_of,
    query_by_extent,
    query_by_geometry,
    query_by_point,
    rings_of,
)
from sun_study.site.geo import Extent, Point, ring_area, ring_centroid

__all__ = [
    "Contour",
    "NamedArea",
    "NamedPoint",
    "Neighbour",
    "RailLines",
    "Road",
    "SiteControls",
    "all_lots",
    "complex_points",
    "contours",
    "height_of_building",
    "heritage",
    "named_areas",
    "neighbour_addresses",
    "park_points",
    "properties_at",
    "rail_corridors",
    "rail_lines",
    "roads",
    "site_controls",
    "site_lots",
    "train_stations",
    "zoning",
]

PORTAL = "https://portal.spatial.nsw.gov.au/server/rest/services"
CADASTRE = f"{PORTAL}/NSW_Land_Parcel_Property_Theme/FeatureServer"
TRANSPORT = f"{PORTAL}/NSW_Transport_Theme/FeatureServer"
FOI = f"{PORTAL}/NSW_Features_of_Interest_Category/FeatureServer"
FOI_TRANSPORT = f"{PORTAL}/NSW_FOI_Transport_Facilities/FeatureServer"
ELEVATION = f"{PORTAL}/NSW_Elevation_and_Depth_Theme/FeatureServer"
ADDRESSING = f"{PORTAL}/NSW_Geocoded_Addressing_Theme/FeatureServer"
EPI = (
    "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services/Planning/"
    "EPI_Primary_Planning_Layers/MapServer"
)


# -- cadastre and planning ---------------------------------------------------


def site_lots(points: Sequence[Point]) -> FeatureCollection:
    """The lot under each address point, de-duplicated by lot id."""
    seen: set[str] = set()
    features: list[dict[str, Any]] = []
    for point in points:
        collection = query_by_point(f"{CADASTRE}/8", point, "lotidstring,plannumber")
        for feature in collection["features"]:
            identifier = str(
                feature["properties"].get("lotidstring")
                or ((feature.get("geometry") or {}).get("coordinates") or [[None]])[0][0]
            )
            if identifier in seen:
                continue
            seen.add(identifier)
            features.append(feature)
    return {"type": "FeatureCollection", "features": features}


def rail_corridors(extent: Extent) -> FeatureCollection:
    return query_by_extent(f"{CADASTRE}/7", extent, "cadid")


def all_lots(extent: Extent) -> FeatureCollection:
    """Every lot boundary in the extent, drawn as a thin cadastral overlay."""
    return query_by_extent(f"{CADASTRE}/8", extent, "objectid")


def properties_at(points: Sequence[tuple[float, float]]) -> FeatureCollection:
    """Property polygons containing the given ``(lon, lat)`` points, in one query."""
    if not points:
        return empty()
    return query_by_geometry(
        f"{CADASTRE}/12",
        {"points": [[lon, lat] for lon, lat in points], "spatialReference": {"wkid": 4326}},
        "esriGeometryMultipoint",
        "propid",
    )


def zoning(extent: Extent) -> FeatureCollection:
    return query_by_extent(f"{EPI}/2", extent, "SYM_CODE,LAY_CLASS,PURPOSE,EPI_NAME,LGA_NAME")


def height_of_building(extent: Extent) -> FeatureCollection:
    return query_by_extent(f"{EPI}/5", extent, "MAX_B_H,UNITS,LAY_CLASS")


def heritage(extent: Extent) -> FeatureCollection:
    return query_by_extent(f"{EPI}/0", extent, "LAY_CLASS,H_NAME,SIG")


@dataclass(frozen=True)
class SiteControls:
    """The planning controls that apply *at* the site, for the summary table.

    By point, not by extent: a sheet spans several zones, but the summary
    must state the one that governs this lot.
    """

    epi_name: str | None = None
    """``Georges River Local Environmental Plan 2021``"""
    lga_name: str | None = None
    zone_code: str | None = None
    zone_purpose: str | None = None
    max_height_m: float | None = None
    fsr: float | None = None
    """``0.55`` -> printed ``0.55:1``"""
    min_lot_size_m2: float | None = None
    heritage: tuple[tuple[str, str | None], ...] = ()
    """``(name, significance)`` per item mapped on the site."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "epi_name": self.epi_name,
            "lga_name": self.lga_name,
            "zone_code": self.zone_code,
            "zone_purpose": self.zone_purpose,
            "max_height_m": self.max_height_m,
            "fsr": self.fsr,
            "min_lot_size_m2": self.min_lot_size_m2,
            "heritage": [list(item) for item in self.heritage],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SiteControls:
        return cls(
            epi_name=data.get("epi_name"),
            lga_name=data.get("lga_name"),
            zone_code=data.get("zone_code"),
            zone_purpose=data.get("zone_purpose"),
            max_height_m=data.get("max_height_m"),
            fsr=data.get("fsr"),
            min_lot_size_m2=data.get("min_lot_size_m2"),
            heritage=tuple((str(n), s) for n, s in data.get("heritage") or []),
        )


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def site_controls(point: Point) -> SiteControls:
    """One missing control must not cost the whole table, so each is independent."""

    def at(layer: int, fields: str) -> dict[str, Any] | None:
        try:
            features = query_by_point(f"{EPI}/{layer}", point, fields)["features"]
        except Exception:
            return None
        return dict(features[0]["properties"]) if features else None

    def heritage_items() -> list[tuple[str, str | None]]:
        try:
            features = query_by_point(f"{EPI}/0", point, "LAY_CLASS,H_NAME,SIG")["features"]
        except Exception:
            return []
        items: list[tuple[str, str | None]] = []
        for feature in features:
            props = feature["properties"]
            name = str(props.get("H_NAME") or props.get("LAY_CLASS") or "").strip()
            if name:
                items.append((name, str(props["SIG"]) if props.get("SIG") else None))
        return items

    with ThreadPoolExecutor(max_workers=5) as pool:
        zone_f = pool.submit(at, 2, "SYM_CODE,LAY_CLASS,PURPOSE,EPI_NAME,LGA_NAME")
        hob_f = pool.submit(at, 5, "MAX_B_H,UNITS")
        fsr_f = pool.submit(at, 1, "FSR,EPI_NAME")
        lot_f = pool.submit(at, 4, "LOT_SIZE,UNITS")
        her_f = pool.submit(heritage_items)
    zone, hob, fsr, lot = zone_f.result(), hob_f.result(), fsr_f.result(), lot_f.result()
    return SiteControls(
        epi_name=str(zone["EPI_NAME"]) if zone and zone.get("EPI_NAME") else None,
        lga_name=str(zone["LGA_NAME"]) if zone and zone.get("LGA_NAME") else None,
        zone_code=str(zone["SYM_CODE"]) if zone and zone.get("SYM_CODE") else None,
        # PURPOSE is null throughout this layer; LAY_CLASS is the readable name.
        zone_purpose=(
            str((zone or {}).get("LAY_CLASS") or (zone or {}).get("PURPOSE") or "").strip()
        )
        or None,
        max_height_m=_positive((hob or {}).get("MAX_B_H")),
        fsr=_positive((fsr or {}).get("FSR")),
        min_lot_size_m2=_positive((lot or {}).get("LOT_SIZE")),
        heritage=tuple(her_f.result()),
    )


# -- topography ---------------------------------------------------------------


@dataclass(frozen=True)
class Road:
    name: str
    hierarchy: int
    lanes: int | None
    coords: tuple[Point, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "hierarchy": self.hierarchy,
            "lanes": self.lanes,
            "coords": [list(p) for p in self.coords],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Road:
        return cls(
            str(data["name"]),
            int(data.get("hierarchy") or 6),
            int(data["lanes"]) if data.get("lanes") is not None else None,
            tuple((float(x), float(y)) for x, y in data["coords"]),
        )


@dataclass(frozen=True)
class NamedPoint:
    name: str
    lon: float
    lat: float
    category: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "lon": self.lon, "lat": self.lat, "category": self.category}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NamedPoint:
        return cls(str(data["name"]), float(data["lon"]), float(data["lat"]), data.get("category"))


@dataclass(frozen=True)
class NamedArea:
    name: str
    lon: float
    lat: float
    area_m2: float

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "lon": self.lon, "lat": self.lat, "area_m2": self.area_m2}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NamedArea:
        return cls(
            str(data["name"]), float(data["lon"]), float(data["lat"]), float(data["area_m2"])
        )


@dataclass(frozen=True)
class RailLines:
    train: tuple[tuple[Point, ...], ...] = ()
    tram: tuple[tuple[Point, ...], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "train": [[list(p) for p in line] for line in self.train],
            "tram": [[list(p) for p in line] for line in self.tram],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RailLines:
        def lines(key: str) -> tuple[tuple[Point, ...], ...]:
            return tuple(
                tuple((float(x), float(y)) for x, y in line) for line in data.get(key) or []
            )

        return cls(lines("train"), lines("tram"))


@dataclass(frozen=True)
class Contour:
    elevation: float
    coords: tuple[Point, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"elevation": self.elevation, "coords": [list(p) for p in self.coords]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Contour:
        return cls(float(data["elevation"]), tuple((float(x), float(y)) for x, y in data["coords"]))


@dataclass(frozen=True)
class Neighbour:
    house_number: str
    lon: float
    lat: float

    def as_dict(self) -> dict[str, Any]:
        return {"house_number": self.house_number, "lon": self.lon, "lat": self.lat}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Neighbour:
        return cls(str(data["house_number"]), float(data["lon"]), float(data["lat"]))


def roads(extent: Extent) -> list[Road]:
    collection = query_by_extent(
        f"{TRANSPORT}/5",
        extent,
        "roadnamebase,roadnametype,functionhierarchy,lanecount",
        "roadnamebase IS NOT NULL",
    )
    found: list[Road] = []
    for feature in collection["features"]:
        props = feature["properties"]
        name = " ".join(str(p) for p in (props.get("roadnamebase"), props.get("roadnametype")) if p)
        lanes = props.get("lanecount")
        for path in paths_of(feature.get("geometry")):
            found.append(
                Road(
                    name=name,
                    hierarchy=int(props.get("functionhierarchy") or 6),
                    lanes=int(lanes) if lanes else None,
                    coords=tuple(path),
                )
            )
    return found


def rail_lines(extent: Extent) -> RailLines:
    """Railway centrelines: heavy rail, and light rail or tram (``classsubtype`` 2)."""
    collection = query_by_extent(f"{TRANSPORT}/7", extent, "railwayname,classsubtype")
    train: list[tuple[Point, ...]] = []
    tram: list[tuple[Point, ...]] = []
    for feature in collection["features"]:
        target = tram if feature["properties"].get("classsubtype") == 2 else train
        target.extend(tuple(path) for path in paths_of(feature.get("geometry")))
    return RailLines(tuple(train), tuple(tram))


def _named_points(collection: FeatureCollection) -> list[NamedPoint]:
    found: list[NamedPoint] = []
    for feature in collection["features"]:
        geometry = feature.get("geometry")
        name = feature["properties"].get("generalname")
        if not geometry or geometry.get("type") != "Point" or not name:
            continue
        found.append(
            NamedPoint(
                str(name), float(geometry["coordinates"][0]), float(geometry["coordinates"][1])
            )
        )
    return found


def train_stations(extent: Extent) -> list[NamedPoint]:
    return _named_points(query_by_extent(f"{FOI_TRANSPORT}/1", extent, "generalname"))


def park_points(extent: Extent) -> list[NamedPoint]:
    """Parks, reserves, ovals, gardens: labels for the open-space fills."""
    return _named_points(
        query_by_extent(
            f"{FOI}/2", extent, "generalname,generalculturaltype", "generalname IS NOT NULL"
        )
    )


def complex_points(extent: Extent) -> list[NamedPoint]:
    """Named building complexes: schools, hospitals, churches, civic, retail."""
    return _named_points(
        query_by_extent(
            f"{FOI}/3", extent, "generalname,buildingcomplextype", "generalname IS NOT NULL"
        )
    )


def named_areas(extent: Extent) -> list[NamedArea]:
    """Named cultural areas with their centroids, for labelling."""
    collection = query_by_extent(
        f"{FOI}/9", extent, "generalname,generalculturaltype", "generalname IS NOT NULL"
    )
    found: list[NamedArea] = []
    for feature in collection["features"]:
        name = feature["properties"].get("generalname")
        rings = rings_of(feature.get("geometry"))
        if not name or not rings or len(rings[0]) < 3:
            continue
        largest = max(rings, key=lambda r: abs(ring_area(r)))
        lon, lat = ring_centroid(largest)
        # Square degrees to square metres, near enough at Sydney latitudes.
        found.append(NamedArea(str(name), lon, lat, abs(ring_area(largest)) * 111_320 * 92_400))
    return found


# -- site scale -----------------------------------------------------------------


def contours(extent: Extent) -> list[Contour]:
    collection = query_by_extent(f"{ELEVATION}/2", extent, "elevation")
    found: list[Contour] = []
    for feature in collection["features"]:
        elevation = feature["properties"].get("elevation")
        if elevation is None:
            continue
        for path in paths_of(feature.get("geometry")):
            found.append(Contour(float(elevation), tuple(path)))
    return found


def neighbour_addresses(extent: Extent) -> list[Neighbour]:
    collection = query_by_extent(f"{ADDRESSING}/1", extent, "housenumber")
    found: list[Neighbour] = []
    for feature in collection["features"]:
        geometry = feature.get("geometry")
        number = feature["properties"].get("housenumber")
        if not geometry or not number or "/" in str(number):
            continue
        found.append(
            Neighbour(
                str(number), float(geometry["coordinates"][0]), float(geometry["coordinates"][1])
            )
        )
    return found
