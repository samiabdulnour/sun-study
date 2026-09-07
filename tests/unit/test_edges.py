"""Boundaries traced where the shadow ends rather than where a cell does.

Every case here is a shape whose area is known on paper, because the point of
the module is accuracy and a regression baseline would only say the answer had
not changed. A circle is the shape a staircase does worst on and the one that
shows the difference plainly: at one metre cells, counting them is over a per
cent out, while a traced outline is a tenth of that and its vertices sit
microns from the true curve.
"""

from __future__ import annotations

import numpy as np
import pytest

from sun_study.core.edges import refined_rings, signed_area

LATTICE = 41


def lattice() -> np.ndarray:
    xs, ys = np.meshgrid(
        np.arange(LATTICE, dtype=float), np.arange(LATTICE, dtype=float), indexing="ij"
    )
    return np.column_stack([xs.ravel(), ys.ravel(), np.zeros(xs.size)])


def disc(centre: tuple[float, float], radius: float):  # type: ignore[no-untyped-def]
    middle = np.array(centre, dtype=float)

    def shaded(points: np.ndarray) -> np.ndarray:
        return np.linalg.norm(np.asarray(points)[:, :2] - middle, axis=1) <= radius

    return shaded


def traced(shaded, tolerance_m: float = 1e-4):  # type: ignore[no-untyped-def]
    points = lattice()
    return refined_rings(
        points, shaded(points), (LATTICE, LATTICE), shaded_at=shaded, tolerance_m=tolerance_m
    )


def test_a_traced_vertex_sits_on_the_true_edge_not_on_the_lattice() -> None:
    """The whole claim. Cells are a metre apart; every vertex should be within
    a fraction of a millimetre of the circle, at whatever angle it meets."""
    rings = traced(disc((20.0, 20.0), 12.3))

    assert len(rings) == 1
    worst = max(abs(float(np.hypot(x - 20.0, y - 20.0)) - 12.3) for ring in rings for x, y in ring)
    assert worst < 0.001, f"{worst * 1000:.3f} mm from the true circle"


def test_the_traced_area_beats_counting_the_cells() -> None:
    """Not merely prettier. A cell either counts whole or not at all, so a
    curve cutting through cells is over a per cent out at one metre; the
    outline follows where it actually runs."""
    shaded = disc((20.0, 20.0), 12.3)
    exact = float(np.pi * 12.3**2)
    cells = float(shaded(lattice()).sum())

    outline = sum(signed_area(ring) for ring in traced(shaded))

    assert abs(outline - exact) < abs(cells - exact) / 5.0


def test_a_hole_comes_back_clockwise_inside_an_anticlockwise_outline() -> None:
    """Winding is the only thing that says which ring is which, and a consumer
    left to re-derive it by containment gets it wrong on the first patch with
    two holes. An annulus is the smallest case that pins it."""
    middle = np.array([20.0, 20.0])

    def annulus(points: np.ndarray) -> np.ndarray:
        radius = np.linalg.norm(np.asarray(points)[:, :2] - middle, axis=1)
        return (radius <= 14.0) & (radius >= 6.0)

    rings = traced(annulus)
    areas = sorted(signed_area(ring) for ring in rings)

    assert len(rings) == 2
    assert areas[0] < 0 < areas[1], "the hole runs the other way round"
    assert sum(areas) == pytest.approx(float(np.pi * (14.0**2 - 6.0**2)), rel=0.002)


def test_two_patches_meeting_nowhere_are_two_rings() -> None:
    left, right = disc((11.0, 20.0), 6.0), disc((29.0, 20.0), 6.0)

    def both(points: np.ndarray) -> np.ndarray:
        return left(points) | right(points)

    rings = traced(both)

    assert len(rings) == 2
    assert all(signed_area(ring) > 0 for ring in rings)


def test_a_coarser_tolerance_is_answered_with_fewer_rays() -> None:
    """Each halving is one more ray over every crossing, so the tolerance is
    what a caller trades against time -- and it must actually be honoured."""
    calls = {"n": 0}
    shaded = disc((20.0, 20.0), 12.3)

    def counted(points: np.ndarray) -> np.ndarray:
        calls["n"] += 1
        return shaded(points)

    points = lattice()
    mask = shaded(points)
    refined_rings(points, mask, (LATTICE, LATTICE), shaded_at=counted, tolerance_m=0.5)
    coarse = calls["n"]
    calls["n"] = 0
    refined_rings(points, mask, (LATTICE, LATTICE), shaded_at=counted, tolerance_m=0.001)

    assert coarse < calls["n"]


def test_nothing_shaded_is_no_ring_at_all() -> None:
    def lit(points: np.ndarray) -> np.ndarray:
        return np.zeros(len(points), dtype=bool)

    assert traced(lit) == []


def test_a_straight_run_is_two_vertices_and_not_forty() -> None:
    """Marching squares puts a vertex on every cell edge it crosses, so a
    straight boundary arrives as one vertex per cell describing a line that
    needs two. Archicad is the reason to care: a contour with thousands of
    points is not a drawing anybody can open."""

    def half(points: np.ndarray) -> np.ndarray:
        return np.asarray(points)[:, 0] <= 20.4

    rings = traced(half)

    assert len(rings) == 1
    assert len(rings[0]) <= 8, f"{len(rings[0])} vertices for a rectangle"


def test_a_diagonal_edge_is_where_refining_earns_its_keep() -> None:
    """The case every real shadow is: an edge at whatever angle the sun makes,
    which a lattice can only approximate as a staircase. Traced, that edge is
    a straight line and needs two vertices to say so -- fewer than the
    staircase, not more -- and it encloses the area the line really cuts."""

    def cut(points: np.ndarray) -> np.ndarray:
        flat = np.asarray(points)
        return flat[:, 1] <= 0.6 * flat[:, 0] + 3.7

    rings = traced(cut)
    assert len(rings) == 1
    assert len(rings[0]) <= 6, f"{len(rings[0])} vertices for a clipped half-plane"

    # Under the line, over the sampled square: the integral of 0.6x + 3.7.
    span = float(LATTICE - 1)
    exact = 0.6 * span**2 / 2.0 + 3.7 * span
    assert signed_area(rings[0]) == pytest.approx(exact, rel=0.002)


def test_a_seamed_hole_is_a_simple_polygon() -> None:
    """CreateHatches takes one contour and no holes, so a hole is seamed out
    to the outline and back. Walking the same line twice gives exactly the
    right area and a contour that touches itself, which Archicad refuses --
    measured, 26 of 194 fills rejected. The return leg is offset by a
    millimetre so the seam is a sliver, which is simple, and costs the seam's
    length times a millimetre."""
    from sun_study.core.edges import bridged, group_regions

    middle = np.array([20.0, 20.0])

    def annulus(points: np.ndarray) -> np.ndarray:
        radius = np.linalg.norm(np.asarray(points)[:, :2] - middle, axis=1)
        return (radius <= 14.0) & (radius >= 6.0)

    outer, holes = group_regions(traced(annulus))[0]
    assert holes, "an annulus has one"

    seamed = bridged(outer, holes)

    assert len(seamed) == len(set(seamed)), "no vertex is visited twice"
    assert signed_area(seamed) == pytest.approx(float(np.pi * (14.0**2 - 6.0**2)), rel=0.002)


def test_a_patch_with_no_holes_is_left_exactly_as_traced() -> None:
    """Seaming is for holes. A solid patch must not acquire a sliver."""
    from sun_study.core.edges import bridged, group_regions

    outer, holes = group_regions(traced(disc((20.0, 20.0), 12.3)))[0]

    assert holes == []
    assert bridged(outer, holes) == outer


def test_a_seam_that_would_cross_itself_is_refused_not_drawn() -> None:
    """Archicad answers a self-intersecting contour the way it answers a bow
    tie -- 'Failed to create new Hatch' -- so a seam that crosses has to be
    caught here. Which side the sliver opens on decides it, and the wrong side
    sends the return leg back over the outgoing one."""
    from sun_study.core.edges import bridged, self_intersects

    turn = np.linspace(0, 2 * np.pi, 40, endpoint=False)
    outer = tuple((float(8 * np.cos(a)), float(8 * np.sin(a))) for a in turn)
    hole = tuple((float(3 * np.cos(-a)), float(3 * np.sin(-a))) for a in turn)

    seamed = bridged(outer, [hole])

    assert seamed is not None
    assert not self_intersects(seamed)
    assert signed_area(seamed) == pytest.approx(signed_area(outer) + signed_area(hole), rel=1e-4)


def test_a_bow_tie_is_reported_as_crossing() -> None:
    """The check itself, on the shape it exists for."""
    from sun_study.core.edges import self_intersects

    assert self_intersects(((0.0, 0.0), (10.0, 10.0), (10.0, 0.0), (0.0, 10.0)))
    assert not self_intersects(((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)))


# -- patches that cannot be one contour ------------------------------------


def test_slicing_a_holed_patch_keeps_its_area_exactly() -> None:
    """The other answer to a fill that takes one contour and no holes, and the
    one that cannot fail. Seaming asks whether a cut exists to every hole
    without crossing anything; on a shadow with several courtyards sometimes
    none does. Slicing asks nothing."""
    from sun_study.core.edges import decomposed

    square = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0))
    hole = ((3.0, 3.0), (3.0, 7.0), (7.0, 7.0), (7.0, 3.0))

    pieces = decomposed(square, [hole])

    assert sum(abs(signed_area(piece)) for piece in pieces) == pytest.approx(100.0 - 16.0)


def test_every_slice_is_a_polygon_archicad_will_take() -> None:
    """Each piece has to be a simple polygon in its own right, or slicing has
    only moved the problem. A slab closing to a point at a vertex is the case
    that catches this: written as a quad it has a corner twice."""
    from sun_study.core.edges import decomposed, self_intersects

    turn = np.linspace(0, 2 * np.pi, 24, endpoint=False)
    outer = tuple((float(9 * np.cos(a)), float(9 * np.sin(a))) for a in turn)
    hole = tuple((float(4 * np.cos(-a)), float(4 * np.sin(-a))) for a in turn)

    pieces = decomposed(outer, [hole])

    assert pieces
    assert all(len(piece) >= 3 for piece in pieces)
    assert all(len(piece) == len(set(piece)) for piece in pieces)
    assert not any(self_intersects(piece) for piece in pieces)
    assert sum(abs(signed_area(piece)) for piece in pieces) == pytest.approx(
        signed_area(outer) + signed_area(hole), rel=1e-9
    )


def test_several_holes_are_no_harder_than_one() -> None:
    """The case seaming fails on: each cut has to dodge the ones before it,
    and slicing has no cuts to dodge."""
    from sun_study.core.edges import decomposed

    turn = np.linspace(0, 2 * np.pi, 24, endpoint=False)
    outer = tuple((float(9 * np.cos(a)), float(9 * np.sin(a))) for a in turn)
    holes = [
        tuple((float(1.5 * np.cos(-a) + shift), float(1.5 * np.sin(-a))) for a in turn)
        for shift in (5.0, -5.0)
    ]

    pieces = decomposed(outer, holes)

    assert sum(abs(signed_area(piece)) for piece in pieces) == pytest.approx(
        signed_area(outer) + sum(signed_area(hole) for hole in holes), rel=1e-9
    )


def test_a_patch_with_no_holes_slices_to_itself() -> None:
    """Nothing to cut, so nothing should be cut."""
    from sun_study.core.edges import decomposed

    square = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0))

    pieces = decomposed(square, [])

    assert len(pieces) == 1
    assert abs(signed_area(pieces[0])) == pytest.approx(100.0)


def test_a_slice_that_is_really_a_line_is_dropped() -> None:
    """Archicad refuses a collinear polygon and takes a very small one, so the
    thing to measure is not area but how far the shape departs from a line.
    Fifty metres by a ten-millionth has an area well above any sensible floor
    and is a line."""
    from sun_study.core.edges import decomposed

    # A square with a hole whose edge grazes the outline, so a slab comes out
    # as good as flat.
    square = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0))
    hole = ((1.0, 1.0), (1.0, 9.0), (9.0, 9.0), (9.0, 1.0 + 1e-9))

    pieces = decomposed(square, [hole])

    assert all(
        2.0
        * abs(signed_area(piece))
        / sum(
            float(np.hypot(b[0] - a[0], b[1] - a[1]))
            for a, b in zip(piece, [*piece[1:], piece[0]], strict=True)
        )
        > 1e-5
        for piece in pieces
    )


def test_a_piece_ends_where_the_shape_divides_not_where_a_vertex_falls() -> None:
    """What makes the count small. Closing a piece at every vertex height is
    the easier thing to write and makes the count follow how many vertices the
    outline happens to have: a ring of 400 points came to 624 pieces. A piece
    should end only where the shape genuinely divides or joins, which for a
    ring is twice."""
    from sun_study.core.edges import decomposed

    turn = np.linspace(0, 2 * np.pi, 400, endpoint=False)
    outer = tuple((float(30 * np.cos(a)), float(30 * np.sin(a))) for a in turn)
    hole = tuple((float(6 * np.cos(-a)), float(6 * np.sin(-a))) for a in turn)

    pieces = decomposed(outer, [hole])

    assert len(pieces) < 150, f"{len(pieces)} pieces for one courtyard"
    assert sum(abs(signed_area(piece)) for piece in pieces) == pytest.approx(
        signed_area(outer) + signed_area(hole), rel=1e-9
    )


def test_stacked_courtyards_divide_and_rejoin_without_losing_area() -> None:
    """Three holes one above another, so the sweep splits and rejoins six
    times. The case where carrying a piece across a division would quietly
    lose or double a slab."""
    from sun_study.core.edges import decomposed, self_intersects

    turn = np.linspace(0, 2 * np.pi, 24, endpoint=False)
    outer = tuple((float(9 * np.cos(a)), float(9 * np.sin(a))) for a in turn)
    holes = [
        tuple((float(1.2 * np.cos(-a)), float(1.2 * np.sin(-a) + shift)) for a in turn)
        for shift in (-5.0, 0.0, 5.0)
    ]

    pieces = decomposed(outer, holes)

    assert not any(self_intersects(piece) for piece in pieces)
    assert sum(abs(signed_area(piece)) for piece in pieces) == pytest.approx(
        signed_area(outer) + sum(signed_area(hole) for hole in holes), rel=1e-9
    )


def test_a_piece_that_closes_to_a_point_is_a_triangle() -> None:
    """Where a piece narrows to nothing the two chains meet at one vertex,
    each arriving along a different edge, so their answers agree to about a
    part in 10^16 and not to the bit. Kept as two points that is a ring with a
    zero-length side, which is degenerate and refused."""
    from sun_study.core.edges import decomposed

    turn = np.linspace(0, 2 * np.pi, 24, endpoint=False)
    outer = tuple((float(9 * np.cos(a)), float(9 * np.sin(a))) for a in turn)
    hole = tuple((float(4 * np.cos(-a)), float(4 * np.sin(-a))) for a in turn)

    pieces = decomposed(outer, [hole])

    for piece in pieces:
        assert len(piece) == len({(round(x, 9), round(y, 9)) for x, y in piece})


def test_a_run_that_pinches_to_a_point_is_two_pieces() -> None:
    """Two lobes of shadow touching at a point. The run never divides, so
    matching by overlap sees one run throughout and carries the piece straight
    through the pinch -- after which the left chain is on the right and the
    piece is a bow tie. Two of 615 pieces on the real project were exactly
    that, and Archicad refuses a bow tie."""
    from sun_study.core.edges import decomposed, self_intersects

    hourglass = ((0.0, 0.0), (10.0, 0.0), (5.0, 5.0), (10.0, 10.0), (0.0, 10.0), (5.0, 5.0))

    pieces = decomposed(hourglass, [])

    assert len(pieces) == 2, "one lobe each side of the waist"
    assert not any(self_intersects(piece) for piece in pieces)
    assert sum(abs(signed_area(piece)) for piece in pieces) == pytest.approx(50.0)
