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

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sun_study.archicad.connection import ArchicadConnection, ArchicadError
from sun_study.archicad.draw import ensure_layer, move_to_layer
from sun_study.archicad.ids import stamp_in_order
from sun_study.archicad.read import GeoLocation
from sun_study.archicad.site_analysis import Frame, _clean_ring, _map_rectangle
from sun_study.site.arcgis import rings_of
from sun_study.site.geo import (
    Point,
    clip_ring,
    dissolve,
    lonlat_to_mercator,
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

#: How far the street blocks stand above the terrain: a kerb. The roads
#: are not modelled; they are the terrain left showing between the blocks,
#: which is how the office's own files do it (a roads mesh with the blocks
#: as holes, and a footpath mesh per block).
KERB_M = 0.15

#: How far the terrain's skirt drops below its lowest point.
SKIRT_M = 2.0


@dataclass(frozen=True)
class ContextModelReport:
    terrain: bool
    contours: int
    blocks: int
    """Street blocks as kerbed meshes; the roads are the terrain between them."""
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
            f"  blocks: {self.blocks} meshes {KERB_M * 1000:.0f} mm above the ground; "
            "the roads are the terrain between them",
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


class _Ground:
    """The ground level anywhere, from the contours, in constant time.

    Every contour vertex goes into a cell of ``cell_m``; a query looks at
    the cells around the point, takes the nearest vertex of each of the two
    nearest distinct levels, and weights them by inverse distance -- the
    site sheet's own rule, without its scan of every point of every
    contour, which for a thousand footprints against a kilometre of contours
    was a billion distances.
    """

    def __init__(self, contours: Sequence[tuple[float, list[Point]]], cell_m: float = 40.0) -> None:
        self.cell = cell_m
        self.cells: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
        for elevation, points in contours:
            for x, y in points:
                key = (int(x // cell_m), int(y // cell_m))
                self.cells.setdefault(key, []).append((x, y, elevation))
        self.empty = not self.cells

    def at(self, point: Point) -> float:
        if self.empty:
            return 0.0
        cx, cy = int(point[0] // self.cell), int(point[1] // self.cell)
        for reach in (1, 3, 8, 25):
            nearest: dict[float, float] = {}
            for i in range(cx - reach, cx + reach + 1):
                for j in range(cy - reach, cy + reach + 1):
                    for x, y, elevation in self.cells.get((i, j), ()):
                        d = math.hypot(x - point[0], y - point[1])
                        if d < nearest.get(elevation, math.inf):
                            nearest[elevation] = d
            if nearest:
                ranked = sorted(nearest.items(), key=lambda item: item[1])
                if len(ranked) == 1 or ranked[0][1] < 1.0:
                    return ranked[0][0]
                (e1, d1), (e2, d2) = ranked[0], ranked[1]
                return (e1 / d1 + e2 / d2) / (1 / d1 + 1 / d2)
        return 0.0


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


class _Heights:
    """The LEP height-of-building control at a point, boxes checked first."""

    def __init__(self, bundle: SiteBundle) -> None:
        self.zones: list[tuple[tuple[float, float, float, float], list[list[Point]], float]] = []
        for feature in bundle.height_of_building.get("features", []):
            try:
                value = float(feature["properties"].get("MAX_B_H"))
            except (TypeError, ValueError):
                continue
            rings = rings_of(feature.get("geometry"))
            if not rings:
                continue
            xs = [x for ring in rings for x, _ in ring]
            ys = [y for ring in rings for _, y in ring]
            self.zones.append(((min(xs), min(ys), max(xs), max(ys)), rings, value))

    def at(self, lon: float, lat: float) -> float | None:
        for (x0, y0, x1, y1), rings, value in self.zones:
            if x0 <= lon <= x1 and y0 <= lat <= y1:
                if any(point_in_ring(lon, lat, ring) for ring in rings):
                    return value
        return None


# -- the model ----------------------------------------------------------------------


def _clear_previous(connection: ArchicadConnection) -> int:
    """Delete the slabs and meshes of the last run: what sits on the tool's
    layer and carries its ID. Returns how many went.

    By the ID alone: the ID is the tool's own mark, and a slab a failed
    move left off the layer still carries it. Three runs had stacked ninety
    slabs before this existed.
    """
    doomed: list[dict[str, Any]] = []
    for kind in ("Slab", "Mesh"):
        found = connection.run_tapir("GetElementsByType", {"elementType": kind})
        # Blocks carry "SA BLOCK n"; they go with the rest.
        elements = found.get("elements") if isinstance(found, dict) else None
        if not isinstance(elements, list) or not elements:
            continue
        details = connection.run_tapir("GetDetailsOfElements", {"elements": elements})
        rows = details.get("detailsOfElements") if isinstance(details, dict) else None
        if not isinstance(rows, list) or len(rows) != len(elements):
            continue
        doomed.extend(
            element
            for element, row in zip(elements, rows, strict=True)
            if isinstance(row, dict)
            and str(row.get("id", "")).startswith(("SA NEIGHBOUR", "SA TERRAIN", "SA BLOCK"))
        )
    if doomed:
        connection.run_tapir("DeleteElements", {"elements": doomed})
    return len(doomed)


def _on_the_floor_plan(connection: ArchicadConnection) -> None:
    """Make the floor plan the current database, and check that it is.

    A slab or a mesh is a model element and cannot be created while a
    worksheet or a layout is current, which is where the sheets leave the
    run standing. Tapir's ``ChangeWindow`` moves the database while the
    window stays (D40); the add-on's ``GetCurrentDatabase`` says whether it
    did, since the answer is not to be believed.
    """
    here = connection.run_loriini("GetCurrentDatabase", {})
    if not (isinstance(here, dict) and here.get("windowType") == "FloorPlan"):
        connection.run_tapir("ChangeWindow", {"windowType": "FloorPlan"})
        here = connection.run_loriini("GetCurrentDatabase", {})
    if isinstance(here, dict) and here.get("windowType") == "FloorPlan":
        # Tell the connection, which otherwise remembers the layout the
        # sheets left it in and refuses every layer read (D63).
        connection.note_model_database(str((here.get("databaseId") or {}).get("guid", "")))
        return
    if True:
        raise ArchicadError(
            "The floor plan could not be made the current database, and a slab or a "
            "mesh cannot be created anywhere else. Click a storey in the Project Map "
            "and run again."
        )


#: How many level-line vertices a terrain mesh is allowed. Archicad triangulates
#: between every one of them, and a kilometre of 1 m contours is far more than
#: a context model needs to read as ground.
MESH_POINTS = 12_000


def _thinned(
    contours: Sequence[tuple[float, list[Point]]], limit: int = MESH_POINTS
) -> list[tuple[float, list[Point]]]:
    """The contours, fewer where there are too many: every second contour and
    every second point, again and again, until the mesh can take them."""
    kept = [(elevation, list(points)) for elevation, points in contours]
    step = 1.0
    while sum(len(points) for _, points in kept) > limit and kept:
        step *= 2.0
        kept = [
            (elevation, points[::2] if len(points) > 4 else points)
            for elevation, points in kept
            if abs(elevation / step - round(elevation / step)) < 1e-9
        ]
        if not kept:
            break
    return kept


# -- the boundary drawn in the file --------------------------------------------------


def boundary_layer(connection: ArchicadConnection, wanted: str = "auto") -> str | None:
    """The layer the site boundary is drawn on: the one named, or with
    ``"auto"`` the first layer whose name mentions a boundary."""
    response = connection.run_tapir("GetAttributesByType", {"attributeType": "Layer"})
    attributes = response.get("attributes") if isinstance(response, dict) else None
    if not isinstance(attributes, list):
        return None
    names = [str(a.get("name", "")) for a in attributes if isinstance(a, dict)]
    if wanted != "auto":
        return next((n for n in names if n.casefold() == wanted.casefold()), None)
    return next((n for n in names if "boundar" in n.casefold()), None)


def boundary_points(connection: ArchicadConnection, layer_name: str) -> list[Point]:
    """Every point of every 2D element on the layer, in project metres:
    object origins, line ends, polyline and fill vertices. Reads the current
    database, so it wants the floor plan in front."""
    response = connection.run_tapir("GetAttributesByType", {"attributeType": "Layer"})
    attributes = response.get("attributes") if isinstance(response, dict) else None
    indexes = {
        a.get("index")
        for a in (attributes or [])
        if isinstance(a, dict) and str(a.get("name", "")).casefold() == layer_name.casefold()
    }
    points: list[Point] = []
    for kind in ("Object", "Line", "PolyLine", "Hatch", "Arc"):
        try:
            found = connection.run_tapir("GetElementsByType", {"elementType": kind})
        except ArchicadError:
            continue
        elements = found.get("elements") if isinstance(found, dict) else None
        if not isinstance(elements, list) or not elements:
            continue
        details = connection.run_tapir("GetDetailsOfElements", {"elements": elements})
        rows = details.get("detailsOfElements") if isinstance(details, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or row.get("layerIndex") not in indexes:
                continue
            d = row.get("details") or {}
            for key in ("origin", "begCoordinate", "endCoordinate"):
                p = d.get(key)
                if isinstance(p, dict) and "x" in p and "y" in p:
                    points.append((float(p["x"]), float(p["y"])))
            for key in ("coordinates", "polygonCoordinates"):
                for p in d.get(key) or []:
                    if isinstance(p, dict) and "x" in p and "y" in p:
                        points.append((float(p["x"]), float(p["y"])))
    return points


# -- the blocks ---------------------------------------------------------------------


def _blocks(bundle: SiteBundle, frame: Frame) -> tuple[list[list[Point]], int]:
    """The street blocks, in the project frame: the cadastral lots clipped to
    the extent, grouped by touching, each group dissolved to its outline.

    The site's own lots are left out, so the block they sit in has a notch
    where the site is and the project's own site model shows there. A
    group whose lots will not dissolve cleanly (three edges meeting at a
    point) falls back to its lots one by one. Returns the rings and how many
    groups fell back.
    """
    e = bundle.extent
    inset = 1.0
    site_keys = {
        _ring_key(ring)
        for feature in bundle.site_lots.get("features", [])
        for ring in rings_of(feature.get("geometry"))
    }
    lots: list[list[Point]] = []
    for feature in bundle.all_lots.get("features", []):
        for ring in rings_of(feature.get("geometry")):
            if _ring_key(ring) in site_keys:
                continue
            merc = [lonlat_to_mercator(lon, lat) for lon, lat in ring]
            clipped = clip_ring(
                merc, e.xmin + inset, e.ymin + inset, e.xmax - inset, e.ymax - inset
            )
            if len(clipped) < 3:
                continue
            projected = _clean_ring(frame.ring([mercator_to_lonlat(x, y) for x, y in clipped]))
            if len(projected) >= 3 and abs(ring_area(projected)) > 1.0:
                lots.append(projected)
    # Strata lots stack on one footprint; one ring of each, or the shared
    # edges are counted five times and the block loses its side.
    seen: set[tuple[tuple[int, int], ...]] = set()
    unique: list[list[Point]] = []
    for ring in lots:
        signature = tuple(sorted((round(x / 0.05), round(y / 0.05)) for x, y in ring))
        if signature not in seen:
            seen.add(signature)
            unique.append(ring)
    lots = unique
    if not lots:
        return [], 0

    # Group by a shared vertex: lots across a road share none.
    def key(p: Point) -> tuple[int, int]:
        return (round(p[0] / 0.05), round(p[1] / 0.05))

    parent = list(range(len(lots)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first_seen: dict[tuple[int, int], int] = {}
    for i, ring in enumerate(lots):
        for p in ring:
            k = key(p)
            j = first_seen.setdefault(k, i)
            if j != i:
                parent[find(i)] = find(j)
    groups: dict[int, list[list[Point]]] = {}
    for i, ring in enumerate(lots):
        groups.setdefault(find(i), []).append(ring)

    blocks: list[list[Point]] = []
    fell_back = 0
    for members in groups.values():
        merged = dissolve(members, tolerance=0.05) if len(members) > 1 else members
        if not merged:
            fell_back += 1
            merged = members
        blocks.extend(ring for ring in merged if len(ring) >= 3 and abs(ring_area(ring)) > 1.0)
    return blocks, fell_back


def _ring_key(ring: Sequence[Point]) -> tuple[tuple[int, int], ...]:
    return tuple(sorted((round(x * 1e6), round(y * 1e6)) for x, y in ring))


def _block_meshes(
    connection: ArchicadConnection,
    blocks: Sequence[Sequence[Point]],
    ground: _Ground,
    layer_index: int,
    floor_index: int,
    floor_level: float,
) -> tuple[int, list[str]]:
    """One kerbed mesh per block. Returns how many were made and what was refused."""
    made = 0
    problems: list[str] = []
    for i, ring in enumerate(blocks):
        outline = [{"x": x, "y": y, "z": ground.at((x, y)) + KERB_M - floor_level} for x, y in ring]
        lowest = min(p["z"] for p in outline)
        try:
            answer = connection.run_loriini(
                "CreateMesh",
                {
                    "outline": outline,
                    "levelLines": [],
                    "skirt": "solid",
                    "skirtLevel": lowest - SKIRT_M,
                    "layerIndex": layer_index,
                    "floorIndex": floor_index,
                    "elementId": f"SA BLOCK {i + 1}",
                },
            )
        except ArchicadError as error:
            problems.append(str(error))
            continue
        if isinstance(answer, dict) and answer.get("success", True) and answer.get("guid"):
            made += 1
        else:
            problems.append(str(answer))
    return made, problems


def _datum_storey(connection: ArchicadConnection) -> tuple[int, float]:
    """The storey nearest level zero, and its level: ``(index, level)``.

    Absolute heights go in relative to it. ``(0, 0.0)`` when the storeys
    cannot be read, which is right for a project whose first storey is the
    datum, as the office's are.
    """
    try:
        response = connection.run_tapir("GetStories", {})
    except ArchicadError:
        return 0, 0.0
    stories = response.get("stories") if isinstance(response, dict) else None
    best: tuple[int, float] | None = None
    for story in stories if isinstance(stories, list) else []:
        if not isinstance(story, dict):
            continue
        try:
            index, level = int(story["index"]), float(story.get("level", 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        if best is None or abs(level) < abs(best[1]):
            best = (index, level)
    return best or (0, 0.0)


def _terrain(
    connection: ArchicadConnection,
    bundle: SiteBundle,
    frame: Frame,
    contours: Sequence[tuple[float, list[Point]]],
    layer_index: int,
) -> list[dict[str, Any]]:
    """One Mesh: the extent at ground level, with every contour as a level line."""
    # A mesh's levels are relative to its home storey, and the Mesh tool's
    # default storey was 39 on the Kogarah study, which put the terrain a
    # hundred metres in the air. Home it on the storey nearest level zero
    # and give every point its height above that.
    floor_index, floor_level = _datum_storey(connection)
    ground = _Ground(contours)
    corners = _map_rectangle(bundle, frame)
    outline = [{"x": x, "y": y, "z": ground.at((x, y)) - floor_level} for x, y in corners]
    sublines: list[dict[str, Any]] = []
    for elevation, points in _thinned(contours):
        run: list[dict[str, float]] = []
        for x, y in points:
            if point_in_ring(x, y, corners):
                run.append({"x": x, "y": y, "z": elevation - floor_level})
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
                "floorIndex": floor_index,
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
    blocks: bool = True,
    storey_m: float = STOREY_M,
    say: Callable[[str], None] | None = None,
) -> ContextModelReport:
    """The terrain and the neighbours, in the model, on the ``LORIINI`` layer."""
    _on_the_floor_plan(connection)
    layer = ensure_layer(connection, LAYER)
    contours = _contours(bundle, frame)
    ground = _Ground(contours)
    heights = _Heights(bundle)
    notes: list[str] = []
    removed = _clear_previous(connection)
    if removed:
        notes.append(f"{removed} slabs and meshes from the last run removed first.")

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

    made_blocks = 0
    if blocks and bundle.all_lots.get("features"):
        rings, fell_back = _blocks(bundle, frame)
        if fell_back:
            notes.append(f"{fell_back} blocks would not dissolve and are drawn lot by lot.")
        if rings:
            floor_index, floor_level = _datum_storey(connection)
            made_blocks, refused = _block_meshes(
                connection, rings, ground, layer.index, floor_index, floor_level
            )
            if refused:
                notes.append(f"{len(refused)} block meshes were refused, e.g. {refused[0][:120]}")
    lift = KERB_M if made_blocks else 0.0

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
                heights.at(building.lon, building.lat),
                storey_m=storey_m,
            )
            assumed += int(guessed)
            slabs.append(
                {
                    "polygonCoordinates": [{"x": x, "y": y} for x, y in ring],
                    "level": ground.at(centre) + lift,
                    "thickness": storeys * storey_m,
                    "referencePlaneLocation": "Bottom",
                }
            )
            identifiers.append(f"SA NEIGHBOUR {storeys} STOREY ({'ASSUMED' if guessed else 'OSM'})")
    made: list[dict[str, Any]] = []
    # The add-on's CreateSlabs takes the layer, the storey and the ID with
    # the outline, so the three passes Tapir needs (create, move, stamp) are
    # one; Tapir's is the fallback for an add-on built before it existed.
    own = _slabs_through_the_addon(
        connection, slabs, identifiers, layer.index, _datum_storey(connection)[0], notes
    )
    if own is not None:
        made = own
    else:
        for start in range(0, len(slabs), 200):
            try:
                response = connection.run_tapir(
                    "CreateSlabs", {"slabsData": slabs[start : start + 200]}
                )
                batch = _elements(response, "CreateSlabs")
            except ArchicadError as error:
                notes.append(
                    f"a batch of {len(slabs[start : start + 200])} slabs was refused: {error}"
                )
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
        blocks=made_blocks,
        buildings=len(made),
        assumed=assumed,
        on_site=on_site,
        layer=layer.name,
        notes=tuple(notes),
    )


def _slabs_through_the_addon(
    connection: ArchicadConnection,
    slabs: Sequence[dict[str, Any]],
    identifiers: Sequence[str],
    layer_index: int,
    floor_index: int,
    notes: list[str],
) -> list[dict[str, Any]] | None:
    """The slabs through Loriini's ``CreateSlabs``; ``None`` when the add-on
    does not have the command, so the caller falls back to Tapir's."""
    made: list[dict[str, Any]] = []
    for start in range(0, len(slabs), 200):
        batch = [
            {
                "contours": [{"points": one["polygonCoordinates"]}],
                "level": one["level"],
                "thickness": one["thickness"],
                "referencePlane": "bottom",
                "layerIndex": layer_index,
                "floorIndex": floor_index,
                "elementId": identifiers[start + i][:255],
            }
            for i, one in enumerate(slabs[start : start + 200])
        ]
        try:
            response = connection.run_loriini("CreateSlabs", {"slabs": batch})
        except ArchicadError as error:
            if "not have the registered" in str(error) and not made:
                return None
            notes.append(f"a batch of {len(batch)} slabs was refused: {error}")
            continue
        elements = response.get("elements") if isinstance(response, dict) else None
        if not isinstance(elements, list):
            notes.append(f"CreateSlabs answered without an element list: {response!r}")
            continue
        made.extend({"elementId": {"guid": str(e.get("guid", ""))}} for e in elements)
    return made


def ground_at_site(bundle: SiteBundle, frame: Frame) -> float | None:
    """The ground level at the site's centre, for the project's altitude."""
    contours = _contours(bundle, frame)
    if not contours:
        return None
    centre = frame.project(*mercator_to_lonlat(*bundle.centre))
    return _Ground(contours).at(centre)
