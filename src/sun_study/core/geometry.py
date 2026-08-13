"""Triangulated geometry and the transforms the analysis needs.

Pure numpy. Everything here is a plain array operation on a triangle soup;
there is no mesh topology, no solid modelling and no file format. IFC parsing
lives in ``ingest``, and the only thing that crosses the boundary is vertices
and faces.

Frames
------
``TriangleMesh`` carries no notion of which way is north. Its coordinates are
whatever frame the caller is working in -- ENU for synthetic test geometry,
the Archicad project frame for a real model. Tying a frame to the compass is
``core.orientation``'s job, and keeping that separate is what makes the
rotation invariance tests in ``tests/unit/test_analytic_cases.py`` meaningful.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

__all__ = ["TriangleMesh", "box", "horizontal_rectangle", "rectangle", "rotation_about_z"]


def rotation_about_z(degrees: float) -> FloatArray:
    """Right-handed rotation matrix about +Z, counter-clockwise seen from above.

    Counter-clockwise in a right-handed frame is the *opposite* sense to a
    compass bearing, which increases clockwise. ``core.orientation`` is where
    that sign is reconciled, once, with a test.
    """
    angle = np.radians(degrees)
    cos, sin = np.cos(angle), np.sin(angle)
    return np.array(
        [[cos, -sin, 0.0], [sin, cos, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )


@dataclass(frozen=True)
class TriangleMesh:
    """A triangle soup: ``vertices`` of shape (n, 3), ``faces`` of shape (m, 3)."""

    vertices: FloatArray
    faces: IntArray

    def __post_init__(self) -> None:
        if self.vertices.ndim != 2 or self.vertices.shape[1] != 3:
            raise ValueError(f"vertices must have shape (n, 3), got {self.vertices.shape}")
        if self.faces.ndim != 2 or self.faces.shape[1] != 3:
            raise ValueError(f"faces must have shape (m, 3), got {self.faces.shape}")
        if self.faces.size and int(self.faces.max()) >= len(self.vertices):
            raise ValueError(
                f"face references vertex {int(self.faces.max())} but there are "
                f"only {len(self.vertices)} vertices"
            )
        if self.faces.size and int(self.faces.min()) < 0:
            raise ValueError("faces must not contain negative indices")

    @property
    def triangle_count(self) -> int:
        return len(self.faces)

    def triangles(self) -> FloatArray:
        """Vertex positions per face, shape (m, 3, 3)."""
        return np.asarray(self.vertices[self.faces], dtype=np.float64)

    def transformed(self, matrix: FloatArray) -> TriangleMesh:
        """Apply a 3x3 linear transform to every vertex."""
        return TriangleMesh(np.asarray(self.vertices @ matrix.T, dtype=np.float64), self.faces)

    def rotated_about_z(self, degrees: float) -> TriangleMesh:
        return self.transformed(rotation_about_z(degrees))

    def translated(self, offset: npt.ArrayLike) -> TriangleMesh:
        shift = np.asarray(offset, dtype=np.float64).reshape(3)
        return TriangleMesh(np.asarray(self.vertices + shift, dtype=np.float64), self.faces)

    @classmethod
    def empty(cls) -> TriangleMesh:
        return cls(np.zeros((0, 3), dtype=np.float64), np.zeros((0, 3), dtype=np.int64))

    @classmethod
    def concatenate(cls, meshes: list[TriangleMesh]) -> TriangleMesh:
        """Merge meshes, offsetting face indices. Vertices are not welded."""
        kept = [mesh for mesh in meshes if mesh.triangle_count]
        if not kept:
            return cls.empty()

        vertices, faces, offset = [], [], 0
        for mesh in kept:
            vertices.append(mesh.vertices)
            faces.append(mesh.faces + offset)
            offset += len(mesh.vertices)
        return cls(
            np.concatenate(vertices).astype(np.float64),
            np.concatenate(faces).astype(np.int64),
        )


def rectangle(origin: npt.ArrayLike, edge_u: npt.ArrayLike, edge_v: npt.ArrayLike) -> TriangleMesh:
    """A parallelogram spanned by two edge vectors from ``origin``, as 2 triangles."""
    start = np.asarray(origin, dtype=np.float64).reshape(3)
    u = np.asarray(edge_u, dtype=np.float64).reshape(3)
    v = np.asarray(edge_v, dtype=np.float64).reshape(3)
    vertices = np.array([start, start + u, start + u + v, start + v], dtype=np.float64)
    faces = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int64)
    return TriangleMesh(vertices, faces)


def horizontal_rectangle(
    centre: npt.ArrayLike, size_x: float, size_y: float, height: float
) -> TriangleMesh:
    """An axis-aligned horizontal panel centred over ``centre`` at ``height``."""
    origin = np.asarray(centre, dtype=np.float64).reshape(3).copy()
    origin[0] -= size_x / 2.0
    origin[1] -= size_y / 2.0
    origin[2] = height
    return rectangle(origin, (size_x, 0.0, 0.0), (0.0, size_y, 0.0))


def box(min_corner: npt.ArrayLike, max_corner: npt.ArrayLike) -> TriangleMesh:
    """An axis-aligned box as 12 triangles, outward-facing winding."""
    low = np.asarray(min_corner, dtype=np.float64).reshape(3)
    high = np.asarray(max_corner, dtype=np.float64).reshape(3)
    if np.any(high <= low):
        raise ValueError(f"max_corner {high} must exceed min_corner {low} on every axis")

    x0, y0, z0 = low
    x1, y1, z1 = high
    vertices = np.array(
        [
            [x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
            [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1],
        ],
        dtype=np.float64,
    )  # fmt: skip
    faces = np.array(
        [
            [0, 3, 2], [0, 2, 1],  # bottom, -Z
            [4, 5, 6], [4, 6, 7],  # top, +Z
            [0, 1, 5], [0, 5, 4],  # -Y
            [2, 3, 7], [2, 7, 6],  # +Y
            [1, 2, 6], [1, 6, 5],  # +X
            [3, 0, 4], [3, 4, 7],  # -X
        ],
        dtype=np.int64,
    )  # fmt: skip
    return TriangleMesh(vertices, faces)
