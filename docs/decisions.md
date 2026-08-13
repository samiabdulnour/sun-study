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

### D20 — `rhino3dm` as a validation-only reader

*Alternative: compare against an IFC re-export of the same scheme.*

The reference Ladybug study was run on `.3dm` geometry. Comparing against an IFC
re-export of it would introduce geometry differences that appear as a systematic
offset — indistinguishable from an engine error, in the one check the project's
credibility rests on. So `ingest/rhino.py` reads the 3dm directly.

`rhino3dm` sits in the dev group beside `pvlib`, is imported lazily, and is a pure
openNURBS wheel needing no Rhino installation or licence. `core` stays numpy-only. The
product still ingests IFC; this is not a second supported input.

Two things that took finding. Rhino stores NURBS Breps, which `rhino3dm` cannot
tessellate — but it *can* reach the render mesh Rhino already cached, and the API is on
`BrepFace.GetMesh`, not on `Brep`. And a fresh `File3dm()` defaults to **millimetres**,
so writing metre coordinates without setting the unit system makes every length 1000×
too small and every area a million times too small.



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

### D21 — Results are written as `string`, not `boolean`, property values

*Milestone M5. Alternative: declare the pass/fail columns `boolean` and guess the literal.*

`SetPropertyValuesOfElements` takes a **display string** and lets Archicad parse it into
the property's declared type. For numbers that is safe and checkable: Tapir's
`PropertyConversionUtils` (in `PropertyCommands.cpp`) hard-codes a `.` decimal
delimiter, metres, square metres and decimal degrees, so `"2.35"` means 2.35 hours
whatever the project's unit preferences are set to. That is read out of the source, not
assumed.

Nothing anywhere states what string a **boolean** property parses from — `"true"`,
`"1"`, a localised `"Yes"`, or something else. A wrong guess does not error; it either
refuses the value or, worse, lands the opposite of the truth in a compliance column. So
`Meets Minimum`, `No Direct Sunlight` and `Counted in Compliance` are `string`
properties holding `Yes`/`No`. A string set from a string cannot be misparsed, and the
columns still sort and filter in an Archicad schedule.

`test_no_property_is_declared_boolean` holds this in place. It can be revisited once
someone at a workstation confirms the literal — that is on the checklist in
[`archicad.md`](archicad.md).

### D22 — Geometry travels by IFC; the JSON API is used for everything else

*Milestone M4. Alternative: read geometry over the Tapir API and skip the export.*

Tapir can return element geometry, so a direct read is possible. It is still the wrong
choice. The IFC path is the one covered by a committed fixture, a golden file and a
validated end-to-end comparison; a second geometry route would need all of that
duplicated, and would silently diverge from the first the moment an Archicad IFC
translator setting changed. The export is also the same file the office already
produces by hand, so what the tool analyses is what a colleague can open and check.

The consequence is that Archicad's own georeferencing is not the source of truth —
the IFC's is. That is deliberate, and it is what makes the north cross-check possible:
two independent statements of the same fact, with disagreement fatal.

### D23 — Archicad's north angle, and why the cross-check compares sums

*Milestone M4. Opened as an assumption; **closed by measurement** on an Archicad 26
project.*

`GetGeoLocation` returns `north` in radians and documents nothing else. The add-on
sources, the JSON schemas and the Grasshopper components are all silent on whether the
angle runs clockwise or anticlockwise, and from which axis. This shipped as a named
guess, `ASSUMED_NORTH_SENSE`, deliberately load-bearing on nothing.

A real export settled it. Three independent numbers out of one nine-storey AC26 model:

| Source | Value |
|---|---|
| `GetGeoLocation` | `north = 0.856118 rad` = 49.0518° |
| `IfcSite` `RefDirection` | `(0.755304, 0.655374)` = `(sin 49.0518°, cos 49.0518°)` |
| Walls, measured in world coordinates | 40.948° = 90° − 49.0518° |

So **`placeInfo.north` is the angle of true north measured counter-clockwise from the
project +X axis, in radians**, and the bearing of project +Y is `degrees(north) − 90`.
The mirrored convention predicts walls at 130.9°; they were at 41°. Two further checks
agree: the offset makes Archicad's default north π/2 rather than 0, which is what
"true north runs along project +Y" ought to report, and substituting all three numbers
into the cross-check balances to 1.6 × 10⁻⁵ degrees — the rounding on six decimal
places of radians.

**The same file exposed a false positive in the cross-check**, which matters more than
the constant did. Archicad reports north in the *project* frame. An IFC export need not
be in that frame: the "Survey Point" model position rotates the geometry through
`IfcSite`'s placement and then writes `TrueNorth` as `(0,1)`, because the world
coordinates it produces genuinely are north-aligned. Comparing Archicad's angle against
`TrueNorth` alone rejected a completely correct export — 49° against 0°.

What is comparable is the total rotation from project frame to true north:

```
project +Y bearing  ==  TrueNorth bearing  -  site placement rotation
```

That identity holds under both model-position options without having to detect which
was used, because whichever half of the file carries the angle, the sum is unchanged.
`ingest.ifc` therefore records `site_rotation_deg` alongside `true_north_bearing_deg`.

**The analysis still uses `TrueNorth` alone**, and must: it reads world coordinates,
which already have the site placement baked in. Adding the rotation there would count
it twice. `test_the_site_rotation_does_not_reach_the_analysis` pins that, and
`test_a_survey_point_export_is_not_reported_as_a_mismatch` pins the false positive
using the measured numbers above.

### D24 — Living-room glazing can be marked on the opening, not the room

*Milestone M4. Extends D6 rather than replacing it; both routes ship.*

D6 identifies a living room by naming the Zone. That works when a practice zones by
*room*. It cannot work at all when a practice zones by **unit**, which is common: there
is no living-room Zone to match, and matching the unit Zone would count every window in
the apartment — bedrooms, bathroom, kitchen — as living-room glazing. That does not
fail. It returns an optimistically wrong number, which is the exact failure this project
exists to prevent.

`SceneConfig.livable_opening_suffix` is the second route. An opening whose ID ends with
the configured marker is living-room glazing, and the space it serves becomes an
apartment. One practice uses `_L`; the tool takes whatever suffix it is given and echoes
it on every run.

Three details, each learned from a real export rather than assumed:

**Doors are in the default `livable_opening_classes`.** A living room's glazing is
usually a balcony slider, which Archicad models with the Door tool. In the reference
project 110 of 252 marked openings were `SD2.x_L` doors — reading windows alone would
have measured 44% less glass and reported it as the whole.

**The marker must be a genuine suffix.** The same practice's library contains
`D06L_Bathroom cavity slider_Livable`, where the trailing `L` means something unrelated.
Matching a bare final letter would sweep in a bathroom door.

**`_read_space_boundaries` had to learn about doors too**, or every marked slider would
fall through to the geometric fallback.

**What the marker means, confirmed by the practice that uses it:** `_L` marks the
openings of *rooms that require sunlight* — habitable rooms. It is a **room-level**
marker and is unrelated to the unit-level `Liveable` / `Adaptable/Livable` properties in
the same project, which record the accessible-housing standard.

**That is wider than ADG 4A-1, which is about living rooms specifically**, and the
difference is not academic. An apartment whose bedroom faces north and whose living room
faces south passes on the bedroom's sun if the two are pooled. The arithmetic in the
reference project points the same way: 252 marked openings across roughly 91 apartments
is 2.8 each, where living-room-only would give one or two — a slider and perhaps a
window — and living-plus-bedrooms predicts about 257.

So the tool reports the distribution rather than assuming either reading.
`_openings_per_apartment` histograms how many marked openings each apartment received
and prints it in the run banner, because that single line says which criterion the
result actually answers and it is invisible in a compliance percentage. Two openings per
apartment is a living-room convention; three or four in a two-bedroom apartment is a
habitable-room one.

Narrowing habitable-room glazing down to living rooms needs a second signal the model
does not yet carry. The balcony slider is a good proxy — the room you step out from is
the living room in nearly every apartment — but it is a proxy, and it is not offered as
a default.

### D25 — Scene selection can key on Archicad layers

*Milestone M4.*

Practices run their modelling standards on layers. A layer matrix says *"all 3D context
elements outside the site boundaries go here"* and *"unit zone duplicate, used to
schedule SEPP 65"*; it promises nothing about what any individual object is called. So
selecting by name asks the wrong question, and a neighbouring building imported from a
survey will not be named "Context".

Layers survive Archicad's IFC export. `IfcPresentationLayerAssignment` points at
representations rather than at products, so the mapping is walked backwards — verified
on a real export, 18,202 products resolved, names verbatim (`01 | Wall.External`).

Three selections can now be keyed on them: which zones are apartments, which are private
open space, and which elements are context. The first matters most: a real project
carries unit zones, GFA zones, NLA zones, storage zones and a SEPP 65 duplicate set, and
assessing all of them inflates the compliance denominator without looking wrong.

**Matching is strict, and a filter that selects nothing is fatal.** Layer names carry
punctuation nobody reproduces from memory — the real project's `06 | Zone.Units` typed
as `06|Zone.Units` matches nothing — and quietly selecting zero zones would report a
building with no apartments as a result rather than as a typo. `SceneConfigError` names
what was asked for and lists the layers the file actually contains. Loose matching was
considered and rejected: a visible failure is cheaper than two layers silently
collapsing into one.

**Zones and slabs are gridded differently, and getting this wrong is invisible.** A
balcony slab is a solid whose walking surface is its top face. A balcony Zone is a
*void*, and its top face is the underside of whatever is above — gridding that puts
every sample a metre into the storey overhead and still returns plausible hours. A Zone
is therefore gridded on its floor, with the sample normals flipped back up.

### D26 — The diagram is drawn natively, on the floor plan, on its own layer

*Milestone M5. Alternative: render an image and place it on the sheet.*

A number in a schedule is the record; a coloured plan is what gets looked at. So the
tool draws one — but as Archicad elements, not as a picture.

`CreateHatches` (Tapir 1.5.7) takes a polygon, and `GetDetailsOfElements` returns each
Zone's `polygonOutline`, so the fill is the apartment's real shape rather than a
bounding box. Each lands on the Zone's own `floorIndex`, which gives a per-storey
diagram set rather than one flattened plan. An exported image would print at one scale,
ignore the pen table, and be unfixable by the person holding the drawing.

**Pens, not colours, and that is a feature.** `CreateHatches` takes pen *indices*. A
practice runs a pen table — `00 FA Pens` in the reference office — and a diagram drawn
from it stays consistent with everything else on the sheet, where an imported analysis
palette would not. So band-to-pen is configuration, the defaults are a stated guess, and
the run echoes the mapping it used with a note to check it. A wrong pen index produces a
plausible diagram in the wrong colours, which nothing downstream catches.

An override naming a band that does not exist is an error rather than a no-op: `--pen
'2-3 hours=42'` — "hours", not "hrs" — would otherwise draw the defaults and look
entirely correct.

**Re-running replaces.** Everything goes on one dedicated layer and the previous run's
Hatches and Texts are deleted first. Without that a second run doubles up: the new fills
land exactly on the old, the plan looks unchanged, and the stale colours underneath are
what print if the top layer is ever hidden. Deletion is scoped to the two element types
this tool creates, so anything else a person put on the layer survives.

**The join is shared with the write-back.** `match_apartments` runs once and feeds both
the property values and the fills. Two independent joins over the same data would agree
almost always, and the time they did not would be a diagram whose colours belonged to
the neighbouring apartments.

Two limits, reported rather than hidden. `CreateHatches` takes a single contour, so an
apartment wrapping a lift core is drawn over the void — the run says how many. And
curved zone edges become straight segments between their nodes.

**Version gating is separate.** `CreateHatches` needs 1.5.7 where the rest of the
package needs 1.5.1, so `require_tapir_at_least` gates drawing on its own. Someone who
wants the numbers should not be blocked by a picture they did not ask for.

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
