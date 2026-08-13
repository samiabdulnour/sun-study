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

## 2. Geometry and occlusion — status: **done**

All six analytic cases are green in CI (`tests/unit/test_analytic_cases.py`). Each has
an answer derivable with trigonometry on paper, so a failure names a specific broken
assumption rather than reporting that the numbers moved.

| # | Case | Result |
|---|---|---|
| 1 | Unobstructed horizontal point | exactly **360.0** min over a 360 min window |
| 2 | North-facing wall, Sydney midwinter | **360.0** min — full window |
| 2 | South-facing wall, Sydney midwinter | **0.0** min |
| 3 | Vertical pole, shadow length and bearing | tip within **1 cm** of closed form, at 5 elevations |
| 4 | Overhang, shadow line on the wall | boundary within **1 cm** of hand calculation, at 4 elevations |
| 5 | Rotate scene **and** north together | invariant to **1e−9** min, at 4 angles |
| 6 | Rotate scene **without** north | changes; a half turn takes 360 min → 0 |

Cases 3 and 4 use hand-specified sun vectors at exact azimuths and elevations rather
than real solar positions. The astronomy is already validated in §1; mixing it in would
mean a shadow-length assertion could fail for a reason unrelated to shadows.

**Two things worth recording, because both nearly hid a weak test.**

*The pole is not a line.* A 100 mm square pole's shadow reaches further than an
idealised zero-width pole's by exactly the distance from its axis to its face along the
solar bearing, `min(w/|sin A|, w/|cos A|)` = 0.0532 m at the tested bearing. Bisecting
the real boundary gives 14.334689 m against a closed form of 14.281480 + 0.0532 =
14.334689 m — exact. The first version of the test used a ±2% tolerance that absorbed
this, and would also have passed with a 2%-wrong shadow. The closed form now includes
the term and the tolerance is 1 cm on a 14 m shadow.

*The overhang boundary is exact.* Bisecting gives 2.422649731 m against a hand
calculation of `3.0 − 1.0·tan(30°)` = 2.422649731 m, to machine precision.

**Case 2 is the tripwire.** A south-facing facade in Sydney receives no direct
midwinter sun at all. A tool reporting otherwise has swapped a sign between the solar
azimuth and the model frame, and every figure it produces is wrong in a way that looks
entirely plausible.

### 2.1 The ray caster

`tests/unit/test_occlusion.py` checks the BVH differentially: it must agree with brute
force **exactly**, on randomised scenes, at triangle counts straddling the leaf size
(1, 2, 7, 8, 9, 31, 32, 33, 250, 1200) and at every leaf size from 1 to 64. Brute force
shares the intersection kernel, so that isolates the traversal; the kernel itself is
checked against hand-computed ray/panel cases, including axis-parallel rays, rays along
the diagonal shared by two triangles, and back-face hits.

### 2.2 Performance

The ray caster is pure numpy with no native extension — see decision D13. That makes it
the production backend on a Windows workstation, not a CI fallback, so its speed is a
correctness-adjacent concern and is measured rather than assumed.

`uv run python scripts/benchmark_occlusion.py`, on a 96,000-triangle scene with 12,600
sample points across 37 sun positions (201,600 rays after back-facing pairs are
skipped):

| Leaf | Nodes | Build (s) | Solve (s) | krays/s |
|---:|---:|---:|---:|---:|
| 8 | 32,767 | 1.12 | 6.59 | 31 |
| 16 | 16,383 | 0.81 | 5.58 | 36 |
| **32** | **8,191** | **0.59** | **5.33** | **38** |
| 64 | 4,095 | 0.55 | 5.96 | 34 |
| 128 | 2,047 | 0.45 | 8.19 | 25 |

32 is the default: the flat part of the curve, and half the build time of 8. A 200
apartment job is roughly 800k rays, so about **20 seconds**. That is comfortably inside
what design-stage iteration needs, and it is the figure to re-check when real IFC
geometry replaces synthetic boxes at M2.

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
