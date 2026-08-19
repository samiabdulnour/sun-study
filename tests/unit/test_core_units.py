"""Unit coverage for geometry, sampling and aggregation.

The analytic cases in ``test_analytic_cases.py`` prove the engine produces the
right physics end to end. These prove the individual pieces behave at their
edges, where the analytic cases never go.
"""

from __future__ import annotations

import numpy as np
import pytest

from sun_study.core.analysis import (
    SunlightResult,
    Weighting,
    cumulative_minutes,
    instant_weights,
    lit_share_per_instant,
    longest_continuous_minutes,
    summarise_by_parent,
    sunlit_matrix,
)
from sun_study.core.geometry import (
    TriangleMesh,
    box,
    horizontal_rectangle,
    rectangle,
    rotation_about_z,
)
from sun_study.core.occlusion import Occluder
from sun_study.core.sampling import (
    DEFAULT_SURFACE_OFFSET_M,
    SamplePoints,
    grid_on_rectangle,
    horizontal_grid,
    single_sample,
)

EMPTY = Occluder(TriangleMesh.empty())


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------
def test_box_is_closed_and_has_twelve_triangles() -> None:
    mesh = box((0.0, 0.0, 0.0), (1.0, 2.0, 3.0))
    assert mesh.triangle_count == 12
    assert mesh.triangles().shape == (12, 3, 3)
    np.testing.assert_allclose(mesh.vertices.min(axis=0), [0.0, 0.0, 0.0])
    np.testing.assert_allclose(mesh.vertices.max(axis=0), [1.0, 2.0, 3.0])


def test_box_rejects_inverted_corners() -> None:
    with pytest.raises(ValueError, match="must exceed"):
        box((1.0, 1.0, 1.0), (0.0, 2.0, 2.0))


def test_rectangle_normal_follows_the_edge_winding() -> None:
    """cross(u, v) decides which way the surface faces, so it must be stable."""
    face = rectangle((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    triangle = face.triangles()[0]
    normal = np.cross(triangle[1] - triangle[0], triangle[2] - triangle[0])
    assert normal[2] > 0.0


def test_horizontal_rectangle_is_centred_at_the_requested_height() -> None:
    panel = horizontal_rectangle((5.0, 7.0, 0.0), 2.0, 4.0, height=3.0)
    np.testing.assert_allclose(panel.vertices[:, 2], 3.0)
    np.testing.assert_allclose(panel.vertices[:, 0].min(), 4.0)
    np.testing.assert_allclose(panel.vertices[:, 1].max(), 9.0)


def test_concatenate_offsets_face_indices() -> None:
    merged = TriangleMesh.concatenate(
        [box((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)), box((5.0, 5.0, 5.0), (6.0, 6.0, 6.0))]
    )
    assert merged.triangle_count == 24
    assert int(merged.faces.max()) == len(merged.vertices) - 1
    # Both boxes survive, rather than the second silently indexing the first.
    assert merged.vertices[:, 0].max() == pytest.approx(6.0)


def test_concatenate_of_nothing_is_empty() -> None:
    assert TriangleMesh.concatenate([]).triangle_count == 0
    assert TriangleMesh.concatenate([TriangleMesh.empty()]).triangle_count == 0


def test_rotation_is_counter_clockwise_seen_from_above() -> None:
    """+X turns towards +Y, the right-handed sense. Pinned because
    ``core.orientation`` depends on this being the opposite of a compass."""
    turned = rotation_about_z(90.0) @ np.array([1.0, 0.0, 0.0])
    np.testing.assert_allclose(turned, [0.0, 1.0, 0.0], atol=1e-15)


def test_rotation_preserves_shape() -> None:
    mesh = box((0.0, 0.0, 0.0), (2.0, 1.0, 1.0))
    turned = mesh.rotated_about_z(37.0)
    original = np.linalg.norm(mesh.vertices - mesh.vertices.mean(axis=0), axis=1)
    rotated = np.linalg.norm(turned.vertices - turned.vertices.mean(axis=0), axis=1)
    np.testing.assert_allclose(sorted(original), sorted(rotated), atol=1e-12)


def test_translation_moves_every_vertex() -> None:
    mesh = box((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)).translated((10.0, -5.0, 2.0))
    np.testing.assert_allclose(mesh.vertices.min(axis=0), [10.0, -5.0, 2.0])


@pytest.mark.parametrize(
    ("vertices", "faces", "message"),
    [
        (np.zeros((3, 2)), np.zeros((1, 3), dtype=np.int64), r"shape \(n, 3\)"),
        (np.zeros((3, 3)), np.zeros((1, 4), dtype=np.int64), r"shape \(m, 3\)"),
        (np.zeros((2, 3)), np.array([[0, 1, 9]], dtype=np.int64), "only 2 vertices"),
        (np.zeros((3, 3)), np.array([[0, 1, -2]], dtype=np.int64), "negative"),
    ],
)
def test_malformed_meshes_are_rejected(
    vertices: np.ndarray, faces: np.ndarray, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        TriangleMesh(vertices, faces)


# ---------------------------------------------------------------------------
# sampling
# ---------------------------------------------------------------------------
def test_grid_spacing_produces_the_expected_count() -> None:
    """A 2.0 x 1.0 m window at 200 mm is a 10 x 5 grid."""
    points = grid_on_rectangle(
        (0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 0.0, 1.0), "w1", spacing_m=0.2
    )
    assert len(points) == 50


def test_samples_are_cell_centred_not_on_the_boundary() -> None:
    """An edge sample would sit exactly in the host wall's plane."""
    points = grid_on_rectangle(
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0), "w", spacing_m=0.5, surface_offset_m=0.0
    )
    assert points.positions[:, 0].min() == pytest.approx(0.25)
    assert points.positions[:, 0].max() == pytest.approx(0.75)


def test_a_window_narrower_than_the_grid_still_gets_a_sample() -> None:
    """Silently dropping a small window would quietly bias the percentage."""
    points = grid_on_rectangle(
        (0.0, 0.0, 0.0), (0.05, 0.0, 0.0), (0.0, 0.0, 0.05), "tiny", spacing_m=0.2
    )
    assert len(points) == 1


def test_surface_offset_pushes_samples_along_the_normal() -> None:
    """Without this the host wall shades its own window."""
    points = grid_on_rectangle(
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0), "w", spacing_m=1.0
    )
    np.testing.assert_allclose(points.normals[0], [0.0, -1.0, 0.0], atol=1e-12)
    assert points.positions[0][1] == pytest.approx(-DEFAULT_SURFACE_OFFSET_M)
    assert points.surface_offset_m == DEFAULT_SURFACE_OFFSET_M


def test_horizontal_grid_faces_up_and_sits_at_the_requested_height() -> None:
    points = horizontal_grid((0.0, 0.0, 10.0), 2.0, 2.0, "balcony", height_m=1.2, spacing_m=1.0)
    np.testing.assert_allclose(points.normals, np.tile([0.0, 0.0, 1.0], (len(points), 1)))
    np.testing.assert_allclose(points.positions[:, 2], 11.2)


def test_horizontal_grid_does_not_double_count_its_height() -> None:
    """The height above the slab is the offset; adding a normal offset too
    would silently lift every open-space sample by another 50 mm."""
    points = horizontal_grid((0.0, 0.0, 0.0), 1.0, 1.0, "pos", height_m=1.0, spacing_m=1.0)
    assert points.positions[0][2] == pytest.approx(1.0)
    assert points.surface_offset_m == 0.0


def test_parent_ids_keep_first_appearance_order() -> None:
    merged = SamplePoints.concatenate(
        [
            single_sample((0.0, 0.0, 0.0), (0, 0, 1), "b"),
            single_sample((1.0, 0.0, 0.0), (0, 0, 1), "a"),
            single_sample((2.0, 0.0, 0.0), (0, 0, 1), "b"),
        ]
    )
    assert merged.unique_parents == ("b", "a")
    np.testing.assert_array_equal(merged.mask_for("b"), [True, False, True])


def test_concatenate_reports_no_offset_when_groups_disagree() -> None:
    """One group's offset must not stand for the whole set on the result."""
    mixed = SamplePoints.concatenate(
        [
            grid_on_rectangle((0, 0, 0), (1, 0, 0), (0, 0, 1), "a", surface_offset_m=0.05),
            grid_on_rectangle((0, 0, 0), (1, 0, 0), (0, 0, 1), "b", surface_offset_m=0.10),
        ]
    )
    assert mixed.surface_offset_m == 0.0


def test_zero_spacing_is_rejected() -> None:
    with pytest.raises(ValueError, match="spacing_m"):
        grid_on_rectangle((0, 0, 0), (1, 0, 0), (0, 0, 1), "w", spacing_m=0.0)


def test_mismatched_sample_arrays_are_rejected() -> None:
    with pytest.raises(ValueError, match="parent ids"):
        SamplePoints(np.zeros((3, 3)), np.zeros((3, 3)), ("only-one",))


# ---------------------------------------------------------------------------
# analysis: weighting
# ---------------------------------------------------------------------------
def test_trapezoidal_weights_sum_to_the_window_length() -> None:
    """37 instants at 10 minutes is a 360 minute window, not 370."""
    weights = instant_weights(37, 10.0, Weighting.TRAPEZOIDAL)
    assert float(weights.sum()) == pytest.approx(360.0)
    assert weights[0] == pytest.approx(5.0)
    assert weights[-1] == pytest.approx(5.0)
    assert weights[1] == pytest.approx(10.0)


def test_uniform_weights_overstate_the_window() -> None:
    assert float(instant_weights(37, 10.0, Weighting.UNIFORM).sum()) == pytest.approx(370.0)


def test_a_single_instant_spans_no_time_under_trapezoidal_weighting() -> None:
    """Deliberate, and the reason shadow-boundary tests assert on booleans."""
    assert float(instant_weights(1, 10.0, Weighting.TRAPEZOIDAL).sum()) == 0.0
    assert float(instant_weights(1, 10.0, Weighting.UNIFORM).sum()) == 10.0


@pytest.mark.parametrize("count", [2, 5, 19, 37, 73])
def test_trapezoidal_weights_always_sum_to_n_minus_one_steps(count: int) -> None:
    weights = instant_weights(count, 10.0, Weighting.TRAPEZOIDAL)
    assert float(weights.sum()) == pytest.approx((count - 1) * 10.0)


def test_invalid_weighting_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="timestep_minutes"):
        instant_weights(10, 0.0)
    with pytest.raises(ValueError, match="instant_count"):
        instant_weights(-1, 10.0)


# ---------------------------------------------------------------------------
# analysis: continuity
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ([1, 1, 1, 1], 30.0),  # four in a row spans three intervals
        ([1, 0, 1, 0], 0.0),  # isolated instants span nothing
        ([0, 1, 1, 0], 10.0),
        ([1, 1, 0, 1], 10.0),
        ([0, 0, 0, 0], 0.0),
    ],
)
def test_longest_continuous_span(pattern: list[int], expected: float) -> None:
    sunlit = np.array([pattern], dtype=bool)
    assert float(longest_continuous_minutes(sunlit, 10.0)[0]) == pytest.approx(expected)


def test_continuous_never_exceeds_cumulative() -> None:
    """An unbroken run is a subset of the total, so it cannot be larger."""
    rng = np.random.default_rng(11)
    sunlit = rng.random((50, 37)) > 0.4
    weights = instant_weights(37, 10.0, Weighting.UNIFORM)
    assert np.all(longest_continuous_minutes(sunlit, 10.0) <= cumulative_minutes(sunlit, weights))


def test_cumulative_rejects_mismatched_weights() -> None:
    with pytest.raises(ValueError, match="weights"):
        cumulative_minutes(np.ones((2, 5), dtype=bool), instant_weights(4, 10.0))


# ---------------------------------------------------------------------------
# analysis: the sunlit matrix
# ---------------------------------------------------------------------------
def test_a_sun_below_the_horizon_lights_nothing() -> None:
    """Filtered on the +Z component before any ray is cast."""
    point = single_sample((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), "p")
    below = np.array([[0.0, 0.5, -0.5]])
    assert not sunlit_matrix(point, EMPTY, below).any()


def test_a_surface_facing_away_is_self_shaded() -> None:
    """No ray is cast: the surface the sample sits on is the occluder."""
    point = single_sample((0.0, 0.0, 0.0), (0.0, -1.0, 0.0), "p")
    from_the_north = np.array([[0.0, 0.7071, 0.7071]])
    assert not sunlit_matrix(point, EMPTY, from_the_north).any()


def test_matrix_shape_follows_points_and_instants() -> None:
    points = grid_on_rectangle((0, 0, 0), (1, 0, 0), (0, 0, 1), "w", spacing_m=0.5)
    suns = np.tile([0.0, 0.7071, 0.7071], (7, 1))
    assert sunlit_matrix(points, EMPTY, suns).shape == (len(points), 7)


def test_empty_inputs_give_an_empty_matrix() -> None:
    empty_points = SamplePoints(np.zeros((0, 3)), np.zeros((0, 3)), ())
    assert sunlit_matrix(empty_points, EMPTY, np.zeros((5, 3))).shape == (0, 5)

    point = single_sample((0, 0, 0), (0, 0, 1), "p")
    assert sunlit_matrix(point, EMPTY, np.zeros((0, 3))).shape == (1, 0)


def test_malformed_sun_vectors_are_rejected() -> None:
    point = single_sample((0, 0, 0), (0, 0, 1), "p")
    with pytest.raises(ValueError, match=r"shape \(n, 3\)"):
        sunlit_matrix(point, EMPTY, np.zeros((4, 2)))


# ---------------------------------------------------------------------------
# analysis: summarising by parent
# ---------------------------------------------------------------------------
def build_two_element_samples() -> SamplePoints:
    return SamplePoints.concatenate(
        [
            single_sample((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), "apartment-1"),
            single_sample((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), "apartment-1"),
            single_sample((2.0, 0.0, 0.0), (0.0, 0.0, 1.0), "apartment-2"),
        ]
    )


def test_summary_carries_the_settings_that_produced_it() -> None:
    """A duration separated from its weighting is not reproducible."""
    points = build_two_element_samples()
    sunlit = np.ones((3, 37), dtype=bool)
    result = summarise_by_parent(points, sunlit, 10.0, weighting=Weighting.TRAPEZOIDAL)

    assert isinstance(result, SunlightResult)
    assert result.parent_ids == ("apartment-1", "apartment-2")
    assert result.timestep_minutes == 10.0
    assert result.weighting is Weighting.TRAPEZOIDAL
    assert result.instant_count == 37
    assert result.window_minutes == pytest.approx(360.0)
    np.testing.assert_allclose(result.cumulative_minutes, [360.0, 360.0])


def test_mean_and_min_reducers_differ_when_samples_disagree() -> None:
    """Half an element in sun is not the same claim as all of it."""
    points = build_two_element_samples()
    sunlit = np.ones((3, 37), dtype=bool)
    sunlit[1, :] = False  # one of apartment-1's two samples is fully shaded

    mean = summarise_by_parent(points, sunlit, 10.0, reducer="mean")
    worst = summarise_by_parent(points, sunlit, 10.0, reducer="min")

    assert mean.cumulative_minutes[0] == pytest.approx(180.0)
    assert worst.cumulative_minutes[0] == pytest.approx(0.0)
    assert mean.cumulative_minutes[1] == pytest.approx(360.0)


def test_unknown_reducer_is_rejected() -> None:
    with pytest.raises(ValueError, match="reducer"):
        summarise_by_parent(build_two_element_samples(), np.ones((3, 5), bool), 10.0, reducer="max")


def test_as_dict_pairs_parents_with_durations() -> None:
    points = build_two_element_samples()
    result = summarise_by_parent(points, np.ones((3, 37), dtype=bool), 10.0)
    assert result.as_dict() == {"apartment-1": 360.0, "apartment-2": 360.0}


# ---------------------------------------------------------------------------
# Per-instant shares. The aggregate says whether an apartment complies; this
# says when, which is what a nine-to-three drawing series is made of.
# ---------------------------------------------------------------------------
def test_lit_share_per_instant_is_weighted_by_area_not_by_sample_count() -> None:
    """One big pane lit and one small pane dark is mostly lit.

    Counting samples would let a 0.8 m highlight window outvote a 6 m slider
    in the same room, which is the wrong answer in the one direction that
    matters -- it flatters a room whose real glazing is in shadow.
    """
    points = SamplePoints(
        positions=np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]),
        normals=np.array([[0.0, -1.0, 0.0], [0.0, -1.0, 0.0]]),
        parent_ids=("apartment", "apartment"),
        areas=np.array([9.0, 1.0]),
    )
    sunlit = np.array([[True, False], [False, False]])

    shares = lit_share_per_instant(points, sunlit)

    assert shares.shape == (1, 2)
    assert shares[0, 0] == pytest.approx(0.9), "the 9 m2 sample carries nine tenths"
    assert shares[0, 1] == pytest.approx(0.0)


def test_lit_share_per_instant_agrees_with_the_aggregate_it_is_drawn_beside() -> None:
    """The series and the compliance number must come from the same booleans.

    With equal sample areas the weighted duration is exactly the share summed
    over instants, so any drift between the two is a bug in one of them --
    and a drawing that disagreed with the schedule beside it would be the
    worst possible failure of this tool.
    """
    points = SamplePoints(
        positions=np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]),
        normals=np.array([[0.0, -1.0, 0.0], [0.0, -1.0, 0.0]]),
        parent_ids=("apartment", "apartment"),
    )
    sunlit = np.array([[True, True, False], [True, False, False]])
    weights = instant_weights(3, 60.0, Weighting.UNIFORM)

    shares = lit_share_per_instant(points, sunlit)
    from_series = float(shares[0] @ weights)
    from_samples = float(np.mean(cumulative_minutes(sunlit, weights)))

    assert from_series == pytest.approx(from_samples)


def test_lit_share_per_instant_survives_a_parent_with_no_area() -> None:
    """A degenerate element must not divide by zero mid-run."""
    points = SamplePoints(
        positions=np.array([[0.0, 0.0, 0.0]]),
        normals=np.array([[0.0, 0.0, 1.0]]),
        parent_ids=("flat",),
        areas=np.array([0.0]),
    )
    shares = lit_share_per_instant(points, np.array([[True, True]]))
    assert shares.shape == (1, 2)
    assert not shares.any()
