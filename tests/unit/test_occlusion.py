"""Correctness of the BVH ray caster.

The acceleration structure is the part most likely to be subtly wrong, because
a bug in it does not crash -- it silently misses an occluder, and the apartment
behind it gains an hour of sun that does not exist.

The main defence is differential: the BVH must agree with brute force on
randomised scenes, exactly, every time. Brute force shares the intersection
kernel, so that test isolates the *traversal*; the kernel itself is checked
separately against hand-computed geometry.
"""

from __future__ import annotations

import numpy as np
import pytest

from sun_study.core.geometry import TriangleMesh, box, rectangle
from sun_study.core.occlusion import _MAX_LEAF_TRIANGLES, Occluder, _triangle_hit


def brute_force(mesh: TriangleMesh, origins: np.ndarray, directions: np.ndarray) -> np.ndarray:
    """Every ray against every triangle, no acceleration structure."""
    return _triangle_hit(
        np.asarray(origins, dtype=np.float64),
        np.asarray(directions, dtype=np.float64),
        mesh.triangles(),
        np.full(len(origins), np.inf),
    )


def random_mesh(
    rng: np.random.Generator, triangle_count: int, spread: float = 10.0
) -> TriangleMesh:
    """A soup of small triangles scattered through a box."""
    centres = rng.uniform(-spread, spread, size=(triangle_count, 1, 3))
    corners = centres + rng.normal(scale=0.6, size=(triangle_count, 3, 3))
    vertices = corners.reshape(-1, 3)
    faces = np.arange(triangle_count * 3, dtype=np.int64).reshape(-1, 3)
    return TriangleMesh(vertices, faces)


# ---------------------------------------------------------------------------
# Differential: BVH vs brute force.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("triangle_count", [1, 2, 7, 8, 9, 31, 32, 33, 250, 1200])
def test_bvh_matches_brute_force(triangle_count: int) -> None:
    """Exact agreement, across tree depths from a single leaf to many levels.

    The counts straddle the leaf size deliberately: off-by-one errors in the
    split live exactly at the boundary where a leaf first has to divide.
    """
    rng = np.random.default_rng(triangle_count)
    mesh = random_mesh(rng, triangle_count)
    occluder = Occluder(mesh)

    origins = rng.uniform(-14.0, 14.0, size=(600, 3))
    directions = rng.normal(size=(600, 3))
    directions /= np.linalg.norm(directions, axis=1, keepdims=True)

    np.testing.assert_array_equal(
        occluder.any_hit(origins, directions), brute_force(mesh, origins, directions)
    )


@pytest.mark.parametrize("leaf_size", [1, 2, 4, 16, 64])
def test_bvh_matches_brute_force_at_any_leaf_size(leaf_size: int) -> None:
    """The leaf size is a tuning knob and must never change the answer."""
    rng = np.random.default_rng(leaf_size)
    mesh = random_mesh(rng, 400)

    origins = rng.uniform(-14.0, 14.0, size=(400, 3))
    directions = rng.normal(size=(400, 3))
    directions /= np.linalg.norm(directions, axis=1, keepdims=True)

    np.testing.assert_array_equal(
        Occluder(mesh, max_leaf_triangles=leaf_size).any_hit(origins, directions),
        brute_force(mesh, origins, directions),
    )


def test_axis_parallel_rays_match_brute_force() -> None:
    """Axis-parallel rays put zeros in the slab test's reciprocal.

    These are the rays a sun study actually fires when the sun is due north at
    solar noon, so the infinity handling has to be right rather than merely
    not crash.
    """
    rng = np.random.default_rng(99)
    mesh = random_mesh(rng, 300)

    origins = rng.uniform(-14.0, 14.0, size=(300, 3))
    directions = np.zeros_like(origins)
    axes = rng.integers(0, 3, size=len(origins))
    signs = rng.choice([-1.0, 1.0], size=len(origins))
    directions[np.arange(len(origins)), axes] = signs

    result = Occluder(mesh).any_hit(origins, directions)
    np.testing.assert_array_equal(result, brute_force(mesh, origins, directions))
    assert np.any(result), "the axis-parallel test scene must produce some hits"


def test_chunking_does_not_change_the_answer() -> None:
    """More rays than the internal chunk size must behave identically."""
    from sun_study.core import occlusion

    rng = np.random.default_rng(5)
    mesh = random_mesh(rng, 120)
    occluder = Occluder(mesh)

    count = occlusion._RAY_CHUNK + 1000
    origins = rng.uniform(-12.0, 12.0, size=(count, 3))
    directions = rng.normal(size=(count, 3))
    directions /= np.linalg.norm(directions, axis=1, keepdims=True)

    chunked = occluder.any_hit(origins, directions)
    piecewise = np.concatenate(
        [
            occluder.any_hit(origins[:500], directions[:500]),
            occluder.any_hit(origins[500:], directions[500:]),
        ]
    )
    np.testing.assert_array_equal(chunked, piecewise)


# ---------------------------------------------------------------------------
# The intersection kernel, against hand-computed geometry.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def unit_panel() -> Occluder:
    """A 2x2 horizontal panel at z = 1, centred on the origin."""
    return Occluder(rectangle((-1.0, -1.0, 1.0), (2.0, 0.0, 0.0), (0.0, 2.0, 0.0)))


@pytest.mark.parametrize(
    ("origin", "direction", "expected"),
    [
        ((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), True),  # straight up through the middle
        ((0.0, 0.0, 0.0), (0.0, 0.0, -1.0), False),  # straight down, away
        ((0.0, 0.0, 2.0), (0.0, 0.0, -1.0), True),  # down onto it from above
        ((1.5, 0.0, 0.0), (0.0, 0.0, 1.0), False),  # up, but outside the panel
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), False),  # sideways, never reaches it
        ((-2.0, 0.0, 0.0), (1.0, 0.0, 1.0), True),  # diagonal, lands at x = -1
        ((0.99, 0.99, 0.0), (0.0, 0.0, 1.0), True),  # just inside the corner
        ((1.01, 1.01, 0.0), (0.0, 0.0, 1.0), False),  # just outside the corner
    ],
)
def test_known_ray_panel_intersections(
    unit_panel: Occluder, origin: tuple[float, ...], direction: tuple[float, ...], expected: bool
) -> None:
    result = unit_panel.any_hit(np.array([origin]), np.array([direction]))
    assert bool(result[0]) is expected


def test_triangles_are_double_sided(unit_panel: Occluder) -> None:
    """IFC winding is not reliable, and a back-facing wall still blocks the sun."""
    from_below = unit_panel.any_hit(np.array([[0.0, 0.0, 0.0]]), np.array([[0.0, 0.0, 1.0]]))
    from_above = unit_panel.any_hit(np.array([[0.0, 0.0, 2.0]]), np.array([[0.0, 0.0, -1.0]]))
    assert bool(from_below[0]) and bool(from_above[0])


def test_max_distance_is_respected(unit_panel: Occluder) -> None:
    """A blocker beyond the ray's reach does not block it."""
    origin, direction = np.array([[0.0, 0.0, 0.0]]), np.array([[0.0, 0.0, 1.0]])
    assert not unit_panel.any_hit(origin, direction, max_distance=0.5)[0]
    assert unit_panel.any_hit(origin, direction, max_distance=1.5)[0]


def test_a_ray_does_not_hit_the_surface_it_starts_on() -> None:
    """Self-intersection guard: a point on a face is not shaded by that face."""
    panel = Occluder(rectangle((-1.0, -1.0, 0.0), (2.0, 0.0, 0.0), (0.0, 2.0, 0.0)))
    assert not panel.any_hit(np.array([[0.0, 0.0, 0.0]]), np.array([[0.0, 0.0, 1.0]]))[0]


def test_a_ray_along_a_shared_edge_is_not_missed() -> None:
    """Two triangles meeting at an edge must not leak a ray between them.

    ``rectangle`` splits into two triangles across a diagonal; a ray aimed
    exactly at that diagonal has to hit one of them.
    """
    panel = Occluder(rectangle((-1.0, -1.0, 1.0), (2.0, 0.0, 0.0), (0.0, 2.0, 0.0)))
    # The shared diagonal runs from (-1,-1) to (1,1); sample along it.
    for t in np.linspace(-0.9, 0.9, 25):
        hit = panel.any_hit(np.array([[t, t, 0.0]]), np.array([[0.0, 0.0, 1.0]]))
        assert bool(hit[0]), f"ray at ({t}, {t}) slipped between the two triangles"


# ---------------------------------------------------------------------------
# Degenerate input.
# ---------------------------------------------------------------------------
def test_empty_occluder_blocks_nothing() -> None:
    occluder = Occluder(TriangleMesh.empty())
    assert occluder.triangle_count == 0
    result = occluder.any_hit(np.zeros((4, 3)), np.tile([0.0, 0.0, 1.0], (4, 1)))
    assert result.shape == (4,)
    assert not result.any()


def test_no_rays_returns_an_empty_result() -> None:
    occluder = Occluder(box((-1.0, -1.0, -1.0), (1.0, 1.0, 1.0)))
    assert occluder.any_hit(np.zeros((0, 3)), np.zeros((0, 3))).shape == (0,)


def test_mismatched_ray_arrays_are_rejected() -> None:
    occluder = Occluder(box((-1.0, -1.0, -1.0), (1.0, 1.0, 1.0)))
    with pytest.raises(ValueError, match="must match"):
        occluder.any_hit(np.zeros((3, 3)), np.zeros((4, 3)))


def test_wrong_ray_shape_is_rejected() -> None:
    occluder = Occluder(box((-1.0, -1.0, -1.0), (1.0, 1.0, 1.0)))
    with pytest.raises(ValueError, match=r"shape \(n, 3\)"):
        occluder.any_hit(np.zeros((3, 2)), np.zeros((3, 2)))


def test_invalid_leaf_size_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_leaf_triangles"):
        Occluder(box((-1.0, -1.0, -1.0), (1.0, 1.0, 1.0)), max_leaf_triangles=0)


def test_degenerate_triangles_do_not_break_the_build() -> None:
    """Zero-area triangles appear in real IFC exports and must be tolerated."""
    vertices = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0], [0.0, 1.0, 1.0]],
        dtype=np.float64,
    )
    faces = np.array([[0, 1, 2], [0, 1, 3]], dtype=np.int64)  # first is collinear
    occluder = Occluder(TriangleMesh(vertices, faces))
    result = occluder.any_hit(np.array([[0.2, 0.2, 0.0]]), np.array([[0.0, 0.0, 1.0]]))
    assert result.shape == (1,)


def test_bvh_actually_builds_a_tree() -> None:
    """Guards against a regression that degrades the BVH into a single leaf.

    Such a regression is invisible: every result stays correct and the tool
    just gets slower on a real building until someone notices a run taking
    minutes. The bound is expressed in terms of the leaf size rather than as a
    magic number, so retuning the leaf size cannot silently defeat it.
    """
    triangle_count = 1000
    occluder = Occluder(random_mesh(np.random.default_rng(3), triangle_count))

    # A median-split tree needs at least ceil(n / leaf) leaves to hold them all.
    minimum_leaves = -(-triangle_count // _MAX_LEAF_TRIANGLES)
    assert occluder.node_count >= minimum_leaves, (
        f"expected at least {minimum_leaves} nodes for {triangle_count} triangles "
        f"at leaf size {_MAX_LEAF_TRIANGLES}, got {occluder.node_count}"
    )


def test_the_tree_check_can_actually_fail() -> None:
    """Proves the previous test discriminates rather than passing vacuously.

    A leaf big enough to swallow the whole scene must collapse to one node.
    """
    occluder = Occluder(random_mesh(np.random.default_rng(3), 1000), max_leaf_triangles=4096)
    assert occluder.node_count == 1
