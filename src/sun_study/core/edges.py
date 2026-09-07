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

__all__ = ["DEFAULT_TOLERANCE_M", "refined_rings"]

#: How close a traced vertex is brought to the true edge. A millimetre is far
#: below what any sheet resolves -- 1:500 makes it two thousandths of a
#: millimetre on paper -- and each halving is one more ray, so ten of them take
#: a one metre cell to under a millimetre. Cheap enough not to economise.
DEFAULT_TOLERANCE_M = 0.001

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
    nx, ny = shape
    if nx < 2 or ny < 2:
        return []
    dark = np.asarray(shaded, dtype=bool).reshape(nx, ny)
    points = np.asarray(positions, dtype=np.float64).reshape(nx, ny, 3)

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

    placed = _crossings(np.array(lower), np.array(upper), shaded_at, tolerance_m)
    return _chain(segments, placed[:, :2])


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
