"""The neighbourhood in the model: the project's location, the terrain, the
buildings next door.

What the site sheet draws flat, this makes solid, from the same bundle:

* **The project location**, set from the address. With the site anchored at
  the project origin the origin's true position *is* the site's centre, so
  the location becomes exactly right and every later run and every sun
  study agrees with it. The survey point is written as the same point on the
  MGA2020 grid, so an IFC export carries a real georeference too.
* **The terrain**, one Mesh from the NSW 1 m contours, its outline the sheet's
  extent at the ground level under each corner and every contour a level
  line inside it.
* **The neighbours**, one slab per building footprint OpenStreetMap records,
  standing on the ground under it, as tall as its storeys say -- or, where
  OSM records none, as tall as the LEP's height-of-building control allows,
  and said to be assumed. The site's own buildings are left out: what stands
  there is what the proposal replaces.

Everything lands on one layer, ``LORIINI``, to be filed properly once the
whole set of outputs is settled; every element carries an ID that says what
it is, so the filing can be done by a Find & Select.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sun_study.archicad.connection import ArchicadConnection, ArchicadError
from sun_study.archicad.draw import ensure_layer, move_to_layer
from sun_study.archicad.ids import stamp_in_order
from sun_study.archicad.read import GeoLocation
from sun_study.archicad.site_analysis import Frame, _clean_ring, _level_at, _map_rectangle
from sun_study.site.arcgis import rings_of
from sun_study.site.geo import (
    Point,
    lonlat_to_mga,
    mercator_to_lonlat,
    mga_zone,
    point_in_ring,
    ring_area,
    ring_centroid,
)
from sun_study.site.pipeline import SiteBundle

__all__ = [
    "LAYER",
    "STOREY_M",
    "ContextModelReport",
    "model_context",
    "set_project_location",
    "storeys_of",
]

#: Where everything generated goes, until the office files it.
LAYER = "LORIINI"

#: Floor to floor, where nothing better is recorded. A NSW apartment floor
#: is 3.1 m under the ADG's 2.7 m ceilings; a house is a little less. One
#: figure, stated, rather than two guesses.
STOREY_M = 3.1

#: How far the terrain's skirt drops below its lowest point.
SKIRT_M = 2.0


@dataclass(frozen=True)
class ContextModelReport:
    terrain: bool
    contours: int
    buildings: int
    assumed: int
    """Buildings whose storeys came from the LEP control or the default."""
    on_site: int
    """Footprints inside the site, left out."""
    layer: str
    notes: tuple[str, ...] = ()

    def describe(self) -> str:
        lines = [
            "  terrain: "
            + (f"one mesh from {self.contours} contours" if self.terrain else "not made"),
            f"  neighbours: {self.buildings} slabs on {self.layer!r}, {self.assumed} with assumed "
            f"storeys, {self.on_site} footprints on the site left out",
        ]
        lines.extend(f"    {note}" for note in self.notes)
        return "\n".join(lines)


# -- the location ---------------------------------------------------------------


def set_project_location(
    connection: ArchicadConnection,
    geo: GeoLocation | None,
    site_centre: tuple[float, float],
    *,
    ground_m: float | None = None,
) -> GeoLocation:
    """Put the project at the site: ``SetGeoLocation`` with the centre's
    longitude and latitude and the survey point on the MGA2020 grid.

    North is kept as the project has it. It is a design decision and turning
    it would move nothing already modelled; only the sun and the georeference
    follow the location. Returns what was set.
    """
    lon, lat = site_centre
    zone = mga_zone(lon)
    east, north = lonlat_to_mga(lon, lat, zone)
    altitude = ground_m if ground_m is not None else (geo.altitude_m if geo else 0.0)
    north_rad = geo.north_radians if geo else 0.0
    response = connection.run_tapir(
        "SetGeoLocation",
        {
            "projectLocation": {
                "longitude": lon,
                "latitude": lat,
                "altitude": altitude,
                "north": north_rad,
            },
            "surveyPoint": {
                "position": {"eastings": east, "northings": north, "elevation": altitude},
                "geoReferencingParameters": {
                    "crsName": f"EPSG:{7800 + zone}",
                    "description": f"GDA2020 / MGA zone {zone}",
                    "geodeticDatum": "GDA2020",
                    "verticalDatum": "AHD",
                    "mapProjection": "Transverse Mercator",
                    "mapZone": str(zone),
                },
            },
        },
    )
    if isinstance(response, dict) and response.get("success") is False:
        raise ArchicadError(f"SetGeoLocation answered {response!r}")
    return GeoLocation(
        latitude_deg=lat, longitude_deg=lon, altitude_m=altitude, north_radians=north_rad
    )


# -- the ground and the storeys ---------------------------------------------------


def _contours(bundle: SiteBundle, frame: Frame) -> list[tuple[float, list[Point]]]:
    return [(c.elevation, frame.ring(c.coords)) for c in bundle.contours if len(c.coords) >= 2]


def _ground(point: Point, contours: Sequence[tuple[float, list[Point]]]) -> float:
    level = _level_at(point, contours) if contours else None
    return level if level is not None else 0.0


def storeys_of(
    levels: int | None,
    height_m: float | None,
    hob_m: float | None,
    *,
    storey_m: float = STOREY_M,
) -> tuple[int, bool]:
    """How many storeys a building gets, and whether that was assumed.

    OSM's storey count first, its recorded height next; failing both, the
    LEP height-of-building control at the point, which is what could stand
    there rather than what does; failing that, two.
    """
    if levels and levels > 0:
        return levels, False
    if height_m and height_m > 0:
        return max(1, round(height_m / storey_m)), False
    if hob_m and hob_m > 0:
        return max(1, int(hob_m / storey_m)), True
    return 2, True


def _hob_at(bundle: SiteBundle, lon: float, lat: float) -> float | None:
    for feature in bundle.height_of_building.get("features", []):
        value = feature["properties"].get("MAX_B_H")
        if value is None:
            continue
        if any(point_in_ring(lon, lat, ring) for ring in rings_of(feature.get("geometry"))):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


# -- the model ----------------------------------------------------------------------


def _on_the_floor_plan(connection: ArchicadConnection) -> None:
    """Make the floor plan the current database, and check that it is.

    A slab or a mesh is a model element and cannot be created while a
    worksheet or a layout is current, which is where the sheets leave the
    run standing. Tapir's ``ChangeWindow`` moves the database while the
    window stays (D40); the add-on's ``GetCurrentDatabase`` says whether it
    did, since the answer is not to be believed.
    """
    here = connection.run_loriini("GetCurrentDatabase", {})
    if isinstance(here, dict) and here.get("windowType") == "FloorPlan":
        return
    connection.run_tapir("ChangeWindow", {"windowType": "FloorPlan"})
    here = connection.run_loriini("GetCurrentDatabase", {})
    if not isinstance(here, dict) or here.get("windowType") != "FloorPlan":
        raise ArchicadError(
            "The floor plan could not be made the current database, and a slab or a "
            "mesh cannot be created anywhere else. Click a storey in the Project Map "
            "and run again."
        )


def _terrain(
    connection: ArchicadConnection,
    bundle: SiteBundle,
    frame: Frame,
    contours: Sequence[tuple[float, list[Point]]],
    layer_index: int,
) -> list[dict[str, Any]]:
    """One Mesh: the extent at ground level, with every contour as a level line."""
    corners = _map_rectangle(bundle, frame)
    outline = [{"x": x, "y": y, "z": _ground((x, y), contours)} for x, y in corners]
    sublines: list[dict[str, Any]] = []
    for elevation, points in contours:
        run: list[dict[str, float]] = []
        for x, y in points:
            if point_in_ring(x, y, corners):
                run.append({"x": x, "y": y, "z": elevation})
            elif len(run) >= 2:
                sublines.append({"coordinates": run})
                run = []
            else:
                run = []
        if len(run) >= 2:
            sublines.append({"coordinates": run})
    lowest = min([p["z"] for p in outline] + [s["coordinates"][0]["z"] for s in sublines])
    # The add-on's command first: Tapir's CreateMeshes answers APIERR_BADINDEX
    # for every mesh on Archicad 26 (measured), so it is only the fallback
    # for an add-on built before the command existed.
    try:
        answer = connection.run_loriini(
            "CreateMesh",
            {
                "outline": outline,
                "levelLines": [s["coordinates"] for s in sublines],
                "skirt": "solid",
                "skirtLevel": lowest - SKIRT_M,
                "layerIndex": layer_index,
                "elementId": "SA TERRAIN",
            },
        )
        guid = str(answer.get("guid", "")) if isinstance(answer, dict) else ""
        if guid:
            return [{"elementId": {"guid": guid}}]
        raise ArchicadError(f"CreateMesh answered {answer!r}")
    except ArchicadError as error:
        if "not have the registered" not in str(error):
            raise
    response = connection.run_tapir(
        "CreateMeshes",
        {
            "meshesData": [
                {
                    "level": 0.0,
                    "polygonCoordinates": outline,
                    "sublines": sublines,
                    "skirtType": "SolidBodyWithSkirt",
                    "skirtLevel": lowest - SKIRT_M,
                    "ridges": "UserDefined",
                    "showLines": False,
                }
            ]
        },
    )
    made = _elements(response, "CreateMeshes")
    move_to_layer(connection, made, layer_index)
    stamp_in_order(connection, made, ["SA TERRAIN"])
    return made


def _elements(response: Any, command: str) -> list[dict[str, Any]]:
    elements = response.get("elements") if isinstance(response, dict) else None
    if not isinstance(elements, list):
        raise ArchicadError(f"{command} returned no element list: {response!r}")
    problems = sorted(
        {
            str((e.get("error") or {}).get("message", "unknown error"))
            for e in elements
            if isinstance(e, dict) and "error" in e
        }
    )
    if problems:
        raise ArchicadError(f"{command} failed for some elements:\n  " + "\n  ".join(problems[:5]))
    return [e for e in elements if isinstance(e, dict) and "elementId" in e]


def model_context(
    connection: ArchicadConnection,
    bundle: SiteBundle,
    frame: Frame,
    *,
    terrain: bool = True,
    buildings: bool = True,
    storey_m: float = STOREY_M,
    say: Callable[[str], None] | None = None,
) -> ContextModelReport:
    """The terrain and the neighbours, in the model, on the ``LORIINI`` layer."""
    _on_the_floor_plan(connection)
    layer = ensure_layer(connection, LAYER)
    contours = _contours(bundle, frame)
    notes: list[str] = []

    made_terrain = False
    if terrain:
        if not contours:
            notes.append("no contours in the bundle; the terrain is not made.")
        else:
            try:
                _terrain(connection, bundle, frame, contours, layer.index)
                made_terrain = True
            except ArchicadError as error:
                notes.append(f"the terrain mesh was refused: {error}")

    site_rings = [frame.ring(ring) for ring in bundle.site_rings]
    slabs: list[dict[str, Any]] = []
    identifiers: list[str] = []
    assumed = 0
    on_site = 0
    if buildings:
        for building in bundle.furniture.buildings:
            ring = _clean_ring(frame.ring(building.ring))
            if len(ring) < 3 or abs(ring_area(ring)) < 4.0:
                continue
            centre = ring_centroid(ring)
            if any(point_in_ring(centre[0], centre[1], site) for site in site_rings):
                on_site += 1
                continue
            storeys, guessed = storeys_of(
                building.levels,
                building.height_m,
                _hob_at(bundle, building.lon, building.lat),
                storey_m=storey_m,
            )
            assumed += int(guessed)
            slabs.append(
                {
                    "polygonCoordinates": [{"x": x, "y": y} for x, y in ring],
                    "level": _ground(centre, contours),
                    "thickness": storeys * storey_m,
                    "referencePlaneLocation": "Bottom",
                }
            )
            identifiers.append(f"SA NEIGHBOUR {storeys} STOREY ({'ASSUMED' if guessed else 'OSM'})")
    made: list[dict[str, Any]] = []
    for start in range(0, len(slabs), 200):
        try:
            response = connection.run_tapir(
                "CreateSlabs", {"slabsData": slabs[start : start + 200]}
            )
            batch = _elements(response, "CreateSlabs")
        except ArchicadError as error:
            notes.append(f"a batch of {len(slabs[start : start + 200])} slabs was refused: {error}")
            continue
        made.extend(batch)
    if made:
        moved = move_to_layer(connection, made, layer.index)
        if moved != len(made):
            notes.append(f"{len(made) - moved} slabs stayed on the Slab tool's default layer.")
        stamp_in_order(connection, made, identifiers[: len(made)])
    if say:
        say(f"  {len(made)} neighbours as slabs, {assumed} with assumed storeys")
    return ContextModelReport(
        terrain=made_terrain,
        contours=len(contours),
        buildings=len(made),
        assumed=assumed,
        on_site=on_site,
        layer=layer.name,
        notes=tuple(notes),
    )


def ground_at_site(bundle: SiteBundle, frame: Frame) -> float | None:
    """The ground level at the site's centre, for the project's altitude."""
    contours = _contours(bundle, frame)
    if not contours:
        return None
    centre = frame.project(*mercator_to_lonlat(*bundle.centre))
    return _level_at(centre, contours)
