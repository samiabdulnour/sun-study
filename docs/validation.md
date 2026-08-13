# Validation

> **The tool is a prototype until the reference comparison in §6 is done.** Nothing it
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

## 3. IFC ingest — status: **done**

`tests/unit/test_ifc_ingest.py`, against a fictional sample building generated by
`tests/fixtures/make_sample_building.py`. The fixture is deliberately awkward in the
ways real Archicad exports are: **millimetre units**, **true north 30° off project
north**, and **one window with no space boundary**.

| Check | Result |
|---|---|
| Declared unit scale | 0.001 (mm) |
| Geometry arrives in | metres — a 2.4 m window measures 2.4 |
| Site elevation | 20.0 m, from a stored 20000 |
| True north bearing | 30.000° |
| Geolocation | −33.75, 151.25 |
| Window → space | 4 of 4 correct: 3 by space boundary, 1 geometric |

### 3.1 Two unit scales, and conflating them is a 1000× error

`ifcopenshell`'s geometry iterator returns vertices **already converted to SI metres**,
whatever the file declares. Raw *attribute* values like `IfcSite.RefElevation` are in
the project's declared units and are **not** converted. So `calculate_unit_scale` must
be applied to attributes and never to geometry.

Applying it to both shrinks the building by a factor of a thousand; applying it to
neither puts the site 20 km up. Both halves are pinned, with failure messages that name
the symptom ("a value near 2400 means the geometry is still in millimetres").

### 3.2 The georeferencing helper defaults silently, so it isn't used for the check

`ifcopenshell.util.geolocation.get_true_north` returns `0` when `TrueNorth` is absent,
and its body swallows exceptions and returns `0` as well. `0` is indistinguishable from
a project genuinely aligned to true north — exactly the invisible "project Y is north"
default §5.3 forbids.

`ingest.ifc` therefore checks for the attribute itself and raises `GeoreferencingError`
naming the Archicad setting to change. The helper is still used for the *conversion*
once presence is established, because its sign convention is documented and tested.
`test_ifcopenshell_helper_would_have_defaulted_silently` pins the behaviour being
avoided, so nobody later "simplifies" the check away.

Three failure modes raise rather than guess: missing `TrueNorth`, missing
`RefLatitude`/`RefLongitude`, and more than one georeferenced `IfcSite` (ambiguous).

### 3.3 North convention, derived rather than assumed

`yaxis2angle` returns "rotate project north **anticlockwise** by this angle to reach
true north". Rotating true north *clockwise* by the same angle therefore lands on
project north, and a clockwise angle from true north is exactly a compass bearing — so
the IFC angle and `SiteOrientation.true_north_bearing_deg` are numerically equal. Pinned
at the cardinal directions in `test_orientation.py`, and end to end by the fixture's 30°.

### 3.4 Window-to-space containment, with the fallback reported

Archicad only writes `IfcRelSpaceBoundary` when space boundary export is enabled, so it
is frequently absent. Windows resolve by boundary where one exists and geometrically
otherwise, and **the count resolved by each route is reported in `Scene.provenance`**. A
silent fallback is how a mis-assigned window becomes a wrong percentage nobody questions.
A window more than 2 m from any space is left `unresolved` rather than attached to
whichever room happens to be nearest.

One live trap, caught by the fixture: Archicad puts the Zone *category* in `LongName`
("Living Room") and the apartment identifier in `Name` ("Apartment L00-A"). Matching
only `Name` finds nothing — and nothing reads as *a compliant building with no living
rooms*, not as a configuration error. `test_a_non_matching_room_name_assesses_nothing`
makes that failure mode visible.

## 4. Golden files — status: **done**

`tests/fixtures/golden_sample_building.json` holds per-apartment minutes for the whole
chain, and CI fails on drift. Regenerate deliberately with `SUN_STUDY_UPDATE_GOLDEN=1`.

| Apartment | Living room | Private open space |
|---|---:|---:|
| L00-A | 106.1 min (1.77 h) | 127.3 min (2.12 h) |
| L00-B | 207.0 min (3.45 h) | 291.1 min (4.85 h) |
| L01-A | 297.2 min (4.95 h) | 256.5 min (4.27 h) |
| L01-B | 360.0 min (6.00 h) | 360.0 min (6.00 h) |

A golden file of four identical numbers would catch almost nothing, so
`test_the_fixture_discriminates` asserts all four differ, that at least one fails the
2-hour threshold, and that the top-floor unshaded apartment sees exactly the full 360
minutes — tying the fixture back to the unobstructed analytic case.

The gradient is produced deliberately: the context block bites into the lower storey and
the A side, and each apartment's window is overhung by the balcony one level up. An
earlier fixture had the context block subtending 47° against a 32.7° peak midwinter
altitude, which blocked every northern ray and made all four apartments identical.

The fixture is byte-for-byte reproducible — GUIDs come from a counter and the SPF header
timestamp is pinned, since IfcOpenShell otherwise stamps the current time.
`test_committed_fixture_matches_its_generator` regenerates it and compares bytes, which
catches both a hand-edited fixture and a generator changed without regenerating.

## 5. Rules and reporting — status: **done**

### 5.1 The thresholds are the published ones

Quoted from the NSW Department of Planning technical note *Solar access requirements in
SEPP 65*, read from the [published PDF](https://www.planning.nsw.gov.au/sites/default/files/2023-03/solar-access-requirements-in-sepp-65.pdf)
rather than from memory. Objective 4A-1 sets three design criteria:

| # | Criterion | Encoded as |
|---|---|---|
| 1 | ≥70% of apartments get ≥**2 h** between 9am–3pm mid winter, **Sydney Metro / Newcastle / Wollongong** | `areas.sydney_metro` = 120 min |
| 2 | **In all other areas**, ≥70% get ≥**3 h** | `areas.other` = 180 min |
| 3 | ≤15% of apartments receive **no** direct sunlight | `maximum_no_sunlight_share` = 0.15 |

**Criterion 2 is easy to miss.** A tool that only knows the 2-hour figure passes
buildings outside Sydney Metro that should fail, so both are encoded and `--area`
selects between them.

Mid winter is 21 June, stated in the note itself: *"measured at mid winter (21 June) as
this is when the sun is lowest in the sky... the 'worst case' scenario"*.

Every threshold carries its citation, and that is **enforced by the schema** — a
ruleset with a blank citation fails to load. A number in a compliance tool that nobody
can trace to a published document is worse than no number at all.

Vegetation exclusion (D10) turns out to have a published basis rather than being mere
convention: *"Solar access is the ability of a building to receive direct sunlight
without obstruction from other buildings or impediments, **not including trees**."*

### 5.2 The engine does not know what the ADG is

Thresholds, the window, the continuity setting and the citations all live in
`rules/rulesets/nsw_adg.yaml`. `rules/assessment.py` reads a validated `Ruleset` and
knows only about durations and shares.

There is deliberately **no `nsw_adg.py`**. The brief's architecture sketch listed one,
but §5.7 is the sharper statement of the same idea — *"the engine reads a ruleset; it
does not know what 'ADG' means"* — and a module named after one jurisdiction is exactly
where a threshold ends up hardcoded. `test_changing_a_threshold_is_a_data_edit_not_a_code_change`
proves the point by inventing a council DCP requiring 3 *continuous* hours, entirely in
YAML, and checking a building that passes the ADG fails it.

Unknown keys are rejected too, so a misspelled `continuty:` fails loudly instead of
being silently ignored while the author believes they changed a setting.

### 5.3 Interpretation is separated from the criteria

Three readings are needed that the published wording does not settle. They are choices,
so they sit in a separate `interpretation:` block, are reported in every output header,
and are recorded as decision D15:

- **`compliance_requires: both`** — "living rooms *and* private open spaces" reads as
  both having to meet the minimum, so an apartment is governed by whichever is worse.
- **`no_sunlight_requires: both`** — criterion 3 speaks of the *apartment* receiving no
  sunlight, so it counts only when nothing it has receives any.
- **`apartments_without_open_space: living_room_only`** — a studio with no balcony is a
  different case from one whose balcony never sees the sun, and collapsing them fails
  it for the wrong reason.

### 5.4 Results carry their assumptions

Both exports carry the disclaimer, the ruleset identifier and version, all citations,
the continuity and weighting settings, the resolved site and north bearing, and the
scene provenance. A results file separated from those is not a weaker record — it is an
unreproducible one that looks exactly like a good one.

The CSV writes them as `#` comment lines above the table, because a CSV gets opened in
Excel, pasted into a report and emailed on. A missing open space is written **blank,
not zero**: no balcony and a balcony in permanent shade are different findings.

### 5.5 End to end on the fixture

`sun-study run tests/fixtures/sample_building.ifc --timezone Australia/Sydney`:

| Apartment | Living room | Open space | Governing | Verdict |
|---|---:|---:|---:|---|
| L00-A | 106.1 | 127.3 | 106.1 | fail |
| L00-B | 207.0 | 291.1 | 207.0 | pass |
| L01-A | 297.2 | 256.5 | 256.5 | pass |
| L01-B | 360.0 | 360.0 | 360.0 | pass |

**3/4 = 75% ≥ 70% target, and 0% ≤ 15% dark cap — complies.** L00-A is the useful case:
its balcony clears two hours but its living room does not, and `compliance_requires:
both` correctly governs it on the living room.

Note the fixture does **not** discriminate between the 2-hour and 3-hour criteria — no
apartment's governing figure falls in the 120–180 band — so the area setting is covered
by a behavioural test rather than by the fixture. That is stated in the test so nobody
misreads it as coverage.

### 5.6 One assignment bug worth recording

Balconies are parented to the apartment they serve so window and open-space results
join on one key. Nearest-bounding-box gets this wrong in a way that looks right: a
balcony sits at its own apartment's floor level, which is *also* flush against the
ceiling of the apartment below, so the two are **exactly equidistant** and the winner
is decided by iteration order. In the fixture that attributed both upper balconies to
the ground-floor apartments and left the upper floors with no open space at all.

The disambiguator is vertical — the apartment stands on top of its balcony, so its
floor should be level with the slab's top surface. Where nothing qualifies the nearest
space is used and the fallback is counted rather than hidden.

---

## 6. Reference comparison against Ladybug — status: **in progress** (M6)

### 6.1 Geometry reading verified against a published figure

The reference project's Rhino model was read with `ingest/rhino.py`. Its residential
tower facade — the surface the published study analysed — measures:

| | Area |
|---|---:|
| Computed from the model's Brep render meshes, vertical faces only | **17 780.01 m²** |
| Published in the study's own summary | **17 780.02 m²** |

A relative difference of 6 parts in ten million, i.e. rounding. That establishes three
things at once: the Brep render-mesh extraction is faithful, the unit handling is right,
and the 30° vertical-face filter selects exactly the surface Ladybug analysed. 25 825 of
25 845 Brep faces carried a cached mesh (99.92%); the 20 without are counted and
reported.

### 6.2 The reference result decoded per band

The model carries the Ladybug output as seven colour-mapped meshes. Reading their vertex
colours and integrating area per colour reproduces the published table **exactly**:

| Colour | Decoded | Published | Band |
|---|---:|---:|---|
| `#08306B` | 7923.71 | 7923.71 | 0hr |
| `#4DB6AC` | 3187.20 | 3187.20 | 1–2hrs |
| `#F4511E` | 2532.10 | 2532.10 | >5hrs |
| `#FFB74D` | 2409.75 | 2409.75 | 4–5hrs |
| `#FFD54F` | 641.55 | 641.55 | 3–4hrs |
| `#E6EE9C` | 601.59 | 601.59 | 2–3hrs |
| `#2B7ABF` | 484.12 | 484.12 | 0–1hr |

This is a **per-face ground truth**, not a summary comparison: every triangle of the
analysed facade carries the band Ladybug assigned it. The comparison can therefore be
made face by face rather than on totals, which would let a positive and a negative error
cancel.

### 6.3 Blocked on site parameters

None of the three Rhino files carries a location. `EarthAnchorPoint` is unset on all of
them and `ModelNorth` is Rhino's default `(0,1,0)`, which is indistinguishable from a
deliberate setting. The model sits in local coordinates near the origin, so there is no
survey grid to convert from either. Latitude, longitude and true north live in the
Grasshopper definition and must be supplied before the comparison can run.

They will not be inferred by fitting them until the bands agree — that would tune the
inputs to the answer and destroy the value of the check.

### 6.4 Still to do

One real project run through both this tool and the existing Grasshopper/Ladybug
script, with per-apartment differences recorded, a tolerance stated, and every outlier
explained.

**Until §6 is complete, the tool is a prototype and the README says so.** This is the
only check that tests the whole chain against something the office already trusts.
Everything above can pass while the tool still samples the wrong surfaces: the fixture
proves the code does what it was told, not that it was told the right thing.
