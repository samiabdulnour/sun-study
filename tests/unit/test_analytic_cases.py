"""The section 7 analytic cases: closed-form geometry the engine must reproduce.

Each case has an answer derivable with trigonometry on paper, so a failure
points at a specific broken assumption rather than at "the numbers moved".

Cases 3 and 4 use hand-specified sun vectors at exact azimuths and elevations
rather than real solar positions. The astronomy is already validated against
published references in ``test_solar.py``; mixing it in here would mean a
shadow-length assertion could fail for a reason that has nothing to do with
shadows. Cases 1, 2, 5 and 6 use real Sydney sun positions, because those cases
are *about* the coupling between the sun and the model frame.
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pytest

from sun_study.core.analysis import (
    Weighting,
    cumulative_minutes,
    instant_weights,
    sunlit_matrix,
)
from sun_study.core.geometry import TriangleMesh, box, rectangle, rotation_about_z
from sun_study.core.occlusion import Occluder
from sun_study.core.orientation import SiteOrientation
from sun_study.core.sampling import SamplePoints, single_sample
from sun_study.core.solar import assessment_times, solar_position

SYDNEY_LATITUDE = -33.8688
SYDNEY_LONGITUDE = 151.2093
ASSESSMENT_DATE = dt.date(2024, 6, 21)
TIMESTEP_MINUTES = 10
WINDOW_MINUTES = 360.0

UP = (0.0, 0.0, 1.0)
NORTH = (0.0, 1.0, 0.0)
SOUTH = (0.0, -1.0, 0.0)
EAST = (1.0, 0.0, 0.0)


def sydney_sun(true_north_bearing_deg: float = 0.0) -> np.ndarray:
    """Sun vectors in the model frame for the ADG window, 37 instants."""
    times = assessment_times(
        ASSESSMENT_DATE, "Australia/Sydney", dt.time(9, 0), dt.time(15, 0), TIMESTEP_MINUTES
    )
    position = solar_position(times, SYDNEY_LATITUDE, SYDNEY_LONGITUDE)
    site = SiteOrientation(
        latitude_deg=SYDNEY_LATITUDE,
        longitude_deg=SYDNEY_LONGITUDE,
        timezone="Australia/Sydney",
        true_north_bearing_deg=true_north_bearing_deg,
    )
    return site.sun_vectors(position)


def sun_vector(azimuth_deg: float, elevation_deg: float) -> np.ndarray:
    """A single unit sun vector at an exact bearing and elevation, ENU."""
    azimuth, elevation = math.radians(azimuth_deg), math.radians(elevation_deg)
    return np.array(
        [
            [
                math.cos(elevation) * math.sin(azimuth),
                math.cos(elevation) * math.cos(azimuth),
                math.sin(elevation),
            ]
        ],
        dtype=np.float64,
    )


def minutes(points: SamplePoints, occluder: Occluder, suns: np.ndarray) -> np.ndarray:
    weights = instant_weights(len(suns), TIMESTEP_MINUTES, Weighting.TRAPEZOIDAL)
    return cumulative_minutes(sunlit_matrix(points, occluder, suns), weights)


def is_lit(points: SamplePoints, occluder: Occluder, suns: np.ndarray) -> bool:
    """Whether a single sample sees the sun at a single instant.

    Shadow-boundary cases are yes/no questions at one instant, so they assert
    on this rather than on a duration. A single instant carries zero minutes
    under trapezoidal weighting -- correctly, since one instant spans no
    interval -- which would make a duration assertion here vacuous.
    """
    matrix = sunlit_matrix(points, occluder, suns)
    assert matrix.shape == (1, 1), f"expected one sample and one instant, got {matrix.shape}"
    return bool(matrix[0, 0])


EMPTY = Occluder(TriangleMesh.empty())


# ---------------------------------------------------------------------------
# Case 1 -- unobstructed horizontal point sees the whole window.
# ---------------------------------------------------------------------------
def test_unobstructed_horizontal_point_receives_the_full_window() -> None:
    """With nothing in the way and the sun up throughout, the answer is 360.

    Exactly 360, not 370: the trapezoidal weighting exists precisely so that
    37 instants across a 6 hour window sum to 6 hours.
    """
    point = single_sample((0.0, 0.0, 0.0), UP, "ground")
    suns = sydney_sun()

    assert np.all(suns[:, 2] > 0.0), "the sun must be up for the whole ADG window in Sydney"
    assert minutes(point, EMPTY, suns)[0] == pytest.approx(WINDOW_MINUTES)


def test_uniform_weighting_overstates_the_window_by_one_step() -> None:
    """The bias that makes the weighting choice worth stating in the output."""
    point = single_sample((0.0, 0.0, 0.0), UP, "ground")
    suns = sydney_sun()
    weights = instant_weights(len(suns), TIMESTEP_MINUTES, Weighting.UNIFORM)
    total = cumulative_minutes(sunlit_matrix(point, EMPTY, suns), weights)[0]

    assert total == pytest.approx(WINDOW_MINUTES + TIMESTEP_MINUTES)
    assert total == pytest.approx(370.0)


# ---------------------------------------------------------------------------
# Case 2 -- the southern hemisphere tripwire.
# ---------------------------------------------------------------------------
def test_north_facing_wall_gets_substantial_midwinter_sun() -> None:
    """In Sydney the midwinter sun stays north of east-west all window."""
    point = single_sample((0.0, 0.0, 1.5), NORTH, "north-window")
    total = minutes(point, EMPTY, sydney_sun())[0]

    assert total == pytest.approx(WINDOW_MINUTES), (
        "a north-facing wall in Sydney should see the sun for the entire "
        f"09:00-15:00 window on 21 June, got {total} minutes"
    )


def test_south_facing_wall_gets_no_midwinter_sun() -> None:
    """If this ever returns anything but zero, the north handling is inverted.

    This is the tripwire. A south-facing facade in Sydney receives no direct
    sun at all in midwinter; a tool that reports otherwise has swapped a sign
    somewhere between the solar azimuth and the model frame, and every
    compliance figure it produces is wrong in a way that looks plausible.
    """
    point = single_sample((0.0, 0.0, 1.5), SOUTH, "south-window")
    assert minutes(point, EMPTY, sydney_sun())[0] == 0.0


def test_the_two_facades_are_not_accidentally_identical() -> None:
    """Guards against a bug that returns the same answer regardless of normal."""
    north = minutes(single_sample((0, 0, 1.5), NORTH, "n"), EMPTY, sydney_sun())[0]
    south = minutes(single_sample((0, 0, 1.5), SOUTH, "s"), EMPTY, sydney_sun())[0]
    assert north > south


# ---------------------------------------------------------------------------
# Case 3 -- a vertical pole, shadow length and bearing against trigonometry.
# ---------------------------------------------------------------------------
POLE_HEIGHT = 10.0
POLE_HALF_WIDTH = 0.05
SUN_AZIMUTH = 20.0  # sun in the north-north-east
SUN_ELEVATION = 35.0


def pole_half_extent_along(bearing_deg: float) -> float:
    """Distance from the pole's axis to its face along a horizontal bearing.

    The pole is a square section, not a line, so its shadow reaches further
    than an idealised zero-width pole's by exactly this much. Ignoring it and
    absorbing the difference in a loose percentage tolerance would leave the
    test unable to distinguish a correct shadow from one a few percent wrong.
    """
    sine = abs(math.sin(math.radians(bearing_deg)))
    cosine = abs(math.cos(math.radians(bearing_deg)))
    return min(
        POLE_HALF_WIDTH / sine if sine else math.inf,
        POLE_HALF_WIDTH / cosine if cosine else math.inf,
    )


def expected_shadow_length(elevation_deg: float, bearing_deg: float = SUN_AZIMUTH) -> float:
    """Where the pole's shadow ends, exactly.

    A ground point at distance d along the anti-solar bearing is lit once the
    ray to the sun is already above the pole where it *enters* the pole's
    footprint, one half-extent nearer than the axis:

        (d - half_extent) * tan(elevation) > height
        =>  d > height / tan(elevation) + half_extent
    """
    return POLE_HEIGHT / math.tan(math.radians(elevation_deg)) + pole_half_extent_along(bearing_deg)


SHADOW_LENGTH = expected_shadow_length(SUN_ELEVATION)

# Tight enough that a shadow even 0.1% out of place fails: the tolerance is
# 1 cm on a 14 m shadow.
SHADOW_TOLERANCE_M = 0.01


def ground_point_at(distance: float, bearing_deg: float) -> SamplePoints:
    bearing = math.radians(bearing_deg)
    return single_sample(
        (distance * math.sin(bearing), distance * math.cos(bearing), 0.0), UP, "ground"
    )


@pytest.fixture(scope="module")
def pole() -> Occluder:
    return Occluder(
        box(
            (-POLE_HALF_WIDTH, -POLE_HALF_WIDTH, 0.0),
            (POLE_HALF_WIDTH, POLE_HALF_WIDTH, POLE_HEIGHT),
        )
    )


def test_shadow_length_matches_closed_form(pole: Occluder) -> None:
    """The shadow tip lands within 1 cm of where trigonometry puts it."""
    assert SHADOW_LENGTH == pytest.approx(14.334689, abs=1e-6)

    suns = sun_vector(SUN_AZIMUTH, SUN_ELEVATION)
    anti_solar = SUN_AZIMUTH + 180.0

    inside = is_lit(ground_point_at(SHADOW_LENGTH - SHADOW_TOLERANCE_M, anti_solar), pole, suns)
    outside = is_lit(ground_point_at(SHADOW_LENGTH + SHADOW_TOLERANCE_M, anti_solar), pole, suns)

    assert not inside, "a point 1 cm short of the shadow tip must be shaded"
    assert outside, "a point 1 cm beyond the shadow tip must be lit"


def test_shadow_falls_on_the_anti_solar_bearing(pole: Occluder) -> None:
    """The shadow points away from the sun, not in some other direction.

    Length alone cannot catch a bearing error, because the wrong bearing at the
    right radius is still the right distance from the pole.
    """
    suns = sun_vector(SUN_AZIMUTH, SUN_ELEVATION)
    radius = SHADOW_LENGTH * 0.5

    assert not is_lit(ground_point_at(radius, SUN_AZIMUTH + 180.0), pole, suns)

    for offset in (90.0, 180.0, 270.0):
        bearing = SUN_AZIMUTH + 180.0 + offset
        assert is_lit(ground_point_at(radius, bearing), pole, suns), (
            f"only the anti-solar bearing should be shaded; {offset} deg off it is not"
        )


@pytest.mark.parametrize("elevation", [15.0, 30.0, 45.0, 60.0, 75.0])
def test_shadow_length_tracks_elevation(pole: Occluder, elevation: float) -> None:
    """Sweeps the closed form across elevations, not just one lucky value."""
    expected = expected_shadow_length(elevation)
    suns = sun_vector(SUN_AZIMUTH, elevation)
    anti_solar = SUN_AZIMUTH + 180.0

    assert not is_lit(ground_point_at(expected - SHADOW_TOLERANCE_M, anti_solar), pole, suns)
    assert is_lit(ground_point_at(expected + SHADOW_TOLERANCE_M, anti_solar), pole, suns)


# ---------------------------------------------------------------------------
# Case 4 -- an overhang, shadow line on the wall below.
# ---------------------------------------------------------------------------
OVERHANG_HEIGHT = 3.0
OVERHANG_PROJECTION = 1.0
OVERHANG_ELEVATION = 30.0

# A north-facing wall point at height z sees the sun when the ray clears the
# overhang tip. The ray rises at tan(elevation) per unit of northward travel,
# so it crosses the overhang plane at y = (height - z) / tan(elevation), and is
# blocked while that is inside the projection:
#     (height - z) / tan(elevation) < projection
#     =>  z > height - projection * tan(elevation)
SHADOW_LINE_Z = OVERHANG_HEIGHT - OVERHANG_PROJECTION * math.tan(math.radians(OVERHANG_ELEVATION))


@pytest.fixture(scope="module")
def overhang() -> Occluder:
    """A horizontal blade projecting north from the wall plane at y = 0."""
    return Occluder(
        rectangle(
            (-5.0, 0.0, OVERHANG_HEIGHT),
            (10.0, 0.0, 0.0),
            (0.0, OVERHANG_PROJECTION, 0.0),
        )
    )


def wall_point(height: float) -> SamplePoints:
    return single_sample((0.0, 0.0, height), NORTH, "wall")


def test_overhang_shadow_line_matches_hand_calculation(overhang: Occluder) -> None:
    assert SHADOW_LINE_Z == pytest.approx(2.4226, abs=1e-4)

    suns = sun_vector(0.0, OVERHANG_ELEVATION)  # due north, solar noon

    assert is_lit(wall_point(SHADOW_LINE_Z - 0.01), overhang, suns), (
        "just below the shadow line the wall is lit"
    )
    assert not is_lit(wall_point(SHADOW_LINE_Z + 0.01), overhang, suns), (
        "just above the shadow line the wall is shaded"
    )


@pytest.mark.parametrize("elevation", [20.0, 30.0, 40.0, 50.0])
def test_overhang_shadow_deepens_with_sun_elevation(overhang: Occluder, elevation: float) -> None:
    """A higher sun drives the shadow further down the wall."""
    line = OVERHANG_HEIGHT - OVERHANG_PROJECTION * math.tan(math.radians(elevation))
    suns = sun_vector(0.0, elevation)

    assert is_lit(wall_point(line - 0.02), overhang, suns)
    assert not is_lit(wall_point(line + 0.02), overhang, suns)


def test_a_point_above_the_overhang_is_unaffected(overhang: Occluder) -> None:
    """Sanity: the blade shades what is below it, not what is above it."""
    suns = sun_vector(0.0, OVERHANG_ELEVATION)
    assert is_lit(wall_point(OVERHANG_HEIGHT + 0.5), overhang, suns)


# ---------------------------------------------------------------------------
# Cases 5 and 6 -- rotation, with and without north.
# ---------------------------------------------------------------------------
def courtyard_scene() -> tuple[TriangleMesh, SamplePoints]:
    """An asymmetric scene, so a rotation bug cannot hide behind symmetry."""
    mesh = TriangleMesh.concatenate(
        [
            box((-8.0, 4.0, 0.0), (8.0, 6.0, 12.0)),  # a wall to the north
            box((3.0, -6.0, 0.0), (5.0, -4.0, 20.0)),  # an off-axis tower
        ]
    )
    positions = np.array(
        [[-3.0, 0.0, 1.5], [0.0, 0.0, 1.5], [3.0, 0.0, 1.5], [0.0, -2.0, 0.0]],
        dtype=np.float64,
    )
    normals = np.array([NORTH, NORTH, NORTH, UP], dtype=np.float64)
    return mesh, SamplePoints(positions, normals, ("a", "b", "c", "pos"))


def rotate_samples(points: SamplePoints, degrees: float) -> SamplePoints:
    matrix = rotation_about_z(degrees)
    return SamplePoints(
        points.positions @ matrix.T,
        points.normals @ matrix.T,
        points.parent_ids,
        surface_offset_m=points.surface_offset_m,
    )


@pytest.mark.parametrize("angle", [30.0, 90.0, 137.5, -45.0])
def test_rotating_the_scene_and_north_together_changes_nothing(angle: float) -> None:
    """Case 5. The building has not moved relative to the sun, so nor may the answer.

    Rotating the geometry counter-clockwise by ``angle`` while advancing the
    true-north bearing of model +Y by the same ``angle`` describes the same
    physical situation in a different coordinate system.
    """
    mesh, points = courtyard_scene()
    baseline = minutes(points, Occluder(mesh), sydney_sun(0.0))

    rotated = minutes(
        rotate_samples(points, angle),
        Occluder(mesh.rotated_about_z(angle)),
        sydney_sun(angle),
    )

    assert np.allclose(rotated, baseline, atol=1e-9), (
        f"rotating scene and north together by {angle} deg changed the result: "
        f"{baseline} -> {rotated}"
    )


def test_rotating_the_scene_without_north_changes_the_result() -> None:
    """Case 6, part one: the same rotation with north left alone must move."""
    mesh, points = courtyard_scene()
    baseline = minutes(points, Occluder(mesh), sydney_sun(0.0))
    rotated = minutes(
        rotate_samples(points, 90.0), Occluder(mesh.rotated_about_z(90.0)), sydney_sun(0.0)
    )

    assert not np.allclose(rotated, baseline), (
        "rotating the scene without rotating north must change the answer; "
        "if it does not, the north bearing is being ignored"
    )


def test_rotating_a_north_facade_to_face_south_extinguishes_it() -> None:
    """Case 6, part two: the change is the *expected* amount, not merely nonzero.

    A half turn takes a north-facing facade to a south-facing one, and in
    Sydney's midwinter that is the difference between full sun and none.
    """
    suns = sydney_sun(0.0)
    facing_north = single_sample((0.0, 0.0, 1.5), NORTH, "w")

    assert minutes(facing_north, EMPTY, suns)[0] == pytest.approx(WINDOW_MINUTES)
    assert minutes(rotate_samples(facing_north, 180.0), EMPTY, suns)[0] == 0.0


@pytest.mark.parametrize("bearing", [0.0, 45.0, 90.0, 180.0, 270.0])
def test_north_bearing_alone_steers_the_facade(bearing: float) -> None:
    """Holding geometry fixed and turning only north sweeps a facade through the sun.

    At a bearing of 180 the model's +Y axis points due south, so a sample whose
    model-frame normal is +Y is physically a south-facing wall and must go dark.
    """
    point = single_sample((0.0, 0.0, 1.5), NORTH, "w")
    total = minutes(point, EMPTY, sydney_sun(bearing))

    if bearing == 0.0:
        assert total[0] == pytest.approx(WINDOW_MINUTES)
    elif bearing == 180.0:
        assert total[0] == 0.0
    else:
        assert 0.0 <= total[0] <= WINDOW_MINUTES


def test_east_facing_facade_gets_morning_sun_only() -> None:
    """An independent directional check that is not north or south.

    An east-facing wall must be lit for the first half of the window and dark
    for the last, which no sign error in the azimuth conversion survives.
    """
    suns = sydney_sun(0.0)
    point = single_sample((0.0, 0.0, 1.5), EAST, "east")
    lit = sunlit_matrix(point, EMPTY, suns)[0]

    midpoint = len(lit) // 2
    assert lit[:midpoint].sum() > lit[midpoint:].sum(), (
        "an east-facing wall must see more morning sun than afternoon sun"
    )
    assert not lit[-1], "an east-facing wall is not in sun at 15:00"
