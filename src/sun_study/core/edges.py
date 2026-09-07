"""Shadow boundaries traced where the shadow really ends, not where a cell does.

A shadow computed from a grid of samples can only be drawn along cell edges,
so its outline comes out as a staircase: at one metre on a 1:500 sheet, a two
millimetre step, well above pen width and plainly visible as pixellation.

Making the grid finer is the obvious answer and the wrong one. Cost goes with
the *area* -- halving the step quadruples the samples -- while all that is
wanted is a better line, which is one-dimensional. Refining everywhere to fix
an edge is paying for a whole plan to improve its border.

Sampling is not what limits the edge either. The occlusion test answers at any
point, not only at lattice points, so the boundary can be *found* rather than
approximated: it is where the answer flips, and a flip can be bracketed and
bisected to whatever tolerance is asked for. Ten bisections take a metre to a
millimetre. What comes out follows the true edge at whatever angle it runs,
which is the thing a finer grid never achieves -- a staircase with smaller
steps is still a staircase.

Why not project the silhouette instead
--------------------------------------
Because of what the shadow lands on. Extruding a massing's silhouette along
the sun and intersecting it with a plane is exact and simple; the receiver
here is terrain and every building standing on it, and intersecting a shadow
volume with that is three-dimensional boolean geometry, followed by polygon
booleans for every difference between one source and the next. The flat-plane
version is reachable and would undo the thing that mattered most: a shadow
reaching a thirty-metre neighbour stops on its roof, and that correction moved
the measured areas by a third at nine in the morning.

Bisection gets the same edge on the receiver that is actually there.

What this does not change
-------------------------
Areas. They are counted from the cell mask, as they always were, because the
mask is what tiles and what differences against the other sources. This module
draws a better line around the same measurement, and a run says so: the drawn
outline and the counted area differ by less than the cell they disagree
within.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]
Ring = tuple[tuple[float, float], ...]

__all__ = [
    "DEFAULT_TOLERANCE_M",
    "SEAM_WIDTH_M",
    "bridged",
    "group_regions",
    "refined_rings",
    "self_intersects",
    "signed_area",
]

#: How close a traced vertex is brought to the true edge. A millimetre is far
#: below what any sheet resolves -- 1:500 makes it two thousandths of a
#: millimetre on paper -- and each halving is one more ray, so ten of them take
#: a one metre cell to under a millimetre. Cheap enough not to economise.
DEFAULT_TOLERANCE_M = 0.001

#: How wide the seam joining a hole to its outline is opened. Zero is the
#: exact answer and the one Archicad refuses: a contour that touches itself is
#: not a simple polygon, whatever its area comes to. A millimetre is below
#: what any sheet resolves and costs the seam's length times a millimetre.
SEAM_WIDTH_M = 0.001

#: Marching squares, as segments between edge crossings. The key is the four
#: corners read anticlockwise from the lower left; the value is the pairs of
#: *edges* a segment runs between, edges numbered 0 south, 1 east, 2 north,
#: 3 west. Shade is the inside, so a segment always keeps shade on its left.
#:
#: The two ambiguous cases -- 5 and 10, where two shaded corners meet only at
#: the cell's centre -- are resolved the way the cell tracer resolves them, by
#: keeping the shaded side apart. A boundary cannot separate the shaded side at
#: a corner and the lit side there as well, and it is the lit side that may
#: leak out.
_CASES: dict[int, tuple[tuple[int, int], ...]] = {
    0: (),
    1: ((3, 0),),
    2: ((0, 1),),
    3: ((3, 1),),
    4: ((1, 2),),
    5: ((3, 2), (1, 0)),
    6: ((0, 2),),
    7: ((3, 2),),
    8: ((2, 3),),
    9: ((2, 0),),
    10: ((0, 3), (2, 1)),
    11: ((2, 1),),
    12: ((1, 3),),
    13: ((1, 0),),
    14: ((0, 3),),
    15: (),
}

#: Which two corners each edge runs between, in the same numbering.
_EDGE_CORNERS: tuple[tuple[int, int], ...] = ((0, 1), (1, 2), (3, 2), (0, 3))


def _crossings(
    lower: FloatArray,
    upper: FloatArray,
    shaded_at: Callable[[FloatArray], BoolArray],
    tolerance_m: float,
) -> FloatArray:
    """Bisect every ``lower``-``upper`` pair to where the answer flips.

    ``lower`` is the shaded end and ``upper`` the lit one, both ``(n, 3)``, and
    every pair is known to straddle a boundary because that is how it was
    chosen. Bisection cannot fail from there: the bracket is halved a fixed
    number of times and the shaded end is carried along, so the answer is
    always inside the original pair and always on the shaded side of the edge
    by less than the tolerance.

    Every pair is stepped together in one vectorised call rather than looped,
    because one ray cast over ten thousand origins costs a fraction of ten
    thousand casts over one -- the traversal is the same work either way and
    the Python is not.
    """
    a = np.array(lower, dtype=np.float64, copy=True)
    b = np.array(upper, dtype=np.float64, copy=True)
    span = float(np.max(np.linalg.norm(b - a, axis=1))) if len(a) else 0.0
    steps = 0
    while span > tolerance_m and steps < 32:
        middle = (a + b) / 2.0
        dark = shaded_at(middle)
        a = np.where(dark[:, None], middle, a)
        b = np.where(dark[:, None], b, middle)
        span /= 2.0
        steps += 1
    return (a + b) / 2.0


def refined_rings(
    positions: FloatArray,
    shaded: BoolArray,
    shape: tuple[int, int],
    *,
    shaded_at: Callable[[FloatArray], BoolArray],
    tolerance_m: float = DEFAULT_TOLERANCE_M,
) -> list[Ring]:
    """The outline of a shaded region, traced to where the shadow really ends.

    ``positions`` are the lattice sample points in row-major ``shape`` order,
    ``shaded`` the mask over them, and ``shaded_at`` answers the same question
    at arbitrary points -- the occlusion test, with the sample's own height
    carried along.

    Marching squares gives the topology: which cells the boundary crosses and
    in what order, which is exactly what a cell mask can be trusted for. The
    *position* of each crossing then comes from bisecting the lattice edge it
    sits on, so the vertices land on the true boundary rather than on the
    lattice. Rings come back closed and oriented, shade on the left.
    """
    inner_x, inner_y = shape
    if inner_x < 2 or inner_y < 2:
        return []
    inner_dark = np.asarray(shaded, dtype=bool).reshape(inner_x, inner_y)
    inner_points = np.asarray(positions, dtype=np.float64).reshape(inner_x, inner_y, 3)

    # A lit border one cell wide, so a region reaching the edge of the grid
    # still closes. Without it the boundary runs off the lattice, the walk
    # never returns to where it started, and the ring is dropped -- the whole
    # shadow silently missing rather than drawn short.
    #
    # The border is not a claim that the ground beyond is sunlit. It is where
    # the sampling stops, and the cell tracer says the same thing by extending
    # half a cell past its last shaded sample. Points on it are answered as
    # lit below, so a boundary bisecting towards it settles on the edge of
    # what was measured.
    nx, ny = inner_x + 2, inner_y + 2
    dark = np.zeros((nx, ny), dtype=bool)
    dark[1:-1, 1:-1] = inner_dark
    points = np.zeros((nx, ny, 3), dtype=np.float64)
    points[1:-1, 1:-1] = inner_points
    step_x = inner_points[1, 0, 0] - inner_points[0, 0, 0]
    step_y = inner_points[0, 1, 1] - inner_points[0, 0, 1]
    points[0, 1:-1], points[-1, 1:-1] = inner_points[0], inner_points[-1]
    points[0, 1:-1, 0] -= step_x
    points[-1, 1:-1, 0] += step_x
    points[:, 0], points[:, -1] = points[:, 1], points[:, -2]
    points[:, 0, 1] -= step_y
    points[:, -1, 1] += step_y

    low = inner_points.reshape(-1, 3).min(axis=0)[:2]
    high = inner_points.reshape(-1, 3).max(axis=0)[:2]
    measured = shaded_at

    def within_grid(query: FloatArray) -> BoolArray:
        """The caller's answer, and lit wherever nothing was measured."""
        flat = np.asarray(query, dtype=np.float64)
        within = np.all((flat[:, :2] >= low) & (flat[:, :2] <= high), axis=1)
        answer = np.zeros(len(flat), dtype=bool)
        if within.any():
            answer[within] = measured(flat[within])
        return answer

    corners = (dark[:-1, :-1], dark[1:, :-1], dark[1:, 1:], dark[:-1, 1:])
    code = (
        corners[0].astype(np.int64)
        + 2 * corners[1].astype(np.int64)
        + 4 * corners[2].astype(np.int64)
        + 8 * corners[3].astype(np.int64)
    )
    cells = np.argwhere((code > 0) & (code < 15))
    if not len(cells):
        return []

    # One bisection for every distinct lattice edge the boundary crosses,
    # rather than one per segment end: an interior edge is shared by two cells
    # and its crossing is the same point for both.
    corner_offsets = ((0, 0), (1, 0), (1, 1), (0, 1))
    # Keyed by the *lattice edge*, named by its two corners, not by the cell
    # and a side. An interior edge belongs to two cells -- one's north is the
    # next one's south -- so keying on the cell gives the same physical
    # crossing two different vertices and the ring never closes.
    wanted: dict[tuple[int, int], int] = {}
    lower: list[FloatArray] = []
    upper: list[FloatArray] = []
    segments: list[tuple[int, int]] = []
    for cell in cells:
        i, j = int(cell[0]), int(cell[1])
        here = int(code[i, j])
        ends: list[int] = []
        for first, second in _CASES[here]:
            for edge in (first, second):
                start, finish = _EDGE_CORNERS[edge]
                sx, sy = corner_offsets[start]
                fx, fy = corner_offsets[finish]
                one, two = (i + sx) * ny + (j + sy), (i + fx) * ny + (j + fy)
                key = (one, two) if one < two else (two, one)
                if key not in wanted:
                    p, q = points[i + sx, j + sy], points[i + fx, j + fy]
                    shaded_first = bool(dark[i + sx, j + sy])
                    wanted[key] = len(lower)
                    lower.append(p if shaded_first else q)
                    upper.append(q if shaded_first else p)
                ends.append(wanted[key])
            # Reversed, so the walk keeps shade on its *left* and an outer
            # ring comes back anticlockwise -- the convention the cell tracer
            # already sets, and the only thing downstream uses to tell an
            # outline from a hole.
            segments.append((ends[-1], ends[-2]))

    placed = _crossings(np.array(lower), np.array(upper), within_grid, tolerance_m)
    return [_straighten(ring, tolerance_m) for ring in _chain(segments, placed[:, :2])]


def _chain(segments: list[tuple[int, int]], vertices: FloatArray) -> list[Ring]:
    """Follow the directed segments round into closed rings.

    Every segment runs with shade on its left, so following each one to the
    segment leaving where it arrived walks the boundary the same way round
    every time -- anticlockwise round shade, clockwise round a hole in it. The
    winding is what tells the two apart downstream, and deriving it afterwards
    by testing containment is what gets a patch with two holes wrong.

    A vertex normally has one segment arriving and one leaving. At the two
    ambiguous cells it has two of each, which is what keeps two shaded corners
    meeting at a point as two boundaries rather than one pinched to nothing;
    the departures are taken in turn, so both are walked.
    """
    leaving: dict[int, list[int]] = {}
    for start, end in segments:
        leaving.setdefault(start, []).append(end)

    rings: list[Ring] = []
    for first in list(leaving):
        while leaving.get(first):
            walk = [first]
            here = leaving[first].pop()
            guard = len(segments) + 1
            while here != first and guard > 0:
                walk.append(here)
                nxt = leaving.get(here)
                if not nxt:
                    # An open chain, which a closed boundary does not produce.
                    # It means the mask reached the edge of the lattice, where
                    # there is no cell to carry the boundary round. Dropped
                    # rather than closed across the gap, because closing it
                    # would claim shade over ground that was never sampled.
                    walk = []
                    break
                here = nxt.pop()
                guard -= 1
            if len(walk) >= 3:
                rings.append(tuple((float(x), float(y)) for x, y in vertices[walk]))
    return rings


def signed_area(ring: Ring) -> float:
    """Twice the signed area, positive anticlockwise. Shoelace, no more."""
    total = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=True):
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _furthest(chain: list[tuple[float, float]]) -> tuple[int, float]:
    """The vertex furthest from the chord between the chain's two ends."""
    first, last = chain[0], chain[-1]
    run_x, run_y = last[0] - first[0], last[1] - first[1]
    length = float(np.hypot(run_x, run_y))
    worst, at = 0.0, 0
    for index in range(1, len(chain) - 1):
        x, y = chain[index]
        away = (
            abs((x - first[0]) * run_y - (y - first[1]) * run_x) / length
            if length > 0.0
            else float(np.hypot(x - first[0], y - first[1]))
        )
        if away > worst:
            worst, at = away, index
    return at, worst


def _simplify(chain: list[tuple[float, float]], tolerance_m: float) -> list[tuple[float, float]]:
    """Douglas-Peucker: keep the ends, and whatever is too far from the chord.

    Recursive by the book. The greedy version this replaces walked the ring
    keeping any vertex far enough from the last one it had kept, which makes
    the answer depend on where the walk began: on a half-plane it found two
    corners where there were four, decided a polygon could not have two, and
    gave up -- leaving a hundred and thirty-eight vertices describing four.
    """
    if len(chain) < 3:
        return chain
    at, worst = _furthest(chain)
    if worst <= tolerance_m:
        return [chain[0], chain[-1]]
    left = _simplify(chain[: at + 1], tolerance_m)
    right = _simplify(chain[at:], tolerance_m)
    return left[:-1] + right


def _straighten(ring: Ring, tolerance_m: float) -> Ring:
    """Drop vertices that describe a line their neighbours already describe.

    Marching squares puts a vertex on every cell edge the boundary crosses, so
    a straight run of forty cells arrives as forty vertices. Archicad is the
    reason to care -- a contour with thousands of points in it is not a
    drawing anybody can open -- and so is the file.

    Nothing moves further than the tolerance it was traced to, so the outline
    stays on the edge it was found on. A curve keeps whatever it needs: a
    circle stays a circle, because every vertex on it really is off the chord
    through its neighbours.

    Split at the vertex furthest from the start before simplifying, because a
    closed ring has no ends and Douglas-Peucker needs two.
    """
    if len(ring) < 4:
        return ring
    points = list(ring)
    opposite, _ = _furthest([*points, points[0]])
    if opposite == 0:
        return ring
    first = _simplify(points[: opposite + 1], tolerance_m)
    second = _simplify([*points[opposite:], points[0]], tolerance_m)
    joined = first[:-1] + second[:-1]
    return tuple(joined) if len(joined) >= 3 else ring


def self_intersects(ring: Ring, limit: int = 4000) -> bool:
    """Whether any two non-adjacent edges of a ring cross.

    Archicad refuses a self-intersecting contour outright -- ``Failed to
    create new Hatch``, the same answer it gives a bow tie -- so this is what
    stands between a traced shadow and a fill that never appears. Every pair
    of edges, which is quadratic and fine at the sizes a traced outline
    reaches; a ring past ``limit`` is reported as crossing rather than
    checked, so the caller falls back instead of stalling on a shape that was
    never going to be drawn as one contour anyway.
    """
    count = len(ring)
    if count < 4:
        return False
    if count > limit:
        return True

    def side(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    for i in range(count):
        a, b = ring[i], ring[(i + 1) % count]
        for j in range(i + 2, count):
            if i == 0 and j == count - 1:
                continue
            c, d = ring[j], ring[(j + 1) % count]
            if (side(c, d, a) > 0) != (side(c, d, b) > 0) and (side(a, b, c) > 0) != (
                side(a, b, d) > 0
            ):
                return True
    return False


def _inside(point: tuple[float, float], ring: Ring) -> bool:
    """Whether a point lies within a ring, by crossing number."""
    x, y = point
    within = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=True):
        if (y1 > y) != (y2 > y):
            crossing = x1 + (y - y1) / (y2 - y1) * (x2 - x1)
            if crossing > x:
                within = not within
    return within


def group_regions(rings: list[Ring]) -> list[tuple[Ring, list[Ring]]]:
    """Pair each outline with the holes that fall inside it.

    Winding says which is which -- anticlockwise is an outline and clockwise a
    hole -- so this only has to decide *whose* hole each one is. The smallest
    containing outline wins, which is what makes a hole inside an island
    inside a patch belong to the island rather than the patch.
    """
    outers = [ring for ring in rings if signed_area(ring) > 0]
    holes = [ring for ring in rings if signed_area(ring) < 0]
    grouped: list[tuple[Ring, list[Ring]]] = [(outer, []) for outer in outers]
    for hole in holes:
        probe = hole[0]
        owners = [index for index, (outer, _) in enumerate(grouped) if _inside(probe, outer)]
        if not owners:
            continue
        smallest = min(owners, key=lambda index: abs(signed_area(grouped[index][0])))
        grouped[smallest][1].append(hole)
    return grouped


def bridged(outer: Ring, holes: list[Ring]) -> Ring | None:
    """One contour for an outline and its holes, seamed together.

    ``CreateHatches`` takes a single contour and no holes, and the two ways
    round that are both bad: fill the hole and claim shade over a sunlit
    courtyard, or tile the whole patch into rectangles and hand somebody a
    drawing they cannot edit. This takes the third -- run a seam from the
    outline to the hole, round the hole, and back along the same seam. The
    contour is single, closed, and encloses exactly the right area, because
    the seam is walked twice in opposite directions and contributes nothing.

    The seam joins the nearest pair of vertices, which for a shadow's own
    courtyards is a short hop across sunlit ground. It is not proof against a
    pathological shape -- a seam could in principle cross another hole -- so
    holes are taken in turn, largest first, and each is seamed to the contour
    as it stands rather than to the original outline.
    """
    contour = list(outer)
    for hole in sorted(holes, key=lambda ring: abs(signed_area(ring)), reverse=True):
        best = min(
            (
                (
                    (contour[i][0] - hole[j][0]) ** 2 + (contour[i][1] - hole[j][1]) ** 2,
                    i,
                    j,
                )
                for i in range(len(contour))
                for j in range(len(hole))
            ),
            key=lambda found: found[0],
        )
        _, at, from_ = best
        walk = [*hole[from_:], *hole[:from_]]
        # The return leg is offset by a hair, so the seam is a sliver rather
        # than a line walked twice. Archicad refuses the zero-width version --
        # measured on Crows Nest, 26 of 194 fills rejected with "Failed to
        # create new Hatch" -- because a contour that touches itself is not a
        # simple polygon, whatever its area comes to. A millimetre makes it
        # simple, and costs the seam's length times a millimetre, which on a
        # shadow is square centimetres.
        seam = (walk[0][0] - contour[at][0], walk[0][1] - contour[at][1])
        length = float(np.hypot(*seam))
        if length <= 0.0:
            continue
        # Which side the sliver opens on is not free: the wrong one sends the
        # return leg back across the outgoing one, and Archicad refuses a
        # contour that crosses itself. Rather than reason about the winding at
        # the junction, both are built and the one that does not cross is
        # kept -- the test is cheap at these sizes and it cannot be wrong.
        candidates = []
        for sign in (1.0, -1.0):
            aside = (
                -seam[1] / length * SEAM_WIDTH_M * sign,
                seam[0] / length * SEAM_WIDTH_M * sign,
            )
            candidates.append(
                [
                    *contour[: at + 1],
                    *walk,
                    (walk[0][0] + aside[0], walk[0][1] + aside[1]),
                    (contour[at][0] + aside[0], contour[at][1] + aside[1]),
                    *contour[at + 1 :],
                ]
            )
        clean = [option for option in candidates if not self_intersects(tuple(option))]
        if not clean:
            # Neither side works, which means the seam crosses something else
            # -- another hole, or a finger of the outline. Refused rather than
            # drawn, so the caller can fall back to a shape that is valid.
            return None
        contour = clean[0]
    return tuple(contour)
