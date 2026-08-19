"""Turning a grid of lit sample points into a few drawable rectangles.

A sun patch on a floor is what a study drawing actually shows: not "this
apartment gets two hours" but "at 09:15 the sun reached this far into the
room". The engine already produces it -- ``sunlit_matrix`` says, for every
sample point and every instant, whether the sun arrived -- but a sample point
is a dot, and a drawing needs an outline.

Rectangles, not a contour
-------------------------
The obvious approach is to trace the boundary of the lit set: marching
squares, an alpha shape, a polygon union. All three want a computational
geometry library the project has ruled out, all three produce polygons with
holes, and ``CreateHatches`` takes a single contour and no holes -- so the
polygon would then have to be cut up again.

The grid itself is already a set of squares. Merging the lit ones into runs,
then merging identical runs across rows, gives a handful of rectangles that
tile the patch exactly. No dependency, no holes to lose, and the stepped edge
this produces is not an approximation of the patch -- it *is* the analysis
resolution, drawn honestly. The office's own reference drawings, produced by a
different tool entirely, have the same stepped edge for the same reason.

The merge is exact
------------------
Every lit cell ends up inside exactly one rectangle and no dark cell is ever
covered, so the drawn area equals the lit area. A run-length merge along x
first, then a vertical merge of runs that share both ends, takes a 6 x 5 m
room at 250 mm from 480 cells to a handful of rectangles, which is the
difference between a drawing Archicad can hold and one it cannot.

One fill per region, where the consumer can carry holes
-------------------------------------------------------
A tiling is still a tiling: the rectangles a single instant produces arrive in
Archicad as hundreds of separate fills, each with its own outline, pen and
background, and the seams between them show on the sheet. Anything that wants
to treat a patch as one object -- outline it, label it, count it -- has to put
the tiling back together first. ``trace_lit_regions`` answers the same question
with one polygon per connected patch.

It needs no library either, because the boundary is already known: a cell edge
is on the boundary exactly when the cell across it is dark. Walking those edges
with the lit side kept on the left yields anticlockwise outer rings and
clockwise holes for nothing, and the edge that comes out is the *same* stepped
edge the rectangles have -- the sampling resolution drawn honestly, not a
contour fitted through the samples.

This does not replace ``merge_lit_cells``. ``CreateHatches`` takes one contour
and no holes, so a route that ends there still wants the tiling; a route that
can carry holes, or that only needs the outline, wants the polygon. Both cover
exactly the lit cells and nothing else, which is the only property the drawing
is allowed to depend on.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]

# Integer lattice coordinates. A cell is addressed by its column and row, a
# vertex by the cell corner it sits on, so cell ``(c, r)`` has corners ``(c, r)``
# through ``(c + 1, r + 1)``. Staying on exact integers until the last step is
# what makes "is this edge on the boundary" a dictionary lookup rather than a
# tolerance, and what makes the collinear test below exact.
_Cell = tuple[int, int]
_Vertex = tuple[int, int]
_Step = tuple[int, int]

__all__ = ["CellRegion", "Rectangle", "merge_lit_cells", "trace_lit_regions"]


class Rectangle(tuple[float, float, float, float]):
    """An axis-aligned rectangle as ``(x_min, y_min, x_max, y_max)``, metres."""

    __slots__ = ()

    @property
    def corners(self) -> tuple[tuple[float, float], ...]:
        """The four corners anticlockwise, ready for a hatch contour.

        Not closed -- ``CreateHatches`` documents that the first point must not
        be repeated at the end.
        """
        x_min, y_min, x_max, y_max = self
        return ((x_min, y_min), (x_max, y_min), (x_max, y_max), (x_min, y_max))

    @property
    def area_m2(self) -> float:
        x_min, y_min, x_max, y_max = self
        return (x_max - x_min) * (y_max - y_min)


def merge_lit_cells(
    positions: FloatArray, lit: BoolArray, spacing_m: float
) -> tuple[Rectangle, ...]:
    """Merge the lit cells of a regular grid into as few rectangles as tile it.

    ``positions`` are cell *centres*, ``(n, 3)`` or ``(n, 2)``; only x and y are
    read, because a patch is drawn in plan. ``lit`` is the parallel boolean.

    Cells are snapped to a lattice of ``spacing_m`` before merging, so floating
    point drift in the grid generator cannot split one row into two. A grid
    that is not actually regular still produces correct rectangles, just more
    of them.
    """
    if spacing_m <= 0:
        raise ValueError(f"spacing_m must be positive, got {spacing_m}")
    if len(positions) != len(lit):
        raise ValueError(f"{len(positions)} positions but {len(lit)} flags")

    grid = np.asarray(positions, dtype=np.float64)
    chosen = grid[np.asarray(lit, dtype=bool)]
    if not len(chosen):
        return ()

    half = spacing_m / 2.0
    # Indexed from the grid's own corner, not from the world origin. A grid
    # generator centres its cells wherever the surface starts, so cell centres
    # are at ``corner + (i + 0.5) * spacing`` and dividing those by the spacing
    # lands on half-integers -- where rint rounds to even and consecutive cells
    # come out as 0, 2, 2, 4 instead of 0, 1, 2, 3. Runs then break apart and
    # every rectangle shifts by half a cell.
    #
    # Taken from the whole grid rather than from the lit cells, so the lattice
    # is the same at every instant. An origin that moved with the patch would
    # redraw the same floor in a different place each time.
    origin = np.array([grid[:, 0].min(), grid[:, 1].min()], dtype=np.float64)
    # Snapped to integers so equality is exact: two cells are in the same row
    # when their row index matches, not when their y values are close enough.
    columns = np.rint((chosen[:, 0] - origin[0]) / spacing_m).astype(np.int64)
    rows = np.rint((chosen[:, 1] - origin[1]) / spacing_m).astype(np.int64)

    runs_by_row: dict[int, list[tuple[int, int]]] = {}
    for row in np.unique(rows):
        in_row = np.sort(columns[rows == row])
        runs_by_row[int(row)] = list(_runs(in_row))

    merged: list[Rectangle] = []
    # (first column, last column) -> the row the open rectangle started at, and
    # the row it currently reaches. A run continues upward only while both of
    # its ends match, which is what keeps the tiling exact.
    open_runs: dict[tuple[int, int], tuple[int, int]] = {}
    for row in sorted(runs_by_row):
        current = set(runs_by_row[row])
        for run, (start_row, last_row) in list(open_runs.items()):
            if run in current and last_row == row - 1:
                open_runs[run] = (start_row, row)
                current.discard(run)
            else:
                merged.append(_rectangle(run, start_row, last_row, spacing_m, half, origin))
                del open_runs[run]
        for run in current:
            open_runs[run] = (row, row)

    for run, (start_row, last_row) in open_runs.items():
        merged.append(_rectangle(run, start_row, last_row, spacing_m, half, origin))

    return tuple(sorted(merged))


def _runs(sorted_columns: npt.NDArray[np.int64]) -> Iterator[tuple[int, int]]:
    """Consecutive column indices, as inclusive ``(first, last)`` pairs."""
    start = previous = int(sorted_columns[0])
    for value in sorted_columns[1:]:
        column = int(value)
        if column == previous:
            continue
        if column == previous + 1:
            previous = column
            continue
        yield start, previous
        start = previous = column
    yield start, previous


def _rectangle(
    run: tuple[int, int],
    start_row: int,
    last_row: int,
    spacing_m: float,
    half: float,
    origin: FloatArray,
) -> Rectangle:
    """One merged block of cells, grown by half a cell to its real extent.

    The samples are cell centres, so a single lit cell is a square of side
    ``spacing_m`` centred on its sample, not a point.
    """
    first_column, last_column = run
    return Rectangle(
        (
            float(origin[0]) + first_column * spacing_m - half,
            float(origin[1]) + start_row * spacing_m - half,
            float(origin[0]) + last_column * spacing_m + half,
            float(origin[1]) + last_row * spacing_m + half,
        )
    )


@dataclass(frozen=True)
class CellRegion:
    """One connected sun patch as a single outline, with its holes.

    Rings are *not* closed: the first point is not repeated at the end, which
    is what ``CreateHatches`` documents and what ``Rectangle.corners`` already
    does, so the two shapes can be handed to the same drawing code.

    ``outer`` runs anticlockwise and every ring in ``holes`` runs clockwise.
    The winding is the only thing that says which ring is which, and a consumer
    left to re-derive it by testing containment gets it wrong on the first
    patch with two holes in it, so it is fixed here and tested.
    """

    outer: tuple[tuple[float, float], ...]
    holes: tuple[tuple[tuple[float, float], ...], ...]


def trace_lit_regions(
    positions: FloatArray, lit: BoolArray, spacing_m: float
) -> tuple[CellRegion, ...]:
    """Trace the union of the lit cells as one polygon per connected patch.

    ``positions`` are cell *centres*, ``(n, 3)`` or ``(n, 2)``; only x and y are
    read, because a patch is drawn in plan. A cell is the square of side
    ``spacing_m`` centred on its sample, not a point, exactly as in
    ``merge_lit_cells``. Nothing lit is left out and nothing dark is taken in:
    the enclosed area, holes subtracted, is the lit cell count times the cell
    area, and a drawing is a claim about sunlight that has to hold exactly.

    The lattice origin comes from the *whole* grid rather than from the lit
    subset, for the reason ``merge_lit_cells`` sets out at length: an origin
    that moved with the patch would put the same floor in a slightly different
    place at every instant and the series would shimmer. Snapping to integers
    first also makes adjacency exact -- two cells touch when their indices
    differ by one, not when their coordinates are close enough -- so drift in
    the grid generator cannot open a seam down the middle of a patch.

    **Cells that meet only at a corner are two regions**, not one. Sun through
    two windows is two patches, and a polygon that joined them would pinch to
    zero width at the corner and then be read, measured and labelled as a
    single patch spanning floor the sun never reached. The other half of that
    choice follows from it: dark cells that meet only at a corner are *one*
    hole, because a boundary cannot separate the lit side at a corner and the
    dark side there as well, and the dark side is the one that may leak out.

    Straight runs are collapsed to their end points, so a row of twenty cells
    contributes two vertices and not twenty-one. Archicad is the reason: a
    contour with four thousand points in it is not a drawing anyone can open.

    Regions come back sorted by their leftmost-then-lowest vertex, and each
    ring starts there too, so the same lit set always produces identical output
    and a redraw does not churn the sheet.
    """
    if spacing_m <= 0:
        raise ValueError(f"spacing_m must be positive, got {spacing_m}")
    if len(positions) != len(lit):
        raise ValueError(f"{len(positions)} positions but {len(lit)} flags")

    grid = np.asarray(positions, dtype=np.float64)
    chosen = grid[np.asarray(lit, dtype=bool)]
    if not len(chosen):
        return ()

    half = spacing_m / 2.0
    origin = np.array([grid[:, 0].min(), grid[:, 1].min()], dtype=np.float64)
    columns = np.rint((chosen[:, 0] - origin[0]) / spacing_m).astype(np.int64)
    rows = np.rint((chosen[:, 1] - origin[1]) / spacing_m).astype(np.int64)
    cells: set[_Cell] = {(int(column), int(row)) for column, row in zip(columns, rows, strict=True)}

    def place(ring: list[_Vertex]) -> tuple[tuple[float, float], ...]:
        """Lattice vertices to metres, half a cell out from the lattice of centres."""
        return tuple(
            (
                float(origin[0]) + vertex[0] * spacing_m - half,
                float(origin[1]) + vertex[1] * spacing_m - half,
            )
            for vertex in _from_lowest_left(ring)
        )

    regions: list[CellRegion] = []
    for component in _components(cells):
        rings = [_drop_collinear(ring) for ring in _boundary_rings(component)]
        # A four-connected component has exactly one anticlockwise ring -- its
        # outside -- and every other ring it produces encloses dark cells.
        outer = next(ring for ring in rings if _twice_signed_area(ring) > 0)
        holes = [ring for ring in rings if _twice_signed_area(ring) < 0]
        regions.append(CellRegion(place(outer), tuple(sorted(place(hole) for hole in holes))))

    return tuple(sorted(regions, key=lambda region: region.outer))


def _components(cells: set[_Cell]) -> Iterator[frozenset[_Cell]]:
    """The four-connected groups of lit cells, each of which becomes one patch.

    Four rather than eight, for the reason ``trace_lit_regions`` gives: two
    cells sharing only a corner come back as two components and are drawn as
    two polygons.
    """
    unvisited = set(cells)
    while unvisited:
        seed = min(unvisited)
        unvisited.discard(seed)
        component = {seed}
        queue = deque([seed])
        while queue:
            column, row = queue.popleft()
            for neighbour in (
                (column + 1, row),
                (column - 1, row),
                (column, row + 1),
                (column, row - 1),
            ):
                if neighbour in unvisited:
                    unvisited.discard(neighbour)
                    component.add(neighbour)
                    queue.append(neighbour)
        yield frozenset(component)


def _boundary_rings(component: frozenset[_Cell]) -> Iterator[list[_Vertex]]:
    """Walk the component's cell edges into closed rings, lit side on the left.

    Every edge is emitted in the direction that keeps the component on its
    left, so the walk comes out anticlockwise around the outside and clockwise
    around anything enclosed. That is where the winding convention comes from:
    it falls out of the geometry rather than being imposed afterwards, so the
    two cannot disagree.
    """
    outgoing: dict[_Vertex, dict[_Step, _Vertex]] = {}
    for column, row in component:
        if (column, row - 1) not in component:
            _add_edge(outgoing, (column, row), (column + 1, row))
        if (column + 1, row) not in component:
            _add_edge(outgoing, (column + 1, row), (column + 1, row + 1))
        if (column, row + 1) not in component:
            _add_edge(outgoing, (column + 1, row + 1), (column, row + 1))
        if (column - 1, row) not in component:
            _add_edge(outgoing, (column, row + 1), (column, row))

    while outgoing:
        start = min(outgoing)
        ring: list[_Vertex] = []
        vertex, step = start, min(outgoing[start])
        while True:
            options = outgoing.get(vertex)
            if options is None:
                break
            # A vertex offers two ways on exactly when the two lit cells meeting
            # there are diagonal, and turning left is what keeps them apart:
            # take the other branch and two patches are welded together through
            # a point of zero width. Everywhere else there is one edge to take
            # and the preference is inert.
            turn = next((candidate for candidate in _turns(step) if candidate in options), None)
            if turn is None:
                break
            ring.append(vertex)
            head = options.pop(turn)
            if not options:
                del outgoing[vertex]
            vertex, step = head, turn
        yield ring


def _add_edge(outgoing: dict[_Vertex, dict[_Step, _Vertex]], tail: _Vertex, head: _Vertex) -> None:
    outgoing.setdefault(tail, {})[_step(tail, head)] = head


def _turns(step: _Step) -> tuple[_Step, _Step, _Step]:
    """Left, straight, right, in the order the walk prefers them."""
    across, along = step
    return (-along, across), step, (along, -across)


def _drop_collinear(ring: list[_Vertex]) -> list[_Vertex]:
    """Keep only the corners, so a straight run costs two points and not twenty.

    Exact rather than tolerant: every edge is one cell side long, so two edges
    are collinear precisely when their integer steps are equal. What this
    prevents is a contour with a vertex per sample -- a 20 x 20 lit block would
    otherwise reach Archicad as an eighty-point square, and a real patch as a
    four-thousand-point one.
    """
    return [
        vertex
        for index, vertex in enumerate(ring)
        if _step(ring[index - 1], vertex) != _step(vertex, ring[(index + 1) % len(ring)])
    ]


def _step(tail: _Vertex, head: _Vertex) -> _Step:
    return (head[0] - tail[0], head[1] - tail[1])


def _twice_signed_area(ring: list[_Vertex]) -> int:
    """Shoelace in whole cells, doubled, so it stays an integer.

    Only the sign is read -- positive is the outside of a patch, negative is a
    hole -- and in integers there is no near-zero case to get wrong.
    """
    return sum(
        tail[0] * head[1] - head[0] * tail[1]
        for tail, head in zip(ring, ring[1:] + ring[:1], strict=True)
    )


def _from_lowest_left(ring: list[_Vertex]) -> list[_Vertex]:
    """Rotate the ring to start at its leftmost, then lowest, vertex.

    Which edge the walk happened to start on is an accident of set iteration
    order; a redraw that emits the same polygon rotated is a diff nobody can
    read. Where that vertex occurs twice -- the pinch where two holes meet at a
    corner -- the smaller rotation wins, so the tie breaks the same way every
    time.
    """
    lowest = min(ring)
    return min(ring[index:] + ring[:index] for index, vertex in enumerate(ring) if vertex == lowest)
