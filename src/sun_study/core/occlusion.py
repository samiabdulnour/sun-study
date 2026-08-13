"""Ray casting against a triangle soup, for shadowing.

Pure numpy, no native extension, no build step. That is a deployment decision
as much as a testing one: Archicad runs on Windows workstations where a native
ray tracer may not install cleanly, and an analysis engine that only works when
``embreex`` happens to build is not deployable. This is the backend, not a
fallback. An embree fast path can be added later behind the same interface, but
nothing depends on it existing.

Design
------
Shadowing only ever asks *is anything in the way*, never *what is the nearest
thing*. Any-hit is dramatically cheaper than nearest-hit: traversal stops at the
first intersection and no depth sorting is needed.

The BVH is a median-split tree over triangle centroids. Traversal is iterative
and batched: at each node the whole active ray set is slab-tested against the
node's bounding box at once, and only the surviving rays descend. The Python
loop runs once per visited node, not once per ray, so the per-ray work stays in
numpy.

Triangles are treated as double-sided. IFC geometry is not reliably wound, and
a back-facing wall still blocks the sun.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from sun_study.core.geometry import TriangleMesh

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]
BoolArray = npt.NDArray[np.bool_]

__all__ = ["Occluder"]

# Triangles per leaf. Measured, not guessed: on a 96k triangle scene with 265k
# rays, 8 gives 36k rays/s, 32 gives 43k, and 128 drops back to 28k. Small
# leaves make the tree deep and the Python-level traversal loop long; large
# leaves push work into brute-force triangle tests. 32 is the flat part of the
# curve, and it also halves build time against 8.
# Reproduce with scripts/benchmark_occlusion.py.
_MAX_LEAF_TRIANGLES = 32

# Rays per traversal chunk. Bounds peak memory: a leaf test allocates
# (chunk x leaf_triangles x 3) temporaries.
_RAY_CHUNK = 65536

# Rays start slightly along their own direction so a sample point sitting
# exactly on its host surface does not report itself as an occluder. This is a
# floating-point guard only; the real defence is the outward offset applied at
# sampling time (brief section 5.5).
_RAY_EPSILON = 1e-9


@dataclass(frozen=True)
class _Nodes:
    """BVH as flat arrays. Leaves have ``count > 0``; internal nodes have -1."""

    bounds_min: FloatArray
    bounds_max: FloatArray
    left: IntArray
    right: IntArray
    start: IntArray
    count: IntArray


def _triangle_hit(
    origins: FloatArray,
    directions: FloatArray,
    triangles: FloatArray,
    max_distance: FloatArray,
) -> BoolArray:
    """Moller-Trumbore, double-sided, vectorised over (rays x triangles).

    Returns one boolean per ray: did it hit any of these triangles strictly
    between ``_RAY_EPSILON`` and its own ``max_distance``.
    """
    v0 = triangles[:, 0, :]
    edge1 = triangles[:, 1, :] - v0
    edge2 = triangles[:, 2, :] - v0

    # (rays, triangles, 3)
    pvec = np.cross(directions[:, None, :], edge2[None, :, :])
    determinant = np.einsum("kj,ikj->ik", edge1, pvec)

    parallel = np.abs(determinant) < 1e-12
    safe_determinant = np.where(parallel, 1.0, determinant)
    inv_determinant = 1.0 / safe_determinant

    tvec = origins[:, None, :] - v0[None, :, :]
    u = np.einsum("ikj,ikj->ik", tvec, pvec) * inv_determinant

    qvec = np.cross(tvec, edge1[None, :, :])
    v = np.einsum("ij,ikj->ik", directions, qvec) * inv_determinant
    t = np.einsum("kj,ikj->ik", edge2, qvec) * inv_determinant

    # A tolerance on the barycentric bounds keeps a ray that grazes the shared
    # edge of two triangles from slipping between them.
    tolerance = 1e-9
    inside = (u >= -tolerance) & (v >= -tolerance) & (u + v <= 1.0 + tolerance)
    within = (t > _RAY_EPSILON) & (t < max_distance[:, None])

    return np.asarray(np.any(~parallel & inside & within, axis=1), dtype=np.bool_)


class Occluder:
    """A triangle soup prepared for shadow queries."""

    def __init__(self, mesh: TriangleMesh, *, max_leaf_triangles: int = _MAX_LEAF_TRIANGLES):
        if max_leaf_triangles < 1:
            raise ValueError(f"max_leaf_triangles must be >= 1, got {max_leaf_triangles}")

        self._triangles = mesh.triangles()
        self._max_leaf_triangles = max_leaf_triangles
        self._order = np.arange(self.triangle_count, dtype=np.int64)
        self._nodes = self._build()

    @property
    def triangle_count(self) -> int:
        return len(self._triangles)

    @property
    def node_count(self) -> int:
        return len(self._nodes.count)

    # -- construction ----------------------------------------------------
    def _build(self) -> _Nodes:
        bounds_min: list[FloatArray] = []
        bounds_max: list[FloatArray] = []
        left: list[int] = []
        right: list[int] = []
        start: list[int] = []
        count: list[int] = []

        if self.triangle_count == 0:
            return _Nodes(
                np.zeros((0, 3)), np.zeros((0, 3)),
                np.zeros(0, np.int64), np.zeros(0, np.int64),
                np.zeros(0, np.int64), np.zeros(0, np.int64),
            )  # fmt: skip

        centroids = self._triangles.mean(axis=1)

        def add_node(lo: int, hi: int) -> int:
            span = self._order[lo:hi]
            corners = self._triangles[span]
            index = len(count)

            bounds_min.append(corners.reshape(-1, 3).min(axis=0))
            bounds_max.append(corners.reshape(-1, 3).max(axis=0))
            left.append(-1)
            right.append(-1)

            if hi - lo <= self._max_leaf_triangles:
                start.append(lo)
                count.append(hi - lo)
                return index

            start.append(-1)
            count.append(-1)

            # Split at the median along the widest extent of the centroids.
            local = centroids[span]
            axis = int(np.argmax(local.max(axis=0) - local.min(axis=0)))
            middle = (hi - lo) // 2
            order = np.argpartition(local[:, axis], middle)
            self._order[lo:hi] = span[order]

            left[index] = add_node(lo, lo + middle)
            right[index] = add_node(lo + middle, hi)
            return index

        add_node(0, self.triangle_count)
        return _Nodes(
            np.asarray(bounds_min, dtype=np.float64),
            np.asarray(bounds_max, dtype=np.float64),
            np.asarray(left, dtype=np.int64),
            np.asarray(right, dtype=np.int64),
            np.asarray(start, dtype=np.int64),
            np.asarray(count, dtype=np.int64),
        )

    # -- queries ---------------------------------------------------------
    def any_hit(
        self,
        origins: FloatArray,
        directions: FloatArray,
        max_distance: npt.ArrayLike | None = None,
    ) -> BoolArray:
        """True where the ray from ``origin`` along ``direction`` hits geometry.

        ``directions`` need not be normalised, but ``max_distance`` is measured
        in units of the direction vector, so normalise them if it matters.
        """
        origins = np.ascontiguousarray(origins, dtype=np.float64)
        directions = np.ascontiguousarray(directions, dtype=np.float64)
        if origins.shape != directions.shape:
            raise ValueError(
                f"origins {origins.shape} and directions {directions.shape} must match"
            )
        if origins.ndim != 2 or origins.shape[1] != 3:
            raise ValueError(f"origins must have shape (n, 3), got {origins.shape}")

        ray_count = len(origins)
        limit = (
            np.full(ray_count, np.inf, dtype=np.float64)
            if max_distance is None
            else np.broadcast_to(np.asarray(max_distance, dtype=np.float64), (ray_count,)).astype(
                np.float64
            )
        )

        hit = np.zeros(ray_count, dtype=np.bool_)
        if self.triangle_count == 0 or ray_count == 0:
            return hit

        for begin in range(0, ray_count, _RAY_CHUNK):
            end = min(begin + _RAY_CHUNK, ray_count)
            hit[begin:end] = self._traverse(
                origins[begin:end], directions[begin:end], limit[begin:end]
            )
        return hit

    def _traverse(
        self, origins: FloatArray, directions: FloatArray, limit: FloatArray
    ) -> BoolArray:
        nodes = self._nodes
        hit = np.zeros(len(origins), dtype=np.bool_)

        with np.errstate(divide="ignore", invalid="ignore"):
            inverse = 1.0 / directions

        stack: list[tuple[int, IntArray]] = [(0, np.arange(len(origins), dtype=np.int64))]
        while stack:
            node, active = stack.pop()

            active = active[~hit[active]]
            if active.size == 0:
                continue

            active = active[
                self._slab_test(
                    origins[active],
                    inverse[active],
                    limit[active],
                    nodes.bounds_min[node],
                    nodes.bounds_max[node],
                )
            ]
            if active.size == 0:
                continue

            leaf_size = int(nodes.count[node])
            if leaf_size > 0:
                begin = int(nodes.start[node])
                triangles = self._triangles[self._order[begin : begin + leaf_size]]
                hit[active] |= _triangle_hit(
                    origins[active], directions[active], triangles, limit[active]
                )
            else:
                stack.append((int(nodes.left[node]), active))
                stack.append((int(nodes.right[node]), active))

        return hit

    @staticmethod
    def _slab_test(
        origins: FloatArray,
        inverse: FloatArray,
        limit: FloatArray,
        lower: FloatArray,
        upper: FloatArray,
    ) -> BoolArray:
        """Standard slab test, tolerant of axis-parallel rays.

        A zero direction component yields +/-inf here, and the inf arithmetic
        gives the right answer for a ray parallel to that slab; only the 0*inf
        case needs the nan_to_num guard.
        """
        with np.errstate(invalid="ignore"):
            t_low = (lower - origins) * inverse
            t_high = (upper - origins) * inverse

        t_low = np.nan_to_num(t_low, nan=-np.inf)
        t_high = np.nan_to_num(t_high, nan=np.inf)

        t_near = np.maximum(np.minimum(t_low, t_high), 0.0).max(axis=1)
        t_far = np.maximum(t_low, t_high).min(axis=1)

        return np.asarray((t_near <= t_far) & (t_near < limit), dtype=np.bool_)
