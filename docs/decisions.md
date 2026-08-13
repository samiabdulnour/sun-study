# Decisions

Domain assumptions matter more than the code here. A wrong one produces confident,
wrong numbers that no amount of testing catches, because the arithmetic is fine — it is
being applied to the wrong windows.

Every decision that changes a headline compliance percentage is recorded here with the
option taken and the reasoning. **Open** decisions are ones the tool cannot sensibly
default; they are listed with a proposed default and must be settled before the
milestone that depends on them.

---

## Settled

### D1 — Solar position: NOAA in-house, pvlib as a dev-only reference

*Milestone M0. Alternatives: pvlib as a runtime dependency; NOAA with no cross-check.*

`core.solar` implements the NOAA algorithm directly, roughly 250 lines and no new
runtime dependency. `pvlib` is installed only in the dev group, where CI uses it to
cross-check a year of positions at seven latitudes against an independent SPA
implementation.

Taking pvlib at runtime would have pulled pandas and scipy into every office install
for a geometry tool, and its pandas-indexed API is an awkward fit for the numpy
`(n_points × n_suns)` ray batching the occlusion engine needs. Keeping it as a
validation reference gets the cross-check without the weight, and mirrors how Ladybug
is treated: a thing we measure against, never a thing we depend on.

Measured accuracy is in [`validation.md`](validation.md).

### D2 — `tzdata` is a runtime dependency

*Milestone M0.*

A pure-data wheel, no code. Python's `zoneinfo` reads the system tz database first and
falls back to this package; Windows ships no system tz database at all, and Windows is
the primary deployment platform. Declared unconditionally rather than Windows-only so
that Linux CI exercises the same code path the office machines will.

### D3 — Timezone is explicit configuration, never inferred

*Milestone M0. Alternative: derive from latitude/longitude via `timezonefinder`.*

The run configuration must name an IANA zone. There is no default and no inference, and
an unknown or missing zone raises `UnknownTimezoneError` rather than falling back.

IFC carries latitude and longitude but no reliable IANA timezone, so something has to
decide that 21 June 09:00 in Sydney means UTC+10. Deriving it from coordinates would
add a dependency with tens of megabytes of boundary data and, worse, make the decision
invisible — exactly the silent-wrong-answer hazard the brief flags around north and
geolocation. An hour of error moves every sun position by 15° of hour angle.

The resolved zone is echoed in the console banner and in every output file header.

### D4 — Azimuth uses the `atan2` form, not NOAA's `arccos` form

*Milestone M0.*

Algebraically identical; NOAA's form divides by `cos(lat) · sin(zenith)`, which is zero
at the poles and at the sub-solar point. `test_azimuth_forms_agree` asserts the two
agree to 1e−9° across a dense grid of latitudes and dates, so the substitution is
verified rather than assumed.

### D5 — Sun vectors use apparent (refracted) elevation

*Milestone M0.*

`SolarPosition` exposes both `elevation_deg` (refraction-corrected) and
`true_elevation_deg` (geometric). `unit_vectors_enu()` defaults to the apparent
position, because refraction is why a low sun's shadow falls where it actually falls.
Near the horizon the two differ by about half a degree, which is roughly one sun
diameter — not negligible for a 9am midwinter assessment. `apparent=False` is available
and the choice will be stated in the output header.

### D13 — `core` stays numpy-only, and the numpy ray caster is the real backend

*Milestone M1. Alternative: `trimesh` + `embreex` as the primary occlusion engine.*

This is a deployment decision. The tool has to run on office Windows workstations
alongside Archicad 26, and a native ray tracer that needs a compiler to install is a
tool that does not deploy. `core.occlusion` is therefore a pure-numpy BVH with no build
step — the production path, not a fallback that only keeps CI green.

The consequence is that its performance is a real concern rather than an academic one,
so it is measured (`scripts/benchmark_occlusion.py`) and the leaf size is tuned from the
measurement rather than guessed. About 38k rays/s on a 96k-triangle scene, roughly 20
seconds for a 200 apartment job. See [`validation.md`](validation.md) §2.2.

The whole of `core` is held to standard library plus numpy, enforced by
`test_architecture.py`. `trimesh` and `ifcopenshell` arrive at M2 confined to `ingest`,
where a failure to install stops geometry loading rather than the analysis engine. An
embree fast path can be added later behind the same `Occluder` interface; nothing will
depend on it existing.

*Still to settle, before M4:* how the tool is actually delivered to a workstation —
`uv tool install` from the repository, a pinned virtual environment, or a frozen
executable. Not urgent, but it should be decided before the Archicad adapter lands
rather than after.

### D14 — Sun vectors are consumed in the model frame, never in ENU

*Milestone M1.*

`core.analysis.sunlit_matrix` takes sun vectors in the same frame as the geometry, and
`core.orientation` is the only thing that converts ENU into that frame. Passing raw ENU
vectors alongside a rotated model is the single easiest way to produce a confident wrong
answer, so the conversion has one home, an explicit derivation in the module docstring,
and cardinal-point tests that pin the sign.

Two consequences worth stating: the model frame must be Z-up, because the below-horizon
test is on the +Z component; and `SiteOrientation` has no default for any field, since a
dataclass default is the easiest way for a guess to become invisible.

---

## Implemented with the proposed defaults — still awaiting confirmation

These are live in the code as `SceneConfig` fields, each echoed in the run banner and
in every output header. They are **defaults, not answers**: the code no longer blocks
on them, but the numbers it prints depend on them being right for the project.

### D6 — What counts as a "living room window"

**This is the one that most changes the headline percentage**, and Archicad will not
tell us. Options: Zone category convention, a window property flag, or a naming
convention.

*Implemented as proposed.* `SceneConfig.living_room_space_names`, default
`("Living Room",)`, matched case-insensitively against the parent `IfcSpace`'s
`LongName` **and** `Name`; a window inherits "living room" from the space it serves.
Override with `--living-room`, repeatable.

Matching both fields matters. Archicad puts the Zone category in `LongName` and the
apartment identifier in `Name`, and an earlier version that checked only `Name` matched
nothing — which reads as *a compliant building with no living rooms*, not as a
configuration error. `test_a_non_matching_room_name_assesses_nothing` keeps that
failure visible, and the run banner always prints the count assessed.

*Also needs deciding:* what happens to a studio apartment where the living space and
bedroom are one Zone, and to a space with no category set. Proposed: fail loudly and
list the offending Zone GUIDs rather than silently assessing or silently skipping.

### D7 — Balcony geometry source

Zones are cleanest, but offices do not always zone balconies. A slab-based fallback
needs a rule for which slabs count.

*Partly implemented.* Slabs whose name starts with a configured prefix (default
`"Balcony"`) are private open space, attached to the apartment they serve, with the
resolution route counted in `Scene.provenance`. The Zone-based route is not built yet;
the fixture has no balcony Zones to develop it against.

Attaching a balcony to its apartment needed more than nearest-neighbour — see
[`validation.md`](validation.md) §5.6 for the equidistance trap that silently gave the
upper storeys no open space at all.

### D8 — Glazing extent

Whole window opening versus glazed area net of frame.

*Implemented as proposed.* The whole `IfcWindow` solid's dominant outward face is
gridded, so the assessed area is the opening rather than the glazed area net of frame.

One limitation worth stating: a curved or heavily faceted window is treated as its
largest flat face, which is wrong for curtain walling. Recorded here rather than
discovered later.

### D9 — Context building extent

Radius cutoff, and whether approved-but-unbuilt developments are included.

*Implemented, default unlimited.* `--context-radius` drops occluders beyond that many
metres, measured from the analysed spaces rather than the file origin, which in Archicad
is often an arbitrary survey point. With no radius given the header says "context radius
unlimited" rather than staying silent. Approved-but-unbuilt developments are whatever
the IFC contains; there is no separate mechanism yet.

### D10 — Vegetation

*Implemented, and it has a published basis.* Vegetation is excluded and the header says
so. This is not merely convention: the NSW technical note defines solar access as
sunlight "without obstruction from other buildings or impediments, **not including
trees**", so excluding vegetation is what the regulation asks for.

---

## Implemented, defaults still awaiting confirmation

### D11 — Sample weighting across the assessment window

**Implemented with the proposed default; still needs your confirmation.**
`core.analysis` offers `TRAPEZOIDAL` (the default) and `UNIFORM`, and the choice travels
on every `SunlightResult`. Switching the default is a one-line change until a ruleset
depends on it.

Not in the brief's list, but found while writing `assessment_times`, and it is the same
class of error.

09:00–15:00 at a 10-minute step yields **37** instants, not 36, because both endpoints
are included. Multiplying a count of sunlit instants by the timestep therefore reports
up to 370 minutes of sun in a 360-minute window — an apartment can come out at 6.17
hours. It is a small bias, always optimistic, and it lands right on the 2-hour
threshold that decides compliance.

*Proposed default:* trapezoidal weighting, so the two endpoints count half. That
preserves the 37 published sun positions, totals exactly 360 minutes, and is the
standard treatment. The alternative — half-open intervals with 36 samples — is also
defensible and simpler; it just discards the 15:00 position.

Whichever is chosen must be stated in the output header, not left implicit.

### D12 — `continuity`: cumulative versus continuous

**Implemented as proposed; still open for confirmation.** `cumulative` is set in
`rules/rulesets/nsw_adg.yaml`, printed in every output header, and carried on every
result record alongside the ruleset name and version. Switching to `continuous` is a
one-line YAML edit and needs no code change, which is proven by a test.

Councils differ; some DCPs require an unbroken duration. It must never be an invisible
assumption, because the same building passes under one reading and fails under the
other.

---

## Settled during massing mode

### D17 — Massing stage measures facade *area*, not apartments

*Alternative: extend the per-apartment metric downward. Not possible.*

The office's massing decks report **"areas on facade get sunlight hours on 21 Jun
>2hrs"** as a percentage, and that is the fitness goal an optimisation run maximises
(29.7% → 38.8% on the reference scheme). It is a share of **square metres**.

The ADG's criterion is a share of *apartments*, and at massing stage there are no
apartments — no Zones, no windows, just a mass. The per-apartment metric therefore
cannot be computed at all, and the area share is not an approximation of it but a
different measurement that happens to use the same 2 hour threshold.

They are never quoted for one another. `sun-study massing` prints an area share and its
header says in words that it is not a compliance figure; `sun-study run` prints the ADG
verdict. The threshold still comes from the ruleset, so both stay anchored to the same
cited number.

### D18 — Samples carry their area, and bands are area-weighted

Every square-metre figure in those decks depends on knowing how much surface each
sample stands for. `SamplePoints` therefore carries `areas`, and `band_by_area` weights
by it rather than counting samples. The two agree only when every sample represents the
same area, which is true of a regular grid on one window and never true of a
triangulated massing.

The band scheme matches the published one exactly, including two details that are
load-bearing: `0hr` is held **separate** from `0–2hrs` (a surface receiving nothing is a
different finding from one receiving forty minutes, and only the former counts for ADG
criterion 3), and the `>2hrs` roll-up is **inclusive** of exactly two hours because the
criterion reads "a minimum of 2 hours".

### D19 — Massing runs default to a 1 m grid

*Alternative: keep the 200 mm developed-model default.*

An optimisation run evaluates hundreds of variants. At 200 mm a facade of roughly
18,000 m² is about 445,000 samples and several minutes per variant — most of a day for
a full run. At 1 m it is about 18,000 samples and a few seconds.

A test asserts the coarse and fine settings agree on the headline share to within 3
percentage points, so the fast setting is not reporting a different building. The
spacing used is printed in the banner and written into every output header, because a
coarse number quoted as a fine one is exactly the failure this project exists to avoid.

---

## Settled during M3

### D15 — Reading the published wording into a per-apartment verdict

*Milestone M3.*

The ADG states the criteria but not how to turn them into a yes or no for one
apartment. Three readings are needed, and because they are readings rather than
regulation they live in a separate `interpretation:` block in the ruleset, are
configurable, and are printed in every output header next to — but visibly distinct
from — the criteria themselves.

**`compliance_requires: both`.** "Living rooms *and* private open spaces of at least
70% of apartments" reads as both having to meet the minimum, so an apartment is
governed by whichever of the two is worse. The fixture's L00-A is exactly this case:
its balcony clears two hours, its living room does not, and it fails.

**`no_sunlight_requires: both`.** Criterion 3 speaks of the *apartment* receiving no
direct sunlight, so it counts only when nothing the apartment has receives any.
`either` would count an apartment whose balcony is in full sun but whose living room is
not, which is a materially harsher rule.

**`apartments_without_open_space: living_room_only`.** Not every apartment has private
open space. Assessing such an apartment on its living room alone is the common reading;
the alternatives are to exclude it from the denominator or to fail it outright, and
which is right is a project-level question. A studio with no balcony is a different
case from one whose balcony never sees the sun, so the two are never collapsed —
`None` and `0.0` stay distinct all the way into the CSV, where the former is blank.

### D16 — There is no `rules/nsw_adg.py`

*Milestone M3. Deviation from the brief's architecture sketch, stated deliberately.*

The brief's §4 layout lists `rules/nsw_adg.py` for "ADG assessment logic", but §5.7
says "the engine reads a ruleset; it does not know what 'ADG' means". Those pull in
different directions and §5.7 is the sharper statement, so the code follows it: the
engine is `rules/assessment.py` and everything ADG-specific is data in
`rules/rulesets/nsw_adg.yaml`.

A module named after one jurisdiction is exactly where the next council's threshold
ends up hardcoded. Adding a DCP that requires three continuous hours should be a new
YAML file and no new code, and a test proves it is.
