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
