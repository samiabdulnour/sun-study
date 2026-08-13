"""Sample point generation on the surfaces being assessed.

Every sample carries four things: where it is, which way it faces, how much
surface area it stands for, and which element it belongs to.

The area is what makes an area-weighted result possible -- "34.79% of the
facade receives more than two hours" is a statement about square metres, not
about sample counts, and the two only agree when every sample happens to
represent the same area. On a triangulated massing they never do.

Two sampling strategies, for two different questions
----------------------------------------------------
``grid_on_rectangle`` grids one known rectangle: a window, a balcony. It gives
a regular grid, which is what you want when the element is a rectangle and the
output is per-element.

``triangle_samples`` subdivides a triangle soup directly, with each sample
carrying its exact share of its triangle's area. That is what a massing study
needs: there are no windows to grid, the facade is whatever the geometry says
it is, and it may be curved, faceted or arbitrarily wound. The samples are not
on a rectangular lattice, which matters for drawing a pretty image and not at
all for computing a percentage.

The outward offset
------------------
Samples sit on the surface, which is coplanar with -- or inside -- the solid
they belong to. A ray fired from exactly that plane hits its own surface and
reports permanent shade. Samples are therefore pushed out along the normal by
a small distance, 50 mm by default (brief section 5.5).

That offset is a real modelling choice, not a fudge: too small and the surface
self-occludes, too large and a sample escapes its own reveal and reports sun a
real window would not see. The value used is recorded on the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]

__all__ = [
    "DEFAULT_GRID_SPACING_M",
    "DEFAULT_SURFACE_OFFSET_M",
    "FaceSelection",
    "SamplePoints",
    "grid_on_rectangle",
    "horizontal_grid",
    "single_sample",
    "triangle_samples",
]

DEFAULT_GRID_SPACING_M = 0.2
DEFAULT_SURFACE_OFFSET_M = 0.05

# A face this close to vertical counts as facade rather than roof. Generous
# enough for a raked or battered facade, tight enough to exclude a flat roof.
DEFAULT_VERTICAL_TOLERANCE_DEG = 30.0


class FaceSelection(StrEnum):
    """Which faces of a solid to sample."""

    ALL = "all"
    VERTICAL = "vertical"
    """Roughly upright: the facade. Excludes roofs and soffits."""
    UPWARD = "upward"
    """Roughly horizontal and facing up: roofs, terraces, ground."""


def _normalise(vectors: FloatArray) -> FloatArray:
    lengths = np.linalg.norm(vectors, axis=-1, keepdims=True)
    if np.any(lengths == 0.0):
        raise ValueError("cannot normalise a zero-length vector")
    return np.asarray(vectors / lengths, dtype=np.float64)


@dataclass(frozen=True)
class SamplePoints:
    """Positions, unit normals, areas and parent element ids, all length n."""

    positions: FloatArray
    normals: FloatArray
    parent_ids: tuple[str, ...]
    areas: FloatArray = field(default_factory=lambda: np.zeros(0, dtype=np.float64))
    """Square metres each sample stands for.

    Defaults to 1.0 per sample when not supplied, which makes an area-weighted
    mean identical to an unweighted one. That is the right default for a
    regular grid over one element and the wrong one for a triangle soup, so
    every generator here sets it explicitly.
    """
    surface_offset_m: float = 0.0

    def __post_init__(self) -> None:
        if self.positions.ndim != 2 or self.positions.shape[1] != 3:
            raise ValueError(f"positions must have shape (n, 3), got {self.positions.shape}")
        if self.normals.shape != self.positions.shape:
            raise ValueError(
                f"normals {self.normals.shape} must match positions {self.positions.shape}"
            )
        if len(self.parent_ids) != len(self.positions):
            raise ValueError(
                f"{len(self.parent_ids)} parent ids for {len(self.positions)} positions"
            )

        if self.areas.size == 0 and len(self.positions):
            object.__setattr__(self, "areas", np.ones(len(self.positions), dtype=np.float64))
        elif len(self.areas) != len(self.positions):
            raise ValueError(f"{len(self.areas)} areas for {len(self.positions)} positions")

    def __len__(self) -> int:
        return len(self.positions)

    @property
    def total_area_m2(self) -> float:
        return float(np.sum(self.areas))

    @property
    def unique_parents(self) -> tuple[str, ...]:
        """Parent ids in first-appearance order, so output ordering is stable."""
        return tuple(dict.fromkeys(self.parent_ids))

    def mask_for(self, parent_id: str) -> npt.NDArray[np.bool_]:
        return np.asarray([pid == parent_id for pid in self.parent_ids], dtype=np.bool_)

    @classmethod
    def empty(cls) -> SamplePoints:
        return cls(
            np.zeros((0, 3), dtype=np.float64),
            np.zeros((0, 3), dtype=np.float64),
            (),
            np.zeros(0, dtype=np.float64),
        )

    @classmethod
    def concatenate(cls, groups: list[SamplePoints]) -> SamplePoints:
        kept = [group for group in groups if len(group)]
        if not kept:
            return cls.empty()

        offsets = {group.surface_offset_m for group in kept}
        return cls(
            np.concatenate([group.positions for group in kept]).astype(np.float64),
            np.concatenate([group.normals for group in kept]).astype(np.float64),
            tuple(pid for group in kept for pid in group.parent_ids),
            np.concatenate([group.areas for group in kept]).astype(np.float64),
            # Only meaningful when every group used the same offset; a mixed
            # set reports 0.0 rather than one group's value standing for all.
            surface_offset_m=next(iter(offsets)) if len(offsets) == 1 else 0.0,
        )


def single_sample(
    position: npt.ArrayLike, normal: npt.ArrayLike, parent_id: str, area_m2: float = 1.0
) -> SamplePoints:
    """One sample, for analytic tests and for degenerate elements."""
    return SamplePoints(
        np.asarray(position, dtype=np.float64).reshape(1, 3),
        _normalise(np.asarray(normal, dtype=np.float64).reshape(1, 3)),
        (parent_id,),
        np.array([area_m2], dtype=np.float64),
    )


def grid_on_rectangle(
    origin: npt.ArrayLike,
    edge_u: npt.ArrayLike,
    edge_v: npt.ArrayLike,
    parent_id: str,
    *,
    spacing_m: float = DEFAULT_GRID_SPACING_M,
    surface_offset_m: float = DEFAULT_SURFACE_OFFSET_M,
) -> SamplePoints:
    """A cell-centred grid over a parallelogram, offset along its normal.

    The normal is ``normalise(cross(edge_u, edge_v))``, so the winding of the
    two edge vectors decides which way the surface faces and therefore which
    way the samples are pushed. Cell centres are used rather than a grid
    including the boundary, so no sample lands exactly on an edge shared with
    the host wall and every sample represents an equal share of the area.
    """
    if spacing_m <= 0.0:
        raise ValueError(f"spacing_m must be positive, got {spacing_m}")

    start = np.asarray(origin, dtype=np.float64).reshape(3)
    u = np.asarray(edge_u, dtype=np.float64).reshape(3)
    v = np.asarray(edge_v, dtype=np.float64).reshape(3)

    cross = np.cross(u, v)
    normal = _normalise(cross.reshape(1, 3))
    # The parallelogram's true area, which is not |u| * |v| unless u and v are
    # perpendicular.
    total_area = float(np.linalg.norm(cross))

    # At least one sample per axis: a window narrower than the grid spacing
    # still has to be assessed rather than silently dropped.
    steps_u = max(1, round(float(np.linalg.norm(u)) / spacing_m))
    steps_v = max(1, round(float(np.linalg.norm(v)) / spacing_m))

    centres_u = (np.arange(steps_u, dtype=np.float64) + 0.5) / steps_u
    centres_v = (np.arange(steps_v, dtype=np.float64) + 0.5) / steps_v
    grid_u, grid_v = np.meshgrid(centres_u, centres_v, indexing="ij")

    positions = (
        start + grid_u.reshape(-1, 1) * u + grid_v.reshape(-1, 1) * v + surface_offset_m * normal
    )
    count = len(positions)
    return SamplePoints(
        np.asarray(positions, dtype=np.float64),
        np.repeat(normal, count, axis=0),
        (parent_id,) * count,
        np.full(count, total_area / count, dtype=np.float64),
        surface_offset_m=surface_offset_m,
    )


def horizontal_grid(
    corner: npt.ArrayLike,
    size_x: float,
    size_y: float,
    parent_id: str,
    *,
    height_m: float = 0.0,
    spacing_m: float = DEFAULT_GRID_SPACING_M,
) -> SamplePoints:
    """An upward-facing grid over an axis-aligned rectangle.

    ``corner`` is the **minimum** x and y corner, not the centre, matching
    ``grid_on_rectangle`` which this delegates to. The parameter was once
    called ``origin``, which read as either and was quietly taken as the centre
    by a caller -- putting a whole ground grid half a site away from the
    building it was meant to cover. Hence the name and this paragraph.

    ``height_m`` lifts the samples above the finished level. Private open space
    is assessed at a configured height above the balcony slab; communal and
    adjoining open space is assessed at ground level.
    """
    start = np.asarray(corner, dtype=np.float64).reshape(3).copy()
    start[2] += height_m
    return grid_on_rectangle(
        start,
        (size_x, 0.0, 0.0),
        (0.0, size_y, 0.0),
        parent_id,
        spacing_m=spacing_m,
        # The height above the slab is the offset; adding another along the
        # normal would double-count it.
        surface_offset_m=0.0,
    )


def _barycentric_lattice(k: int) -> tuple[FloatArray, FloatArray]:
    """Centroids of the k^2 congruent sub-triangles of a unit triangle.

    Subdividing each edge into k gives k(k+1)/2 upward and k(k-1)/2 downward
    sub-triangles -- k^2 in total, each of exactly 1/k^2 of the area. Using
    their centroids means every sample stands for the same area and none sits
    on an edge.
    """
    u: list[float] = []
    v: list[float] = []
    for row in range(k):
        for column in range(k - row):
            u.append((column + 1.0 / 3.0) / k)
            v.append((row + 1.0 / 3.0) / k)
        for column in range(k - row - 1):
            u.append((column + 2.0 / 3.0) / k)
            v.append((row + 2.0 / 3.0) / k)
    return np.asarray(u, dtype=np.float64), np.asarray(v, dtype=np.float64)


def triangle_samples(
    triangles: FloatArray,
    parent_ids: list[str] | tuple[str, ...],
    *,
    spacing_m: float = DEFAULT_GRID_SPACING_M,
    surface_offset_m: float = DEFAULT_SURFACE_OFFSET_M,
    faces: FaceSelection = FaceSelection.ALL,
    vertical_tolerance_deg: float = DEFAULT_VERTICAL_TOLERANCE_DEG,
    min_triangle_area_m2: float = 1e-9,
) -> SamplePoints:
    """Sample a triangle soup directly, area-exactly.

    ``triangles`` has shape (m, 3, 3) and ``parent_ids`` names the owning
    element for each. Each triangle is subdivided into ``k**2`` pieces where
    ``k`` is chosen so a piece is about ``spacing_m`` across, and every sample
    carries exactly its share of the triangle's area.

    This is the massing-stage counterpart to ``grid_on_rectangle``. It makes no
    assumption that the surface is rectangular, planar across elements, axis
    aligned or consistently wound, which is what lets it grid a whole facade
    whose only description is "these triangles".
    """
    if spacing_m <= 0.0:
        raise ValueError(f"spacing_m must be positive, got {spacing_m}")

    triangles = np.asarray(triangles, dtype=np.float64)
    if triangles.ndim != 3 or triangles.shape[1:] != (3, 3):
        raise ValueError(f"triangles must have shape (m, 3, 3), got {triangles.shape}")
    if len(parent_ids) != len(triangles):
        raise ValueError(f"{len(parent_ids)} parent ids for {len(triangles)} triangles")

    cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    double_area = np.linalg.norm(cross, axis=1)
    areas = double_area / 2.0

    keep = areas > min_triangle_area_m2
    if faces is not FaceSelection.ALL:
        with np.errstate(invalid="ignore", divide="ignore"):
            unit_z = np.where(
                double_area > 0, cross[:, 2] / np.where(double_area > 0, double_area, 1.0), 0.0
            )
        if faces is FaceSelection.VERTICAL:
            # |n_z| small means the face is upright.
            keep &= np.abs(unit_z) <= np.sin(np.radians(vertical_tolerance_deg))
        else:  # UPWARD
            keep &= unit_z >= np.cos(np.radians(vertical_tolerance_deg))

    if not keep.any():
        return SamplePoints.empty()

    triangles = triangles[keep]
    areas = areas[keep]
    normals = cross[keep] / double_area[keep, None]
    kept_parents = [parent_ids[i] for i in np.flatnonzero(keep)]

    # One sub-triangle roughly spacing_m across. A triangle smaller than a cell
    # still gets one sample: dropping it would quietly shrink the denominator.
    cell_area = spacing_m * spacing_m
    subdivisions = np.maximum(1, np.ceil(np.sqrt(areas / cell_area)).astype(np.int64))

    position_groups, normal_groups, area_groups = [], [], []
    parent_groups: list[str] = []

    for k in np.unique(subdivisions):
        selected = subdivisions == k
        block = triangles[selected]
        block_normals = normals[selected]
        block_areas = areas[selected]
        indices = np.flatnonzero(selected)

        bary_u, bary_v = _barycentric_lattice(int(k))
        bary_w = 1.0 - bary_u - bary_v
        per_triangle = len(bary_u)

        points = (
            bary_w[None, :, None] * block[:, 0][:, None, :]
            + bary_u[None, :, None] * block[:, 1][:, None, :]
            + bary_v[None, :, None] * block[:, 2][:, None, :]
        )
        points = points + surface_offset_m * block_normals[:, None, :]

        position_groups.append(points.reshape(-1, 3))
        normal_groups.append(np.repeat(block_normals, per_triangle, axis=0).reshape(-1, 3))
        area_groups.append(np.repeat(block_areas / per_triangle, per_triangle).reshape(-1))
        parent_groups.extend(kept_parents[i] for i in indices for _ in range(per_triangle))

    return SamplePoints(
        np.concatenate(position_groups).astype(np.float64),
        np.concatenate(normal_groups).astype(np.float64),
        tuple(parent_groups),
        np.concatenate(area_groups).astype(np.float64),
        surface_offset_m=surface_offset_m,
    )
