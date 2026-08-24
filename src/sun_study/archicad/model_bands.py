"""Colouring the model itself: a banded skin of real 3D elements.

What is and is not possible
---------------------------
The obvious way to make the reference study's facade picture would be to
recolour the building -- give every wall the surface of the band it falls in.
That cannot be done through this API, and the finding is worth recording
because it is not obvious from the command list:

* ``GetDetailsOfElements`` reports a ``surfaceId`` **only** for library-part
  based elements (Objects). Walls, Slabs, Roofs and Meshes report geometry and
  nothing about their appearance, so their surface can be neither read nor
  written. Measured on Archicad 26 with Tapir 1.5.7.
* ``SetDetailsOfElements`` reaches ``floorIndex``, ``layerIndex``,
  ``drawIndex`` and a small ``typeSpecificDetails`` union whose ``WallSettings``
  is purely geometric. There is no setter anywhere in the add-on that attaches
  a Surface to an existing wall.
* ``CreateMorphs`` -- the natural element for a coloured skin, since a morph
  takes a ``surfaceId`` directly -- validates its input on this build and then
  fails to create anything. A morph is not reachable here either.

What *is* reachable is creating new geometry that already carries the colour.
``CreateWalls`` takes a ``buildingMaterialId``, ``CreateBuildingMaterials``
takes a ``cutSurfaceIndex``, and ``CreateSurfaces`` takes an RGB colour. So a
band becomes a Surface, then a Building Material, then a set of thin upright
walls standing just proud of the real facade. The result is native 3D: it
shows in the 3D window, in a 3D document, in a rendering, and it prints. It
lives on its own layer and deletes as a set.

The two traps, both measured
----------------------------
**A hidden layer silently refuses everything.** New walls land on whatever
layer the Wall tool defaults to, which on the reference project is a hidden
one. ``DeleteElements`` and ``SetDetailsOfElements`` both answer
``{"success": true}`` and do nothing while that layer is hidden -- so the skin
is created, the move to its own layer reports success, and the walls stay
where they were. Every layer involved is therefore forced visible for the
duration and put back afterwards, and the result is re-read rather than
believed.

**Creation follows the current database, not the visible window.** With a
layout current, ``CreateWalls`` still answers with element ids. Callers must
make a floor plan current first.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from sun_study.archicad import naming
from sun_study.archicad.connection import ArchicadConnection, ArchicadError
from sun_study.archicad.draw import BandStyle, LayerState, ensure_layer
from sun_study.archicad.layers import borrowed
from sun_study.archicad.penetration import MAX_FIT_RESIDUAL_M, box_centre
from sun_study.archicad.read import elements_by_ifc_ids, zones
from sun_study.archicad.series import ensure_model_database
from sun_study.core.facade import PanelRectangle
from sun_study.core.geometry import PlanTransform, fit_plan_transform
from sun_study.ingest.ifc import IfcModel

__all__ = [
    "BandMaterial",
    "ModelBandReport",
    "apply_favorite_to_defaults",
    "clear_model_bands",
    "draw_model_bands",
    "ensure_band_materials",
    "fit_to_project",
]

#: Prefix on every attribute this module creates, so a project's own surfaces
#: and building materials are never at risk of being overwritten by name. Read
#: through ``naming.prefix()`` at the moment of use, because it is chosen per
#: run and a constant here would freeze the default at import.

#: How thick the skin is, and how far its centreline stands off the real face.
#: Thin enough to read as a coat of paint at any scale the facade is drawn at,
#: proud enough that the renderer never has to choose between two surfaces in
#: the same plane.
DEFAULT_THICKNESS_M = 0.04
DEFAULT_STANDOFF_M = 0.03

#: Reflectance of a band surface, as Archicad wants it: a percentage in
#: ``[0..100]``, truncated to a whole number. Full on both so the colour is
#: the same colour everywhere it appears.
AMBIENT_PERCENT = 100
DIFFUSE_PERCENT = 100

#: Walls per ``CreateWalls`` call. A facade at half-metre spacing merges into a
#: few thousand rectangles, which is a large but unremarkable JSON body; this
#: keeps any single request well inside what the add-on will accept.
_BATCH = 250


@dataclass(frozen=True)
class BandMaterial:
    """One band's Surface and Building Material, as Archicad now holds them."""

    label: str
    surface_index: int
    surface_id: dict[str, str]
    material_id: dict[str, str]


@dataclass(frozen=True)
class ModelBandReport:
    """What the skin came to."""

    walls_drawn: int
    walls_removed: int
    layer: LayerState
    areas_m2: tuple[tuple[str, float], ...]
    borrowed_layers: tuple[str, ...]
    """Layers unhidden for the duration and put back."""

    @property
    def total_area_m2(self) -> float:
        return sum(area for _, area in self.areas_m2)

    def describe(self) -> str:
        lines = [
            f"  {self.walls_drawn} elements on layer {self.layer.index} "
            f"({self.walls_removed} from the previous run removed)"
        ]
        total = self.total_area_m2
        for label, area in self.areas_m2:
            share = area / total if total else 0.0
            lines.append(f"    {label:<9} {area:9.2f} m²   {share:6.2%}")
        lines.append(f"    {'total':<9} {total:9.2f} m²")
        if self.borrowed_layers:
            lines.append(
                f"    unhidden and restored: {', '.join(self.borrowed_layers)}"
            )
        return "\n".join(lines)


def ensure_band_materials(
    connection: ArchicadConnection, bands: Sequence[BandStyle]
) -> tuple[BandMaterial, ...]:
    """A Surface and a Building Material per band, in the reference colours.

    Both are created with ``overwriteExisting``, so a second run reuses the
    attributes it made the first time instead of filling the project's
    attribute list with duplicates. The names carry a prefix for the same
    reason: overwriting by name is only safe when the name is certainly ours.

    The colour goes on the Surface because that is what a renderer shows; the
    Building Material exists only because a wall has no surface of its own to
    set, and points at the Surface through ``cutSurfaceIndex``.
    """
    if not bands:
        return ()

    names = [naming.named(band.label) for band in bands]
    connection.run_tapir(
        "CreateSurfaces",
        {
            "surfaceDataArray": [
                {
                    "name": name,
                    "materialType": "General",
                    "surfaceColor": {
                        "red": band.rgb[0] / 255.0,
                        "green": band.rgb[1] / 255.0,
                        "blue": band.rgb[2] / 255.0,
                    },
                    # Percentages, 0..100, truncated to whole numbers -- not
                    # the 0..1 fractions the colour above takes. Sent as
                    # fractions they become ambient 1% and diffuse 0%, which
                    # is a surface that reflects nothing: the band colours are
                    # all correctly stored and the model still renders black.
                    #
                    # Both are full. The picture is a diagram, so a band has
                    # to read as its legend colour wherever it appears, not
                    # shade off with the angle of the face it is on -- that
                    # shading is a second, competing signal about sunlight in
                    # a drawing whose whole subject is sunlight. Specular and
                    # shine stay at zero for the same reason: a highlight
                    # reads as sun on a face that may have had none.
                    "ambientReflection": AMBIENT_PERCENT,
                    "diffuseReflection": DIFFUSE_PERCENT,
                    "specularReflection": 0,
                    "transparency": 0,
                    "shine": 0,
                }
                for name, band in zip(names, bands, strict=True)
            ],
            "overwriteExisting": True,
        },
    )

    indices = _surface_indices(connection, names)
    missing = [name for name in names if name not in indices]
    if missing:
        raise ArchicadError(
            f"Created surfaces {missing} but Archicad does not list them. "
            f"Without an index the band colour cannot be attached to a "
            f"building material."
        )

    made = connection.run_tapir(
        "CreateBuildingMaterials",
        {
            "buildingMaterialDataArray": [
                {
                    "name": name,
                    "id": naming.prefix(),
                    "cutSurfaceIndex": indices[name],
                    "cutFillPen": band.fill_pen,
                    "cutFillBackgroundPen": band.background_pen,
                }
                for name, band in zip(names, bands, strict=True)
            ],
            "overwriteExisting": True,
        },
    )
    materials = made.get("attributeIds") if isinstance(made, dict) else None
    if not isinstance(materials, list) or len(materials) != len(bands):
        raise ArchicadError(f"CreateBuildingMaterials returned {made!r}")

    return tuple(
        BandMaterial(
            label=band.label,
            surface_index=indices[name],
            surface_id={},
            material_id=dict(entry["attributeId"]),
        )
        for name, band, entry in zip(names, bands, materials, strict=True)
    )


def _surface_indices(connection: ArchicadConnection, names: Sequence[str]) -> dict[str, int]:
    """Look the freshly made surfaces back up by name, for their indices.

    ``CreateSurfaces`` answers with attribute *ids*, and a building material
    wants an *index*. There is no converting one to the other except by
    reading the list back.
    """
    response = connection.run_tapir("GetAttributesByType", {"attributeType": "Surface"})
    attributes = response.get("attributes") if isinstance(response, dict) else None
    if not isinstance(attributes, list):
        raise ArchicadError(f"GetAttributesByType returned no surface list: {response!r}")
    wanted = set(names)
    return {
        str(row["name"]): int(row["index"])
        for row in attributes
        if isinstance(row, dict) and str(row.get("name")) in wanted and "index" in row
    }


def fit_to_project(connection: ArchicadConnection, model: IfcModel) -> PlanTransform:
    """Fit the export's plan frame onto the project's, from the Zones.

    The IFC world frame and Archicad's project frame are not the same and need
    not be: an export made with the Survey Point option is north-aligned, so it
    is the *project* that is rotated relative to it -- on the reference project
    by 279.9 degrees, with a shift on top. Geometry computed from the export
    and created in the project unchanged therefore appears beside the building
    and turned, which is exactly how the first facade skin came out.

    Zones are the pairing because a Zone exists identically on both sides: the
    same room, with a GlobalId in the file and an outline in the project. The
    plan *box centre* of each is the point fitted, not a mean of its vertices,
    for the reason ``box_centre`` gives.
    """
    spaces = [
        element
        for element in model.elements
        if element.ifc_class == "IfcSpace" and len(element.mesh.vertices)
    ]
    if not spaces:
        raise ArchicadError(
            "The export has no IfcSpace, so there is nothing to fit the "
            "project frame against. Include Zones in the IFC export filter, or "
            "the skin cannot be placed on the building."
        )

    found = elements_by_ifc_ids(connection, [space.global_id for space in spaces])
    by_guid = {zone.guid: zone for zone in zones(connection)}

    source: list[list[float]] = []
    target: list[list[float]] = []
    for space in spaces:
        guids = found.get(space.global_id, [])
        if len(guids) != 1:
            continue
        zone = by_guid.get(guids[0])
        if zone is None or not zone.outline:
            continue
        source.append(box_centre(space.mesh.vertices))
        target.append(box_centre(np.array(zone.outline, dtype=np.float64)))

    if len(source) < 2:
        raise ArchicadError(
            f"Only {len(source)} Zone(s) could be paired between the export and "
            f"the project, and fitting the frame needs two. Without it the skin "
            f"would be built beside the building rather than on it."
        )

    transform = fit_plan_transform(np.array(source), np.array(target))
    if transform.rmse_m > MAX_FIT_RESIDUAL_M:
        raise ArchicadError(
            f"The export and the project do not agree on where the building is: "
            f"fitting {len(source)} Zones leaves {transform.rmse_m:.2f} m of "
            f"residual, over the {MAX_FIT_RESIDUAL_M:g} m limit. The export is "
            f"probably not of this project's current state."
        )
    return transform


def apply_favorite_to_defaults(connection: ArchicadConnection, favorite: str) -> None:
    """Set the Wall tool's defaults from a Favorite, before anything is built.

    This exists for one setting the API cannot reach any other way: **a wall's
    surface override**. A wall shows its building material's surface only when
    the override is off; with it on it shows the overriding surface, and every
    band then comes out the same colour whatever material it carries. On the
    reference project the Wall tool's default has the override on, so the first
    coloured skin was built correctly -- the right material, pointing at the
    right surface -- and rendered uniformly grey.

    Nothing in the add-on turns an override off. ``WallSettings`` is geometry
    only, and ``CreateWalls`` on this build has no ``favoriteName`` field.
    What is left is the tool defaults, which ``CreateWalls`` inherits: so a
    Favorite made once, by hand, with the override off is enough to fix every
    later run.

    The defaults are a shared, visible piece of the session -- changing them
    changes what the next wall somebody draws looks like -- so this is only
    ever done when a caller names a Favorite.
    """
    known = connection.run_tapir("GetFavoritesByType", {"elementType": "Wall"})
    names = known.get("favorites") if isinstance(known, dict) else None
    if isinstance(names, list) and favorite not in [str(name) for name in names]:
        raise ArchicadError(
            f"No Wall favorite named {favorite!r}. Make one in Archicad from a "
            f"wall whose surface override is switched off, name it {favorite!r}, "
            f"and the skin will take its colours from the band materials. "
            f"Without it every band renders in whatever surface the Wall tool "
            f"currently overrides with."
        )
    connection.run_tapir("ApplyFavoritesToElementDefaults", {"favorites": [favorite]})


def clear_model_bands(connection: ArchicadConnection, layer: LayerState) -> int:
    """Remove the previous run's skin. Returns how many went.

    Scoped to the tool's own layer, and verified by re-reading: a delete on a
    hidden layer answers ``success`` and removes nothing, and this layer is
    hidden in every layer combination the project had before the tool existed.
    """
    with borrowed(connection, identifiers=[layer.identifier]):
        mine = _walls_on_layer(connection, layer.index)
        if not mine:
            return 0
        connection.run_tapir("DeleteElements", {"elements": mine})
        left = _walls_on_layer(connection, layer.index)
        if left:
            raise ArchicadError(
                f"Asked Archicad to delete {len(mine)} elements from layer "
                f"{layer.index} and {len(left)} are still there. The layer is "
                f"probably locked; unlock it and run again, or the new skin "
                f"will be drawn on top of the old one."
            )
        return len(mine)


def draw_model_bands(
    connection: ArchicadConnection,
    *,
    bands: Sequence[BandStyle],
    rectangles: Sequence[Sequence[PanelRectangle]],
    layer_name: str,
    transform: PlanTransform,
    favorite: str | None = None,
    thickness_m: float = DEFAULT_THICKNESS_M,
    standoff_m: float = DEFAULT_STANDOFF_M,
) -> ModelBandReport:
    """Build the banded skin. ``rectangles`` is one sequence per band.

    Every rectangle becomes one thin wall standing ``standoff_m`` proud of the
    face it came from, in the band's colour -- upright on the facade, lying
    flat on a deck or under a soffit. The walls are created first (they land
    on the Wall tool's default layer, which cannot be chosen) and are then
    moved onto the tool's own layer.
    """
    if len(rectangles) != len(bands):
        raise ValueError(f"{len(rectangles)} rectangle groups for {len(bands)} bands")
    if thickness_m <= 0.0:
        raise ValueError(f"thickness_m must be positive, got {thickness_m}")

    # Reads are scoped to the current database, and a 3D window answers
    # ``GetElementsByType`` with what it happens to be showing rather than
    # with the model: on one run that returned 873 of the 1,968 walls the
    # previous pass had drawn, so the clear-out reported removing 873 and
    # believed itself finished. A floor plan sees all of them.
    ensure_model_database(connection)

    if favorite:
        apply_favorite_to_defaults(connection, favorite)

    layer = ensure_layer(connection, layer_name)
    materials = ensure_band_materials(connection, bands)
    removed = clear_model_bands(connection, layer)

    created: list[dict[str, Any]] = []
    areas: list[tuple[str, float]] = []
    for material, group in zip(materials, rectangles, strict=True):
        areas.append((material.label, sum(r.area_m2 for r in group)))
        data = [
            _wall_for(rectangle, material, transform, thickness_m, standoff_m)
            for rectangle in group
        ]
        for start in range(0, len(data), _BATCH):
            created.extend(_create_walls(connection, data[start : start + _BATCH]))

    borrowed = _move_to_layer(connection, created, layer)
    return ModelBandReport(
        walls_drawn=len(created),
        walls_removed=removed,
        layer=layer,
        areas_m2=tuple(areas),
        borrowed_layers=borrowed,
    )


def _wall_for(
    rectangle: PanelRectangle,
    material: BandMaterial,
    transform: PlanTransform,
    thickness_m: float,
    standoff_m: float,
) -> dict[str, Any]:
    """One rectangle as a thin wall, pushed clear of the face behind it.

    A wall is the only element this add-on will create with a building
    material on it — ``CreateSlabs``, ``CreateMeshes`` and ``CreateRoofs`` all
    take a shape and no material — so a flat patch has to be a wall too. That
    is less of a contortion than it sounds, because a wall *is* a box: give it
    the rectangle's long side as its length, the short side as its thickness
    and 40 mm as its height, and it lies on a balcony deck as a coloured
    plate. The only thing lost is that a wall cannot lean, which is why
    sloping faces are not panelled at all.

    The standoff is applied in the export's frame, *before* the transform, so
    that it stays perpendicular to the face. Applying it afterwards would push
    every rectangle along a direction rotated away from its own wall. Heights
    are untouched throughout: the two frames differ in plan only.
    """
    if rectangle.is_upright:
        offset = rectangle.normal * (standoff_m + thickness_m / 2.0)
        ends = transform.apply(np.vstack((rectangle.start + offset, rectangle.end + offset)))
        base_z = float(rectangle.start[2])
        height, width = float(rectangle.height_m), thickness_m
    else:
        # The centre-line of the plate, running along its longer plan axis.
        corners = rectangle.corners
        ends = transform.apply(
            np.vstack(((corners[0] + corners[3]) / 2.0, (corners[1] + corners[2]) / 2.0))
        )
        # A face looking up is coated on top; a soffit is coated underneath,
        # and a wall is always built upward from its base.
        up = float(rectangle.normal[2]) > 0.0
        level = float(corners[0][2])
        base_z = level + standoff_m if up else level - standoff_m - thickness_m
        height, width = thickness_m, float(rectangle.height_m)

    start, end = ends[0], ends[1]
    return {
        "begCoordinate": {"x": float(start[0]), "y": float(start[1])},
        "endCoordinate": {"x": float(end[0]), "y": float(end[1])},
        "zCoordinate": base_z,
        "height": height,
        "thickness": width,
        "structureType": "Basic",
        "buildingMaterialId": material.material_id,
        "referenceLineLocation": "Center",
    }


def _create_walls(
    connection: ArchicadConnection, data: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Create one batch, refusing to report a half-drawn band as a whole one."""
    response = connection.run_tapir("CreateWalls", {"wallsData": data})
    elements = response.get("elements") if isinstance(response, dict) else None
    if not isinstance(elements, list):
        raise ArchicadError(f"CreateWalls returned no element list: {response!r}")

    made = [item for item in elements if isinstance(item, dict) and "elementId" in item]
    problems = {
        str((item.get("error") or {}).get("message", "unknown error"))
        for item in elements
        if isinstance(item, dict) and "error" in item
    }
    if problems:
        raise ArchicadError(
            f"CreateWalls failed for {len(elements) - len(made)} of {len(data)} "
            f"elements:\n  " + "\n  ".join(sorted(problems)[:5])
        )
    return made


def _move_to_layer(
    connection: ArchicadConnection, elements: list[dict[str, Any]], layer: LayerState
) -> tuple[str, ...]:
    """Put every new wall on the tool's layer. Returns the layers borrowed.

    A wall cannot be told its layer at creation, so this is a second pass. It
    is also where the hidden-layer trap bites hardest: the Wall tool's default
    layer is often one the project keeps switched off, and the move then
    reports success for every element and moves none of them. So the source
    layers are read from the elements themselves, shown for the duration, and
    put back exactly as they were.
    """
    if not elements:
        return ()

    rows = _details(connection, elements)
    sources = {int(row["layerIndex"]) for row in rows if "layerIndex" in row} - {layer.index}
    if not sources:
        return ()

    with borrowed(connection, sorted(sources)) as restored:
        for start in range(0, len(elements), _BATCH):
            connection.run_tapir(
                "SetDetailsOfElements",
                {
                    "elementsWithDetails": [
                        {
                            "elementId": element["elementId"],
                            "details": {"layerIndex": layer.index},
                        }
                        for element in elements[start : start + _BATCH]
                    ]
                },
            )
        stayed = [
            row
            for row in _details(connection, elements)
            if int(row.get("layerIndex", -1)) != layer.index
        ]
        if stayed:
            raise ArchicadError(
                f"{len(stayed)} of {len(elements)} new elements would not move to "
                f"layer {layer.index}. They are on a locked layer, and are now "
                f"loose in the model -- delete them by layer before running again."
            )
    return restored


def _details(
    connection: ArchicadConnection, elements: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Element rows, in batches, because the whole skin is thousands of them."""
    rows: list[dict[str, Any]] = []
    for start in range(0, len(elements), _BATCH):
        response = connection.run_tapir(
            "GetDetailsOfElements", {"elements": elements[start : start + _BATCH]}
        )
        got = response.get("detailsOfElements") if isinstance(response, dict) else None
        if isinstance(got, list):
            rows.extend(row for row in got if isinstance(row, dict))
    return rows


def _walls_on_layer(connection: ArchicadConnection, index: int) -> list[dict[str, Any]]:
    """Every Wall sitting on one layer."""
    response = connection.run_tapir("GetElementsByType", {"elementType": "Wall"})
    elements = response.get("elements") if isinstance(response, dict) else None
    if not isinstance(elements, list) or not elements:
        return []
    rows = _details(connection, elements)
    if len(rows) != len(elements):
        raise ArchicadError(
            f"Asked for the details of {len(elements)} walls and got {len(rows)}. "
            f"Without them the tool's own elements cannot be told from the "
            f"building's, and deleting the wrong ones is not recoverable."
        )
    return [
        element
        for element, row in zip(elements, rows, strict=True)
        if int(row.get("layerIndex", -1)) == index
    ]
