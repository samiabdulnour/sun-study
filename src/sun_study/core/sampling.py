"""Sample point generation on the surfaces being assessed.

Every sample carries three things: where it is, which way it faces, and which
element it belongs to. The parent id is what lets results aggregate back to an
apartment without the caller keeping a parallel bookkeeping structure.

The outward offset
------------------
Window samples sit on the glazing plane, which is coplanar with -- or inside --
the host wall. A ray fired from exactly that plane will hit the wall, the
reveal or the frame and report the window as permanently shaded. Samples are
therefore pushed out along the surface normal by a small distance, 50 mm by
default (brief section 5.5).

That offset is a real modelling choice, not a fudge: too small and the host
wall self-occludes, too large and a sample escapes its own reveal and reports
sun that a real window would not see. The value used is recorded on the result
so a run can be reproduced and argued about.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]

__all__ = [
    "DEFAULT_GRID_SPACING_M",
    "DEFAULT_SURFACE_OFFSET_M",
    "SamplePoints",
    "grid_on_rectangle",
    "horizontal_grid",
    "single_sample",
]

DEFAULT_GRID_SPACING_M = 0.2
DEFAULT_SURFACE_OFFSET_M = 0.05


def _normalise(vectors: FloatArray) -> FloatArray:
    lengths = np.linalg.norm(vectors, axis=-1, keepdims=True)
    if np.any(lengths == 0.0):
        raise ValueError("cannot normalise a zero-length vector")
    return np.asarray(vectors / lengths, dtype=np.float64)


@dataclass(frozen=True)
class SamplePoints:
    """Positions, unit normals and parent element ids, all length n."""

    positions: FloatArray
    normals: FloatArray
    parent_ids: tuple[str, ...]
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

    def __len__(self) -> int:
        return len(self.positions)

    @property
    def unique_parents(self) -> tuple[str, ...]:
        """Parent ids in first-appearance order, so output ordering is stable."""
        return tuple(dict.fromkeys(self.parent_ids))

    def mask_for(self, parent_id: str) -> npt.NDArray[np.bool_]:
        return np.asarray([pid == parent_id for pid in self.parent_ids], dtype=np.bool_)

    @classmethod
    def concatenate(cls, groups: list[SamplePoints]) -> SamplePoints:
        kept = [group for group in groups if len(group)]
        if not kept:
            return cls(np.zeros((0, 3), dtype=np.float64), np.zeros((0, 3), dtype=np.float64), ())

        offsets = {group.surface_offset_m for group in kept}
        return cls(
            np.concatenate([group.positions for group in kept]).astype(np.float64),
            np.concatenate([group.normals for group in kept]).astype(np.float64),
            tuple(pid for group in kept for pid in group.parent_ids),
            # Only meaningful when every group used the same offset; a mixed
            # set reports 0.0 rather than one group's value standing for all.
            surface_offset_m=next(iter(offsets)) if len(offsets) == 1 else 0.0,
        )


def single_sample(position: npt.ArrayLike, normal: npt.ArrayLike, parent_id: str) -> SamplePoints:
    """One sample, for analytic tests and for degenerate elements."""
    return SamplePoints(
        np.asarray(position, dtype=np.float64).reshape(1, 3),
        _normalise(np.asarray(normal, dtype=np.float64).reshape(1, 3)),
        (parent_id,),
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

    normal = _normalise(np.cross(u, v).reshape(1, 3))

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
    return SamplePoints(
        np.asarray(positions, dtype=np.float64),
        np.repeat(normal, len(positions), axis=0),
        (parent_id,) * len(positions),
        surface_offset_m=surface_offset_m,
    )


def horizontal_grid(
    origin: npt.ArrayLike,
    size_x: float,
    size_y: float,
    parent_id: str,
    *,
    height_m: float = 0.0,
    spacing_m: float = DEFAULT_GRID_SPACING_M,
) -> SamplePoints:
    """An upward-facing grid over a rectangle, for balconies and open space.

    ``height_m`` lifts the samples above the finished level. Private open space
    is assessed at a configured height above the balcony slab; communal and
    adjoining open space is assessed at ground level.
    """
    start = np.asarray(origin, dtype=np.float64).reshape(3).copy()
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
