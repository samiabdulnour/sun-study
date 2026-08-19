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

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

import numpy as np
import numpy.typing as npt

from sun_study.core.geometry import TriangleMesh
from sun_study.core.occlusion import Occluder
from sun_study.core.orientation import SiteOrientation
from sun_study.core.sampling import (
    DEFAULT_GRID_SPACING_M,
    DEFAULT_SURFACE_OFFSET_M,
    FaceSelection,
    SamplePoints,
    grid_on_rectangle,
    horizontal_grid,
    triangle_samples,
)
from sun_study.ingest.ifc import IfcElement, IfcModel

FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]

__all__ = [
    "DEFAULT_MASSING_SPACING_M",
    "MassingConfig",
    "MassingScene",
    "Scene",
    "SceneConfig",
    "SceneConfigError",
    "WindowAssignment",
    "build_massing_scene",
    "build_scene",
    "planar_face_grid",
]

ResolutionMethod = Literal["space-boundary", "geometric", "unresolved"]

# A window more than this from any space is not serving one. Roughly a room's
# depth: far enough to tolerate a deep reveal, near enough that a stairwell
# window does not get attached to a bedroom.
WINDOW_MAX_DISTANCE_M = 2.0

#: How much nearer a room this run is not assessing has to be before a window
#: is taken to belong to it rather than to the apartment it resolved to. See
#: ``_belongs_elsewhere`` for why a bare comparison is not enough.
UNASSESSED_OWNER_MARGIN_M = 0.5


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

    Ignored when ``livable_opening_suffix`` is set, because that route
    identifies the glazing directly and does not need the room named.
    """

    livable_opening_suffix: str | None = None
    """D24. Openings whose ID ends with this are the living-room glazing.

    The alternative to naming rooms, and the better one where a practice zones
    by *unit* rather than by room -- there is then no living-room Zone to match
    against, and every window in the unit would otherwise count, bedrooms and
    bathrooms included.

    Matched case-insensitively against ``Name``, which is where Archicad puts
    the element ID. The space each marked opening serves becomes an apartment,
    so this replaces ``living_room_space_names`` rather than narrowing it.
    """

    livable_opening_classes: tuple[str, ...] = ("IfcWindow", "IfcDoor")
    """Which classes ``livable_opening_suffix`` is matched against.

    Doors are in the default deliberately. A living room's glazing is usually
    a balcony slider, which Archicad models with the Door tool -- 110 of 252
    marked openings in one reference export -- so windows alone would silently
    measure well under half the glass.
    """

    balcony_name_prefixes: tuple[str, ...] = ("Balcony",)
    """D7. Slabs whose name starts with one of these are private open space.

    The fallback route. Where the practice zones its balconies, prefer
    ``open_space_zone_layers``: a Zone is the balcony's actual extent, while a
    slab is whatever was drawn under it.
    """

    apartment_zone_layers: tuple[str, ...] = ()
    """Zones on these Archicad layers are the apartments being assessed.

    Empty means every ``IfcSpace`` is a candidate. Naming layers matters once
    a file carries more than one kind of Zone -- unit zones, GFA zones, a
    SEPP 65 duplicate set -- because counting a GFA zone as an apartment
    changes the denominator of the compliance percentage without looking wrong.
    """

    apartment_zone_names: tuple[str, ...] = ()
    """Only zones with these names are apartments, on top of the layer filter.

    A layer is not always enough. One real project keeps 15 dwellings and 20
    balconies together on ``06 | Zone.Units``, told apart only by name --
    ``G08`` against ``BY`` -- and a balcony counted as an apartment is silent:
    it simply has no living room, so it reads as a flat that failed rather
    than as one that does not exist. Twenty of those in a denominator of
    thirty-five halves the compliance percentage.
    """

    open_space_zone_layers: tuple[str, ...] = ()
    """Zones on these Archicad layers are private open space.

    Takes precedence over ``balcony_name_prefixes`` when set, and resolves D7
    in the direction the decision hoped for.
    """

    open_space_zone_names: tuple[str, ...] = ()
    """Only zones with these names are private open space, on top of the layer.

    The mirror of ``apartment_zone_names``, and needed for the same reason: a
    practice that keeps its dwellings and its balconies on one layer tells
    them apart by name alone. On the reference project, ``06 | Zone.Units``
    carries 15 dwellings named ``G08`` and 20 balconies named ``BY``, so
    without this the balconies can only be reached by naming a layer that
    would drag the dwellings in with them, and every apartment reports as
    having no private open space at all.
    """

    floor_patch_spacing_m: float | None = None
    """Grid spacing for the sun patch drawn on the floor, or None for no patch.

    Opt-in, because it is a second ray-cast pass over a second set of samples
    and it answers a different question. The assessment asks whether the
    *glazing* saw the sun for two hours; a patch asks how far into the room
    the sun reached at one instant, which is what a study drawing shows and
    what nobody can read off a duration.

    Coarser than the assessment grid by default. The patch is drawn, not
    quoted: 250 mm is a stepped edge a reader understands as a sampling
    resolution, while 200 mm over a whole floor plate is four times the
    rectangles for a difference nobody can see on an A1.
    """

    floor_patch_height_m: float = 0.05
    """How far above the floor the patch is sampled.

    Not the 1 m open-space plane: this is a picture of sun *on the floor*, and
    sampling it at waist height would put the patch a metre out of place under
    a low winter sun -- at 20 degrees elevation, 2.7 m out.
    """

    exclude_above_m: float | None = None
    """Drop every element whose geometry lies entirely above this height, in
    project metres.

    The knob for hotlinked masters. A practice parks its unit-type masters
    high above the site, and they export as ordinary geometry on the *same
    layers* as the real building -- ``01 | Wall.External`` is the wall of the
    real tower and of the master alike -- so no layer filter can separate
    them, and geometry above a building can only shade it. On the reference
    project the real building tops out at 85 m and three sets of masters sit
    at 157 m, 166 m and 262 m; cutting at 100 m drops them and, with them,
    four Zones that would otherwise have entered the denominator as
    apartments.

    Entirely above, not partly: a roof that crosses the plane is kept. The
    height is a project coordinate, which is what the overhead warning reports
    so the number can be read straight off a run that did not use this.
    """

    context_layers: tuple[str, ...] = ()
    """Elements on these layers shade the subject but are never measured.

    Kept separate from a distance cutoff: a neighbouring building is context
    however close it stands.
    """

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
        if self.livable_opening_suffix:
            rooms = (
                f"openings whose ID ends '{self.livable_opening_suffix}' "
                f"({'/'.join(c.removeprefix('Ifc') for c in self.livable_opening_classes)})"
            )
        else:
            rooms = ", ".join(self.living_room_space_names) or "all spaces"
        named = f" named {list(self.open_space_zone_names)}" if self.open_space_zone_names else ""
        open_space = (
            f"zones on layers {list(self.open_space_zone_layers)}{named}"
            if self.open_space_zone_layers
            else f"slabs by prefix {list(self.balcony_name_prefixes)}"
        )
        zones = (
            f"apartment zones on layers {list(self.apartment_zone_layers)} | "
            if self.apartment_zone_layers
            else ""
        )
        if self.apartment_zone_names:
            zones += f"named {list(self.apartment_zone_names)} | "
        context = f"context layers {list(self.context_layers)} | " if self.context_layers else ""
        patch = (
            f"floor patch at {self.floor_patch_spacing_m:g} m | "
            if self.floor_patch_spacing_m
            else ""
        )
        cut = (
            f"geometry above {self.exclude_above_m:g} m dropped | "
            if self.exclude_above_m is not None
            else ""
        )
        return (
            f"timezone {self.timezone} | living rooms matched by [{rooms}] | "
            f"{zones}{context}"
            f"private open space from {open_space} | "
            f"grid {self.grid_spacing_m * 1000:.0f} mm | "
            f"surface offset {self.surface_offset_m * 1000:.0f} mm | "
            f"open space at {self.open_space_height_m:g} m | "
            f"context radius {radius} | "
            f"{patch}"
            f"{cut}"
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

    floor_samples: SamplePoints | None = None
    """The floors of the assessed apartments and their open space, gridded in
    plan. Present only when ``SceneConfig.floor_patch_spacing_m`` asks for it."""

    floor_is_open_space: BoolArray | None = None
    """Which of those cells are balcony rather than room, parallel to
    ``floor_samples``.

    Both are parented to the apartment, because a patch belongs to the flat it
    is drawn on. But the ADG asks separately about the living room and about
    the private open space, and the office's own drawings annotate them
    separately too, so the two have to stay tellable apart after the fact."""

    glazed_occluders: TriangleMesh | None = None
    """The occluder set with the glazing taken out of it.

    A window is a solid in the export, so the set that answers "is this pane
    lit" is exactly the wrong one for "did the sun get through that pane and
    onto the floor" -- the pane would shade the room behind it. The opening in
    the wall is a real hole in the wall mesh, so removing the glazing leaves
    the sun a way in and leaves every wall, sill and balcony above still
    blocking it."""

    def describe(self) -> str:
        by_method: dict[str, int] = {}
        for assignment in self.assignments:
            by_method[assignment.method] = by_method.get(assignment.method, 0) + 1
        routes = ", ".join(f"{name} {count}" for name, count in sorted(by_method.items()))
        lines = [
            self.orientation.describe(),
            f"  {self.config.describe()}",
            f"  occluders {self.occluders.triangle_count} triangles | "
            f"{len(self.window_samples)} window samples | "
            f"{len(self.open_space_samples)} open space samples"
            + (f" | {len(self.floor_samples)} floor samples" if self.floor_samples else ""),
            f"  window to space resolution: {routes or 'none'}",
            f"  {self._openings_summary()}",
        ]
        overhead = self.provenance.get("geometry_above_building")
        if isinstance(overhead, int) and overhead:
            lines.append(
                f"  WARNING: {overhead} elements "
                f"({self.provenance.get('geometry_above_building_triangles')} triangles) "
                f"sit entirely above the apartments -- up to "
                f"{self.provenance.get('geometry_above_building_top_m')} m against a "
                f"top-of-apartments of {self.provenance.get('top_of_apartments_m')} m."
            )
            lines.append(
                "    Geometry above a building can only shade it. Hotlinked "
                "unit-type masters are parked overhead and export as ordinary "
                "geometry, which makes every apartment dark and reads as a badly "
                "oriented scheme. Exclude the hotlink layers from the export."
            )
        return "\n".join(lines)

    def _openings_summary(self) -> str:
        """How many openings each apartment got, and what that implies.

        Printed rather than buried in the provenance because it is the one
        line that says which criterion the run is actually answering. Marking
        only living-room glazing gives most apartments one or two openings;
        marking every room that requires sunlight gives a two-bedroom
        apartment three or four, and folding those together lets an apartment
        pass ADG 4A-1 on sun its living room never sees.
        """
        histogram = self.provenance.get("openings_per_apartment") or {}
        if not isinstance(histogram, dict) or not histogram:
            return "openings per apartment: none assessed"

        total = sum(histogram.values())
        weighted = sum(count * apartments for count, apartments in histogram.items())
        spread = ", ".join(f"{count}x{apartments}" for count, apartments in histogram.items())
        return (
            f"openings per apartment: {spread} "
            f"(mean {weighted / total:.2f} across {total} apartments)"
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
    clip_to_face: bool = False,
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

    The grid covers the face's **bounding rectangle**, which is the face itself
    for a window and is not for a room. ``clip_to_face`` drops the samples that
    fall outside the face's own triangles: an L-shaped flat otherwise gets a
    grid over the whole rectangle it fits inside, and a sun patch drawn from it
    reaches into rooms the apartment does not contain. Measured on the
    reference project, 37% of the drawn patch area sat outside any apartment
    before this. It is off by default because a window needs no clipping and
    the test is not free.

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

    grid = grid_on_rectangle(
        corner,
        edge_u,
        edge_v,
        parent_id,
        spacing_m=spacing_m,
        surface_offset_m=surface_offset_m,
    )
    if not clip_to_face:
        return grid
    return _clip_to_triangles(grid, vertices, origin, axis_u, axis_v, surface_offset_m, normal)


def _clip_to_triangles(
    grid: SamplePoints,
    vertices: FloatArray,
    origin: FloatArray,
    axis_u: FloatArray,
    axis_v: FloatArray,
    surface_offset_m: float,
    normal: FloatArray,
) -> SamplePoints | None:
    """Keep only the samples that land on the face, not merely in its box.

    Both the samples and the face are flattened onto the face's own basis and
    tested by barycentric sign, which is exact for a triangulated surface and
    needs nothing but numpy.
    """
    # The samples were lifted off the surface by the offset; put them back on
    # the plane before testing, or every one of them misses it.
    on_plane = grid.positions - surface_offset_m * normal - origin
    points = np.column_stack((on_plane @ axis_u, on_plane @ axis_v))

    corners = vertices.reshape(-1, 3, 3) - origin
    triangles = np.stack((corners @ axis_u, corners @ axis_v), axis=-1)  # (n_triangles, 3, 2)

    a, b, c = triangles[:, 0, :], triangles[:, 1, :], triangles[:, 2, :]
    v0, v1 = b - a, c - a
    denominator = v0[:, 0] * v1[:, 1] - v1[:, 0] * v0[:, 1]
    usable = np.abs(denominator) > 1e-12
    if not usable.any():
        return grid

    a, v0, v1 = a[usable], v0[usable], v1[usable]
    denominator = denominator[usable]

    offset = points[:, None, :] - a[None, :, :]
    u = (offset[..., 0] * v1[None, :, 1] - offset[..., 1] * v1[None, :, 0]) / denominator
    v = (offset[..., 1] * v0[None, :, 0] - offset[..., 0] * v0[None, :, 1]) / denominator
    # A small tolerance so a sample exactly on a shared edge belongs to the
    # face rather than falling between two triangles of it.
    inside = ((u >= -1e-9) & (v >= -1e-9) & (u + v <= 1.0 + 1e-9)).any(axis=1)
    if not inside.any():
        return None

    return SamplePoints(
        positions=grid.positions[inside],
        normals=grid.normals[inside],
        parent_ids=tuple(np.asarray(grid.parent_ids)[inside].tolist()),
        areas=grid.areas[inside],
        surface_offset_m=grid.surface_offset_m,
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


def _belongs_elsewhere(
    element: IfcElement, owner: IfcElement, unassessed: tuple[IfcElement, ...]
) -> bool:
    """Whether a room this run is not assessing is the better owner.

    Clearly nearer, not merely nearer. A living-room slider sits in the wall
    between the unit Zone and its own balcony Zone, near enough to equidistant
    from both that a bare nearest-wins hands it to whichever the iteration
    reaches first: on the reference project, resolving that way lost 3 of 40
    marked openings. A neighbouring apartment's window, which is what this is
    for, is metres away.
    """
    if not unassessed:
        return False
    here = _distance_to_box(element.centroid, *owner.bounds)
    there = min(_distance_to_box(element.centroid, *space.bounds) for space in unassessed)
    return there + UNASSESSED_OWNER_MARGIN_M < here


def _drop_other_rooms_windows(
    model: IfcModel,
    assignments: tuple[WindowAssignment, ...],
    spaces: tuple[IfcElement, ...],
    unassessed: tuple[IfcElement, ...],
) -> tuple[WindowAssignment, ...]:
    """Drop the windows that belong to a room outside the assessed set.

    Windows resolve against the assessed rooms alone, so filtering an
    apartment out leaves its glazing looking for a home and the nearest
    surviving apartment takes it. On the fixture, restricting a run to one
    apartment doubled its window count and moved its living room from 106 to
    202 minutes -- a fail turned into a pass by narrowing the denominator,
    which must never move the numerator.

    An unresolved window is kept: "no room could be found for it" is a fact
    about the model and is reported as one, while "it serves a room this run
    is not assessing" is not.
    """
    if not unassessed:
        return assignments
    by_id = {space.global_id: space for space in spaces}
    elsewhere = {space.global_id for space in unassessed}
    kept = []
    for assignment in assignments:
        owner = by_id.get(assignment.space_id) if assignment.space_id else None
        window = model.by_id(assignment.window_id)
        if owner is None or window is None:
            kept.append(assignment)
            continue
        # A space boundary is the export stating which room the window serves,
        # and that beats any distance.
        if model.space_boundaries.get(assignment.window_id) in elsewhere:
            continue
        if _belongs_elsewhere(window, owner, unassessed):
            continue
        kept.append(assignment)
    return tuple(kept)


def _open_space_grid(
    element: IfcElement, owner_id: str, config: SceneConfig
) -> SamplePoints | None:
    """Grid the walking surface of one piece of private open space.

    A balcony *slab* is a solid: its walking surface is the top face, and the
    assessment plane sits above it. A balcony *Zone* is a void, and its top
    face is the underside of whatever is overhead -- gridding that would put
    every sample a metre into the storey above, silently, and still return a
    plausible number.

    So a Zone is gridded on its floor instead, which means taking the face
    whose normal points *down* out of the volume, offsetting against that
    normal to rise off the floor, and then flipping the normals back up. Open
    space faces the sky whichever way the face it was derived from pointed.
    """
    if element.ifc_class != "IfcSpace":
        return planar_face_grid(
            element.mesh,
            owner_id,
            np.array([0.0, 0.0, 1.0]),
            spacing_m=config.grid_spacing_m,
            surface_offset_m=config.open_space_height_m,
        )

    samples = planar_face_grid(
        element.mesh,
        owner_id,
        np.array([0.0, 0.0, -1.0]),
        spacing_m=config.grid_spacing_m,
        surface_offset_m=-config.open_space_height_m,
    )
    if samples is None:
        return None
    return replace(samples, normals=-samples.normals)


def _floor_grid(element: IfcElement, owner_id: str, config: SceneConfig) -> SamplePoints | None:
    """Grid a Zone's floor in plan, for drawing the sun patch on it.

    The same trick as ``_open_space_grid`` uses on a balcony Zone, and for the
    same reason: a Zone is a void, so its *top* face is the underside of the
    slab above and gridding that puts every sample in the storey overhead.
    Take the face whose normal points down out of the volume, offset against
    that normal to rise off the floor, then flip the normals back up, because
    a floor faces the sky whichever way the face it came from pointed.

    Only the height differs, and it differs for a stated reason: open space is
    assessed at 1 m, while a patch is a picture of sun on the floor and has to
    be sampled there.
    """
    spacing = config.floor_patch_spacing_m
    if spacing is None:
        return None
    if element.ifc_class != "IfcSpace":
        # A balcony *slab* is a solid, so the surface anybody stands on -- and
        # the one a patch lands on -- is its top face, not its underside.
        return planar_face_grid(
            element.mesh,
            owner_id,
            np.array([0.0, 0.0, 1.0]),
            spacing_m=spacing,
            surface_offset_m=config.floor_patch_height_m,
            clip_to_face=True,
        )
    samples = planar_face_grid(
        element.mesh,
        owner_id,
        np.array([0.0, 0.0, -1.0]),
        spacing_m=spacing,
        surface_offset_m=-config.floor_patch_height_m,
        clip_to_face=True,
    )
    if samples is None:
        return None
    return replace(samples, normals=-samples.normals)


class SceneConfigError(Exception):
    """A scene setting cannot be satisfied by this model.

    Kept distinct from a bad model: the file is fine, the question asked of it
    is not. The message names what the model does contain, because the answer
    is almost always a layer name typed slightly differently.
    """


def _require_matches(
    label: str, matched: Sequence[object], wanted: Sequence[str], model: IfcModel
) -> None:
    """Fail when a layer filter selects nothing, and say what was available.

    Matching strictly and failing loudly beats matching loosely: a filter that
    quietly selects nothing produces a building with no apartments, which
    reads as a compliance result rather than as a typo. Layer names carry
    punctuation and spacing that nobody reproduces from memory -- ``06 |
    Zone.Units`` typed as ``06|Zone.Units`` matches nothing -- so the fix is
    to show the list rather than to guess at the intent.
    """
    if matched or not wanted:
        return
    available = sorted({e.layer for e in model.elements if e.layer})
    listed = "\n    ".join(available) if available else "(the export carried no layers at all)"
    raise SceneConfigError(
        f"{label} {list(wanted)} matched nothing in {model.path.name}.\n"
        f"  Layers present:\n    {listed}"
    )


def _openings_per_apartment(
    assignments: Sequence[WindowAssignment], apartments: Collection[str | None]
) -> dict[int, int]:
    """How many openings each apartment got, as a histogram of counts.

    Reported because it is the cheapest way to tell what a marking convention
    actually marks, and that changes which ADG criterion the result answers.

    A convention that marks only living-room glazing gives most apartments one
    or two openings -- a balcony slider and perhaps a window. One that marks
    every room requiring sunlight gives a two-bedroom apartment three or four.
    The distinction is invisible in a compliance percentage and decisive for
    it: ADG 4A-1 is about living rooms, and folding bedroom glazing in lets an
    apartment pass on sun its living room never sees.

    Keyed by opening count, valued by how many apartments had that many.
    """
    per_apartment: dict[str, int] = {}
    for assignment in assignments:
        if assignment.space_id is None or assignment.space_id not in apartments:
            continue
        per_apartment[assignment.space_id] = per_apartment.get(assignment.space_id, 0) + 1

    histogram: dict[int, int] = {}
    for count in per_apartment.values():
        histogram[count] = histogram.get(count, 0) + 1
    return dict(sorted(histogram.items()))


def _named_one_of(element: IfcElement, names: Sequence[str]) -> bool:
    """Whether a zone carries one of the named Zone names.

    Empty means no filter, so the common case is unaffected. Archicad writes
    the Zone name into ``LongName`` and the apartment identifier into ``Name``,
    and both are checked for the same reason ``_is_living_room`` checks both:
    which one holds what varies by translator, and matching neither reads as a
    building with no apartments rather than as a filter that missed.
    """
    if not names:
        return True
    wanted = {" ".join(entry.split()).casefold() for entry in names}
    candidates = (element.name, getattr(element, "long_name", "") or "")
    return any(" ".join(value.split()).casefold() in wanted for value in candidates if value)


def _on_layer(element: IfcElement, layers: Sequence[str]) -> bool:
    """Whether an element sits on one of the named Archicad layers.

    Case-insensitive and whitespace-tolerant, because a layer name typed into
    a command line will not match ``01 | Wall.External`` byte for byte, and
    failing on that would be a trap rather than a safeguard.
    """
    if not layers:
        return False
    actual = " ".join(element.layer.split()).casefold()
    return any(" ".join(wanted.split()).casefold() == actual for wanted in layers)


def _is_livable_opening(element: IfcElement, config: SceneConfig) -> bool:
    """D24, in code: does this opening's ID carry the livable marker?

    Matched against ``Name``, which is where Archicad's element ID lands. The
    suffix must be a genuine suffix -- a project using ``D06L`` for something
    unrelated must not be swept up by a ``_L`` convention, and in the reference
    model the two really did coexist.
    """
    suffix = config.livable_opening_suffix
    if not suffix:
        return False
    if element.ifc_class not in config.livable_opening_classes:
        return False
    return element.name.casefold().endswith(suffix.casefold())


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
    model, cut_above = _cut_above(model, config.exclude_above_m)

    all_spaces = model.of_class("IfcSpace")
    open_space_zones = tuple(
        space
        for space in all_spaces
        if _on_layer(space, config.open_space_zone_layers)
        and _named_one_of(space, config.open_space_zone_names)
    )
    # A balcony Zone is not an apartment, so it never enters the denominator
    # even when the apartment layer filter is left wide open.
    open_space_ids = {zone.global_id for zone in open_space_zones}
    spaces = tuple(
        space
        for space in all_spaces
        if space.global_id not in open_space_ids
        and (not config.apartment_zone_layers or _on_layer(space, config.apartment_zone_layers))
        and _named_one_of(space, config.apartment_zone_names)
    )
    _require_matches("apartment zone layers", spaces, config.apartment_zone_layers, model)
    if config.apartment_zone_names and not spaces:
        raise SceneConfigError(
            f"No zones are named any of {list(config.apartment_zone_names)}. "
            f"Run 'sun-study archicad-info' to see the zone names on each layer."
        )
    _require_matches(
        "open space zone layers", open_space_zones, config.open_space_zone_layers, model
    )
    if config.open_space_zone_names and not open_space_zones:
        raise SceneConfigError(
            f"No zones on {list(config.open_space_zone_layers)} are named any of "
            f"{list(config.open_space_zone_names)}. Run 'sun-study archicad-info' "
            f"to see the zone names on each layer."
        )

    apartment_ids = {space.global_id for space in spaces}
    # The rooms the filters excluded. They are not assessed, but a window or a
    # balcony may still belong to one, and one that does is dropped rather
    # than handed to the nearest apartment that survived. Open-space Zones are
    # deliberately not here: a marked slider sits in the wall between a living
    # room and its own balcony, so treating the balcony as a rival owner would
    # drop exactly the glazing D24 marks.
    unassessed = tuple(
        space
        for space in all_spaces
        if space.global_id not in apartment_ids and space.global_id not in open_space_ids
    )

    if config.livable_opening_suffix:
        # D24: the glazing is marked, so the room needs no name and every
        # space a marked opening serves is an apartment.
        openings = tuple(e for e in model.elements if _is_livable_opening(e, config))
        assignments = _drop_other_rooms_windows(
            model, _assign_windows(model, openings, spaces), spaces, unassessed
        )
        living_rooms = {a.space_id for a in assignments if a.space_id is not None}
    else:
        openings = model.of_class("IfcWindow")
        assignments = _drop_other_rooms_windows(
            model, _assign_windows(model, openings, spaces), spaces, unassessed
        )
        living_rooms = {space.global_id for space in spaces if _is_living_room(space, config)}

    space_by_id = {space.global_id: space for space in spaces}

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

    # D7. A Zone is the balcony's actual extent; a slab is whatever happened to
    # be drawn under it, and may run past the balustrade or stop short of it.
    # So where the practice zones its balconies, those win.
    if config.open_space_zone_layers:
        open_space_elements: tuple[IfcElement, ...] = open_space_zones
    else:
        open_space_elements = tuple(
            slab
            for slab in model.of_class("IfcSlab")
            if any(
                slab.name.casefold().startswith(prefix.casefold())
                for prefix in config.balcony_name_prefixes
            )
        )

    balcony_groups, unattached_open_space = [], 0
    owned_open_space: list[tuple[IfcElement, str]] = []
    open_space_routes: dict[str, int] = {}
    for slab in open_space_elements:
        # Parent open space to the apartment it serves, not to the slab, so
        # window and open-space results join on one key downstream. Communal
        # open space has no apartment and is counted separately rather than
        # attached to the nearest one.
        # Resolved against the excluded rooms as well, because what decides
        # this one is vertical -- an apartment stands on top of its balcony --
        # and a distance margin cannot separate a balcony from the apartment
        # whose ceiling it is flush against. Where the winner is a room this
        # run is not assessing, the balcony is dropped: it is neither ours nor
        # communal, and counting it as either is a wrong number rather than a
        # missing one.
        owner, route = _open_space_owner(
            slab,
            spaces + unassessed,
            config.open_space_max_distance_m,
            config.open_space_level_tolerance_m,
        )
        if owner is not None and owner.global_id not in apartment_ids:
            route = "another-room"
            open_space_routes[route] = open_space_routes.get(route, 0) + 1
            continue
        open_space_routes[route] = open_space_routes.get(route, 0) + 1
        if owner is None:
            unattached_open_space += 1
            continue

        samples = _open_space_grid(slab, owner.global_id, config)
        if samples is not None:
            balcony_groups.append(samples)
        # Kept for the floor patch, which grids the same Zone at floor level
        # rather than at the 1 m assessment plane.
        owned_open_space.append((slab, owner.global_id))

    floor_groups: list[SamplePoints] = []
    floor_kinds: list[BoolArray] = []
    if config.floor_patch_spacing_m:
        for space in spaces:
            grid = _floor_grid(space, space.global_id, config)
            if grid is not None:
                floor_groups.append(grid)
                floor_kinds.append(np.zeros(len(grid), dtype=np.bool_))
        for zone, owner_id in owned_open_space:
            grid = _floor_grid(zone, owner_id, config)
            if grid is not None:
                floor_groups.append(grid)
                floor_kinds.append(np.ones(len(grid), dtype=np.bool_))

    provenance: dict[str, object] = {
        "source": model.path.name,
        "schema": model.schema,
        "length_unit_scale": model.length_unit_scale,
        "site_elevation_m": model.site_elevation_m,
        "true_north_bearing_deg": orientation.normalised_bearing_deg,
        "windows_total": len(openings),
        "windows_assessed": assessed,
        "windows_skipped": skipped,
        "spaces_total": len(spaces),
        "elements_above_cut": cut_above,
        "floor_samples": len(SamplePoints.concatenate(floor_groups)) if floor_groups else 0,
        "living_rooms_matched": len(living_rooms),
        "openings_per_apartment": _openings_per_apartment(assignments, living_rooms),
        "balconies_matched": len(balcony_groups),
        "open_space_unattached": unattached_open_space,
        "open_space_resolution": open_space_routes,
        "resolved_by_space_boundary": sum(1 for a in assignments if a.method == "space-boundary"),
        "resolved_geometrically": sum(1 for a in assignments if a.method == "geometric"),
        "unresolved": sum(1 for a in assignments if a.method == "unresolved"),
        "occluder_triangles": occluders.triangle_count,
        "vegetation_included": config.include_vegetation,
        **_geometry_overhead(model, spaces),
    }

    return Scene(
        orientation=orientation,
        occluders=occluders,
        window_samples=SamplePoints.concatenate(window_groups),
        open_space_samples=SamplePoints.concatenate(balcony_groups),
        floor_samples=SamplePoints.concatenate(floor_groups) if floor_groups else None,
        floor_is_open_space=(np.concatenate(floor_kinds) if floor_kinds else None),
        glazed_occluders=(
            model.occluder_mesh(transparent=config.livable_opening_classes)
            if config.floor_patch_spacing_m
            else None
        ),
        assignments=assignments,
        config=config,
        provenance=provenance,
    )


def _cut_above(model: IfcModel, height_m: float | None) -> tuple[IfcModel, int]:
    """Remove every element that lies entirely above ``height_m``.

    Done to the model rather than to the occluder set, because a parked
    hotlink master brings its Zones with it: on the reference project four of
    the fifteen Zones matching the apartment filter were master copies at
    157 m, and counting them as apartments is the same mistake as letting them
    cast a shadow. Cutting once, before anything is selected, keeps the two
    from drifting apart.
    """
    if height_m is None:
        return model, 0
    kept = tuple(
        element
        for element in model.elements
        if not len(element.mesh.vertices) or float(element.mesh.vertices[:, 2].min()) <= height_m
    )
    return replace(model, elements=kept), len(model.elements) - len(kept)


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
        for element in model.occluders()
        if float(np.linalg.norm(element.centroid[:2] - subject[:2])) <= radius_m
    ]
    return TriangleMesh.concatenate(kept)


# ---------------------------------------------------------------------------
# Massing mode.
#
# At massing stage there are no apartments -- no Zones, no windows, just a mass
# and its context. The ADG's per-apartment criterion cannot be computed, so the
# metric that drives the design loop is the share of *facade area* receiving at
# least two hours, plus the same banding on the ground for public domain and
# communal open space.
#
# Everything below therefore works from raw geometry. It never looks for an
# IfcSpace and never needs one.
# ---------------------------------------------------------------------------

DEFAULT_MASSING_SPACING_M = 1.0
"""Coarser than the 200 mm used for a developed model, and deliberately so.

An optimisation run evaluates hundreds of variants. At 200 mm a facade of
roughly 18,000 m2 is about 445,000 samples and several minutes per variant; at
1 m it is about 18,000 samples and a few seconds. The spacing used is recorded
on every result so a coarse number can never be mistaken for a fine one.
"""


@dataclass(frozen=True)
class MassingConfig:
    """Settings for a massing-stage study."""

    timezone: str

    context_name_prefixes: tuple[str, ...] = ("Context",)
    """Elements whose name starts with one of these are occluders only.

    They shade the subject but are not themselves analysed, so they stay out of
    the facade-area denominator. Everything else is subject.
    """

    context_layers: tuple[str, ...] = ()
    """Archicad layers whose elements are occluders only.

    The same distinction as ``context_name_prefixes``, keyed on the thing a
    practice actually controls. A modelling standard says *"all 3D context
    elements outside the site boundaries go on this layer"*; it does not
    promise anything about what each object is called, and a neighbouring
    building imported from a survey will not be named "Context".

    An element matching either route is context.
    """

    facade_spacing_m: float = DEFAULT_MASSING_SPACING_M
    ground_spacing_m: float = DEFAULT_MASSING_SPACING_M
    surface_offset_m: float = DEFAULT_SURFACE_OFFSET_M

    vertical_tolerance_deg: float = 30.0
    """How far from upright a face may be and still count as facade."""

    ground_level_m: float | None = None
    """Ground plane height. Defaults to the lowest point of the geometry."""

    ground_margin_m: float = 10.0
    """How far beyond the subject's footprint to grid the ground."""

    ground_sample_height_m: float = 0.0
    footprint_probe_height_m: float = 2.5
    """A ground sample with geometry this close above it is inside a building.

    Chosen so a point indoors is excluded while open ground under a high tower
    soffit is kept. A balcony two metres up will mask the ground beneath it,
    which is the intended reading: that ground is covered.
    """

    def describe(self) -> str:
        ground = (
            f"{self.ground_level_m:g} m" if self.ground_level_m is not None else "auto (lowest)"
        )
        return (
            f"timezone {self.timezone} | "
            f"facade grid {self.facade_spacing_m:g} m | "
            f"ground grid {self.ground_spacing_m:g} m | "
            f"surface offset {self.surface_offset_m * 1000:.0f} mm | "
            f"facade = faces within {self.vertical_tolerance_deg:g} deg of vertical | "
            f"context prefixes {list(self.context_name_prefixes)}"
            + (f" or layers {list(self.context_layers)}" if self.context_layers else "")
            + " | "
            f"ground level {ground}, margin {self.ground_margin_m:g} m | "
            f"vegetation excluded"
        )


@dataclass(frozen=True)
class MassingScene:
    """Facade and ground samples for a massing study."""

    orientation: SiteOrientation
    occluders: TriangleMesh
    facade_samples: SamplePoints
    ground_samples: SamplePoints
    config: MassingConfig
    provenance: dict[str, object] = field(default_factory=dict)

    def describe(self) -> str:
        return (
            f"{self.orientation.describe()}\n"
            f"  {self.config.describe()}\n"
            f"  occluders {self.occluders.triangle_count} triangles | "
            f"{len(self.facade_samples)} facade samples "
            f"({self.facade_samples.total_area_m2:.1f} m2) | "
            f"{len(self.ground_samples)} ground samples "
            f"({self.ground_samples.total_area_m2:.1f} m2)"
        )


def _is_context(element: IfcElement, config: MassingConfig) -> bool:
    return _on_layer(element, config.context_layers) or any(
        element.name.casefold().startswith(prefix.casefold())
        for prefix in config.context_name_prefixes
    )


def build_massing_scene(model: IfcModel, config: MassingConfig) -> MassingScene:
    """Assemble a massing-stage scene: facade and ground samples, no Zones.

    The whole model is the occluder set, including the subject: self-shading
    between towers on the same podium is precisely what a massing study is
    measuring.
    """
    orientation = model.orientation(config.timezone)

    # IfcSpace is a void, never a solid; it would shade the building from
    # inside. Everything else occludes, subject and context alike.
    solids = model.occluders()
    subject = [element for element in solids if not _is_context(element, config)]
    context = [element for element in solids if _is_context(element, config)]

    occluders = TriangleMesh.concatenate([element.mesh for element in solids])

    # Facade: upright faces of the subject only. Context towers shade but are
    # not part of the denominator, or the percentage would describe the
    # neighbourhood rather than the scheme.
    facade_groups = [
        triangle_samples(
            element.mesh.triangles(),
            [element.global_id] * element.mesh.triangle_count,
            spacing_m=config.facade_spacing_m,
            surface_offset_m=config.surface_offset_m,
            faces=FaceSelection.VERTICAL,
            vertical_tolerance_deg=config.vertical_tolerance_deg,
        )
        for element in subject
        if element.mesh.triangle_count
    ]
    facade = SamplePoints.concatenate([group for group in facade_groups if len(group)])

    ground = _ground_grid(subject, occluders, config)

    provenance: dict[str, object] = {
        "mode": "massing",
        "source": model.path.name,
        "schema": model.schema,
        "length_unit_scale": model.length_unit_scale,
        "true_north_bearing_deg": orientation.normalised_bearing_deg,
        "subject_elements": len(subject),
        "context_elements": len(context),
        "occluder_triangles": occluders.triangle_count,
        "facade_samples": len(facade),
        "facade_area_m2": round(facade.total_area_m2, 3),
        "facade_spacing_m": config.facade_spacing_m,
        "ground_samples": len(ground),
        "ground_area_m2": round(ground.total_area_m2, 3),
        "ground_spacing_m": config.ground_spacing_m,
        "vegetation_included": False,
    }
    return MassingScene(
        orientation=orientation,
        occluders=occluders,
        facade_samples=facade,
        ground_samples=ground,
        config=config,
        provenance=provenance,
    )


def _ground_grid(
    subject: list[IfcElement], occluders: TriangleMesh, config: MassingConfig
) -> SamplePoints:
    """An open-ground grid around the subject, with building footprints removed.

    Footprints are found by firing a short ray straight up from each candidate
    point: if anything is directly overhead within
    ``footprint_probe_height_m`` the point is inside a building rather than on
    open ground. That reuses the ray caster instead of needing polygon
    booleans, and it handles an L-shaped or perforated footprint exactly, which
    a bounding box would not.
    """
    if not subject:
        return SamplePoints.empty()

    lower = np.min([element.bounds[0] for element in subject], axis=0)
    upper = np.max([element.bounds[1] for element in subject], axis=0)
    ground_z = config.ground_level_m if config.ground_level_m is not None else float(lower[2])

    # horizontal_grid takes the minimum corner, not the centre.
    margin = config.ground_margin_m
    grid = horizontal_grid(
        (float(lower[0]) - margin, float(lower[1]) - margin, ground_z),
        float(upper[0] - lower[0]) + 2.0 * margin,
        float(upper[1] - lower[1]) + 2.0 * margin,
        "ground",
        height_m=config.ground_sample_height_m,
        spacing_m=config.ground_spacing_m,
    )
    if len(grid) == 0 or occluders.triangle_count == 0:
        return grid

    probe = Occluder(occluders)
    covered = probe.any_hit(
        grid.positions,
        np.tile(np.array([0.0, 0.0, 1.0]), (len(grid), 1)),
        max_distance=config.footprint_probe_height_m,
    )
    open_ground = ~covered
    if not open_ground.any():
        return SamplePoints.empty()

    return SamplePoints(
        grid.positions[open_ground],
        grid.normals[open_ground],
        tuple(np.asarray(grid.parent_ids)[open_ground].tolist()),
        grid.areas[open_ground],
        surface_offset_m=grid.surface_offset_m,
    )


#: How far above the topmost apartment geometry has to sit before it is
#: reported as suspect. A roof, a lift overrun and a parapet are all real and
#: all within a storey or two; 15 m is above any of those and far below where
#: a hotlink master gets parked.
OVERHEAD_SUSPICION_M = 15.0


def _geometry_overhead(model: IfcModel, spaces: Sequence[IfcElement]) -> dict[str, object]:
    """How much occluding geometry sits above the building being assessed.

    The failure this exists to catch: a practice parks its hotlinked unit-type
    *masters* far above the site -- one project has three sets, at roughly
    64 m, 158 m and 280 m. Exported by an unfiltered layer combination they
    become ordinary geometry, and geometry above a building can only shade it.
    Every apartment then comes back dark, which reads as a badly oriented
    scheme rather than as a floating copy of itself blocking the sun.

    Nothing here is fatal. A tall neighbour is legitimately overhead, and so
    is a roof. This reports the height and the share, and lets a person judge.
    """
    if not spaces:
        return {}
    top_of_building = max(float(space.mesh.vertices[:, 2].max()) for space in spaces)

    above = [
        element
        for element in model.occluders()
        if len(element.mesh.vertices)
        and float(element.mesh.vertices[:, 2].min()) > top_of_building + OVERHEAD_SUSPICION_M
    ]
    if not above:
        return {"geometry_above_building": 0}

    triangles = sum(element.mesh.triangle_count for element in above)
    highest = max(float(element.mesh.vertices[:, 2].max()) for element in above)
    return {
        "geometry_above_building": len(above),
        "geometry_above_building_triangles": triangles,
        "geometry_above_building_top_m": round(highest, 1),
        "top_of_apartments_m": round(top_of_building, 1),
    }
