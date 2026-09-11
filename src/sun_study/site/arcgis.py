"""A minimal ArcGIS REST query client.

Asks for ``f=json`` (Esri JSON) rather than ``f=geojson``, because some NSW
servers answer 503 to the latter, and converts to GeoJSON-shaped features
here. Every query asks for ``outSR=4326`` so what comes back is longitude and
latitude, whatever the layer is stored in.

A feature is a plain dictionary -- ``{"type": "Feature", "geometry": {...},
"properties": {...}}`` -- so a bundle can be written to disk as JSON and read
back without a class per layer.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from sun_study.site.geo import Extent, Point
from sun_study.site.http import get_json

__all__ = [
    "Feature",
    "FeatureCollection",
    "empty",
    "paths_of",
    "query_by_extent",
    "query_by_geometry",
    "query_by_point",
    "query_where",
    "rings_of",
]

Feature = dict[str, Any]
FeatureCollection = dict[str, Any]

_BASE = {"f": "json", "outSR": "4326"}


def empty() -> FeatureCollection:
    return {"type": "FeatureCollection", "features": []}


def _to_geojson(data: Any) -> FeatureCollection:
    features: list[Feature] = []
    for entry in (data or {}).get("features") or []:
        geometry: dict[str, Any] | None = None
        shape = entry.get("geometry")
        if isinstance(shape, dict):
            if "rings" in shape:
                geometry = {"type": "Polygon", "coordinates": shape["rings"]}
            elif "paths" in shape:
                geometry = {"type": "MultiLineString", "coordinates": shape["paths"]}
            elif "x" in shape:
                geometry = {"type": "Point", "coordinates": [shape["x"], shape["y"]]}
        features.append(
            {"type": "Feature", "geometry": geometry, "properties": entry.get("attributes") or {}}
        )
    return {"type": "FeatureCollection", "features": features}


def query_by_extent(
    layer_url: str, extent: Extent, out_fields: str = "*", where: str = "1=1"
) -> FeatureCollection:
    """Everything intersecting the extent, paged until the server says done."""
    features: list[Feature] = []
    offset = 0
    while True:
        data = get_json(
            f"{layer_url}/query",
            {
                **_BASE,
                "where": where,
                "geometry": json.dumps(extent.esri()),
                "geometryType": "esriGeometryEnvelope",
                "inSR": "3857",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": out_fields,
                "resultOffset": str(offset),
            },
            label=layer_url,
        )
        page = _to_geojson(data)["features"]
        features.extend(page)
        if not data.get("exceededTransferLimit") or not page:
            break
        offset += len(page)
        if offset > 20_000:
            break
    return {"type": "FeatureCollection", "features": features}


def query_by_point(layer_url: str, point: Point, out_fields: str = "*") -> FeatureCollection:
    """Everything containing one Web Mercator point."""
    data = get_json(
        f"{layer_url}/query",
        {
            **_BASE,
            "where": "1=1",
            "geometry": json.dumps(
                {"x": point[0], "y": point[1], "spatialReference": {"wkid": 3857}}
            ),
            "geometryType": "esriGeometryPoint",
            "inSR": "3857",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": out_fields,
        },
        label=layer_url,
    )
    return _to_geojson(data)


def query_by_geometry(
    layer_url: str, geometry: dict[str, Any], geometry_type: str, out_fields: str = "*"
) -> FeatureCollection:
    """Everything intersecting a geometry given in longitude and latitude."""
    data = get_json(
        f"{layer_url}/query",
        {
            **_BASE,
            "where": "1=1",
            "geometry": json.dumps(geometry),
            "geometryType": geometry_type,
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": out_fields,
        },
        label=layer_url,
    )
    return _to_geojson(data)


def query_where(
    layer_url: str, where: str, out_fields: str = "*", count: int | None = None
) -> FeatureCollection:
    params = {**_BASE, "where": where, "outFields": out_fields}
    if count:
        params["resultRecordCount"] = str(count)
    return _to_geojson(get_json(f"{layer_url}/query", params, label=layer_url))


def rings_of(geometry: dict[str, Any] | None) -> list[list[Point]]:
    """Every ring of a polygon or multipolygon, as ``(lon, lat)`` pairs."""
    if not geometry:
        return []
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    if kind == "Polygon":
        rings: Sequence[Sequence[Sequence[float]]] = coordinates
    elif kind == "MultiPolygon":
        rings = [ring for polygon in coordinates for ring in polygon]
    else:
        return []
    return [[(float(p[0]), float(p[1])) for p in ring] for ring in rings]


def paths_of(geometry: dict[str, Any] | None) -> list[list[Point]]:
    """Every path of a line or multiline, as ``(lon, lat)`` pairs."""
    if not geometry:
        return []
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    if kind == "LineString":
        paths: Sequence[Sequence[Sequence[float]]] = [coordinates]
    elif kind == "MultiLineString":
        paths = coordinates
    else:
        return []
    return [[(float(p[0]), float(p[1])) for p in path] for path in paths]
