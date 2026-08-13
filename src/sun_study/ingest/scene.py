"""Turning an IFC model into something the analysis engine can run on.

This is where the domain assumptions live -- the ones in section 6 of the brief
that change the headline compliance percentage. None of them is silently
applied. Every one is a field on ``SceneConfig`` with a stated default, and
every one is echoed in ``Scene.provenance`` so the run's assumptions travel
with its numbers.

Resolving which space a window serves
-------------------------------------
Archicad only writes ``IfcRelSpaceBoundary`` when space boundary export is
enabled, so it is frequently absent. Windows are resolved by boundary where one
exists and geometrically otherwise, and the count resolved by each route is
reported. A silent fallback is how a mis-assigned window becomes a wrong
apartment percentage that nobody questions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import numpy.typing as npt

from sun_study.core.geometry import TriangleMesh
from sun_study.core.orientation import SiteOrientation
from sun_study.core.sampling import (
    DEFAULT_GRID_SPACING_M,
    DEFAULT_SURFACE_OFFSET_M,
    SamplePoints,
    grid_on_rectangle,
)
from sun_study.ingest.ifc import IfcElement, IfcModel

FloatArray = npt.NDArray[np.float64]

__all__ = [
    "Scene",
    "SceneConfig",
    "WindowAssignment",
    "build_scene",
    "planar_face_grid",
]

ResolutionMethod = Literal["space-boundary", "geometric", "unresolved"]

# A window more than this from any space is not serving one. Roughly a room's
# depth: far enough to tolerate a deep reveal, near enough that a stairwell
# window does not get attached to a bedroom.
WINDOW_MAX_DISTANCE_M = 2.0


@dataclass(frozen=True)
class SceneConfig:
    """The domain assumptions, all explicit and all reported.

    Defaults are the ones proposed in ``docs/decisions.md`` D6 to D10. They are
    defaults, not answers: D6 in particular changes the headline percentage and
    needs confirming per project.
    """

    timezone: str
    """D3. Required. IFC carries no IANA zone and the tool never infers one."""

    living_room_space_names: tuple[str, ...] = ("Living Room",)
    """D6. Matched against ``IfcSpace.LongName``, then ``Name``, case-insensitively.

    Empty means "assess every space", which is almost never right for the ADG
    but is useful for a first look at an unfamiliar model.
    """

    balcony_name_prefixes: tuple[str, ...] = ("Balcony",)
    """D7. Slabs whose name starts with one of these are private open space."""

    open_space_level_tolerance_m: float = 0.5
    """How near an apartment's floor must be to a balcony's top surface.

    See ``_open_space_owner`` for why proximity alone cannot decide this.
    """

    open_space_max_distance_m: float = 3.0
    """How far private open space may sit from the apartment it belongs to.

    A balcony projects beyond the facade, so it is further from its space's
    bounding box than a window is. Beyond this it is treated as communal rather
    than attached to whichever apartment happens to be closest.
    """

    grid_spacing_m: float = DEFAULT_GRID_SPACING_M
    surface_offset_m: float = DEFAULT_SURFACE_OFFSET_M
    open_space_height_m: float = 1.0
    """Height above finished level at which open space is assessed."""

    context_radius_m: float | None = None
    """D9. Occluders beyond this distance from the subject are dropped.

    ``None`` means no cutoff, and the run reports that it used none.
    """

    include_vegetation: bool = False
    """D10. Vegetation is excluded, always, and the output says so."""

    def describe(self) -> str:
        radius = (
            f"{self.context_radius_m:g} m" if self.context_radius_m is not None else "unlimited"
        )
        rooms = ", ".join(self.living_room_space_names) or "all spaces"
        return (
            f"timezone {self.timezone} | living rooms matched by [{rooms}] | "
            f"balconies by prefix {list(self.balcony_name_prefixes)} | "
            f"grid {self.grid_spacing_m * 1000:.0f} mm | "
            f"surface offset {self.surface_offset_m * 1000:.0f} mm | "
            f"open space at {self.open_space_height_m:g} m | "
            f"context radius {radius} | "
            f"vegetation {'included' if self.include_vegetation else 'excluded'}"
        )


@dataclass(frozen=True)
class WindowAssignment:
    """Which space a window was judged to serve, and how that was decided."""

    window_id: str
    window_name: str
    space_id: str | None
    space_name: str | None
    method: ResolutionMethod


@dataclass(frozen=True)
class Scene:
    """Geometry, samples and orientation, ready for the analysis engine."""

    orientation: SiteOrientation
    occluders: TriangleMesh
    window_samples: SamplePoints
    open_space_samples: SamplePoints
    assignments: tuple[WindowAssignment, ...]
    config: SceneConfig
    provenance: dict[str, object] = field(default_factory=dict)

    def describe(self) -> str:
        by_method: dict[str, int] = {}
        for assignment in self.assignments:
            by_method[assignment.method] = by_method.get(assignment.method, 0) + 1
        routes = ", ".join(f"{name} {count}" for name, count in sorted(by_method.items()))
        return (
            f"{self.orientation.describe()}\n"
            f"  {self.config.describe()}\n"
            f"  occluders {self.occluders.triangle_count} triangles | "
            f"{len(self.window_samples)} window samples | "
            f"{len(self.open_space_samples)} open space samples\n"
            f"  window to space resolution: {routes or 'none'}"
        )


def _triangle_normals_and_areas(mesh: TriangleMesh) -> tuple[FloatArray, FloatArray]:
    triangles = mesh.triangles()
    cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    areas = np.linalg.norm(cross, axis=1) / 2.0
    with np.errstate(invalid="ignore", divide="ignore"):
        normals = cross / (2.0 * areas[:, None])
    return np.nan_to_num(normals), areas


def planar_face_grid(
    mesh: TriangleMesh,
    parent_id: str,
    outward_hint: FloatArray,
    *,
    spacing_m: float = DEFAULT_GRID_SPACING_M,
    surface_offset_m: float = DEFAULT_SURFACE_OFFSET_M,
    angle_tolerance_deg: float = 5.0,
) -> SamplePoints | None:
    """Grid the dominant planar face of a mesh, facing along ``outward_hint``.

    A window exported from Archicad is a thin solid, so it has two large
    opposite faces. This picks the one whose normal best agrees with the hint
    -- normally "away from the space this window serves" -- and grids its
    extent.

    Works for a rotated window because the in-plane basis is derived from the
    face normal rather than assumed axis-aligned. It does *not* handle a curved
    or faceted window as anything but its largest flat face, which is a real
    limitation for curtain walling and is recorded rather than hidden.

    Returns ``None`` when the mesh has no usable planar face.
    """
    if mesh.triangle_count == 0:
        return None

    normals, areas = _triangle_normals_and_areas(mesh)
    usable = areas > 1e-12
    if not usable.any():
        return None

    hint = np.asarray(outward_hint, dtype=np.float64).reshape(3)
    norm = float(np.linalg.norm(hint))
    hint = hint / norm if norm > 0 else np.array([0.0, 1.0, 0.0])

    # Score each candidate direction by the face area that agrees with it,
    # keeping only those pointing broadly the way the hint does.
    alignment = normals @ hint
    outward = usable & (alignment > 0.0)
    if not outward.any():
        return None

    best = int(np.argmax(np.where(outward, areas * alignment, -np.inf)))
    normal = normals[best]

    cosine = float(np.cos(np.radians(angle_tolerance_deg)))
    coplanar = usable & ((normals @ normal) >= cosine)
    vertices = mesh.triangles()[coplanar].reshape(-1, 3)
    if len(vertices) < 3:
        return None

    # An in-plane orthonormal basis, chosen so the "up" axis is as vertical as
    # the plane allows; a window grid that ran diagonally would still be
    # correct but would be much harder to read in a diagnostic dump.
    world_up = np.array([0.0, 0.0, 1.0])
    if abs(float(normal @ world_up)) > 0.99:
        world_up = np.array([0.0, 1.0, 0.0])
    axis_u = np.cross(world_up, normal)
    axis_u /= np.linalg.norm(axis_u)
    axis_v = np.cross(normal, axis_u)

    origin = vertices[0]
    local = vertices - origin
    u_extent = local @ axis_u
    v_extent = local @ axis_v

    corner = origin + u_extent.min() * axis_u + v_extent.min() * axis_v
    edge_u = (u_extent.max() - u_extent.min()) * axis_u
    edge_v = (v_extent.max() - v_extent.min()) * axis_v
    if np.linalg.norm(edge_u) < 1e-9 or np.linalg.norm(edge_v) < 1e-9:
        return None

    # grid_on_rectangle takes its normal from cross(edge_u, edge_v); flip the
    # pair if that would face inward.
    if float(np.cross(edge_u, edge_v) @ normal) < 0.0:
        edge_u, edge_v = edge_v, edge_u

    return grid_on_rectangle(
        corner,
        edge_u,
        edge_v,
        parent_id,
        spacing_m=spacing_m,
        surface_offset_m=surface_offset_m,
    )


def _distance_to_box(point: FloatArray, lower: FloatArray, upper: FloatArray) -> float:
    """Euclidean distance from a point to an axis-aligned box, zero if inside."""
    outside = np.maximum(np.maximum(lower - point, point - upper), 0.0)
    return float(np.linalg.norm(outside))


def _nearest_space(
    element: IfcElement, spaces: tuple[IfcElement, ...], max_distance_m: float
) -> IfcElement | None:
    """The space an element most likely belongs to, or None if none is close.

    A window sits in the wall and a balcony hangs off it, so neither is
    *inside* its space; nearest-bounding-box is the workable test. The distance
    limit matters: without it every stairwell window and communal terrace gets
    attached to whichever apartment happens to be nearest, which is a silently
    wrong answer rather than a missing one.
    """
    if not spaces:
        return None
    nearest = min(spaces, key=lambda s: _distance_to_box(element.centroid, *s.bounds))
    if _distance_to_box(element.centroid, *nearest.bounds) > max_distance_m:
        return None
    return nearest


def _assign_windows(
    model: IfcModel, windows: tuple[IfcElement, ...], spaces: tuple[IfcElement, ...]
) -> tuple[WindowAssignment, ...]:
    """Resolve each window to the space it serves, recording the route taken."""
    by_id = {space.global_id: space for space in spaces}
    assignments = []

    for window in windows:
        boundary_space = model.space_boundaries.get(window.global_id)
        if boundary_space is not None and boundary_space in by_id:
            space = by_id[boundary_space]
            assignments.append(
                WindowAssignment(
                    window.global_id, window.name, space.global_id, space.name, "space-boundary"
                )
            )
            continue

        # Geometric fallback: the space whose bounding box the window is
        # nearest to. A window sits in the wall, so it is just outside its own
        # space rather than inside it, and a containment test would find
        # nothing.
        if not spaces:
            assignments.append(
                WindowAssignment(window.global_id, window.name, None, None, "unresolved")
            )
            continue

        nearest = _nearest_space(window, spaces, WINDOW_MAX_DISTANCE_M)
        if nearest is None:
            assignments.append(
                WindowAssignment(window.global_id, window.name, None, None, "unresolved")
            )
        else:
            assignments.append(
                WindowAssignment(
                    window.global_id, window.name, nearest.global_id, nearest.name, "geometric"
                )
            )

    return tuple(assignments)


def _is_living_room(space: IfcElement, config: SceneConfig) -> bool:
    """D6, in code. Matches ``LongName`` first, then ``Name``.

    Archicad puts the Zone *category* in ``LongName`` ("Living Room") and the
    apartment identifier in ``Name`` ("Apartment L00-A"). Checking only one of
    them matches nothing on half of all real models, and matching nothing looks
    like a compliant building with no living rooms rather than like a bug.
    """
    if not config.living_room_space_names:
        return True
    haystack = f"{space.long_name} {space.name}".casefold()
    return any(name.casefold() in haystack for name in config.living_room_space_names)


def _open_space_owner(
    slab: IfcElement,
    spaces: tuple[IfcElement, ...],
    max_distance_m: float,
    level_tolerance_m: float,
) -> tuple[IfcElement | None, str]:
    """The apartment a balcony belongs to: the one you step out from.

    Nearest-bounding-box alone gets this wrong, and wrong in a way that looks
    right. A balcony sits at its own apartment's floor level, which means it is
    also flush against the ceiling of the apartment below -- so the two are
    *exactly* equidistant and the winner is decided by iteration order. In the
    fixture that silently attributed both upper balconies to the ground-floor
    apartments and left the upper floors with no open space at all.

    The disambiguator is vertical: the apartment stands on top of its balcony,
    so its floor should be level with the slab's top surface. Where no space
    qualifies the nearest one is used and the fallback is counted, because a
    balcony modelled at an unusual level is still somebody's balcony.

    Returns the owner and the route taken.
    """
    if not spaces:
        return None, "unresolved"

    reachable = [
        space
        for space in spaces
        if _distance_to_box(slab.centroid, *space.bounds) <= max_distance_m
    ]
    if not reachable:
        return None, "unresolved"

    slab_top = float(slab.bounds[1][2])
    standing_on = [
        space
        for space in reachable
        if abs(float(space.bounds[0][2]) - slab_top) <= level_tolerance_m
    ]
    if standing_on:
        owner = min(standing_on, key=lambda s: _distance_to_box(slab.centroid, *s.bounds))
        return owner, "level-matched"

    owner = min(reachable, key=lambda s: _distance_to_box(slab.centroid, *s.bounds))
    return owner, "nearest-fallback"


def build_scene(model: IfcModel, config: SceneConfig) -> Scene:
    """Assemble an analysis-ready scene from an IFC model.

    The subject building is part of the occluder set, per brief section 5.6:
    self-shading from balconies, fins and reveals is the effect being measured,
    not noise to be removed.
    """
    orientation = model.orientation(config.timezone)

    spaces = model.of_class("IfcSpace")
    windows = model.of_class("IfcWindow")
    assignments = _assign_windows(model, windows, spaces)

    space_by_id = {space.global_id: space for space in spaces}
    living_rooms = {space.global_id for space in spaces if _is_living_room(space, config)}

    occluders = model.occluder_mesh()
    if config.context_radius_m is not None:
        occluders = _clip_to_radius(model, config.context_radius_m)

    window_groups, assessed, skipped = [], 0, 0
    for assignment in assignments:
        window = model.by_id(assignment.window_id)
        if window is None or assignment.space_id is None:
            skipped += 1
            continue
        if assignment.space_id not in living_rooms:
            skipped += 1
            continue

        space = space_by_id[assignment.space_id]
        # Outward is away from the space the window serves. Using the building
        # centroid instead would point the wrong way for a courtyard window.
        hint = window.centroid - space.centroid
        samples = planar_face_grid(
            window.mesh,
            assignment.space_id,
            hint,
            spacing_m=config.grid_spacing_m,
            surface_offset_m=config.surface_offset_m,
        )
        if samples is None:
            skipped += 1
            continue
        window_groups.append(samples)
        assessed += 1

    balcony_groups, unattached_open_space = [], 0
    open_space_routes: dict[str, int] = {}
    for slab in model.of_class("IfcSlab"):
        if not any(
            slab.name.casefold().startswith(prefix.casefold())
            for prefix in config.balcony_name_prefixes
        ):
            continue

        # Parent open space to the apartment it serves, not to the slab, so
        # window and open-space results join on one key downstream. Communal
        # open space has no apartment and is counted separately rather than
        # attached to the nearest one.
        owner, route = _open_space_owner(
            slab,
            spaces,
            config.open_space_max_distance_m,
            config.open_space_level_tolerance_m,
        )
        open_space_routes[route] = open_space_routes.get(route, 0) + 1
        if owner is None:
            unattached_open_space += 1
            continue

        samples = planar_face_grid(
            slab.mesh,
            owner.global_id,
            np.array([0.0, 0.0, 1.0]),
            spacing_m=config.grid_spacing_m,
            surface_offset_m=config.open_space_height_m,
        )
        if samples is not None:
            balcony_groups.append(samples)

    provenance: dict[str, object] = {
        "source": model.path.name,
        "schema": model.schema,
        "length_unit_scale": model.length_unit_scale,
        "site_elevation_m": model.site_elevation_m,
        "true_north_bearing_deg": orientation.normalised_bearing_deg,
        "windows_total": len(windows),
        "windows_assessed": assessed,
        "windows_skipped": skipped,
        "spaces_total": len(spaces),
        "living_rooms_matched": len(living_rooms),
        "balconies_matched": len(balcony_groups),
        "open_space_unattached": unattached_open_space,
        "open_space_resolution": open_space_routes,
        "resolved_by_space_boundary": sum(1 for a in assignments if a.method == "space-boundary"),
        "resolved_geometrically": sum(1 for a in assignments if a.method == "geometric"),
        "unresolved": sum(1 for a in assignments if a.method == "unresolved"),
        "occluder_triangles": occluders.triangle_count,
        "vegetation_included": config.include_vegetation,
    }

    return Scene(
        orientation=orientation,
        occluders=occluders,
        window_samples=SamplePoints.concatenate(window_groups),
        open_space_samples=SamplePoints.concatenate(balcony_groups),
        assignments=assignments,
        config=config,
        provenance=provenance,
    )


def _clip_to_radius(model: IfcModel, radius_m: float) -> TriangleMesh:
    """Drop occluders whose centroid is beyond ``radius_m`` of the spaces.

    The subject is taken to be the extent of the analysed spaces, so context
    distance is measured from the building being assessed rather than from the
    project origin, which in Archicad is often an arbitrary survey point.
    """
    spaces = model.of_class("IfcSpace")
    if not spaces:
        return model.occluder_mesh()

    centres = np.array([space.centroid for space in spaces])
    subject = centres.mean(axis=0)

    kept = [
        element.mesh
        for element in model.elements
        if element.ifc_class != "IfcSpace"
        and float(np.linalg.norm(element.centroid[:2] - subject[:2])) <= radius_m
    ]
    return TriangleMesh.concatenate(kept)
