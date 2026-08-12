"""Print measured residuals for ``core.solar`` against every reference.

The test suite asserts the residuals are inside tolerance. This prints what
they actually are, so the margin is visible rather than merely sufficient, and
so a regression that stays inside tolerance is still noticeable.

    uv run python scripts/validation_report.py

Its output is pasted into docs/validation.md. Regenerate it there when the
solar code changes.
"""

from __future__ import annotations

import datetime as dt

import numpy as np

from sun_study.core.solar import assessment_times, julian_day, solar_position

UTC = dt.UTC


def _rule(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def _row(label: str, actual: float, expected: float, unit: str = "deg") -> None:
    print(f"  {label:<28} {actual:>18.9f}  {expected:>18.9f}  {actual - expected:>+14.3e} {unit}")


def _header() -> None:
    print(f"  {'quantity':<28} {'computed':>18}  {'published':>18}  {'residual':>14}")


def nrel_worked_example() -> None:
    """NREL/TP-560-34302 Table A5.1."""
    _rule("NREL/TP-560-34302 Table A5.1 -- full SPA, a different algorithm")
    print("  2003-10-17 12:30:30 UTC-7, lat 39.742476, lon -105.1786")
    time = dt.datetime(2003, 10, 17, 19, 30, 30, tzinfo=UTC)
    result = solar_position([time], 39.742476, -105.1786)
    _header()
    _row("julian day", float(julian_day([time])[0]), 2452930.312847, "d")
    _row("apparent longitude", float(result.apparent_longitude_deg[0]), 204.0085537528)
    _row("right ascension", float(result.right_ascension_deg[0]), 202.22741)
    _row("declination", float(result.declination_deg[0]), -9.31434)
    _row("observer hour angle", float(result.hour_angle_deg[0]), 11.105900)
    _row("equation of time", float(result.equation_of_time_min[0]), 14.641503, "min")
    _row("zenith", float(result.zenith_deg[0]), 50.11162)
    _row("azimuth", float(result.azimuth_deg[0]), 194.34024)


def noaa_spreadsheet() -> None:
    """NOAA_Solar_Calculations_day.ods -- same algorithm, transcription check."""
    _rule("NOAA reference spreadsheet -- same algorithm, must match to 1e-9")
    print("  2010-06-21 00:06:00 UTC-7, lat 40, lon -105")
    time = dt.datetime(2010, 6, 21, 7, 6, 0, tzinfo=UTC)
    result = solar_position([time], 40.0, -105.0)
    _header()
    _row("julian day", float(julian_day([time])[0]), 2455368.79583333, "d")
    _row("right ascension", float(result.right_ascension_deg[0]), 89.8094800844214)
    _row("declination", float(result.declination_deg[0]), 23.4383706192869)
    _row("equation of time", float(result.equation_of_time_min[0]), -1.71536757846953, "min")
    _row("true elevation", float(result.true_elevation_deg[0]), -26.5537621191813)
    _row("refraction", float(result.refraction_deg[0]), 0.0115456865919242)
    _row("apparent elevation", float(result.elevation_deg[0]), -26.5422164325893)
    _row("azimuth", float(result.azimuth_deg[0]), 1.09867118388445)


def pvlib_sweep() -> None:
    """Independent SPA implementation, swept across the year."""
    try:
        import pandas as pd
        import pvlib
    except ImportError:
        print("\n  pvlib not installed; skipping sweep")
        return

    _rule("pvlib SPA cross-check -- worst error over a year, sun above 1 deg")
    print(
        "  'separation' is the angle between the two sun direction vectors, which is"
        "\n  the physically meaningful error. Raw azimuth error is reported for"
        "\n  reference but is ill-conditioned when the sun passes near the zenith."
    )
    print(
        f"\n  {'location':<26} {'lat':>9} {'lon':>10} "
        f"{'separation':>12} {'d(elev)':>11} {'d(azim)':>11} {'min zenith':>11}"
    )

    locations = [
        ("Sydney", -33.8688, 151.2093),
        ("Melbourne", -37.8136, 144.9631),
        ("Brisbane", -27.4698, 153.0251),
        ("London", 51.5074, -0.1278),
        ("NREL Golden", 39.742476, -105.1786),
        ("Equator / Greenwich", 0.0, 0.0),
        ("High southern latitude", -64.0, -60.0),
    ]
    times = [
        dt.datetime(2024, month, day, hour, 0, tzinfo=UTC)
        for month in range(1, 13)
        for day in (1, 11, 21)
        for hour in range(0, 24, 3)
    ]

    for name, latitude, longitude in locations:
        result = solar_position(times, latitude, longitude)
        reference = pvlib.solarposition.spa_python(
            pd.DatetimeIndex(times),
            latitude=latitude,
            longitude=longitude,
            altitude=0.0,
            pressure=101325.0,
            temperature=12.0,
            delta_t=67.0,
        )
        ref_elev = np.asarray(reference["apparent_elevation"], dtype=np.float64)
        ref_azim = np.asarray(reference["azimuth"], dtype=np.float64)
        up = ref_elev > 1.0

        d_elev = float(np.max(np.abs(result.elevation_deg - ref_elev)[up]))
        d_azim = float(
            np.max(np.abs(((result.azimuth_deg - ref_azim) + 180.0) % 360.0 - 180.0)[up])
        )

        elev_rad, azim_rad = np.radians(ref_elev), np.radians(ref_azim)
        expected = np.column_stack(
            (
                np.cos(elev_rad) * np.sin(azim_rad),
                np.cos(elev_rad) * np.cos(azim_rad),
                np.sin(elev_rad),
            )
        )
        separation = np.degrees(
            np.arccos(np.clip(np.sum(result.unit_vectors_enu() * expected, axis=1), -1.0, 1.0))
        )
        d_sep = float(np.max(separation[up]))
        min_zenith = float(np.min(90.0 - ref_elev[up]))

        print(
            f"  {name:<26} {latitude:>9.4f} {longitude:>10.4f} "
            f"{d_sep:>12.2e} {d_elev:>11.2e} {d_azim:>11.2e} {min_zenith:>10.2f}d"
        )

    _rule("pvlib SPA cross-check -- the ADG window the tool actually assesses")
    adg_times = list(
        assessment_times(dt.date(2024, 6, 21), "Australia/Sydney", dt.time(9), dt.time(15), 10)
    )
    result = solar_position(adg_times, -33.8688, 151.2093)
    reference = pvlib.solarposition.spa_python(
        pd.DatetimeIndex(adg_times),
        latitude=-33.8688,
        longitude=151.2093,
        altitude=0.0,
        pressure=101325.0,
        temperature=12.0,
        delta_t=67.0,
    )
    ref_elev = np.asarray(reference["apparent_elevation"], dtype=np.float64)
    ref_azim = np.asarray(reference["azimuth"], dtype=np.float64)
    print(f"  Sydney, 21 June, 09:00-15:00 AEST, {len(adg_times)} instants at 10 min")
    print(f"  max elevation error  {np.max(np.abs(result.elevation_deg - ref_elev)):.3e} deg")
    print(
        "  max azimuth error    "
        f"{np.max(np.abs(((result.azimuth_deg - ref_azim) + 180.0) % 360.0 - 180.0)):.3e} deg"
    )
    print(
        f"  solar noon elevation {np.max(result.elevation_deg):.4f} deg, "
        f"azimuth {result.azimuth_deg[int(np.argmax(result.elevation_deg))]:.4f} deg"
    )


if __name__ == "__main__":
    print("core.solar validation report")
    print("=" * 84)
    nrel_worked_example()
    noaa_spreadsheet()
    pvlib_sweep()
    print()
