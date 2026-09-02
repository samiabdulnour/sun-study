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

__all__ = [
    "PlanTransform",
    "TriangleMesh",
    "box",
    "fit_plan_transform",
    "horizontal_rectangle",
    "prism",
    "rectangle",
    "rotation_about_z",
    "triangulate",
]


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


def _signed_area(outline: FloatArray) -> float:
    """Twice the signed area of a closed 2D ring. Positive is counter-clockwise."""
    x, y = outline[:, 0], outline[:, 1]
    return float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _inside(triangle: FloatArray, point: FloatArray) -> bool:
    """Whether a point lies within a triangle, edges counting as inside.

    Sign tests on the three edge cross products. A point exactly on an edge
    gives zero and is treated as inside, which is the conservative direction
    here: it refuses an ear rather than clipping one that swallows a vertex.
    """
    a, b, c = triangle
    d1 = (point[0] - b[0]) * (a[1] - b[1]) - (a[0] - b[0]) * (point[1] - b[1])
    d2 = (point[0] - c[0]) * (b[1] - c[1]) - (b[0] - c[0]) * (point[1] - c[1])
    d3 = (point[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (point[1] - a[1])
    negative = d1 < 0.0 or d2 < 0.0 or d3 < 0.0
    positive = d1 > 0.0 or d2 > 0.0 or d3 > 0.0
    return not (negative and positive)


def triangulate(outline: npt.ArrayLike) -> IntArray:
    """Triangle indices for one simple polygon, by ear clipping.

    A site boundary is a closed ring of a handful of points and is not
    reliably convex -- a battleaxe block, a splayed corner, a right-of-way cut
    out of one side are all ordinary -- so a triangle fan from vertex zero is
    wrong on real land. Ear clipping is the smallest thing that is right for
    any simple polygon, needs no dependency, and at this size costs nothing:
    an eight-sided boundary is 6 triangles found in a few dozen comparisons.

    Holes are not supported and are not wanted: this triangulates a boundary
    so it can be extruded into an occluder, and a hole in a site is still land
    that a compliant envelope could be built over.

    Returned indices are into the outline as given, so the caller keeps its own
    vertex order and can reuse it for the walls.
    """
    ring = np.asarray(outline, dtype=np.float64)
    if ring.ndim != 2 or ring.shape[1] < 2:
        raise ValueError(f"outline must be (n, 2) or (n, 3), got {ring.shape}")
    ring = ring[:, :2]
    if len(ring) < 3:
        raise ValueError(f"a polygon needs at least 3 points, got {len(ring)}")

    # Ear clipping is stated for counter-clockwise rings. Work on indices so a
    # clockwise boundary -- which is just as likely off a survey -- is walked
    # in reverse rather than copied and flipped.
    order = list(range(len(ring)))
    if _signed_area(ring) < 0.0:
        order.reverse()

    faces: list[tuple[int, int, int]] = []
    guard = 0
    while len(order) > 3:
        # Every vertex reflex means the ring is not simple -- self-crossing, or
        # duplicated points. Bail with what is built rather than spin: the
        # caller gets an under-triangulated cap, which shades slightly less
        # than it should, and never a hang.
        clipped = False
        for position in range(len(order)):
            previous = order[position - 1]
            current = order[position]
            following = order[(position + 1) % len(order)]
            triangle = ring[[previous, current, following]]
            if _signed_area(triangle) <= 0.0:
                continue  # reflex, or degenerate: not an ear
            rest = [index for index in order if index not in (previous, current, following)]
            if any(_inside(triangle, ring[index]) for index in rest):
                continue  # something else is in it, so clipping would overlap
            faces.append((previous, current, following))
            order.pop(position)
            clipped = True
            break
        guard += 1
        if not clipped or guard > len(ring):
            break
    if len(order) == 3:
        faces.append((order[0], order[1], order[2]))
    return np.array(faces, dtype=np.int64).reshape(-1, 3)


def prism(outline: npt.ArrayLike, base_z: float, top_z: float) -> TriangleMesh:
    """A closed solid: one polygon swept vertically between two levels.

    What a planning envelope is -- a site boundary and a height limit -- and
    the only shape needed to ask what an as-of-right building on this land
    would have shadowed. Closed on all six sides rather than walls alone: a
    ray fired from a ground sample *inside* the footprint leaves through the
    cap, and an open-topped prism would let that sample see the sun straight
    through the roof of the thing shading it.

    ``outline`` is a ring of (x, y) or (x, y, z); any z is ignored, because the
    two levels are the arguments. The ring must not repeat its first point at
    the end -- a duplicate is dropped rather than refused, since that is how
    half of the world writes a closed polygon.
    """
    ring = np.asarray(outline, dtype=np.float64)
    if ring.ndim != 2 or ring.shape[1] < 2:
        raise ValueError(f"outline must be (n, 2) or (n, 3), got {ring.shape}")
    ring = ring[:, :2]
    if len(ring) > 1 and np.allclose(ring[0], ring[-1]):
        ring = ring[:-1]
    if len(ring) < 3:
        raise ValueError(f"a prism needs at least 3 distinct points, got {len(ring)}")
    if top_z <= base_z:
        raise ValueError(f"top_z {top_z} must be above base_z {base_z}")

    count = len(ring)
    vertices = np.vstack(
        [
            np.column_stack([ring, np.full(count, base_z)]),
            np.column_stack([ring, np.full(count, top_z)]),
        ]
    )

    cap = triangulate(ring)
    faces: list[tuple[int, int, int]] = []
    # The base wound one way and the cap the other, so both face outward. The
    # ray caster does not read winding, but a mesh that is consistent is one
    # that can be looked at in a viewer when a shadow comes out wrong.
    for a, b, c in cap:
        faces.append((int(a), int(c), int(b)))
        faces.append((count + int(a), count + int(b), count + int(c)))
    for index in range(count):
        following = (index + 1) % count
        faces.append((index, following, count + following))
        faces.append((index, count + following, count + index))

    return TriangleMesh(vertices, np.array(faces, dtype=np.int64))


@dataclass(frozen=True)
class PlanTransform:
    """A rotation and a shift taking one plan frame onto another.

    The frames in question are the IFC export's world coordinates and
    Archicad's project coordinates. They are not the same and need not be: an
    export made with the Survey Point option is already north-aligned, so it
    is the *project* that is rotated relative to it. Anything computed from
    the export -- a sun patch, say -- therefore lands in the wrong place and
    at the wrong angle if it is drawn into the project unchanged.
    """

    rotation: FloatArray
    """2x2, applied before the shift."""
    offset: FloatArray
    """Metres, in the target frame."""
    rmse_m: float
    """How well the fitted pairs actually agree. The number that decides
    whether the transform may be used at all."""

    per_pair_m: tuple[float, ...] = ()
    """How far each pair is out on its own, in the order it was given.

    Kept because ``rmse_m`` alone is unactionable. It is a root-mean-square
    over every pair, so one zone that has moved and twenty that have not read
    as a middling number spread over all of them, and the message a person
    gets names none of them. A refusal that says *which* pair disagrees is the
    difference between "the export is stale" and "this one zone was edited".
    """
    keys: tuple[str, ...] = ()
    """What each pair is, parallel to ``per_pair_m``, when the caller knows.

    Empty from the solver, which is given points and no names. The adapter
    fills it in, because there the pairs are zones and a zone has a name.
    """
    without_each_m: tuple[float, ...] = ()
    """What ``rmse_m`` would be if each pair in turn were dropped.

    The per-pair distances alone do not answer the question a person actually
    has, which is *whether one zone is the problem*. A rigid fit cannot
    isolate an outlier on its own: it rotates and shifts to split the
    difference, so one zone moved by two metres leaves every other pair half a
    metre out as well. Measured -- a single 2 m outlier among five pairs reads
    as 1.27, 0.62, 0.58, 0.28, 0.17 m, which is indistinguishable by eye from
    a uniformly stale export.

    Refitting without each pair does separate them. If dropping one takes the
    rest under the limit, that one is the disagreement and the others were
    only carrying its error. If no single removal helps, the two frames really
    are out of step and the export is the thing to replace.

    Empty below four pairs, where it could not mean anything: drop one of
    three and the remaining two fit perfectly by construction, so every entry
    would read zero and accuse whichever zone was asked about last.
    """

    def apply(self, points: FloatArray) -> FloatArray:
        """Map ``(n, 2)`` plan points into the target frame."""
        flat = np.asarray(points, dtype=np.float64)[:, :2]
        return np.asarray(flat @ self.rotation.T + self.offset, dtype=np.float64)

    def name_of(self, index: int) -> str:
        """What to call one pair in a message."""
        return self.keys[index] if index < len(self.keys) else f"pair {index + 1}"

    def worst(self, limit: int = 3) -> list[tuple[str, float]]:
        """The pairs that disagree most, furthest first."""
        named = zip(
            self.keys or [f"pair {n + 1}" for n in range(len(self.per_pair_m))],
            self.per_pair_m,
            strict=False,
        )
        return sorted(named, key=lambda pair: -pair[1])[:limit]

    def blame(self, limit_m: float) -> tuple[str, float] | None:
        """The single pair whose removal would bring the fit inside ``limit_m``.

        ``None`` when no one pair explains the disagreement, which is itself
        an answer: the frames are out of step rather than one zone having
        moved. Ambiguity counts as no answer too -- if two different removals
        each rescue the fit, naming either would send somebody to edit a zone
        that may be innocent.
        """
        rescued = [
            (index, rmse) for index, rmse in enumerate(self.without_each_m) if rmse <= limit_m
        ]
        if len(rescued) != 1:
            return None
        index, rmse = rescued[0]
        return self.name_of(index), rmse

    def describe_disagreement(self, limit_m: float, limit: int = 3) -> str:
        """Why the fit is out, as lines a refusal can carry.

        The per-pair distances first, because they are the evidence, then the
        leave-one-out verdict, because it is the part somebody can act on.
        """
        if not self.per_pair_m:
            return ""
        lines = [f"    {name} is {metres:.2f} m out" for name, metres in self.worst(limit)]
        rest = len(self.per_pair_m) - len(lines)
        if rest > 0:
            lines.append(f"    ...and {rest} more, the closest {min(self.per_pair_m):.2f} m out")

        if not self.without_each_m:
            verdict = (
                f"  {len(self.per_pair_m)} pairs is too few to tell one moved Zone from "
                f"a stale export. Put more Zones in the export to find out which."
            )
        elif (blamed := self.blame(limit_m)) is not None:
            name, rmse = blamed
            verdict = (
                f"  Leaving {name!r} out fits the other {len(self.per_pair_m) - 1} to "
                f"{rmse:.2f} m, inside the {limit_m:g} m limit. That one Zone is the "
                f"disagreement: it has been moved, reshaped, or matched to the wrong "
                f"element since the export. The rest of the model agrees with itself."
            )
        else:
            # Two ways to have no culprit, and they are not the same advice.
            # Either no removal helps at all, or several do -- and several is
            # the shape a uniformly drifted export takes, because with the
            # error spread evenly any one pair can be dropped to bring the
            # rest inside. Saying "dropping the worst still leaves 0.43 m"
            # over a 0.5 m limit, which the first draft did, reads as though
            # 0.43 were the failure.
            rescued = sum(1 for rmse in self.without_each_m if rmse <= limit_m)
            verdict = (
                f"  No one Zone stands out: {rescued} of them would each rescue the fit "
                f"on their own, which is what an export drifted as a whole looks like "
                f"rather than one Zone having moved. Re-export from the open file."
                if rescued
                else f"  No single Zone explains it -- the best any one removal manages "
                f"is {min(self.without_each_m):.2f} m, still over the {limit_m:g} m "
                f"limit. The export is out of step with the project as a whole, so "
                f"re-export it from the open file."
            )
        return "\n".join(["  how far each pair is out, worst first:", *lines, verdict])


def fit_plan_transform(source: FloatArray, target: FloatArray) -> PlanTransform:
    """Fit the rotation and shift that best takes ``source`` onto ``target``.

    Kabsch in two dimensions, over matched pairs -- in practice one pair per
    apartment, its centroid as the export sees it against its centroid as
    Archicad does. Rigid on purpose: rotation and translation only, no scale
    and no reflection. Both frames are metres, so a fitted scale would not be
    a discovery about the model but a symptom of a mismatched pairing, and
    letting it absorb the error would hide exactly the failure ``rmse_m``
    exists to expose.

    Two pairs are the minimum and three are the fewest that can disagree. With
    two, ``rmse_m`` is zero by construction and says nothing -- the caller has
    to treat a thin fit as unverified rather than as perfect.
    """
    a = np.asarray(source, dtype=np.float64)[:, :2]
    b = np.asarray(target, dtype=np.float64)[:, :2]
    if len(a) != len(b):
        raise ValueError(f"{len(a)} source points against {len(b)} target points")
    if len(a) < 2:
        raise ValueError(f"a plan transform needs at least two pairs, got {len(a)}")

    rotation, offset, rmse, residuals = _kabsch_2d(a, b)

    # Refit once without each pair, so a refusal can say whether any single
    # pair is the whole disagreement. There are as many pairs as there are
    # paired Zones -- tens at most -- and each fit is the SVD of a 2x2, so the
    # diagnosis costs far less than the round trip that read the Zones. Below
    # four pairs it is not computed at all: see ``without_each_m``.
    without_each: tuple[float, ...] = ()
    if len(a) >= 4:
        without_each = tuple(
            _kabsch_2d(np.delete(a, drop, axis=0), np.delete(b, drop, axis=0))[2]
            for drop in range(len(a))
        )

    return PlanTransform(
        rotation=rotation,
        offset=offset,
        rmse_m=rmse,
        per_pair_m=tuple(float(distance) for distance in np.linalg.norm(residuals, axis=1)),
        without_each_m=without_each,
    )


def _kabsch_2d(a: FloatArray, b: FloatArray) -> tuple[FloatArray, FloatArray, float, FloatArray]:
    """The fit itself: rotation, shift, RMSE and the per-pair residuals.

    Separate from ``fit_plan_transform`` because the leave-one-out diagnosis
    calls it once per pair, and a fit that built its own diagnosis each time
    would recurse.
    """
    centre_a, centre_b = a.mean(axis=0), b.mean(axis=0)
    covariance = (a - centre_a).T @ (b - centre_b)
    u, _, vt = np.linalg.svd(covariance)
    # The determinant guard is what keeps this a rotation. Without it a noisy
    # or mis-paired fit can come back as a reflection, which draws a mirrored
    # plan that looks almost right.
    correction = np.diag([1.0, float(np.sign(np.linalg.det(vt.T @ u.T)))])
    rotation = vt.T @ correction @ u.T
    offset = centre_b - rotation @ centre_a

    residuals = b - (a @ rotation.T + offset)
    rmse = float(np.sqrt(np.mean(np.sum(residuals**2, axis=1)))) if len(a) else 0.0
    return rotation, offset, rmse, residuals
