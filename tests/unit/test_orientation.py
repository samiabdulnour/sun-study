"""The ENU-to-model conversion, pinned at the cardinal points.

The module docstring in ``core.orientation`` derives the sign of the rotation
algebraically. This checks the derivation against cases that can be reasoned
about without algebra, because a sign error here is invisible in every other
test that happens to use a bearing of zero.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from sun_study.core.orientation import (
    SiteOrientation,
    enu_to_model_matrix,
    sun_vectors_in_model_frame,
)
from sun_study.core.solar import solar_position

UTC = dt.UTC

EAST = np.array([1.0, 0.0, 0.0])
NORTH = np.array([0.0, 1.0, 0.0])
SOUTH = np.array([0.0, -1.0, 0.0])
WEST = np.array([-1.0, 0.0, 0.0])
UP = np.array([0.0, 0.0, 1.0])


def to_model(vector: np.ndarray, bearing_deg: float) -> np.ndarray:
    return np.asarray(enu_to_model_matrix(bearing_deg) @ vector)


# ---------------------------------------------------------------------------
# Worked cardinal cases.
# ---------------------------------------------------------------------------
def test_zero_bearing_is_the_identity() -> None:
    """A model already drawn to true north needs no rotation at all."""
    np.testing.assert_allclose(enu_to_model_matrix(0.0), np.eye(3), atol=1e-15)


def test_bearing_90_puts_model_y_along_east() -> None:
    """Reasoned without algebra.

    A bearing of 90 means the model's +Y axis points due east. In a
    right-handed Z-up frame that makes model +X point due south. So a sun in
    the true north must arrive as model -X, and a sun in the true east as
    model +Y.
    """
    np.testing.assert_allclose(to_model(NORTH, 90.0), [-1.0, 0.0, 0.0], atol=1e-12)
    np.testing.assert_allclose(to_model(EAST, 90.0), [0.0, 1.0, 0.0], atol=1e-12)
    np.testing.assert_allclose(to_model(SOUTH, 90.0), [1.0, 0.0, 0.0], atol=1e-12)
    np.testing.assert_allclose(to_model(WEST, 90.0), [0.0, -1.0, 0.0], atol=1e-12)


def test_bearing_180_reverses_the_horizontal_axes() -> None:
    """Model +Y pointing due south turns a northerly sun into model -Y."""
    np.testing.assert_allclose(to_model(NORTH, 180.0), [0.0, -1.0, 0.0], atol=1e-12)
    np.testing.assert_allclose(to_model(EAST, 180.0), [-1.0, 0.0, 0.0], atol=1e-12)


@pytest.mark.parametrize("bearing", [0.0, 17.0, 90.0, 180.0, 273.4, 360.0, -45.0])
def test_up_is_never_disturbed(bearing: float) -> None:
    """North is a rotation about the vertical, so it can never tilt the sun."""
    np.testing.assert_allclose(to_model(UP, bearing), UP, atol=1e-14)


@pytest.mark.parametrize("bearing", [0.0, 30.0, 137.5, -20.0])
def test_the_rotation_is_orthonormal(bearing: float) -> None:
    """Lengths and angles must survive, or the sun vectors stop being unit."""
    matrix = enu_to_model_matrix(bearing)
    np.testing.assert_allclose(matrix @ matrix.T, np.eye(3), atol=1e-14)
    assert float(np.linalg.det(matrix)) == pytest.approx(1.0)


def test_bearings_compose() -> None:
    """Two turns equal one bigger turn; catches a sign that flips with size."""
    np.testing.assert_allclose(
        enu_to_model_matrix(50.0) @ enu_to_model_matrix(20.0),
        enu_to_model_matrix(70.0),
        atol=1e-13,
    )


def test_bearing_360_equals_bearing_0() -> None:
    np.testing.assert_allclose(enu_to_model_matrix(360.0), enu_to_model_matrix(0.0), atol=1e-13)


# ---------------------------------------------------------------------------
# Applied to real sun vectors.
# ---------------------------------------------------------------------------
def test_sun_vectors_stay_unit_length_under_rotation() -> None:
    times = [dt.datetime(2024, 6, 21, hour, 0, tzinfo=UTC) for hour in range(0, 24, 2)]
    position = solar_position(times, -33.87, 151.21)
    for bearing in (0.0, 37.0, 180.0, 299.5):
        vectors = sun_vectors_in_model_frame(position, bearing)
        np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-12)


@pytest.mark.parametrize("bearing", [10.0, 30.0, 90.0, 200.0])
def test_north_bearing_subtracts_from_the_suns_model_azimuth(bearing: float) -> None:
    """The sign that the whole module turns on, asserted directly.

    A fixed sun sits at compass azimuth ``A``. Turning the model's +Y axis
    ``bearing`` degrees east of true north moves +Y *towards* the sun, so the
    sun's azimuth measured from +Y becomes ``A - bearing``, not ``A + bearing``.
    Getting this backwards mirrors every facade about the north-south axis,
    which leaves the north and south tripwires passing and quietly ruins every
    east and west result.
    """
    times = [dt.datetime(2024, 6, 21, 2, 0, tzinfo=UTC)]
    position = solar_position(times, -33.87, 151.21)

    def plan_azimuth(vector: np.ndarray) -> float:
        return float(np.degrees(np.arctan2(vector[0], vector[1])) % 360.0)

    at_zero = plan_azimuth(sun_vectors_in_model_frame(position, 0.0)[0])
    rotated = plan_azimuth(sun_vectors_in_model_frame(position, bearing)[0])

    assert rotated == pytest.approx((at_zero - bearing) % 360.0, abs=1e-9)


# ---------------------------------------------------------------------------
# SiteOrientation: no silent defaults.
# ---------------------------------------------------------------------------
def test_site_orientation_requires_every_field() -> None:
    """No field may acquire a default; a default is how a guess goes unnoticed."""
    with pytest.raises(TypeError):
        SiteOrientation(latitude_deg=-33.87, longitude_deg=151.21)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("latitude", "longitude"), [(-91.0, 151.0), (91.0, 151.0), (-33.0, 181.0), (-33.0, -181.0)]
)
def test_out_of_range_coordinates_are_rejected(latitude: float, longitude: float) -> None:
    with pytest.raises(ValueError, match="tude_deg"):
        SiteOrientation(latitude, longitude, "Australia/Sydney", 0.0)


def test_non_finite_bearing_is_rejected() -> None:
    with pytest.raises(ValueError, match="true_north_bearing_deg"):
        SiteOrientation(-33.87, 151.21, "Australia/Sydney", float("nan"))


@pytest.mark.parametrize(
    ("bearing", "expected"), [(0.0, 0.0), (370.0, 10.0), (-30.0, 330.0), (720.0, 0.0)]
)
def test_bearing_is_normalised_for_reporting(bearing: float, expected: float) -> None:
    site = SiteOrientation(-33.87, 151.21, "Australia/Sydney", bearing)
    assert site.normalised_bearing_deg == pytest.approx(expected)


def test_describe_echoes_everything_a_human_must_check() -> None:
    """The banner has to let someone catch a wrong site before reading results."""
    line = SiteOrientation(-33.8688, 151.2093, "Australia/Sydney", 12.5).describe()
    assert "33.868800S" in line
    assert "151.209300E" in line
    assert "Australia/Sydney" in line
    assert "12.500" in line
    assert "southern" in line


def test_describe_marks_the_northern_hemisphere() -> None:
    line = SiteOrientation(51.5074, -0.1278, "Europe/London", 0.0).describe()
    assert "51.507400N" in line
    assert "0.127800W" in line
    assert "northern" in line
