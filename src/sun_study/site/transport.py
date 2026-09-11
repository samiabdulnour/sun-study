"""Bus stops from Transport for NSW, and walking isochrones from Valhalla.

TfNSW is authoritative for stops but needs a free key from
opendata.transport.nsw.gov.au in ``TFNSW_API_KEY``; without one the OSM stops
that came with the route query stand in. Valhalla's public server is shared
infrastructure: one request per run, generous timeout.
"""

from __future__ import annotations

import json
import math
import os
import urllib.parse
from dataclasses import dataclass
from typing import Any

from sun_study.site.geo import Extent, Point, mercator_to_lonlat
from sun_study.site.http import fetch, get_json
from sun_study.site.osm import Stop, cluster_stops

__all__ = ["Isochrone", "bus_stops_tfnsw", "isochrones", "tfnsw_key"]

COORD_API = "https://api.transport.nsw.gov.au/v1/tp/coord"
VALHALLA = "https://valhalla1.openstreetmap.de/isochrone"


def tfnsw_key() -> str | None:
    return (os.environ.get("TFNSW_API_KEY") or "").strip() or None


def _coord_request(lon: float, lat: float, radius_m: int, key: str) -> list[dict[str, Any]]:
    params = {
        "outputFormat": "rapidJSON",
        "coord": f"{lon}:{lat}:EPSG:4326",
        "coordOutputFormat": "EPSG:4326",
        "inclFilter": "1",
        "type_1": "BUS_POINT",
        "radius_1": str(radius_m),
        "version": "10.2.1.42",
    }
    data = get_json(
        COORD_API,
        params,
        attempts=4,
        timeout=45.0,
        headers={"Authorization": f"apikey {key}"},
        label="TfNSW coord",
    )
    return list(data.get("locations") or [])


def bus_stops_tfnsw(extent: Extent, key: str) -> list[Stop]:
    """Every bus stop in the extent, via a 2x2 grid of radius queries, then
    stands and platforms of one stop (within 30 m) clustered into one symbol."""
    cx, cy = extent.centre
    qw, qh = extent.width / 4.0, extent.height / 4.0
    radius = math.ceil(math.hypot(qw, qh)) + 150
    by_id: dict[str, Stop] = {}
    for x, y in ((cx - qw, cy - qh), (cx + qw, cy - qh), (cx - qw, cy + qh), (cx + qw, cy + qh)):
        lon, lat = mercator_to_lonlat(x, y)
        for location in _coord_request(lon, lat, radius, key):
            coord = location.get("coord") or []
            if len(coord) < 2:
                continue
            stop_lat, stop_lon = float(coord[0]), float(coord[1])
            by_id[str(location.get("id") or f"{stop_lat},{stop_lon}")] = Stop(
                stop_lon, stop_lat, location.get("name")
            )

    return cluster_stops(list(by_id.values()))


@dataclass(frozen=True)
class Isochrone:
    minutes: int
    rings: tuple[tuple[Point, ...], ...]

    def as_dict(self) -> dict[str, Any]:
        return {"minutes": self.minutes, "rings": [[list(p) for p in r] for r in self.rings]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Isochrone:
        return cls(
            int(data["minutes"]),
            tuple(tuple((float(x), float(y)) for x, y in ring) for ring in data["rings"]),
        )


def isochrones(lon: float, lat: float, minutes: tuple[int, ...] = (5, 10)) -> list[Isochrone]:
    """Walking catchments around a point, as polygons, shortest first."""
    request = {
        "locations": [{"lat": lat, "lon": lon}],
        "costing": "pedestrian",
        "contours": [{"time": m} for m in minutes],
        "polygons": True,
    }
    body = fetch(
        f"{VALHALLA}?json={urllib.parse.quote(json.dumps(request))}",
        attempts=4,
        timeout=60.0,
        label="isochrones",
    )
    data = json.loads(body.decode("utf-8"))
    found: list[Isochrone] = []
    for feature in data.get("features") or []:
        geometry = feature.get("geometry") or {}
        contour = (feature.get("properties") or {}).get("contour")
        if contour is None:
            continue
        if geometry.get("type") == "Polygon":
            rings = geometry["coordinates"]
        elif geometry.get("type") == "MultiPolygon":
            rings = [ring for polygon in geometry["coordinates"] for ring in polygon]
        else:
            continue
        found.append(
            Isochrone(
                int(contour),
                tuple(tuple((float(p[0]), float(p[1])) for p in ring) for ring in rings),
            )
        )
    return sorted(found, key=lambda iso: iso.minutes)
