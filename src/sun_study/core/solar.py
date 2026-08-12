"""Solar position from latitude, longitude and instant.

Pure astronomy. No weather data, no I/O, no Archicad, no IFC.

Algorithm
---------
The NOAA Solar Calculator algorithm, which is Jean Meeus' *Astronomical
Algorithms* truncated for solar work. Coefficients here were transcribed from
NOAA's own published spreadsheet ``NOAA_Solar_Calculations_day.ods``
(https://gml.noaa.gov/grad/solcalc/calcdetails.html), cell by cell, rather than
from memory or from a third-party reimplementation.

Two deliberate departures from that spreadsheet, both covered by tests:

1. Azimuth uses the ``atan2`` form (NREL SPA equations 45-46) instead of NOAA's
   ``arccos`` form. The two are algebraically identical; the ``arccos`` form
   divides by ``cos(lat) * sin(zenith)``, which is zero at the poles and at the
   sub-solar point and loses precision near them. ``test_azimuth_forms_agree``
   asserts the two agree to 1e-9 degrees over a dense grid, so faithfulness to
   NOAA is verified rather than assumed.
2. The spreadsheet works in local clock time and carries a timezone column.
   This module works exclusively in UTC and requires timezone-aware datetimes,
   so a naive datetime cannot silently be interpreted as the wrong instant.

Accuracy is validated in ``tests/unit/test_solar.py`` against the worked example
published in NREL/TP-560-34302 (Reda & Andreas), Table A5.1.

Conventions
-----------
Fixed here and relied on by every layer above. Changing one silently changes
every result the tool produces.

latitude    degrees, positive north.
longitude   degrees, positive east.
azimuth     degrees clockwise from **true** north: 0 N, 90 E, 180 S, 270 W.
elevation   degrees above the horizon.
ENU frame   right-handed Cartesian, +X east, +Y north, +Z up.

Nothing in this module knows about a *model's* coordinate frame. Rotating the
ENU frame onto the Archicad project frame by the project's true-north bearing is
``ingest.scene``'s job, and is kept separate on purpose: this module is the part
that can be checked against published astronomy, and mixing a project rotation
into it would destroy that property.

Refraction
----------
``elevation_deg`` is the *apparent* elevation, corrected for atmospheric
refraction, and is what the sun vectors are built from: refraction is why a
shadow at 9am falls where it actually falls. ``true_elevation_deg`` is the
geometric position. Near the horizon the two differ by about half a degree, so
which one a downstream calculation uses is never an incidental choice.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]

__all__ = [
    "SolarPosition",
    "UnknownTimezoneError",
    "assessment_times",
    "julian_day",
    "julian_day_from_calendar",
    "resolve_timezone",
    "solar_position",
]

# 1582-10-15, the first day of the Gregorian calendar. Dates before it are
# interpreted as Julian, matching NREL/TP-560-34302 Table A4.1.
_GREGORIAN_START_JD = 2299160.0

# J2000.0 epoch: 2000-01-01 12:00 TT.
_J2000_JD = 2451545.0
_DAYS_PER_JULIAN_CENTURY = 36525.0


class UnknownTimezoneError(Exception):
    """Raised when a timezone name cannot be resolved.

    Deliberately fatal. Guessing a timezone silently shifts every sun position
    by whole hours, which produces a plausible-looking and completely wrong
    compliance percentage.
    """


def resolve_timezone(name: str) -> ZoneInfo:
    """Resolve an IANA timezone name, failing loudly when it is unavailable.

    The tool never infers a timezone from latitude and longitude, and never
    defaults to one. It must be stated in the run configuration and echoed in
    the output header.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UnknownTimezoneError(
            f"Unknown IANA timezone {name!r}. Set an explicit timezone for this "
            f"project, for example 'Australia/Sydney'. If the name looks correct, "
            f"the tz database may be missing: install the 'tzdata' package."
        ) from exc


def julian_day_from_calendar(year: int, month: int, day: float) -> float:
    """Julian Day from a calendar date, where ``day`` may carry a fraction.

    Meeus chapter 7. Dates from 1582-10-15 are treated as Gregorian and earlier
    dates as Julian. Valid for negative years, which lets the published test
    table in NREL/TP-560-34302 Table A4.1 be used verbatim as a unit test.

    ``day`` is the day of month plus the fraction of the day elapsed since
    midnight UT, so 12:00 UT on the first of a month is ``day=1.5``.
    """
    if month <= 2:
        year -= 1
        month += 12

    # Meeus' INT() is floor, including for negative years; math.floor matches.
    jd = math.floor(365.25 * (year + 4716)) + math.floor(30.6001 * (month + 1)) + day - 1524.5

    if jd >= _GREGORIAN_START_JD:
        a = math.floor(year / 100.0)
        jd += 2 - a + math.floor(a / 4.0)
    return jd


def _require_aware(moment: dt.datetime) -> dt.datetime:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(
            f"Naive datetime {moment!r}. Every instant must carry a timezone so that "
            f"a clock time cannot be silently read as the wrong instant."
        )
    return moment.astimezone(dt.UTC)


def julian_day(times: Sequence[dt.datetime]) -> FloatArray:
    """Julian Day (UT) for each timezone-aware datetime."""
    out = np.empty(len(times), dtype=np.float64)
    for i, moment in enumerate(times):
        utc = _require_aware(moment)
        day_fraction = (
            utc.hour + utc.minute / 60.0 + (utc.second + utc.microsecond / 1e6) / 3600.0
        ) / 24.0
        out[i] = julian_day_from_calendar(utc.year, utc.month, utc.day + day_fraction)
    return out


def _refraction_deg(true_elevation_deg: FloatArray) -> FloatArray:
    """Atmospheric refraction correction, degrees, added to true elevation.

    NOAA spreadsheet column AF. Piecewise in arcseconds, converted to degrees.
    All branches are evaluated before selection, so the divide-by-zero at
    elevation exactly 0 is expected and suppressed; that value is never chosen.
    """
    elev = true_elevation_deg
    with np.errstate(divide="ignore", invalid="ignore"):
        tan_e = np.tan(np.radians(elev))
        high = 58.1 / tan_e - 0.07 / tan_e**3 + 0.000086 / tan_e**5
        low = 1735.0 + elev * (-518.2 + elev * (103.4 + elev * (-12.79 + elev * 0.711)))
        below = -20.772 / tan_e

    arcseconds: FloatArray = np.select(
        [elev > 85.0, elev > 5.0, elev > -0.575],
        [np.zeros_like(elev), high, low],
        default=below,
    )
    return np.asarray(arcseconds / 3600.0, dtype=np.float64)


@dataclass(frozen=True)
class SolarPosition:
    """Sun positions for a series of instants at one location.

    Every array is parallel to ``times_utc``. Angles are degrees.
    """

    times_utc: tuple[dt.datetime, ...]
    latitude_deg: float
    longitude_deg: float

    julian_day: FloatArray
    declination_deg: FloatArray
    equation_of_time_min: FloatArray
    hour_angle_deg: FloatArray
    right_ascension_deg: FloatArray
    apparent_longitude_deg: FloatArray

    true_elevation_deg: FloatArray
    refraction_deg: FloatArray
    elevation_deg: FloatArray
    azimuth_deg: FloatArray

    def __len__(self) -> int:
        return len(self.times_utc)

    @property
    def zenith_deg(self) -> FloatArray:
        """Apparent zenith angle, 90 minus the refraction-corrected elevation."""
        return np.asarray(90.0 - self.elevation_deg, dtype=np.float64)

    @property
    def true_zenith_deg(self) -> FloatArray:
        """Geometric zenith angle, ignoring refraction."""
        return np.asarray(90.0 - self.true_elevation_deg, dtype=np.float64)

    def above_horizon(self, minimum_elevation_deg: float = 0.0) -> npt.NDArray[np.bool_]:
        """Mask of instants where the sun is up.

        Sun positions below the horizon must be dropped before ray casting; a
        below-horizon sun otherwise lights the scene from underneath.
        """
        return np.asarray(self.elevation_deg > minimum_elevation_deg, dtype=np.bool_)

    def unit_vectors_enu(self, *, apparent: bool = True) -> FloatArray:
        """Unit vectors pointing **towards** the sun, shape ``(n, 3)``, in ENU.

        Columns are east, north, up. ``apparent=True`` uses the
        refraction-corrected elevation, which is what shadows follow.
        """
        elevation = self.elevation_deg if apparent else self.true_elevation_deg
        elev = np.radians(elevation)
        azim = np.radians(self.azimuth_deg)
        cos_elev = np.cos(elev)
        return np.asarray(
            np.column_stack(
                (
                    cos_elev * np.sin(azim),  # east
                    cos_elev * np.cos(azim),  # north
                    np.sin(elev),  # up
                )
            ),
            dtype=np.float64,
        )


def solar_position(
    times: Sequence[dt.datetime],
    latitude_deg: float,
    longitude_deg: float,
) -> SolarPosition:
    """Sun positions for timezone-aware instants at one location.

    ``times`` may be in any timezone; each is converted to UTC first. Naive
    datetimes raise.
    """
    if not -90.0 <= latitude_deg <= 90.0:
        raise ValueError(f"latitude_deg {latitude_deg} outside [-90, 90]")
    if not -180.0 <= longitude_deg <= 180.0:
        raise ValueError(f"longitude_deg {longitude_deg} outside [-180, 180]")

    utc_times = tuple(_require_aware(moment) for moment in times)
    jd = julian_day(utc_times)
    jc = (jd - _J2000_JD) / _DAYS_PER_JULIAN_CENTURY

    # NOAA spreadsheet columns I through T. Column letters are given so each
    # line can be checked against the published source.
    geom_mean_long = np.mod(280.46646 + jc * (36000.76983 + jc * 0.0003032), 360.0)  # I
    geom_mean_anom = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)  # J
    eccentricity = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)  # K

    anom_rad = np.radians(geom_mean_anom)
    equation_of_centre = (  # L
        np.sin(anom_rad) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
        + np.sin(2.0 * anom_rad) * (0.019993 - 0.000101 * jc)
        + np.sin(3.0 * anom_rad) * 0.000289
    )

    true_long = geom_mean_long + equation_of_centre  # M
    omega = np.radians(125.04 - 1934.136 * jc)
    apparent_long = true_long - 0.00569 - 0.00478 * np.sin(omega)  # P

    mean_obliquity = (
        23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0
    )  # Q
    obliquity = mean_obliquity + 0.00256 * np.cos(omega)  # R

    app_long_rad = np.radians(apparent_long)
    obliq_rad = np.radians(obliquity)

    # S. NREL/TP-560-34302 step 3.9.2 limits right ascension to [0, 360); the
    # spreadsheet leaves atan2's (-180, 180] range as-is, so normalise here.
    right_ascension = np.mod(
        np.degrees(np.arctan2(np.cos(obliq_rad) * np.sin(app_long_rad), np.cos(app_long_rad))),
        360.0,
    )
    declination = np.degrees(np.arcsin(np.sin(obliq_rad) * np.sin(app_long_rad)))  # T

    var_y = np.tan(obliq_rad / 2.0) ** 2  # U
    mean_long_rad = np.radians(geom_mean_long)
    equation_of_time = 4.0 * np.degrees(  # V, minutes
        var_y * np.sin(2.0 * mean_long_rad)
        - 2.0 * eccentricity * np.sin(anom_rad)
        + 4.0 * eccentricity * var_y * np.sin(anom_rad) * np.cos(2.0 * mean_long_rad)
        - 0.5 * var_y * var_y * np.sin(4.0 * mean_long_rad)
        - 1.25 * eccentricity * eccentricity * np.sin(2.0 * anom_rad)
    )

    # Column AB, with timezone fixed at zero because we are already in UTC.
    minutes_past_utc_midnight = np.array(
        [t.hour * 60.0 + t.minute + (t.second + t.microsecond / 1e6) / 60.0 for t in utc_times],
        dtype=np.float64,
    )
    true_solar_time = np.mod(
        minutes_past_utc_midnight + equation_of_time + 4.0 * longitude_deg, 1440.0
    )
    # Column AC. true_solar_time is already reduced to [0, 1440), so the
    # spreadsheet's negative branch is unreachable here.
    hour_angle = true_solar_time / 4.0 - 180.0

    lat_rad = math.radians(latitude_deg)
    decl_rad = np.radians(declination)
    hour_rad = np.radians(hour_angle)

    cos_zenith = np.clip(
        math.sin(lat_rad) * np.sin(decl_rad)
        + math.cos(lat_rad) * np.cos(decl_rad) * np.cos(hour_rad),
        -1.0,
        1.0,
    )
    true_elevation = 90.0 - np.degrees(np.arccos(cos_zenith))  # AD, AE
    refraction = _refraction_deg(true_elevation)  # AF
    elevation = true_elevation + refraction  # AG

    # NREL/TP-560-34302 equations 45-46: astronomers' azimuth measured westward
    # from south, then rotated to eastward from north. See the module docstring
    # for why this replaces NOAA's arccos form.
    astronomers_azimuth = np.degrees(
        np.arctan2(
            np.sin(hour_rad),
            np.cos(hour_rad) * math.sin(lat_rad) - np.tan(decl_rad) * math.cos(lat_rad),
        )
    )
    azimuth = np.mod(astronomers_azimuth + 180.0, 360.0)

    return SolarPosition(
        times_utc=utc_times,
        latitude_deg=latitude_deg,
        longitude_deg=longitude_deg,
        julian_day=jd,
        declination_deg=declination,
        equation_of_time_min=equation_of_time,
        hour_angle_deg=hour_angle,
        right_ascension_deg=right_ascension,
        apparent_longitude_deg=np.mod(apparent_long, 360.0),
        true_elevation_deg=true_elevation,
        refraction_deg=refraction,
        elevation_deg=elevation,
        azimuth_deg=azimuth,
    )


def assessment_times(
    date: dt.date,
    timezone: ZoneInfo | str,
    start: dt.time,
    end: dt.time,
    timestep_minutes: int,
) -> tuple[dt.datetime, ...]:
    """Local clock instants across an assessment window, endpoints included.

    The ADG window of 09:00 to 15:00 at a 10 minute step yields 37 instants.
    Both endpoints are included, so the instants span 360 minutes but number
    37, not 36. Any aggregation that multiplies a count of instants by the
    timestep must account for that or it will report up to 370 minutes of sun
    in a 360 minute window; see ``core.analysis``.

    The returned datetimes are localised with fold=0. On a DST transition day a
    repeated local hour therefore resolves to the first occurrence. NSW is on
    AEST through the 21 June assessment date, so this does not arise for the
    ADG case, but it is recorded here rather than left to be discovered.
    """
    if timestep_minutes <= 0:
        raise ValueError(f"timestep_minutes must be positive, got {timestep_minutes}")

    zone = resolve_timezone(timezone) if isinstance(timezone, str) else timezone

    start_dt = dt.datetime.combine(date, start, tzinfo=zone)
    end_dt = dt.datetime.combine(date, end, tzinfo=zone)
    if end_dt <= start_dt:
        raise ValueError(f"end {end} must be after start {start}")

    span_minutes = (end_dt - start_dt).total_seconds() / 60.0
    steps = round(span_minutes / timestep_minutes)
    if not math.isclose(steps * timestep_minutes, span_minutes, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError(
            f"Window {start}-{end} is {span_minutes:g} minutes, which is not a whole "
            f"number of {timestep_minutes} minute steps. Choose a timestep that divides "
            f"the window so every sample represents the same duration."
        )

    return tuple(start_dt + dt.timedelta(minutes=timestep_minutes * i) for i in range(steps + 1))
