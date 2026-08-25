"""Planar faces of a mesh, gridded one plane at a time.

Why this exists next to ``triangle_samples``
--------------------------------------------
The massing study scatters samples over triangles. That is the right way to
*measure* facade area -- it follows the geometry exactly, whatever shape it is
-- and the wrong way to *draw* it. A picture of the facade is made of
rectangles, and a rectangle only means anything in the plane of the face it
lies on: cells scattered over a triangle soup have no rows and no columns to
merge along.

So the same geometry is taken a second way here. Triangles are grouped into
planar faces, each face is gridded on a lattice in its own frame, and what
comes out carries the two-dimensional cell coordinates that
``merge_lit_cells`` needs. A band of colour on one face then reduces to as few
rectangles as tile it, and each of those becomes one thin Archicad element.

Upright and flat faces both become panels: the facade and the balcony decks,
terraces and soffits, which take more sun than any wall does. Each gets a
frame whose ``cross(axis_u, axis_v)`` is its outward normal, so a merged
rectangle is wound anticlockwise seen from outside whichever way the face
points. Sloping faces are not panels and are left to the measuring path --
the elements this feeds cannot lean.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from sun_study.core.geometry import TriangleMesh
from sun_study.core.patches import Rectangle
from sun_study.core.sampling import (
    DEFAULT_GRID_SPACING_M,
    DEFAULT_SURFACE_OFFSET_M,
    SamplePoints,
)

FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]

__all__ = [
    "FacePanel",
    "PanelRectangle",
    "face_panels",
    "vertical_face_panels",
]

#: How finely a face normal and its plane offset are quantised before
#: triangles are grouped by them. Coarse enough that a tessellated flat
#: surface stays one face, fine enough that two leaves of a cavity wall
#: 50 mm apart stay two.
_ANGLE_QUANTUM_DEG = 2.0
_OFFSET_QUANTUM_M = 0.01


@dataclass(frozen=True)
class PanelRectangle:
    """One merged rectangle, placed back in the world.

    Held as four corners because a face may be upright or flat and the two
    are described differently by the elements they become — an upright patch
    by a base line and a height, a flat one by a plan outline and a level.
    Corners are the shape both derive from, so the geometry says it once.

    Wound ``(u_min, v_min)``, ``(u_max, v_min)``, ``(u_max, v_max)``,
    ``(u_min, v_max)`` in the panel's frame, which is anticlockwise seen from
    outside the face.
    """

    corners: FloatArray
    """``(4, 3)`` world positions."""
    normal: FloatArray
    """Unit outward normal of the face this came from."""
    area_m2: float

    @property
    def is_upright(self) -> bool:
        """Whether this belongs on a wall rather than lying flat."""
        return bool(abs(float(self.normal[2])) < 0.5)

    @property
    def start(self) -> FloatArray:
        """World ``(x, y, z)`` of the first corner — an upright face's base."""
        return np.asarray(self.corners[0], dtype=np.float64)

    @property
    def end(self) -> FloatArray:
        """The second corner: the other end of an upright face's base."""
        return np.asarray(self.corners[1], dtype=np.float64)

    @property
    def height_m(self) -> float:
        """Extent along the panel's second axis — a height when upright."""
        return float(np.linalg.norm(self.corners[3] - self.corners[0]))

    @property
    def width_m(self) -> float:
        """Extent along the panel's first axis."""
        return float(np.linalg.norm(self.corners[1] - self.corners[0]))

    @property
    def base_z(self) -> float:
        return float(self.corners[0][2])


@dataclass(frozen=True)
class FacePanel:
    """One planar face, upright or flat, with a lattice of cells over it.

    ``cell_uv`` holds each sample's position in the panel's own frame, which
    is what makes the cells mergeable; ``samples`` holds the same points in
    the world, which is what the ray caster needs. They are parallel.
    """

    parent_id: str
    origin: FloatArray
    """World position of the lattice's own corner: ``u = v = 0``."""
    axis_u: FloatArray
    """Unit in-plane vector: along the face when upright, world east when flat."""
    axis_v: FloatArray
    """Unit in-plane vector: world up when upright, otherwise the other
    horizontal one. ``cross(axis_u, axis_v)`` is always the outward normal."""
    normal: FloatArray
    """Unit outward normal. Horizontal on an upright face, vertical on a flat one."""
    spacing_m: float
    samples: SamplePoints
    cell_uv: FloatArray
    """``(n, 2)`` cell centres in the panel frame, metres from ``origin``."""

    def __len__(self) -> int:
        return len(self.samples)

    @property
    def area_m2(self) -> float:
        return self.samples.total_area_m2

    def rectangles(self, chosen: BoolArray) -> tuple[PanelRectangle, ...]:
        """Merge the chosen cells and put the result back in the world."""
        from sun_study.core.patches import merge_lit_cells

        merged = merge_lit_cells(self.cell_uv, chosen, self.spacing_m)
        return tuple(self._to_world(rectangle) for rectangle in merged)

    def _to_world(self, rectangle: Rectangle) -> PanelRectangle:
        u_min, v_min, u_max, v_max = rectangle
        corners = np.array(
            [
                self.origin + u * self.axis_u + v * self.axis_v
                for u, v in ((u_min, v_min), (u_max, v_min), (u_max, v_max), (u_min, v_max))
            ],
            dtype=np.float64,
        )
        return PanelRectangle(
            corners=corners,
            normal=self.normal,
            area_m2=float(rectangle.area_m2),
        )


def vertical_face_panels(
    mesh: TriangleMesh,
    parent_id: str,
    *,
    spacing_m: float = DEFAULT_GRID_SPACING_M,
    surface_offset_m: float = DEFAULT_SURFACE_OFFSET_M,
    vertical_tolerance_deg: float = 5.0,
    min_face_area_m2: float = 0.25,
) -> list[FacePanel]:
    """The upright faces only. See ``face_panels``."""
    return face_panels(
        mesh,
        parent_id,
        spacing_m=spacing_m,
        surface_offset_m=surface_offset_m,
        vertical_tolerance_deg=vertical_tolerance_deg,
        min_face_area_m2=min_face_area_m2,
        horizontal=False,
    )


def face_panels(
    mesh: TriangleMesh,
    parent_id: str,
    *,
    spacing_m: float = DEFAULT_GRID_SPACING_M,
    surface_offset_m: float = DEFAULT_SURFACE_OFFSET_M,
    vertical_tolerance_deg: float = 5.0,
    min_face_area_m2: float = 0.25,
    upright: bool = True,
    horizontal: bool = True,
) -> list[FacePanel]:
    """Every planar face of one mesh, each gridded in its own plane.

    Upright faces are the facade; horizontal ones are the balcony decks,
    terraces and soffits, which take more sun than any wall does and are half
    of what a solar diagram is about. Both are gridded the same way and differ
    only in the frame chosen for them.

    Both sides of every face come back as separate panels, facing opposite
    ways. That is deliberate: which side is the outside is not knowable from
    one element, and a sun study that quietly assessed the wrong face of every
    wall would look entirely reasonable. Colouring both costs some elements the
    viewer never sees and gets the outside right without guessing.

    ``min_face_area_m2`` drops the slivers -- the top of a wall, the reveal of
    an opening -- which are real, and would otherwise each become a panel of
    one cell. A face narrower or shorter than the grid spacing drops out too,
    whatever its area, because no cell centre lands on it; on a facade that is
    the end of a wall seen edge-on, which contributes nothing to the picture
    and would be invisible at any printed scale.
    """
    if spacing_m <= 0.0:
        raise ValueError(f"spacing_m must be positive, got {spacing_m}")

    triangles = mesh.triangles()
    if not len(triangles):
        return []

    edge_a = triangles[:, 1, :] - triangles[:, 0, :]
    edge_b = triangles[:, 2, :] - triangles[:, 0, :]
    cross = np.cross(edge_a, edge_b)
    lengths = np.linalg.norm(cross, axis=1)
    real = lengths > 1e-12
    if not real.any():
        return []

    normals = np.zeros_like(cross)
    normals[real] = cross[real] / lengths[real, None]
    areas = lengths / 2.0

    # Upright means the normal is horizontal, so its vertical component is
    # what the tolerance is on; flat is the same test the other way up.
    tolerance = np.sin(np.radians(vertical_tolerance_deg))
    wanted = np.zeros(len(triangles), dtype=bool)
    if upright:
        wanted |= np.abs(normals[:, 2]) <= tolerance
    if horizontal:
        wanted |= np.abs(normals[:, 2]) >= np.cos(np.radians(vertical_tolerance_deg))
    wanted &= real
    if not wanted.any():
        return []

    panels: list[FacePanel] = []
    for indices in _planar_groups(triangles[wanted], normals[wanted]):
        group = triangles[wanted][indices]
        if float(areas[wanted][indices].sum()) < min_face_area_m2:
            continue
        panel = _panel_for(
            group,
            normals[wanted][indices],
            parent_id,
            spacing_m=spacing_m,
            surface_offset_m=surface_offset_m,
        )
        if panel is not None and len(panel):
            panels.append(panel)
    return panels


def _planar_groups(triangles: FloatArray, normals: FloatArray) -> list[npt.NDArray[np.intp]]:
    """Indices of the triangles sharing a plane *and* a facing direction.

    Keyed on the quantised normal and the quantised plane offset, so a
    tessellated flat wall comes back as one face while its two leaves, and its
    two sides, stay apart.
    """
    offsets = np.einsum("ij,ij->i", normals, triangles[:, 0, :])
    quantum = np.sin(np.radians(_ANGLE_QUANTUM_DEG))
    # All three components of the normal, not just the two a facade needs: a
    # slab's top and its soffit are both horizontal and differ only in which
    # way up they face, so keying on x and y alone puts them in one group and
    # the panel comes out facing an averaged nothing.
    keys = np.column_stack(
        (
            np.round(normals[:, 0] / quantum),
            np.round(normals[:, 1] / quantum),
            np.round(normals[:, 2] / quantum),
            np.round(offsets / _OFFSET_QUANTUM_M),
        )
    )
    _, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    return [np.flatnonzero(inverse == group) for group in range(len(first))]


def _frame_for(normal: FloatArray) -> tuple[FloatArray, FloatArray, FloatArray] | None:
    """Snap a face normal to upright or flat and give it a frame.

    ``cross(axis_u, axis_v)`` is the normal in both cases, so the corner
    winding a merged rectangle comes out with is anticlockwise seen from
    outside the face, whichever way the face points.

    The normal is snapped rather than used as measured. A face that is within
    the tolerance of upright is *treated* as upright, and the elements built
    from it are upright exactly — a wall cannot lean, so a frame carrying a
    half-degree of tilt would only put the geometry slightly out of the plane
    it is meant to coat.
    """
    if abs(float(normal[2])) < 0.5:
        flat = np.array([float(normal[0]), float(normal[1]), 0.0])
        length = float(np.linalg.norm(flat))
        if length < 1e-9:
            return None
        upright_normal = flat / length
        axis_v = np.array([0.0, 0.0, 1.0])
        axis_u = np.cross(axis_v, upright_normal)
        return upright_normal, axis_u / np.linalg.norm(axis_u), axis_v

    # Flat. Up gets the right-handed frame; down gets it mirrored, so that
    # the cross product still points out of the face rather than through it.
    facing = 1.0 if float(normal[2]) > 0.0 else -1.0
    return (
        np.array([0.0, 0.0, facing]),
        np.array([1.0, 0.0, 0.0]),
        np.array([0.0, facing, 0.0]),
    )


def _panel_for(
    triangles: FloatArray,
    normals: FloatArray,
    parent_id: str,
    *,
    spacing_m: float,
    surface_offset_m: float,
) -> FacePanel | None:
    """Grid one planar group, keeping only the cells that land on the face."""
    normal = normals.mean(axis=0)
    length = float(np.linalg.norm(normal))
    if length < 1e-9:
        return None
    normal = normal / length

    frame = _frame_for(normal)
    if frame is None:
        return None
    normal, axis_u, axis_v = frame

    corners = triangles.reshape(-1, 3)
    reference = corners[0]
    local_u = (corners - reference) @ axis_u
    local_v = (corners - reference) @ axis_v
    u_min, u_max = float(local_u.min()), float(local_u.max())
    v_min, v_max = float(local_v.min()), float(local_v.max())
    if u_max - u_min < 1e-6 or v_max - v_min < 1e-6:
        return None

    origin = reference + u_min * axis_u + v_min * axis_v

    # Cell centres, indexed from the panel's own corner so that dividing a
    # centre by the spacing lands on a half-integer -- the property
    # ``merge_lit_cells`` relies on to keep consecutive cells consecutive.
    columns = max(1, int(np.ceil((u_max - u_min) / spacing_m)))
    rows = max(1, int(np.ceil((v_max - v_min) / spacing_m)))
    centres_u = (np.arange(columns, dtype=np.float64) + 0.5) * spacing_m
    centres_v = (np.arange(rows, dtype=np.float64) + 0.5) * spacing_m
    grid_u, grid_v = np.meshgrid(centres_u, centres_v, indexing="ij")
    uv = np.column_stack((grid_u.reshape(-1), grid_v.reshape(-1)))

    inside = _on_face(uv, triangles, origin, axis_u, axis_v)
    if not inside.any():
        return None
    uv = uv[inside]

    positions = (
        origin + uv[:, 0, None] * axis_u + uv[:, 1, None] * axis_v + surface_offset_m * normal
    )
    count = len(positions)
    samples = SamplePoints(
        positions=np.asarray(positions, dtype=np.float64),
        normals=np.repeat(normal.reshape(1, 3), count, axis=0),
        parent_ids=(parent_id,) * count,
        areas=np.full(count, spacing_m * spacing_m, dtype=np.float64),
        surface_offset_m=surface_offset_m,
    )
    return FacePanel(
        parent_id=parent_id,
        origin=np.asarray(origin, dtype=np.float64),
        axis_u=np.asarray(axis_u, dtype=np.float64),
        axis_v=axis_v,
        normal=np.asarray(normal, dtype=np.float64),
        spacing_m=spacing_m,
        samples=samples,
        cell_uv=np.asarray(uv, dtype=np.float64),
    )


def _on_face(
    uv: FloatArray,
    triangles: FloatArray,
    origin: FloatArray,
    axis_u: FloatArray,
    axis_v: FloatArray,
) -> BoolArray:
    """Which lattice cells land on the face rather than merely in its box.

    Barycentric sign in the panel's own basis, which is exact for a
    triangulated surface and needs nothing but numpy. Without it a face with a
    courtyard, a chamfer or any concavity is coloured across the hole.
    """
    flat = triangles - origin
    projected = np.stack((flat @ axis_u, flat @ axis_v), axis=-1)  # (m, 3, 2)

    a, b, c = projected[:, 0, :], projected[:, 1, :], projected[:, 2, :]
    v0, v1 = b - a, c - a
    denominator = v0[:, 0] * v1[:, 1] - v1[:, 0] * v0[:, 1]
    usable = np.abs(denominator) > 1e-12
    if not usable.any():
        return np.ones(len(uv), dtype=bool)

    a, v0, v1, denominator = a[usable], v0[usable], v1[usable], denominator[usable]
    offset = uv[:, None, :] - a[None, :, :]
    s = (offset[..., 0] * v1[None, :, 1] - offset[..., 1] * v1[None, :, 0]) / denominator
    t = (offset[..., 1] * v0[None, :, 0] - offset[..., 0] * v0[None, :, 1]) / denominator
    return np.asarray(((s >= -1e-9) & (t >= -1e-9) & (s + t <= 1.0 + 1e-9)).any(axis=1), dtype=bool)
