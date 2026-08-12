# Validation

> **The tool is a prototype until the reference comparison in §4 is done.** Nothing it
> prints is a submission number until then, and the README, the CLI banner and every
> exported file say so.

The failure mode of a sunlight-hours tool is being *plausibly* wrong. A shadow diagram
that is 40 minutes optimistic looks exactly like one that is correct. So every layer is
checked against something external, and the checks run in CI rather than living in
someone's memory of having once eyeballed it.

Regenerate the numbers below with:

```bash
uv run python scripts/validation_report.py
```

---

## 1. Sun position — status: **done**

Two independent published sources, both transcribed from the primary documents rather
than from memory or from a third-party reimplementation.

### 1.1 NREL/TP-560-34302 — absolute accuracy

Reda, I. and Andreas, A., *Solar Position Algorithm for Solar Radiation Applications*,
NREL, revised January 2008. <https://www.nrel.gov/docs/fy08osti/34302.pdf>

This is the **strong** check. SPA is a different and more accurate algorithm (±0.0003°,
valid −2000 to 6000) than the NOAA one implemented here, so agreement demonstrates
correctness in absolute terms rather than merely reproducing our own arithmetic.

**Table A4.1 — Julian Day.** All 16 published values reproduce exactly, including the
Julian-calendar cases and negative years down to 1 January −4712 → JD 0.0.

**Table A5.1 — worked example.** 17 October 2003, 12:30:30 at UTC−7, latitude
39.742476°, longitude −105.1786°, elevation 1830.14 m, pressure 820 mbar, 11 °C,
ΔT 67 s.

| Quantity | Computed | Published | Residual |
|---|---:|---:|---:|
| Julian day | 2452930.312847222 | 2452930.312847 | +2.2e−07 d |
| Apparent longitude | 204.012352993° | 204.008553753° | +3.8e−03° |
| Right ascension | 202.230963352° | 202.227410° | +3.6e−03° |
| Declination | −9.315803745° | −9.314340° | −1.5e−03° |
| Observer hour angle | 11.108052447° | 11.105900° | +2.2e−03° |
| Equation of time | 14.646609789 min | 14.641503 min | +5.1e−03 min |
| **Zenith** | **50.108637857°** | **50.111620°** | **−3.0e−03°** |
| **Azimuth** | **194.342582861°** | **194.340240°** | **+2.3e−03°** |

**Against the 0.1° milestone criterion: zenith is inside by a factor of 34, azimuth by
a factor of 43.**

The residual is not noise, and it is worth naming rather than waving at. NOAA is
geocentric while SPA is topocentric, so solar parallax (up to 0.0024°) is absent; NOAA
also ignores ΔT, placing the ephemeris at UT rather than TT, worth about 8e−04° of
solar longitude. Both are far below the tolerance and neither grows with time within
the tool's working range. Correcting them would mean implementing full SPA, which buys
nothing here: the geometry tolerances downstream — a 200 mm sample grid, a 50 mm
surface offset — dominate this by orders of magnitude.

### 1.2 NOAA reference spreadsheet — transcription

`NOAA_Solar_Calculations_day.ods`, from <https://gml.noaa.gov/grad/solcalc/calcdetails.html>

This is the **exact** check. It is the *same* algorithm, so it must agree to floating
point, and it catches a single mistyped digit in a transcribed coefficient — the kind
of error a 0.1° tolerance would hide completely.

Spreadsheet inputs: latitude 40°, longitude −105°, timezone −7, 21 June 2010, row 2 at
00:06:00 local. Expected values are the spreadsheet's own stored cell values.

| Quantity | Cell | Residual |
|---|---|---:|
| Julian day | F2 | +3.3e−09 d |
| Right ascension | S2 | −5.8e−12° |
| Declination | T2 | −4.6e−14° |
| Equation of time | V2 | +1.2e−12 min |
| True elevation | AE2 | +1.4e−14° |
| Refraction | AF2 | −5.0e−17° |
| Apparent elevation | AG2 | −6.4e−14° |
| Azimuth | AH2 | +4.8e−13° |

Agreement at machine precision. The transcription is exact.

### 1.3 pvlib SPA cross-check — coverage

The worked example proves one instant. This proves there is no region of the input
space where the implementation quietly falls apart: a full year at 3-hour resolution,
at seven latitudes, against pvlib's independent implementation of full SPA.

`pvlib` is a validation reference only. It is never a runtime dependency, and a CI job
installs the package without it to keep that boundary from rotting.

Worst error with the sun above 1°. *Separation* is the angle between the two sun
direction vectors, which is the quantity a ray cast actually depends on.

| Location | Separation | Δ elevation | Δ azimuth | Min zenith |
|---|---:|---:|---:|---:|
| Sydney | 1.10e−02° | 9.86e−03° | 2.47e−02° | 17.31° |
| Melbourne | 1.13e−02° | 9.55e−03° | 2.58e−02° | 16.73° |
| Brisbane | 1.10e−02° | 1.04e−02° | 2.35e−02° | 15.66° |
| London | 1.07e−02° | 8.14e−03° | 1.32e−02° | 28.07° |
| NREL Golden | 1.31e−02° | 1.19e−02° | 1.43e−02° | 20.95° |
| Equator / Greenwich | 1.32e−02° | 1.32e−02° | 8.67e−02° | 1.82° |
| High southern latitude | 1.15e−02° | 7.67e−03° | 1.25e−02° | 41.68° |

Separation is a uniform 1.1–1.3e−02° everywhere — about 8× inside the tolerance and
notably flat across latitude, which is what you want to see.

**On the equator's Δ azimuth of 8.67e−02°.** This is an artifact of the coordinate
system, not an error in the position. At the equator the sun passes within 1.82° of the
zenith, where azimuth is ill-conditioned: near the pole of the horizontal coordinate
system a 0.013° change in position swings the bearing by nearly 0.1° while moving the
sun almost nowhere. The separation column for that same row is 1.32e−02°, in line with
every other location. The test asserts on separation for exactly this reason, and
checks raw azimuth only below 85° elevation — asserting on azimuth near the zenith
would measure the singularity rather than the algorithm.

### 1.4 The window that actually matters

Sydney, 21 June, 09:00–15:00 AEST, 37 instants at 10 minutes:

- max elevation error **7.96e−04°**
- max azimuth error **8.18e−04°**
- solar noon elevation **32.7134°**, azimuth **359.1816°**

Two sanity checks on that last line, both independent of the code. Peak elevation
should be `90 − |latitude − declination|` = `90 − |−33.8688 − 23.44|` ≈ 32.7°, and it
is. And the midwinter noon sun sits at azimuth 359.18° — **in the north**, as it must
in the southern hemisphere. If the north handling were inverted this would read ~180°.
That assertion is `test_sydney_midwinter_noon_sun_is_in_the_north`.

### 1.5 Timezone

`test_sydney_21_june_is_aest_not_aedt` asserts that every instant in the 21 June window
carries a UTC+10 offset and that 09:00 AEST maps to 2024-06-20 23:00 UTC. NSW does not
observe daylight saving in June; a fixed +11 offset, or a tz database that silently
failed to load, would shift every sun position by an hour. The test runs on Windows in
CI as well as Linux, because Windows has no system tz database and depends on the
`tzdata` package being present.

---

## 2. Geometry and occlusion — status: **not started** (M1)

The analytic cases required before the occlusion engine is trusted:

- Unobstructed horizontal point → exactly the full window duration.
- Unobstructed vertical surface facing due north (southern hemisphere) → substantial
  midwinter sun; facing due south → zero. The tripwire for inverted north handling.
- Single vertical pole of known height on a flat plane → shadow length and bearing at a
  given instant, against closed-form trigonometry.
- Box with a known overhang → shadow line on the wall below at solar noon, against hand
  calculation.
- Rotate the scene *and* true north together → results invariant.
- Rotate the scene *without* north → results change by exactly the rotation.

## 3. Golden files — status: **not started** (M3)

Expected outputs for the fixture models, committed, with CI failing on drift.

## 4. Reference comparison against Ladybug — status: **not started** (M6)

One real project run through both this tool and the existing Grasshopper/Ladybug
script, with per-apartment differences recorded, a tolerance stated, and every outlier
explained.

**Until this section is filled in, the tool is a prototype and the README says so.**
This is the only check that tests the whole chain — ingest, north, sampling, occlusion,
aggregation and rules together — against something the office already trusts. Sections
1 to 3 can all pass while the tool still samples the wrong windows.
