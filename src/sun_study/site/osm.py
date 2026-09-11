"""OpenStreetMap through Overpass: what NSW open data lacks.

Bus stops and routes for the context sheet; trees, hydrants, poles, lamps,
one-way streets, driveways, kerbside parking, utilities and building
footprints for the site sheet. Failure is soft: the sheet is drawn without.

Building footprints carry their ring and any recorded storey count, which is
what the next step -- a massing of the neighbours in the model -- will read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sun_study.site.geo import Extent, Point
from sun_study.site.http import Log, overpass

__all__ = ["Building", "BusData", "Furniture", "Stop", "Utility", "bus_data", "furniture"]


@dataclass(frozen=True)
class Stop:
    lon: float
    lat: float
    name: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"lon": self.lon, "lat": self.lat, "name": self.name}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Stop:
        return cls(float(data["lon"]), float(data["lat"]), data.get("name"))


Line = tuple[Point, ...]


def _lines(data: list[Any]) -> tuple[Line, ...]:
    return tuple(tuple((float(x), float(y)) for x, y in line) for line in data or [])


def _lines_out(lines: tuple[Line, ...]) -> list[list[list[float]]]:
    return [[list(p) for p in line] for line in lines]


@dataclass(frozen=True)
class BusData:
    stops: tuple[Stop, ...] = ()
    routes: tuple[Line, ...] = ()
    tram_stops: tuple[Stop, ...] = ()
    tram_routes: tuple[Line, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "stops": [s.as_dict() for s in self.stops],
            "routes": _lines_out(self.routes),
            "tram_stops": [s.as_dict() for s in self.tram_stops],
            "tram_routes": _lines_out(self.tram_routes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BusData:
        return cls(
            tuple(Stop.from_dict(s) for s in data.get("stops") or []),
            _lines(data.get("routes") or []),
            tuple(Stop.from_dict(s) for s in data.get("tram_stops") or []),
            _lines(data.get("tram_routes") or []),
        )


def bus_data(extent: Extent, *, log: Log | None = None) -> BusData:
    south, west, north, east = extent.lonlat_bbox()
    bbox = f"{south},{west},{north},{east}"
    query = f"""[out:json][timeout:60];
(
  node["highway"="bus_stop"]({bbox});
  node["railway"="tram_stop"]({bbox});
  relation["route"="bus"]({bbox});
  relation["route"~"^(tram|light_rail)$"]({bbox});
);
out geom;"""
    data = overpass(query, log=log)

    stops: list[Stop] = []
    tram_stops: list[Stop] = []
    routes: list[Line] = []
    tram_routes: list[Line] = []
    seen_bus: set[int] = set()
    seen_tram: set[int] = set()
    for element in data.get("elements") or []:
        tags = element.get("tags") or {}
        if element.get("type") == "node":
            stop = Stop(float(element["lon"]), float(element["lat"]), tags.get("name"))
            if tags.get("highway") == "bus_stop":
                stops.append(stop)
            elif tags.get("railway") == "tram_stop":
                tram_stops.append(stop)
        elif element.get("type") == "relation":
            is_bus = tags.get("route") == "bus"
            is_tram = tags.get("route") in ("tram", "light_rail")
            if not is_bus and not is_tram:
                continue
            seen = seen_bus if is_bus else seen_tram
            target = routes if is_bus else tram_routes
            for member in element.get("members") or []:
                if member.get("type") != "way" or not member.get("geometry"):
                    continue
                if member.get("ref") in seen:
                    continue
                seen.add(member["ref"])
                target.append(tuple((float(g["lon"]), float(g["lat"])) for g in member["geometry"]))
    return BusData(tuple(stops), tuple(routes), tuple(tram_stops), tuple(tram_routes))


@dataclass(frozen=True)
class Building:
    """A footprint, with its storeys and height where OSM records them."""

    lon: float
    lat: float
    ring: Line
    levels: int | None = None
    height_m: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "lon": self.lon,
            "lat": self.lat,
            "ring": [list(p) for p in self.ring],
            "levels": self.levels,
            "height_m": self.height_m,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Building:
        return cls(
            float(data["lon"]),
            float(data["lat"]),
            tuple((float(x), float(y)) for x, y in data.get("ring") or []),
            int(data["levels"]) if data.get("levels") is not None else None,
            float(data["height_m"]) if data.get("height_m") is not None else None,
        )


@dataclass(frozen=True)
class Utility:
    kind: str
    """``power``, ``water``, ``sewer`` or ``gas``."""
    coords: Line

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "coords": [list(p) for p in self.coords]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Utility:
        return cls(str(data["kind"]), tuple((float(x), float(y)) for x, y in data["coords"]))


@dataclass(frozen=True)
class Furniture:
    trees: tuple[Point, ...] = ()
    hydrants: tuple[Point, ...] = ()
    power_poles: tuple[Point, ...] = ()
    street_lamps: tuple[Point, ...] = ()
    oneway_roads: tuple[tuple[str | None, Line], ...] = ()
    buildings: tuple[Building, ...] = ()
    utilities: tuple[Utility, ...] = ()
    driveways: tuple[Line, ...] = ()
    parking_lanes: tuple[tuple[str, Line], ...] = field(default=())
    """``(side, line)`` with side ``left``, ``right`` or ``both``."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "trees": [list(p) for p in self.trees],
            "hydrants": [list(p) for p in self.hydrants],
            "power_poles": [list(p) for p in self.power_poles],
            "street_lamps": [list(p) for p in self.street_lamps],
            "oneway_roads": [
                {"name": name, "coords": [list(p) for p in line]}
                for name, line in self.oneway_roads
            ],
            "buildings": [b.as_dict() for b in self.buildings],
            "utilities": [u.as_dict() for u in self.utilities],
            "driveways": _lines_out(self.driveways),
            "parking_lanes": [
                {"side": side, "coords": [list(p) for p in line]}
                for side, line in self.parking_lanes
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Furniture:
        def points(key: str) -> tuple[Point, ...]:
            return tuple((float(x), float(y)) for x, y in data.get(key) or [])

        return cls(
            trees=points("trees"),
            hydrants=points("hydrants"),
            power_poles=points("power_poles"),
            street_lamps=points("street_lamps"),
            oneway_roads=tuple(
                (entry.get("name"), tuple((float(x), float(y)) for x, y in entry["coords"]))
                for entry in data.get("oneway_roads") or []
            ),
            buildings=tuple(Building.from_dict(b) for b in data.get("buildings") or []),
            utilities=tuple(Utility.from_dict(u) for u in data.get("utilities") or []),
            driveways=_lines(data.get("driveways") or []),
            parking_lanes=tuple(
                (str(entry["side"]), tuple((float(x), float(y)) for x, y in entry["coords"]))
                for entry in data.get("parking_lanes") or []
            ),
        )


def _parked(value: Any) -> bool:
    return bool(value) and value not in ("no", "none")


def _number(value: Any) -> float | None:
    try:
        return float(str(value).split()[0])
    except (TypeError, ValueError, IndexError):
        return None


def furniture(extent: Extent, *, log: Log | None = None) -> Furniture:
    south, west, north, east = extent.lonlat_bbox()
    bbox = f"{south},{west},{north},{east}"
    query = f"""[out:json][timeout:60];
(
  node["natural"="tree"]({bbox});
  node["emergency"="fire_hydrant"]({bbox});
  node["power"="pole"]({bbox});
  node["highway"="street_lamp"]({bbox});
  way["highway"]["oneway"="yes"]({bbox});
  way["building"]({bbox});
  way["power"="line"]({bbox});
  way["power"="minor_line"]({bbox});
  way["man_made"="pipeline"]({bbox});
  way["service"="driveway"]({bbox});
  way["highway"]["parking:lane:left"]({bbox});
  way["highway"]["parking:lane:right"]({bbox});
  way["highway"]["parking:both"]({bbox});
  way["highway"]["parking:left"]({bbox});
  way["highway"]["parking:right"]({bbox});
);
out geom;"""
    data = overpass(query, log=log)

    trees: list[Point] = []
    hydrants: list[Point] = []
    poles: list[Point] = []
    lamps: list[Point] = []
    oneway: list[tuple[str | None, Line]] = []
    buildings: list[Building] = []
    utilities: list[Utility] = []
    driveways: list[Line] = []
    parking: list[tuple[str, Line]] = []

    for element in data.get("elements") or []:
        tags = element.get("tags") or {}
        if element.get("type") == "node":
            point = (float(element["lon"]), float(element["lat"]))
            if tags.get("natural") == "tree":
                trees.append(point)
            elif tags.get("emergency") == "fire_hydrant":
                hydrants.append(point)
            elif tags.get("power") == "pole":
                poles.append(point)
            elif tags.get("highway") == "street_lamp":
                lamps.append(point)
            continue
        if element.get("type") != "way" or not element.get("geometry"):
            continue
        coords: Line = tuple((float(g["lon"]), float(g["lat"])) for g in element["geometry"])
        if tags.get("service") == "driveway":
            driveways.append(coords)
            continue
        left = tags.get("parking:lane:left") or tags.get("parking:left")
        right = tags.get("parking:lane:right") or tags.get("parking:right")
        both = tags.get("parking:both")
        if tags.get("highway") and (_parked(left) or _parked(right) or _parked(both)):
            parking.append(
                ("both" if _parked(both) else "left" if _parked(left) else "right", coords)
            )
            continue
        if tags.get("building"):
            levels = _number(tags.get("building:levels"))
            buildings.append(
                Building(
                    lon=sum(p[0] for p in coords) / len(coords),
                    lat=sum(p[1] for p in coords) / len(coords),
                    ring=coords,
                    levels=int(levels) if levels is not None else None,
                    height_m=_number(tags.get("height")),
                )
            )
        elif tags.get("power") in ("line", "minor_line"):
            utilities.append(Utility("power", coords))
        elif tags.get("man_made") == "pipeline":
            substance = (tags.get("substance") or "").lower()
            kind = "gas" if substance == "gas" else "sewer" if substance == "sewage" else "water"
            utilities.append(Utility(kind, coords))
        elif tags.get("highway") and tags.get("oneway") == "yes":
            oneway.append((tags.get("name"), coords))

    return Furniture(
        trees=tuple(trees),
        hydrants=tuple(hydrants),
        power_poles=tuple(poles),
        street_lamps=tuple(lamps),
        oneway_roads=tuple(oneway),
        buildings=tuple(buildings),
        utilities=tuple(utilities),
        driveways=tuple(driveways),
        parking_lanes=tuple(parking),
    )
