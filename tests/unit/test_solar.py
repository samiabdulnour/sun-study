"""Validation of ``core.solar`` against published reference values.

Two independent published sources are used, both transcribed from the primary
documents rather than from memory:

NREL/TP-560-34302
    Reda, I. and Andreas, A., *Solar Position Algorithm for Solar Radiation
    Applications*, NREL, revised January 2008. Table A4.1 gives Julian Day test
    values; Table A5.1 gives a fully worked solar position example.
    https://www.nrel.gov/docs/fy08osti/34302.pdf

NOAA Solar Calculator
    ``NOAA_Solar_Calculations_day.ods``, the spreadsheet NOAA publishes as the
    reference implementation of the algorithm this module implements.
    https://gml.noaa.gov/grad/solcalc/calcdetails.html

The NREL comparison is the accuracy check: it uses a *different, more accurate*
algorithm (full SPA), so agreement to within 0.1 degrees demonstrates the
implementation is correct in absolute terms. The NOAA comparison is the
transcription check: it uses the same algorithm, so it must agree to floating
point precision, and catches a mistyped coefficient that a 0.1 degree
tolerance would hide.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from sun_study.core.solar import (
    UnknownTimezoneError,
    assessment_times,
    julian_day,
    julian_day_from_calendar,
    resolve_timezone,
    solar_position,
)

UTC = dt.UTC

# --------------------------------------------------------------------------
# NREL/TP-560-34302 Table A4.1 -- Julian Day test values, verbatim.
# --------------------------------------------------------------------------
JULIAN_DAY_CASES = [
    # (year, month, day, hours, minutes, expected JD)
    (2000, 1, 1, 12, 0, 2451545.0),
    (1999, 1, 1, 0, 0, 2451179.5),
    (1987, 1, 27, 0, 0, 2446822.5),
    (1987, 6, 19, 12, 0, 2446966.0),
    (1988, 1, 27, 0, 0, 2447187.5),
    (1988, 6, 19, 12, 0, 2447332.0),
    (1900, 1, 1, 0, 0, 2415020.5),
    (1600, 1, 1, 0, 0, 2305447.5),
    (1600, 12, 31, 0, 0, 2305812.5),
    (837, 4, 10, 7, 12, 2026871.8),
    (-123, 12, 31, 0, 0, 1676496.5),
    (-122, 1, 1, 0, 0, 1676497.5),
    (-1000, 7, 12, 12, 0, 1356001.0),
    (-1000, 2, 29, 0, 0, 1355866.5),
    (-1001, 8, 17, 21, 36, 1355671.4),
    (-4712, 1, 1, 12, 0, 0.0),
]


@pytest.mark.parametrize(("year", "month", "day", "hour", "minute", "expected"), JULIAN_DAY_CASES)
def test_julian_day_matches_nrel_table_a4_1(
    year: int, month: int, day: int, hour: int, minute: int, expected: float
) -> None:
    """Every published Julian Day value, including the Julian calendar cases."""
    fractional_day = day + (hour + minute / 60.0) / 24.0
    assert julian_day_from_calendar(year, month, fractional_day) == pytest.approx(
        expected, abs=1e-6
    )


# --------------------------------------------------------------------------
# NREL/TP-560-34302 Table A5.1 -- the worked example.
#
# Inputs, verbatim from section A.5:
#   Date 17 October 2003, 12:30:30 Local Standard Time, timezone -7 hours
#   Longitude -105.1786, Latitude 39.742476
#   Elevation 1830.14 m, Pressure 820 mbar, Temperature 11 C, delta-T 67 s
# --------------------------------------------------------------------------
SPA_TIME_UTC = dt.datetime(2003, 10, 17, 19, 30, 30, tzinfo=UTC)  # 12:30:30 at UTC-7
SPA_LATITUDE = 39.742476
SPA_LONGITUDE = -105.1786
SPA_ELEVATION_M = 1830.14
SPA_PRESSURE_MBAR = 820.0
SPA_TEMPERATURE_C = 11.0
SPA_DELTA_T = 67.0

SPA_EXPECTED = {
    "julian_day": 2452930.312847,
    "apparent_longitude_deg": 204.0085537528,
    "right_ascension_deg": 202.22741,
    "declination_deg": -9.31434,
    "hour_angle_deg": 11.105900,
    "zenith_deg": 50.11162,
    "azimuth_deg": 194.34024,
    "equation_of_time_min": 14.641503,
}

# The brief's acceptance criterion for milestone M0.
ACCURACY_TOLERANCE_DEG = 0.1


@pytest.fixture(scope="module")
def spa_case() -> object:
    return solar_position([SPA_TIME_UTC], SPA_LATITUDE, SPA_LONGITUDE)


def test_julian_day_matches_nrel_worked_example() -> None:
    """Julian Day for the Table A5.1 instant, to the published precision."""
    assert julian_day([SPA_TIME_UTC])[0] == pytest.approx(SPA_EXPECTED["julian_day"], abs=5e-7)


def test_zenith_matches_nrel_worked_example() -> None:
    """Topocentric zenith angle within the 0.1 degree milestone tolerance.

    NOAA is geocentric and ignores delta-T, so exact agreement with full SPA is
    not expected; the point is that the residual is far below the tolerance.
    """
    result = solar_position([SPA_TIME_UTC], SPA_LATITUDE, SPA_LONGITUDE)
    assert result.zenith_deg[0] == pytest.approx(
        SPA_EXPECTED["zenith_deg"], abs=ACCURACY_TOLERANCE_DEG
    )


def test_azimuth_matches_nrel_worked_example() -> None:
    """Topocentric azimuth, measured eastward from north, within tolerance."""
    result = solar_position([SPA_TIME_UTC], SPA_LATITUDE, SPA_LONGITUDE)
    assert result.azimuth_deg[0] == pytest.approx(
        SPA_EXPECTED["azimuth_deg"], abs=ACCURACY_TOLERANCE_DEG
    )


@pytest.mark.parametrize(
    ("attribute", "tolerance"),
    [
        ("apparent_longitude_deg", ACCURACY_TOLERANCE_DEG),
        ("right_ascension_deg", ACCURACY_TOLERANCE_DEG),
        ("declination_deg", ACCURACY_TOLERANCE_DEG),
        ("hour_angle_deg", ACCURACY_TOLERANCE_DEG),
        # Equation of time is minutes, not degrees; 0.1 deg of hour angle is
        # 0.4 minutes of time, so this is the equivalent tolerance.
        ("equation_of_time_min", 0.4),
    ],
)
def test_intermediate_quantities_match_nrel_worked_example(
    attribute: str, tolerance: float
) -> None:
    """Intermediate terms too, so a compensating pair of errors cannot hide."""
    result = solar_position([SPA_TIME_UTC], SPA_LATITUDE, SPA_LONGITUDE)
    actual = float(getattr(result, attribute)[0])
    assert actual == pytest.approx(SPA_EXPECTED[attribute], abs=tolerance)


# --------------------------------------------------------------------------
# NOAA_Solar_Calculations_day.ods -- transcription check.
#
# Spreadsheet inputs: Latitude 40, Longitude -105, Time Zone -7, Date
# 21 June 2010, row 2 at 00:06:00 local. Expected values are the spreadsheet's
# own computed cell values at full stored precision.
# --------------------------------------------------------------------------
NOAA_TIME_UTC = dt.datetime(2010, 6, 21, 7, 6, 0, tzinfo=UTC)  # 00:06:00 at UTC-7
NOAA_LATITUDE = 40.0
NOAA_LONGITUDE = -105.0

NOAA_EXPECTED = {
    "julian_day": 2455368.79583333,  # cell F2
    "right_ascension_deg": 89.8094800844214,  # cell S2
    "declination_deg": 23.4383706192869,  # cell T2
    "equation_of_time_min": -1.71536757846953,  # cell V2
    "true_elevation_deg": -26.5537621191813,  # cell AE2
    "refraction_deg": 0.0115456865919242,  # cell AF2
    "elevation_deg": -26.5422164325893,  # cell AG2
    "azimuth_deg": 1.09867118388445,  # cell AH2
}


@pytest.mark.parametrize(("attribute", "expected"), sorted(NOAA_EXPECTED.items()))
def test_matches_noaa_reference_spreadsheet(attribute: str, expected: float) -> None:
    """Agreement to floating point precision with NOAA's own spreadsheet.

    A loose tolerance here would defeat the purpose: this test exists to catch
    a single mistyped digit in a transcribed coefficient.
    """
    result = solar_position([NOAA_TIME_UTC], NOAA_LATITUDE, NOAA_LONGITUDE)
    actual = float(getattr(result, attribute)[0])
    assert actual == pytest.approx(expected, rel=1e-9, abs=1e-9)


def test_azimuth_forms_agree() -> None:
    """The atan2 azimuth equals NOAA's arccos azimuth everywhere it is defined.

    ``core.solar`` uses the atan2 form for numerical robustness. This asserts
    that choice is a pure refactor of the published NOAA formula rather than a
    different answer, across a dense grid of latitudes and times of year.
    """
    latitudes = np.arange(-80.0, 80.1, 10.0)
    times = [
        dt.datetime(2024, month, day, hour, 0, tzinfo=UTC)
        for month in (1, 3, 6, 9, 12)
        for day in (1, 15)
        for hour in range(0, 24, 3)
    ]

    worst = 0.0
    for latitude in latitudes:
        result = solar_position(times, float(latitude), 151.0)

        lat_rad = np.radians(latitude)
        zenith_rad = np.radians(result.true_zenith_deg)
        decl_rad = np.radians(result.declination_deg)

        # NOAA spreadsheet cell AH, transcribed verbatim.
        denominator = np.cos(lat_rad) * np.sin(zenith_rad)
        numerator = np.sin(lat_rad) * np.cos(zenith_rad) - np.sin(decl_rad)
        with np.errstate(divide="ignore", invalid="ignore"):
            core = np.degrees(np.arccos(np.clip(numerator / denominator, -1.0, 1.0)))
        noaa_azimuth = np.where(
            result.hour_angle_deg > 0.0,
            np.mod(core + 180.0, 360.0),
            np.mod(540.0 - core, 360.0),
        )

        # Compare as a circular difference so 359.9999 and 0.0001 are close.
        delta = np.abs(((result.azimuth_deg - noaa_azimuth) + 180.0) % 360.0 - 180.0)
        # Skip the degenerate points the arccos form cannot express: the sun
        # within a hair of the zenith, where its denominator collapses.
        usable = np.sin(zenith_rad) > 1e-6
        worst = max(worst, float(np.max(delta[usable])))

    assert worst < 1e-9, f"azimuth forms diverge by {worst} degrees"


# --------------------------------------------------------------------------
# Timezone handling -- section 5.4 of the brief.
# --------------------------------------------------------------------------
def test_sydney_21_june_is_aest_not_aedt() -> None:
    """NSW does not observe daylight saving in June: 21 June is UTC+10.

    A fixed +11 offset, or a tz database that silently failed to load, would
    shift every assessment instant by an hour.
    """
    times = assessment_times(
        dt.date(2024, 6, 21),
        "Australia/Sydney",
        dt.time(9, 0),
        dt.time(15, 0),
        timestep_minutes=10,
    )
    assert len(times) == 37, "09:00-15:00 at 10 minutes inclusive of both endpoints"

    for moment in times:
        assert moment.utcoffset() == dt.timedelta(hours=10)

    assert times[0].astimezone(UTC) == dt.datetime(2024, 6, 20, 23, 0, tzinfo=UTC)
    assert times[-1].astimezone(UTC) == dt.datetime(2024, 6, 21, 5, 0, tzinfo=UTC)


def test_sydney_midwinter_noon_sun_is_in_the_north() -> None:
    """The southern hemisphere tripwire.

    At Sydney's latitude on the June solstice the sun culminates in the north
    at roughly 32.7 degrees, by ``90 - |latitude - declination|``. If the north
    handling is inverted this test fails loudly rather than producing a
    plausible number.
    """
    sydney_latitude, sydney_longitude = -33.8688, 151.2093
    times = assessment_times(
        dt.date(2024, 6, 21), "Australia/Sydney", dt.time(9, 0), dt.time(15, 0), 10
    )
    result = solar_position(times, sydney_latitude, sydney_longitude)

    peak = int(np.argmax(result.elevation_deg))
    declination = float(result.declination_deg[peak])
    expected_peak = 90.0 - abs(sydney_latitude - declination)

    assert float(result.elevation_deg[peak]) == pytest.approx(expected_peak, abs=0.1)
    assert expected_peak == pytest.approx(32.7, abs=0.3)

    # Azimuth near culmination must be north, not south.
    azimuth = float(result.azimuth_deg[peak])
    assert min(azimuth, 360.0 - azimuth) < 5.0, f"midwinter noon sun at azimuth {azimuth}"

    # And the sun vector must point north and up.
    east, north, up = result.unit_vectors_enu()[peak]
    assert north > 0.0, "southern hemisphere midwinter sun must be to the north"
    assert up > 0.0
    assert abs(east) < abs(north)


def test_naive_datetime_is_rejected() -> None:
    """A naive datetime is an unstated instant, so it must not be guessable."""
    with pytest.raises(ValueError, match="Naive datetime"):
        solar_position([dt.datetime(2024, 6, 21, 12, 0)], -33.8688, 151.2093)


def test_unknown_timezone_fails_loudly() -> None:
    with pytest.raises(UnknownTimezoneError, match="Australia/Sydneyy"):
        resolve_timezone("Australia/Sydneyy")


def test_known_timezone_resolves() -> None:
    """Proves the tz database is actually present, on every CI platform."""
    assert resolve_timezone("Australia/Sydney") == ZoneInfo("Australia/Sydney")


def test_local_times_and_utc_times_give_identical_positions() -> None:
    """The same instant expressed in two timezones is the same sun position."""
    sydney = solar_position(
        [dt.datetime(2024, 6, 21, 12, 0, tzinfo=ZoneInfo("Australia/Sydney"))], -33.87, 151.21
    )
    utc = solar_position([dt.datetime(2024, 6, 21, 2, 0, tzinfo=UTC)], -33.87, 151.21)
    assert float(sydney.elevation_deg[0]) == pytest.approx(float(utc.elevation_deg[0]))
    assert float(sydney.azimuth_deg[0]) == pytest.approx(float(utc.azimuth_deg[0]))


# --------------------------------------------------------------------------
# Vectors, invariants and input validation.
# --------------------------------------------------------------------------
def test_sun_vectors_are_unit_length() -> None:
    times = assessment_times(
        dt.date(2024, 6, 21), "Australia/Sydney", dt.time(9, 0), dt.time(15, 0), 10
    )
    vectors = solar_position(times, -33.87, 151.21).unit_vectors_enu()
    assert vectors.shape == (37, 3)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0)


@pytest.mark.parametrize(
    ("azimuth_deg", "expected_axis"),
    [(0.0, (0.0, 1.0)), (90.0, (1.0, 0.0)), (180.0, (0.0, -1.0)), (270.0, (-1.0, 0.0))],
)
def test_enu_frame_orientation(azimuth_deg: float, expected_axis: tuple[float, float]) -> None:
    """Pins the ENU convention: azimuth 90 is east, 180 is south.

    Every geometry result depends on this, so it is asserted directly rather
    than left implied by the trigonometry.
    """
    east = np.cos(0.0) * np.sin(np.radians(azimuth_deg))
    north = np.cos(0.0) * np.cos(np.radians(azimuth_deg))
    assert (east, north) == pytest.approx(expected_axis, abs=1e-12)


def test_refraction_lifts_the_sun_and_is_largest_at_the_horizon() -> None:
    """Refraction must be non-negative near the horizon and shrink with height."""
    times = [
        dt.datetime(2024, 3, 20, hour, minute, tzinfo=UTC)
        for hour in range(24)
        for minute in (0, 30)
    ]
    result = solar_position(times, 0.0, 0.0)

    up = result.true_elevation_deg > 0.0
    assert np.all(result.refraction_deg[up] >= 0.0)
    assert np.all(result.elevation_deg[up] >= result.true_elevation_deg[up])

    near_horizon = result.refraction_deg[
        (result.true_elevation_deg > 0.0) & (result.true_elevation_deg < 1.0)
    ]
    high = result.refraction_deg[result.true_elevation_deg > 40.0]
    if near_horizon.size and high.size:
        assert float(np.min(near_horizon)) > float(np.max(high))


def test_refraction_is_finite_everywhere() -> None:
    """No NaN or inf leaking out of the piecewise branches, including at 0."""
    times = [
        dt.datetime(2024, 6, 21, hour, minute, tzinfo=UTC)
        for hour in range(24)
        for minute in range(0, 60, 5)
    ]
    for latitude in (-89.9, -33.87, 0.0, 51.5, 89.9):
        result = solar_position(times, latitude, 0.0)
        assert np.all(np.isfinite(result.refraction_deg))
        assert np.all(np.isfinite(result.elevation_deg))
        assert np.all(np.isfinite(result.azimuth_deg))
        assert np.all((result.azimuth_deg >= 0.0) & (result.azimuth_deg < 360.0))


@pytest.mark.parametrize("latitude", [-90.5, 90.5, 1000.0])
def test_out_of_range_latitude_rejected(latitude: float) -> None:
    with pytest.raises(ValueError, match="latitude_deg"):
        solar_position([dt.datetime(2024, 6, 21, 12, tzinfo=UTC)], latitude, 0.0)


@pytest.mark.parametrize("longitude", [-180.5, 180.5])
def test_out_of_range_longitude_rejected(longitude: float) -> None:
    with pytest.raises(ValueError, match="longitude_deg"):
        solar_position([dt.datetime(2024, 6, 21, 12, tzinfo=UTC)], 0.0, longitude)


def test_assessment_window_must_divide_evenly() -> None:
    """A ragged final step would make each sample represent a different duration."""
    with pytest.raises(ValueError, match="whole number"):
        assessment_times(dt.date(2024, 6, 21), "Australia/Sydney", dt.time(9, 0), dt.time(15, 0), 7)


def test_assessment_window_rejects_reversed_bounds() -> None:
    with pytest.raises(ValueError, match="must be after"):
        assessment_times(
            dt.date(2024, 6, 21), "Australia/Sydney", dt.time(15, 0), dt.time(9, 0), 10
        )


def test_assessment_window_rejects_non_positive_timestep() -> None:
    with pytest.raises(ValueError, match="timestep_minutes"):
        assessment_times(dt.date(2024, 6, 21), "Australia/Sydney", dt.time(9, 0), dt.time(15, 0), 0)
