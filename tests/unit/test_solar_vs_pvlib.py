"""Cross-check ``core.solar`` against pvlib's independent SPA implementation.

pvlib is a *validation reference*, never a runtime dependency. It implements
the full NREL SPA algorithm, which is both a different algorithm and a
different codebase from the NOAA one under test, so agreement across a broad
sweep of latitudes, dates and times of day is real evidence rather than a
tautology.

The published worked example in ``test_solar.py`` proves correctness at one
instant. This proves there is no region of the input space where the
implementation quietly falls apart.

Skipped when pvlib is absent so the suite still runs on a bare install.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from sun_study.core.solar import solar_position

pvlib = pytest.importorskip("pvlib", reason="pvlib is an optional validation reference")
pd = pytest.importorskip("pandas")

pytestmark = pytest.mark.validation

UTC = dt.UTC

# The brief's milestone M0 criterion.
TOLERANCE_DEG = 0.1

# NOAA is geocentric; SPA is topocentric. Comparing them below the horizon is
# meaningless because refraction models diverge sharply there, and shadow
# casting never uses a below-horizon sun anyway.
MIN_ELEVATION_DEG = 1.0


def _pvlib_reference(
    times: list[dt.datetime], latitude: float, longitude: float
) -> tuple[np.ndarray, np.ndarray]:
    index = pd.DatetimeIndex(times)
    result = pvlib.solarposition.spa_python(
        index,
        latitude=latitude,
        longitude=longitude,
        altitude=0.0,
        pressure=101325.0,
        temperature=12.0,
        delta_t=67.0,
    )
    return (
        np.asarray(result["apparent_elevation"], dtype=np.float64),
        np.asarray(result["azimuth"], dtype=np.float64),
    )


def _angular_difference(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Smallest separation between two bearings, in degrees.

    Wraps, so 359.999 and 0.001 are two thousandths apart rather than nearly
    a full turn.
    """
    return np.asarray(np.abs(((a - b) + 180.0) % 360.0 - 180.0), dtype=np.float64)


LOCATIONS = [
    pytest.param(-33.8688, 151.2093, id="sydney"),
    pytest.param(-37.8136, 144.9631, id="melbourne"),
    pytest.param(-27.4698, 153.0251, id="brisbane"),
    pytest.param(51.5074, -0.1278, id="london"),
    pytest.param(39.742476, -105.1786, id="nrel-golden"),
    pytest.param(0.0, 0.0, id="equator-prime-meridian"),
    pytest.param(-64.0, -60.0, id="high-southern-latitude"),
]


def _direction_vectors(elevation_deg: np.ndarray, azimuth_deg: np.ndarray) -> np.ndarray:
    elev, azim = np.radians(elevation_deg), np.radians(azimuth_deg)
    return np.column_stack((np.cos(elev) * np.sin(azim), np.cos(elev) * np.cos(azim), np.sin(elev)))


@pytest.mark.parametrize(("latitude", "longitude"), LOCATIONS)
def test_sun_direction_tracks_pvlib_across_the_year(latitude: float, longitude: float) -> None:
    """Sweep a whole year at three-hour resolution at each location.

    The assertion is on the angle between the two sun *direction vectors*,
    not on azimuth, because that is the quantity a ray cast actually depends
    on. Azimuth alone is ill-conditioned near the zenith: at the equator the
    sun passes within 2 degrees of overhead, where a 0.01 degree difference in
    position swings azimuth by nearly 0.1 degrees while moving the sun almost
    nowhere. Asserting on azimuth there would measure the coordinate
    singularity rather than the algorithm.
    """
    times = [
        dt.datetime(2024, month, day, hour, 0, tzinfo=UTC)
        for month in range(1, 13)
        for day in (1, 11, 21)
        for hour in range(0, 24, 3)
    ]

    result = solar_position(times, latitude, longitude)
    reference_elevation, reference_azimuth = _pvlib_reference(times, latitude, longitude)

    daylight = reference_elevation > MIN_ELEVATION_DEG
    assert daylight.any(), "test grid must contain daytime samples"

    expected = _direction_vectors(reference_elevation, reference_azimuth)
    separation = np.degrees(
        np.arccos(np.clip(np.sum(result.unit_vectors_enu() * expected, axis=1), -1.0, 1.0))
    )
    worst_separation = float(np.max(separation[daylight]))
    assert worst_separation < TOLERANCE_DEG, (
        f"worst sun direction error {worst_separation:.5f} deg at lat {latitude}, lon {longitude}"
    )

    elevation_error = float(np.max(np.abs(result.elevation_deg - reference_elevation)[daylight]))
    assert elevation_error < TOLERANCE_DEG, (
        f"worst elevation error {elevation_error:.5f} deg at lat {latitude}, lon {longitude}"
    )

    # Azimuth on its own, away from the zenith singularity described above.
    well_conditioned = daylight & (reference_elevation < 85.0)
    if well_conditioned.any():
        azimuth_error = float(
            np.max(_angular_difference(result.azimuth_deg, reference_azimuth)[well_conditioned])
        )
        assert azimuth_error < TOLERANCE_DEG, (
            f"worst azimuth error {azimuth_error:.5f} deg at lat {latitude}, lon {longitude}"
        )


def test_adg_assessment_window_matches_pvlib_closely() -> None:
    """The window the tool actually assesses, at the resolution it uses.

    This is the case that matters commercially: Sydney, 21 June, 09:00-15:00
    AEST at ten minute steps.
    """
    from sun_study.core.solar import assessment_times

    times = list(
        assessment_times(
            dt.date(2024, 6, 21), "Australia/Sydney", dt.time(9, 0), dt.time(15, 0), 10
        )
    )
    result = solar_position(times, -33.8688, 151.2093)
    reference_elevation, reference_azimuth = _pvlib_reference(times, -33.8688, 151.2093)

    elevation_error = float(np.max(np.abs(result.elevation_deg - reference_elevation)))
    azimuth_error = float(np.max(_angular_difference(result.azimuth_deg, reference_azimuth)))

    assert elevation_error < 0.02, f"elevation error {elevation_error:.5f} deg"
    assert azimuth_error < 0.02, f"azimuth error {azimuth_error:.5f} deg"


def test_sun_vectors_track_pvlib() -> None:
    """The ENU vectors, not just the angles, agree with the reference.

    Guards the vector construction itself: a swapped sine and cosine would
    leave elevation and azimuth correct while pointing the rays elsewhere.
    """
    times = [
        dt.datetime(2024, month, 21, hour, 0, tzinfo=UTC)
        for month in (3, 6, 9, 12)
        for hour in range(0, 24, 2)
    ]
    latitude, longitude = -33.8688, 151.2093

    result = solar_position(times, latitude, longitude)
    reference_elevation, reference_azimuth = _pvlib_reference(times, latitude, longitude)

    elev = np.radians(reference_elevation)
    azim = np.radians(reference_azimuth)
    expected = np.column_stack(
        (np.cos(elev) * np.sin(azim), np.cos(elev) * np.cos(azim), np.sin(elev))
    )

    daylight = reference_elevation > MIN_ELEVATION_DEG
    angle_between = np.degrees(
        np.arccos(np.clip(np.sum(result.unit_vectors_enu() * expected, axis=1), -1.0, 1.0))
    )
    assert float(np.max(angle_between[daylight])) < TOLERANCE_DEG
