"""Turning lit grid cells into the shapes a drawing is made of.

The invariant that matters is exactness, and it is the same for both shapes:
the rectangles must cover every lit cell, no dark cell, and no cell twice, and
the traced polygons must enclose exactly the same set. A patch drawn one cell
too generous is a compliance claim nobody made.
"""

from __future__ import annotations

import numpy as np
import pytest

from sun_study.core.patches import CellRegion, Rectangle, merge_lit_cells, trace_lit_regions

SPACING = 0.25


def grid(columns: int, rows: int, spacing: float = SPACING) -> np.ndarray:
    """Cell centres of a ``columns x rows`` lattice, row-major."""
    return np.array(
        [
            [column * spacing, row * spacing, 0.0]
            for row in range(rows)
            for column in range(columns)
        ],
        dtype=np.float64,
    )


def covered_cells(rectangles: tuple[Rectangle, ...], positions: np.ndarray) -> np.ndarray:
    """Which sample points fall inside any rectangle."""
    inside = np.zeros(len(positions), dtype=bool)
    for x_min, y_min, x_max, y_max in rectangles:
        inside |= (
            (positions[:, 0] > x_min)
            & (positions[:, 0] < x_max)
            & (positions[:, 1] > y_min)
            & (positions[:, 1] < y_max)
        )
    return inside


def test_a_fully_lit_grid_becomes_one_rectangle() -> None:
    """The whole point of merging: 48 cells must not be 48 fills."""
    positions = grid(8, 6)
    rectangles = merge_lit_cells(positions, np.ones(len(positions), dtype=bool), SPACING)

    assert len(rectangles) == 1
    assert rectangles[0] == pytest.approx((-0.125, -0.125, 1.875, 1.375))
    assert rectangles[0].area_m2 == pytest.approx(8 * 6 * SPACING**2), (
        "the merged area must equal the lit area, or the drawing overstates the sun"
    )


def test_nothing_lit_draws_nothing() -> None:
    positions = grid(4, 4)
    assert merge_lit_cells(positions, np.zeros(len(positions), dtype=bool), SPACING) == ()


def test_a_lit_cell_is_a_square_not_a_point() -> None:
    """Samples are cell centres, so one lit cell covers a whole cell."""
    positions = np.array([[1.0, 2.0, 0.0]])
    (rectangle,) = merge_lit_cells(positions, np.array([True]), SPACING)

    assert rectangle == pytest.approx((0.875, 1.875, 1.125, 2.125))
    assert rectangle.area_m2 == pytest.approx(SPACING**2)


def test_the_merge_covers_every_lit_cell_and_no_dark_one() -> None:
    """An L-shaped patch, which is what a real one looks like."""
    positions = grid(6, 6)
    lit = (positions[:, 0] < 0.4) | (positions[:, 1] < 0.4)

    rectangles = merge_lit_cells(positions, lit, SPACING)
    covered = covered_cells(rectangles, positions)

    assert (covered == lit).all(), "the tiling must be exact in both directions"
    assert sum(r.area_m2 for r in rectangles) == pytest.approx(int(lit.sum()) * SPACING**2)


def test_rows_merge_upward_only_while_both_ends_agree() -> None:
    """Two stacked rows of different width are two rectangles, not one.

    Merging them into their bounding box would colour cells the sun never
    reached, which is the failure this whole module has to avoid.
    """
    positions = grid(4, 2)
    lit = np.array([True, True, True, True, True, True, False, False])

    rectangles = merge_lit_cells(positions, lit, SPACING)

    assert len(rectangles) == 2
    assert (covered_cells(rectangles, positions) == lit).all()


def test_a_hole_in_the_patch_survives_the_merge() -> None:
    """CreateHatches cannot draw a hole, so the merge must not need one."""
    positions = grid(5, 5)
    lit = np.ones(len(positions), dtype=bool)
    middle = (np.isclose(positions[:, 0], 2 * SPACING)) & (np.isclose(positions[:, 1], 2 * SPACING))
    lit[middle] = False

    rectangles = merge_lit_cells(positions, lit, SPACING)

    assert (covered_cells(rectangles, positions) == lit).all(), (
        "the shaded cell must stay uncovered without any rectangle having a hole"
    )
    assert sum(r.area_m2 for r in rectangles) == pytest.approx(24 * SPACING**2)


def test_two_separate_patches_stay_separate() -> None:
    """Sun through two windows is two patches, and a bridge between them
    would be sunlight on a piece of floor that never saw any."""
    positions = grid(7, 1)
    lit = np.array([True, True, False, False, False, True, True])

    rectangles = merge_lit_cells(positions, lit, SPACING)

    assert len(rectangles) == 2
    assert (covered_cells(rectangles, positions) == lit).all()


def test_floating_point_drift_does_not_split_a_row() -> None:
    """Grid generators accumulate error; a row must still merge as one run."""
    positions = grid(6, 1)
    positions[:, 1] += np.linspace(-1e-9, 1e-9, 6)

    rectangles = merge_lit_cells(positions, np.ones(6, dtype=bool), SPACING)

    assert len(rectangles) == 1


def test_the_corners_are_ready_for_a_hatch() -> None:
    """Anticlockwise, four points, first not repeated."""
    corners = Rectangle((0.0, 0.0, 2.0, 1.0)).corners

    assert corners == ((0.0, 0.0), (2.0, 0.0), (2.0, 1.0), (0.0, 1.0))


def test_a_nonsense_spacing_is_refused() -> None:
    with pytest.raises(ValueError, match="spacing_m must be positive"):
        merge_lit_cells(grid(2, 2), np.ones(4, dtype=bool), 0.0)


def test_mismatched_lengths_are_refused() -> None:
    with pytest.raises(ValueError, match="positions but"):
        merge_lit_cells(grid(2, 2), np.ones(3, dtype=bool), SPACING)


def test_a_grid_offset_by_half_a_cell_still_merges_into_one_run() -> None:
    """Cell centres sit at ``corner + (i + 0.5) * spacing``, so dividing them
    by the spacing lands on half-integers -- where rint rounds to even and
    consecutive cells come out 0, 2, 2, 4. Runs broke apart and every
    rectangle shifted half a cell, on any grid whose corner happened to be a
    whole number.
    """
    positions = np.array([[0.25, 0.25, 0.0], [0.75, 0.25, 0.0], [1.25, 0.25, 0.0]])

    rectangles = merge_lit_cells(positions, np.ones(3, dtype=bool), 0.5)

    assert len(rectangles) == 1, "three cells side by side are one run"
    assert rectangles[0] == pytest.approx((0.0, 0.0, 1.5, 0.5)), (
        "and the rectangle covers exactly the cells, not something half a cell off"
    )


def test_the_lattice_does_not_move_with_the_patch() -> None:
    """Every instant must draw on the same lattice: an origin taken from the
    lit cells would redraw the same floor somewhere slightly different each
    time, and the series would shimmer."""
    positions = grid(6, 1)
    morning = merge_lit_cells(positions, np.array([1, 1, 0, 0, 0, 0], dtype=bool), SPACING)
    afternoon = merge_lit_cells(positions, np.array([0, 0, 0, 0, 1, 1], dtype=bool), SPACING)

    assert morning[0][0] == pytest.approx(-SPACING / 2)
    assert afternoon[0][0] == pytest.approx(4 * SPACING - SPACING / 2)


Ring = tuple[tuple[float, float], ...]


def ring_area(ring: Ring) -> float:
    """Signed shoelace area, positive anticlockwise.

    Written out here rather than imported, so the area test checks the traced
    geometry against arithmetic and not against the module's own opinion of it.
    """
    return 0.5 * sum(
        x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1], strict=True)
    )


def flat(ring: Ring) -> tuple[float, ...]:
    """One flat sequence of numbers, because ``pytest.approx`` will not nest."""
    return tuple(value for point in ring for value in point)


def region_area(region: CellRegion) -> float:
    """Outer ring less its holes -- which is a plain sum, the holes being clockwise."""
    return ring_area(region.outer) + sum(ring_area(hole) for hole in region.holes)


def encloses(region: CellRegion, x: float, y: float) -> bool:
    """Ray casting over the outer ring and the holes at once.

    A point inside a hole crosses both that ring and the outer one, so the
    parity puts it back outside without any containment bookkeeping.
    """
    crossings = 0
    for ring in (region.outer, *region.holes):
        for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1], strict=True):
            if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
                crossings += 1
    return crossings % 2 == 1


def enclosed_cells(regions: tuple[CellRegion, ...], positions: np.ndarray) -> np.ndarray:
    """Which sample points fall inside any traced region."""
    return np.array(
        [any(encloses(region, x, y) for region in regions) for x, y in positions[:, :2]],
        dtype=bool,
    )


def lit_at(
    positions: np.ndarray, cells: set[tuple[int, int]], spacing: float = SPACING
) -> np.ndarray:
    """Select cells by ``(column, row)`` index, which is how the shapes below read."""
    wanted = np.zeros(len(positions), dtype=bool)
    for column, row in cells:
        wanted |= np.isclose(positions[:, 0], column * spacing) & np.isclose(
            positions[:, 1], row * spacing
        )
    return wanted


def test_a_single_lit_cell_traces_to_one_square() -> None:
    """Samples are cell centres, so one lit cell is a square and not a point."""
    positions = np.array([[1.0, 2.0, 0.0]])

    (region,) = trace_lit_regions(positions, np.array([True]), SPACING)

    assert flat(region.outer) == pytest.approx(
        flat(((0.875, 1.875), (1.125, 1.875), (1.125, 2.125), (0.875, 2.125)))
    ), "the square must be centred on the sample, half a cell out on every side"
    assert region.holes == ()


def test_nothing_lit_traces_nothing() -> None:
    positions = grid(4, 4)
    assert trace_lit_regions(positions, np.zeros(len(positions), dtype=bool), SPACING) == ()


def test_a_solid_block_keeps_only_its_corners() -> None:
    """400 lit cells are four points, not 1600.

    Collinear vertices along a straight run are the whole reason this can go to
    Archicad: a contour with a point per cell edge is thousands of points long
    and unusable in the drawing.
    """
    positions = grid(20, 20)

    (region,) = trace_lit_regions(positions, np.ones(len(positions), dtype=bool), SPACING)

    assert len(region.outer) == 4, f"a rectangle is four points, got {len(region.outer)}"
    assert flat(region.outer) == pytest.approx(
        flat(((-0.125, -0.125), (4.875, -0.125), (4.875, 4.875), (-0.125, 4.875)))
    )


def test_an_l_shaped_patch_is_one_polygon_with_six_corners() -> None:
    """An L is what a real patch looks like, and it has six corners.

    The rectangle merge needs two fills for it; the point of tracing is that it
    needs one, with the reflex corner drawn rather than implied by a seam.
    """
    positions = grid(6, 6)
    lit = (positions[:, 0] < 0.4) | (positions[:, 1] < 0.4)

    (region,) = trace_lit_regions(positions, lit, SPACING)

    assert len(region.outer) == 6, f"an L has six corners, got {len(region.outer)}"
    assert (enclosed_cells((region,), positions) == lit).all()


def test_a_ring_of_cells_keeps_its_hole() -> None:
    """The dark cell in the middle must come back as a hole, not be filled in.

    This is exactly what the rectangle merge cannot express, and colouring it
    would claim sun on a piece of floor that was in shadow.
    """
    positions = grid(5, 5)
    lit = ~lit_at(positions, {(2, 2)})

    (region,) = trace_lit_regions(positions, lit, SPACING)

    assert len(region.holes) == 1, "the shaded cell is a hole in the patch"
    assert flat(region.holes[0]) == pytest.approx(
        flat(((0.375, 0.375), (0.375, 0.625), (0.625, 0.625), (0.625, 0.375)))
    )
    assert region_area(region) == pytest.approx(24 * SPACING**2)


def test_the_outer_ring_is_anticlockwise_and_the_holes_clockwise() -> None:
    """The winding is the only thing that says which ring is which.

    A consumer that has to guess by testing containment gets it wrong the first
    time a patch has two holes in it, so the convention is fixed and asserted.
    """
    positions = grid(5, 5)
    lit = ~lit_at(positions, {(1, 1), (3, 3)})

    (region,) = trace_lit_regions(positions, lit, SPACING)

    assert ring_area(region.outer) > 0, "the outer ring runs anticlockwise"
    assert len(region.holes) == 2, "two dark cells apart from each other are two holes"
    for hole in region.holes:
        assert ring_area(hole) < 0, "every hole runs clockwise"


def test_two_disjoint_blobs_stay_two_regions() -> None:
    """Sun through two windows is two patches, and one polygon spanning both
    would colour the dark floor between them."""
    positions = grid(7, 1)
    lit = np.array([True, True, False, False, False, True, True])

    regions = trace_lit_regions(positions, lit, SPACING)

    assert len(regions) == 2
    assert (enclosed_cells(regions, positions) == lit).all()


def test_cells_meeting_only_at_a_corner_are_two_regions() -> None:
    """Four-connectivity, stated in the docstring and pinned here.

    Joining them would produce a polygon that pinches to zero width at the
    corner -- a single fill Archicad renders as one patch, measured and
    labelled as one patch, spanning floor the sun never reached.
    """
    positions = grid(2, 2)
    lit = lit_at(positions, {(0, 0), (1, 1)})

    regions = trace_lit_regions(positions, lit, SPACING)

    assert len(regions) == 2, "a corner touch is not a connection"
    assert all(len(region.outer) == 4 for region in regions), "each is a plain square"


def test_two_holes_meeting_at_a_corner_are_one_hole() -> None:
    """The other half of choosing four-connectivity for the lit cells.

    A boundary cannot separate the lit side at a corner and the dark side there
    as well. The dark side is the one allowed to join, so two diagonal dark
    cells are a single eight-point hole that pinches -- and the area still has
    to come out at two cells short.
    """
    positions = grid(4, 4)
    lit = ~lit_at(positions, {(1, 1), (2, 2)})

    (region,) = trace_lit_regions(positions, lit, SPACING)

    assert len(region.holes) == 1, "diagonally touching dark cells are one hole"
    assert ring_area(region.holes[0]) == pytest.approx(-2 * SPACING**2)
    assert region_area(region) == pytest.approx(14 * SPACING**2)


def test_the_enclosed_area_is_exactly_the_lit_area() -> None:
    """The invariant the drawing rests on, measured by shoelace.

    A patch drawn one cell too generous, or one hole too few, is a compliance
    claim nobody made. Checked on a shape with both a reflex corner and a hole,
    because those are the two ways the trace can go wrong.
    """
    positions = grid(7, 5)
    lit = (positions[:, 0] < 1.1) | (positions[:, 1] < 0.6)
    lit &= ~lit_at(positions, {(1, 1)})

    regions = trace_lit_regions(positions, lit, SPACING)
    traced = sum(region_area(region) for region in regions)

    assert traced == pytest.approx(int(lit.sum()) * SPACING**2), (
        "the shoelace area, holes subtracted, must equal the lit cell area exactly"
    )


def test_no_dark_cell_is_enclosed_and_no_lit_cell_left_out() -> None:
    """Area alone would let the trace swap a lit cell for a dark one of equal
    size, so every sample is tested against the polygon itself."""
    positions = grid(9, 6)
    lit = ((positions[:, 0] + positions[:, 1]) % 0.75) < 0.5
    lit |= lit_at(positions, {(4, 4)})

    regions = trace_lit_regions(positions, lit, SPACING)

    assert (enclosed_cells(regions, positions) == lit).all(), (
        "the polygons must enclose exactly the lit cells, in both directions"
    )


def test_a_non_square_grid_traces_to_its_own_extent() -> None:
    """Columns and rows are not interchangeable; a transposed extent would put
    the patch outside the room."""
    positions = grid(7, 3)

    (region,) = trace_lit_regions(positions, np.ones(len(positions), dtype=bool), SPACING)

    assert flat(region.outer) == pytest.approx(
        flat(((-0.125, -0.125), (1.625, -0.125), (1.625, 0.625), (-0.125, 0.625)))
    )
    assert region_area(region) == pytest.approx(7 * 3 * SPACING**2)


def test_the_regions_come_back_in_the_same_order_however_the_samples_arrive() -> None:
    """Redrawing the same instant must produce the same file.

    The trace walks sets and dictionaries, so without an explicit ordering the
    rings and the regions would come out in whatever order the samples happened
    to hash into -- and every re-run would churn the sheet.
    """
    positions = grid(6, 6)
    lit = lit_at(positions, {(0, 0), (1, 0), (0, 1), (3, 3), (4, 3), (4, 4), (2, 5)})
    order = np.array([31, 5, 18, 0, 22, 9, 35, 12, 27, 3, 14, 20, 8, 33, 1, 25, 17, 6])
    order = np.concatenate([order, np.setdiff1d(np.arange(36), order)])

    assert trace_lit_regions(positions[order], lit[order], SPACING) == trace_lit_regions(
        positions, lit, SPACING
    ), "the output must not depend on the order the samples arrive in"


def test_the_traced_lattice_does_not_move_with_the_patch() -> None:
    """The origin comes from the whole grid, not from the lit cells.

    An origin taken from the patch would redraw the same floor somewhere
    slightly different at every instant and the series would shimmer.
    """
    positions = grid(6, 1)

    (morning,) = trace_lit_regions(positions, np.array([1, 1, 0, 0, 0, 0], dtype=bool), SPACING)
    (afternoon,) = trace_lit_regions(positions, np.array([0, 0, 0, 0, 1, 1], dtype=bool), SPACING)

    assert morning.outer[0][0] == pytest.approx(-SPACING / 2)
    assert afternoon.outer[0][0] == pytest.approx(4 * SPACING - SPACING / 2)


def test_a_nonsense_spacing_is_refused_by_the_trace() -> None:
    with pytest.raises(ValueError, match="spacing_m must be positive"):
        trace_lit_regions(grid(2, 2), np.ones(4, dtype=bool), 0.0)


def test_mismatched_lengths_are_refused_by_the_trace() -> None:
    with pytest.raises(ValueError, match="positions but"):
        trace_lit_regions(grid(2, 2), np.ones(3, dtype=bool), SPACING)
