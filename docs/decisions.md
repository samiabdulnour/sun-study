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
palette would not. Which pen carries which band is then read out of the project itself
rather than configured; see [D27](#d27--the-pen-is-derived-from-the-colour-not-configured).

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

### D27 — The pen is derived from the colour, not configured

*Milestone M5. Supersedes the "defaults are a stated guess" half of [D26](#d26--the-diagram-is-drawn-natively-on-the-floor-plan-on-its-own-layer).*

A pen index means nothing outside the pen table it came from. Pen 92 is a mid blue in
one office and a hairline black in the next, so any hard-coded default is guaranteed
wrong in somebody's project — and wrong in the worst way, because it draws a complete,
plausible diagram in colours nobody chose. Nothing downstream catches that. Asking every
new user to supply seven `--pen` numbers before their first run is the other way to be
wrong: it makes the tool feel broken out of the box.

The colour is the part that everybody already agrees on. The seven band colours were
decoded from the reference study's own legend during the Ladybug validation — by
integrating area per colour until the published table reproduced exactly — so they are
measured, not chosen. So the colour is the input and the pen is looked up: read the
project's active pen table over `GetPenTables`, and give each band the nearest pen by
Euclidean RGB distance.

**The distance is reported, because `min()` always answers.** A palette with no yellow
in it still returns *a* pen for the 3–4 hour band, and the only sign that the answer is
poor is how far it had to reach. Each run prints the mapping with a quality label —
`exact`, `close`, `POOR MATCH` — and a poor match names the `--pen` override that fixes
it. Explicit overrides are applied after matching, so one band can be corrected without
losing the other six.

**The assignment is one-to-one, and that took a real project to notice.** Matching each
band independently is the obvious implementation, and on the first office pen table it
put the 3–4 and 4–5 hour bands both on pen 124: their reference colours are only 30
apart, and the palette had a single amber nearest to both. Distinct bands drawn in one
colour make the plan unable to show where the four-hour line falls — and it looks
finished, so nothing prompts a second look. So pens are claimed globally: every band–pen
pairing is ranked by distance, the closest wins, and that band and that pen both leave
the pool. Greedy rather than optimal — with seven bands it is within a hair of the best
assignment, and "closest pairing first" is a rule a person can follow when checking why
a band got the pen it did.

Where there are fewer pens than bands the tail keeps its default rather than reusing a
pen. An incomplete mapping that says so beats a complete one that hides a boundary.

**Distinct pens are still not distinct colours.** A palette can hold two ambers a hair
apart, and a one-to-one assignment will use both — technically correct, unreadable at
the boundary. So a separate check reports any two bands whose assigned pens are closer
than `INDISTINGUISHABLE_RGB`. That threshold is 30, measured rather than chosen: it is
the tightest adjacent pair in the reference legend itself, so anything closer is a
finer distinction than the published study asks a reader to make.

Plain Euclidean RGB, not a perceptual metric. The job is picking the obvious match out
of a palette of a few hundred, not ranking near-misses, and a metric nobody can compute
in their head is harder to argue with when it is wrong.

Two failure modes are deliberately not fatal. A project that lists no pen table warns and
keeps the guessed defaults rather than refusing to draw. And when several pen tables
exist but the build will not say which is active, the first is used — not knowing is a
worse reason to stop than drawing from the table that is almost always the only one.

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

### D28 — The sheet is made through the View Map, not by drawing into a worksheet

*Milestone M5. The last step of the practice's own described sequence.*

The workflow as described ends "copy everything in a worksheet, the plan and create
legend in worksheet". Taken literally that means creating the fills *inside* a Worksheet
database, and Archicad's API will not do it: `CreateHatches` takes a layer and a storey
but no database, so it draws into whatever is currently active. There is no
`SetActiveDatabase` in the add-on. The only way to honour the literal reading is to ask a
person to open the worksheet first and then run the tool — an implicit precondition that
fails silently, drawing an apartment diagram into whatever happened to be on screen.

So the fills stay on the floor plan, where the practice already agreed they could live
("it can be in a floor plan of course in own layer"), and the sheet is made from that:

    GetNavigatorItemTree     1.1.7   find the storeys that carry fills
    CloneProjectMapItemToViewMap
                             1.1.7   a View per storey, to place from
    CreateLayout             1.4.0   the sheet
    CreateDrawings           1.4.0   the plan on the sheet, at a stated scale

**This is better than the literal reading, not merely possible.** A Drawing placed from
a View stays *linked*: re-run the study, the fills change, and the sheet updates itself.
Geometry copied into a worksheet is a snapshot, and a snapshot of a compliance diagram is
the exact failure this whole tool exists to remove — a picture that was true when it was
made and silently is not any more.

**Storeys are matched on the floor number, not the name.** A Story navigator item carries
its floor number in `prefix`, which is the same integer `DrawReport.storeys` reports.
Storey naming is a practice's own business — `Level 08`, `L08`, `08 RESIDENTIAL` — and a
tool matching on it finds nothing the first time a project names them differently.

**Nothing is deleted and nothing existing is edited.** A re-run creates a *new* Layout.
A sheet can carry a title block, notes and a revision history that nobody wants
regenerated, so tidying up is a person's decision; the run names what it made so there is
something to tidy up by. This is the opposite of the drawing layer's policy, and
deliberately: that layer is the tool's own and nothing else goes on it, while a Layout is
shared ground.

**A failed sheet does not fail the run.** By the time layouts are attempted the numbers
are written and the fills are drawn — those are the deliverable. Losing the convenience
on top is worth a warning, not an exit code.

### D29 — Rooms are label objects, matched to apartments by position and storey

*Milestone M6. Settled by measurement on a real project, not by convention.*

A Zone in this practice's projects is a whole apartment **unit**. The rooms inside it are
not Zones at all — they are GDL label objects (`Room Name and Size Label 19`) carrying a
short code in the `room_txt` parameter. Measured on the reference project: 329 such
labels, 299 with a code, and the vocabulary is

    K 33 · S 32 · LY 29 · L/D 29 · EN 29 · B 29 · B1 28 · B2 28 · B3 26 · ST 18 · UT 9

Three of those read backwards from the obvious guess, and all three were settled by
looking at a typical floor plan rather than by inference: **`ST` is study, `S` is
storage, and `B` is the bathroom** — the bedrooms are `B1`, `B2`, `B3`, drawn with beds
in them, while `B` is a 1.8 × 3.1 room beside the ensuite. A letter is not a
description, and a bedroom is habitable where a bathroom is not.

`L/D` is living/dining, and ADG 4A-1 is about living rooms, so **that code is the only
thing in the model that separates the room the standard cares about from the rooms it
does not.** Neither the Zone nor the window carries it.

The vocabulary is built in rather than configured, because it was measured. `--living`
only *adds* codes; it cannot remove one, since a caller able to replace the measured set
would silence it invisibly. Two of the codes read backwards from the obvious guess and
were confirmed by the practice: **`ST` is study, `S` is storage.** Neither is a living
room, so ADG 4A-1 is unaffected today — it would matter the moment a ruleset asked about
habitable rooms. `BP` remains unclassified because nobody has said what it is, and every
unrecognised code is printed each run: an unclassified living room is simply not
assessed, and no other line of output would say so.

The parameter name was found rather than guessed. `archicad-objects --parameter` exists
for exactly that: parameter names belong to whoever authored the library part, and every
office library names them differently.

**The join is geometric, and the storey half of it is not optional.** A label is
annotation: nothing in the model relates it to a Zone. So a label belongs to the
apartment whose outline contains its point — *on the same storey*.

**Containment alone is too strict, and the failure was one-sided.** On the reference
project, strict containment matched 75 rooms into 10 of 10 apartments — every ensuite,
bedroom and kitchen — and **not one of the 14 living rooms on those same storeys.** That
is not a coincidence: a label is dragged to wherever it reads well on the drawing, and
for the biggest room in a plan that is often past the wall the zone stops at. Requiring
containment therefore lost precisely the rooms ADG 4A-1 is about, while looking like it
had worked.

So a label outside every outline is attached to the *nearest* apartment on its storey,
within `DEFAULT_TOLERANCE_M`. Every use of the tolerance is reported with its distance,
because each one is a judgement made on a person's behalf.

**Reporting the distances immediately falsified the reasoning above.** The measured
reaches were **0.00 m to 0.30 m, median 0.00 m** — the labels are not dragged anywhere,
they sit *exactly on* the outline, and a point on an edge is neither in nor out by a
strict test. Coincident geometry, not loose draughting. The tolerance is therefore 0.5 m
rather than the 1.5 m first guessed: past the worst case observed, and too small to reach
into a neighbouring room, since no habitable room is a metre wide. A tolerance that spans
a real room would eventually attach a bedroom to the flat next door and never say so.
`--tolerance 0` restores strict containment.

**A flat has one living room, so several means the zone is wrong.** With the tolerance in
place the reference project matched 145 labels into 10 apartments — a median of 11 rooms
each and 30 in the worst. Those are not apartments. `UNIQUE_ROOM_CODES` names the rooms a
unit has exactly one of (living, kitchen, laundry, ensuite; *not* bedrooms, since `B1`,
`B2` and `B3` are already distinct), and a zone holding two of any of them is reported.
The cause is a zone outline spanning several units, or apartments sitting on a layer the
run did not read — and either way every downstream number is wrong while still reading as
plausible, which is the failure mode this whole document exists to prevent.

The storey test is what makes this safe. These labels live inside hotlinked unit-type
modules whose masters are parked above the building; the reference project has three such
sets, at roughly 64 m, 158 m and 280 m, on storeys 9–14, 38–39 and 71–76. **A master's
label sits at the same X and Y as the placed instance it came from.** Point-in-polygon
alone therefore matches it to a real apartment — confidently, silently — and every
apartment ends up holding two or four copies of every room. Comparing storeys is the only
thing that tells the copy from the original, and there is a test named after that trap.

**Codes are configuration, echoed every run.** `--living` defaults to `L/D` and the room
mix found is always printed, because which code means "living room" is a practice's own
business and a wrong one moves the headline percentage without any other symptom.

Two consequences worth stating plainly. An apartment with no label in it cannot be
assessed against 4A-1 at all — the run names those rather than assuming the whole unit is
a living room. And labels with a blank code are dropped rather than counted as unnamed
rooms; 30 of 329 were blank, and a room with no name cannot be classified as anything.

---

### D30 — Parked hotlink masters are cut by height, not by layer

`--exclude-above <metres>` drops every element whose geometry lies **entirely** above a
stated height, spaces included.

The earlier advice, recorded above and in `archicad.md`, was to exclude the hotlink
layers from the export. On the reference project that is not possible: the masters carry
the *same* layers as the real building. `01 | Wall.External` is the tower's external wall
and the master's external wall, and no layer combination separates them.

What does separate them is height. The reference project's real building occupies z 57 m
to 85 m; its masters sit at 157–163 m (`L1`, `L2-L3`), 166–179 m (`CORE A`, `CORE B` and
their variants) and 262–281 m (`3B - T01`, `4B - T01`, `3B - Penthouse` and the rest).
Cutting at 100 m removes 1,500 elements and, with them, **four Zones that had been
counted as apartments** — the master copies of the unit type, which matched the apartment
filter as exactly as the real ones did and brought their own marked glazing with them.

Cut once, at the model, before anything is selected. Doing it to the occluder set alone
would leave those four Zones in the denominator: a run would then report 14 apartments
where the building has 10, and the four extras would carry sunlight figures measured
90 m above the site.

**Entirely above, not partly.** A roof that crosses the plane is kept. The threshold is a
project coordinate, which is exactly what the existing overhead warning prints, so the
number can be read straight off a run that did not use the flag.

Height, not storey name, because a storey filter would need the office's own storey
naming to be stable across projects and it is not — `L1` here is a master, `LEVEL 01` is
real, and the next project will use neither.

---

### D31 — Filtering the apartments must not move their numbers

Selecting which Zones are apartments is a *denominator* decision. It changed the
numerator, and silently.

Windows resolve to the space they serve by nearest bounding box, and that search ran over
the surviving spaces only. Filter an apartment out and its glazing goes looking for a new
home: on the fixture, restricting a run to `Apartment L00-A` doubled its window count and
took its living room from **106 minutes to 202** — a fail turned into a pass by naming one
zone. Balconies did the same through the level-matching owner search.

Resolving against every room instead is the obvious repair and it is worse. A marked
living-room slider sits in the wall *between* the unit Zone and its own balcony Zone, near
enough to equidistant that the balcony wins as often as not, and the glazing is then
dropped as somebody else's: 3 of 40 marked openings on the reference project.

So a window keeps the apartment it resolved to unless a room outside the run is **clearly**
nearer — `UNASSESSED_OWNER_MARGIN_M`, 0.5 m — or unless an `IfcRelSpaceBoundary` names
that outside room, which is the export stating the answer outright and beats any distance.
Open space is decided the other way round, over every room including the excluded ones,
because what settles a balcony is vertical (the apartment stands on top of it) and no
distance margin can separate a balcony from the ceiling it is flush against. One dropped
that way is reported as `another-room` rather than merged into the communal count.

---

### D32 — The per-instant series is kept, because "when" is not recoverable from "how long"

`sunlit_matrix` has always produced an `(n_points, n_instants)` boolean, and
`_durations` has always collapsed it to two floats per apartment and dropped it.
That is everything the assessment needs and nothing a drawing needs: a study
sheet showing 09:00, 09:15, 09:30 cannot be reconstructed from "106 minutes".

`core.analysis.lit_share_per_instant` reduces the matrix to
`(n_parents, n_instants)` — the **area-weighted** share of each element's
glazing or open space in sun at each instant — and `PipelineResult.instants`
carries it out with the clock times beside it. Cost is one float per apartment
per instant; a 200-apartment job at 37 instants is 60 kB.

**Area-weighted, not sample-counted**, for the same reason durations are: a 6 m
slider and a 0.8 m highlight window in one room are not one vote each, and
counting samples flatters a room whose real glazing is in shadow.

**It agrees with the compliance number by construction** and there is a test
that says so: with equal sample areas, the share summed over instants against
the same weights *is* the cumulative duration. A drawing that disagreed with
the schedule printed beside it would be the worst failure this tool could have,
so the two are tied to one matrix rather than computed twice.

The share is clipped to `[0, 1]`. It is a fraction by construction, but a sum
divided by itself lands a hair above 1.0 for a fully lit element and no caller
should have to know that.

---

### D33 — D28 was right about the mechanism and wrong about the limit

D28 states: *"There is no `SetActiveDatabase` in the add-on."* There is no
command by that name, but `ChangeWindow` does the same job —
`{"databaseId": ..., "windowType": ...}` calls
`ACAPI_Database_ChangeCurrentDatabase`, which is precisely what element creation
follows. Measured live: a fill created with a worksheet active lands **in the
worksheet** and is absent from the floor plan.

So the literal reading of the office workflow — draw the study into a worksheet
— *is* reachable, and D28's conclusion has to rest on its second argument
rather than its first. That argument still holds and is the stronger one: a
Drawing placed from a View stays **linked**, so re-running the study updates the
sheet, while geometry drawn into a worksheet is a snapshot. Both are now
offered; the linked route stays the default.

Two limits decide how a worksheet target must be exposed, and both are
measured rather than assumed:

- **The worksheet has to exist already.** One created in the same session
  cannot be activated — `-2130313110`, before and after `RebuildView`, whether
  the id comes from `CreateWorksheets` or from the navigator. So the tool names
  a worksheet and asks for it to exist, rather than making one and drawing into
  it.
- **A worksheet has no storeys.** `floorInd` is meaningless there, and six
  storeys of fills drawn into one worksheet land on top of each other. A
  worksheet target is therefore per storey, or it is one storey only.

See `archicad.md` for the measurements.

---

### D34 — The patch is drawn on the floor, and the compliance number is not read off it

`--patch-grid` grids the floor of every assessed apartment and its open space,
casts against an occluder set with the **glazing removed**, and draws the lit
cells. That is a second question about the same building, not a refinement of
the first, and the two are kept apart on purpose:

| | Assessment | Patch |
|---|---|---|
| Surface | the glazing plane | the floor |
| Occluders | everything, glazing included | glazing removed |
| Grid | 200 mm | 250 mm |
| Answers | did this apartment get two hours | where was the sun at 09:15 |

**Nothing about compliance changed.** The ADG figure still comes off the
glazing, still by the route the golden file and the pvlib cross-check cover.
The patch is a drawing, and a drawing that quietly became the compliance
number would be the worst of both -- a coarser grid, a different surface, and a
threshold nobody agreed to.

**The glazing has to come out of the occluder set** or the answer is trivially
"no sun indoors, ever": a window exported from Archicad is a solid, so the pane
shades the room behind it. The opening in the wall is a real hole in the wall
mesh, so removing the glazing leaves the sun a way in and leaves every wall,
sill, reveal and balcony above still blocking it. Measured on the fixture:
2244 lit floor cells with the panes solid, 2409 with them removed.

**Rectangles, not a contour.** Marching squares, alpha shapes and polygon
unions all want a dependency this project has ruled out, all produce polygons
with holes, and `CreateHatches` takes a single contour and no holes. The grid
is already a set of squares: merging lit cells into runs and merging identical
runs across rows tiles the patch exactly, with no dependency and nothing lost.
The stepped edge that results is not an approximation of the patch -- it is the
sampling resolution, drawn honestly, and the office's own reference drawings
have the same edge for the same reason.

**Sampled at 50 mm, not at the 1 m open-space plane.** A patch is a picture of
sun on the floor, and under a 20 degree winter sun a metre of height is 2.7 m
of displacement -- the patch would be in the wrong room.

---

### D35 — The series is one row per level, and the level comes from the geometry

A Worksheet has no storeys. Six levels of apartments drawn at their own
coordinates therefore land on top of each other, and the tile becomes a
composite of the whole tower: a plan of nothing.

It is also wrong *numerically*, which is what settled it. `merge_lit_cells`
snaps to a plan lattice, so cells from six levels at the same x and y merge
into one rectangle and the lit area is counted once instead of six times. The
composite reported 134 m² where the levels sum to 364 m².

So the sheet is levels down the side and time across the top, which is what a
study sheet has always been.

**The level is taken from the floor's own elevation, not from the storey the
export names.** On the reference project every `IfcSpace` comes through with
`storey` unset -- the first attempt drew all ten apartments as one row and
looked plausible. The geometry always knows what level it is on. Where a storey
name *is* present it is used as the label, and the elevation is the fallback.

---

### D36 — The study drawing goes on the floor plan, and the patch is fitted onto it

`--plan-instant` draws, per apartment and per instant: the sun patch, the
outline of the assessed area, and a text block. That is the reference
deliverable's own language, read off its drawings. The whole-day banded
diagram (`--draw`) stays, because it answers a different question -- did this
flat pass -- but it is no longer the only picture on offer.

**On the floor plan, not in a worksheet**, because the plan linework is
already there and the patch has to be read against the rooms it falls in. The
worksheet series ([D35](#d35)) is the opposite trade: the whole day at once,
deliberately abstract, with no plan under it. One layer per instant keeps the
moments separable.

**The patch has to be fitted onto the project frame.** It is computed in the
export's world coordinates and those are not Archicad's project coordinates:
an export made with the Survey Point option is already north-aligned, so the
project is rotated relative to it. `core.geometry.fit_plan_transform` fits a
rigid transform -- rotation and translation, never scale, never reflection --
over one pair per apartment, and the residual is a **refusal** above 0.5 m
rather than a warning, because a patch drawn through a bad transform lands on
the wrong flat and looks entirely plausible.

Two measurements shaped that:

* **Pair on the dwelling, not on its floor cells.** The cells include the
  balcony, which sits on one side of the flat and drags the centre by a
  different amount for every apartment: 2.96 m of residual, and the drawing
  refused for a reason that had nothing to do with the model.
* **Compare box centres, not means.** A mean is weighted by how the points
  happen to be distributed, and an outline's vertices and a grid's cells are
  not distributed alike.

After both, the reference project fits to **195 mm** across ten apartments --
under one grid cell.

---

### D37 — A floor grid is clipped to its room; a window grid is not

`planar_face_grid` grids the *bounding rectangle* of the face it picks. For a
window that is the face. For a room it is not: an L-shaped flat gets a grid
over the rectangle it fits inside, and the sun patch drawn from it reaches
into rooms the apartment does not contain.

Measured on the reference project by reading the drawn fills back out of
Archicad and testing them against the Zone outlines: **37% of the patch area
sat outside any apartment**. With `clip_to_face=True` on the floor grids, the
same check gives 129.7 m² inside and 1.9 m² out -- and that remainder is edge
cells displaced by the 195 mm transform residual, which is less than one cell.

The clip is a barycentric test against the face's own triangles, exact for a
triangulated surface and needing nothing but numpy. It is **off by default**:
a window needs no clipping, and this is not free.

It also corrects the reported areas. An unclipped grid overstates both the
floor and the lit part of it, and those figures are what the annotation prints
against each flat.

---

### D38 — A worksheet left in front empties the next export, and only a person can clear it

Recorded because this tool creates the situation and cannot undo it.

`draw_patch_series` activates a worksheet, and on AC26 nothing switches the
window back: `ChangeWindow` with a floor plan's `databaseId`, with a
`storyIndex`, or with neither, all return `{"success": true}` and leave the
worksheet on screen. `windowType: "Section"` fails outright and `"3D"` is not a
valid value at all.

What makes it dangerous is that everything *else* keeps working. Element reads
answer normally — 1,415 walls, 142 zones — because those follow the current
**database**, which did move. Only the IFC export follows the **window**, and
with a worksheet in front it writes 5.8 kB: an `IfcSite`, an `IfcBuilding`, no
storeys and no elements. The next run then fails three steps later, in the
scene filter, as `apartment zone layers matched nothing` — pointing at a layer
name that was right all along.

An earlier version of this file called that cosmetic, on the strength of one
export that came out whole. That was a single measurement against a mechanism
nobody had established, and it was wrong. The behaviour above is repeatable.

So the series is drawn **last** in a run, the run says plainly that a floor
plan must be opened before the next one, and `_connect` puts the *database*
back even though it cannot move the window.

---

### D39 — Sheet geometry: four units, and a save that makes a layout readable

Placing a Drawing correctly took four separate corrections, each of which
looked like the last one had worked. Recorded together because they only make
sense together.

**The scale is on the view; the Drawing is placed at 100%.** `CreateDrawings`'
`scale` field is a *magnification*, not a scale denominator. Passing 200 for
"1:200" put the drawing on at 20000%. The view carries the scale
(`drawingScale: 200`) and the Drawing goes on at `1.0`.

**Positions are in metres; the page is described in millimetres.**
`GetLayoutSettings` reports an A1 as `841 x 594`, and a Drawing's position is
in metres. Computing a grid from the page size and passing it straight through
put a drawing meant for x = 200 mm at x = 200 m — a quarter of a kilometre off
a sheet 0.841 m wide. Those two bugs hid each other: the magnification made
the drawing enormous, the units put it far away, and each made the other look
plausible.

**The angle comes from the Drawing tool's default**, not from the view. Every
drawing arrived at 279.9° — the project's own north — with the view's
`rotation` at 0. There is no angle field on `CreateDrawings`; it is fixed
afterwards with `RotateElements`, which takes no angle either, only a centre
and the two ends of an arc.

**Saving is what makes a layout readable.** A layout created in the current
session answers `GetDetailsOfElements` with a per-element error, which is what
made the first attempt at measure-and-move impossible — and what preceded
Archicad exiting. After `SaveProject` the same read answers normally. So the
order is: create, **save**, measure, straighten, measure again, move. Twice,
because rotating changes the bounding box and the tiling needs the new one.

`SaveProject` also means a long run can commit its work in stages instead of
holding hours of drawing in an unsaved file.

---

### D40 — The current database is not the current window, and reads follow the database

A run kept reporting zero apartments on a project holding 142 zones. The
window said `FloorPlan`; the *database* was a Layout, left there by the
previous step's `ChangeWindow`, and `GetElementsByType` follows the database.

So `ensure_model_database` does not ask what the window is. It asks whether a
read can see any walls, and switches when it cannot. The window type is
advisory; what a read returns is the fact.

The two are genuinely independent, and which one an operation follows has to
be established rather than assumed:

| | follows |
|---|---|
| Element creation (`CreateHatches`, `CreateTexts`) | the current **database** |
| Element reads (`GetElementsByType`) | the current **database** |
| Element deletion | the current **database**, but silently refuses on a hidden layer |
| The IFC export | the current **window** |

---

### D41 — A patch is drawn as one outline where that is exact, and as tiles where it is not

`trace_lit_regions` walks the boundary of the lit cells and returns one
polygon per connected patch, which is what a reader expects a sun patch to be
and what an editor can work with: on the reference project it took a banded
plan from 2,354 fills to 946.

It is used only where it is **exact**. `CreateHatches` takes a single contour
and no holes, so a patch with a hole in it can be drawn as one shape only by
filling the hole — claiming sunlight on floor that never saw any. Those fall
back to the tiled rectangles, which cover the same area exactly and only need
more of them. A drawing is a claim about sunlight, and a tidier drawing is not
worth a false one.


### D42 — An element's surface cannot be set; new geometry carries the colour instead

The reference study's facade page shows the building painted by hours of sun.
The obvious implementation is to give every wall the surface of its band. That
is not reachable through this API, and the search for it is worth recording so
nobody repeats it:

* `GetDetailsOfElements` reports a `surfaceId` only for library-part based
  elements — Objects. A Wall, Slab, Roof or Mesh reports geometry and nothing
  about its appearance, so its surface can be neither read nor written.
* `SetDetailsOfElements` reaches `floorIndex`, `layerIndex`, `drawIndex` and a
  `typeSpecificDetails` union whose `WallSettings` is purely geometric. There
  is no setter anywhere in the add-on that attaches a Surface to an existing
  element.
* `CreateMorphs` — the natural element for a coloured skin, since a morph takes
  a `surfaceId` directly — validates its input on this build and then answers
  `Failed to create morph` for every shape tried, box and explicit body alike.
  A morph is not reachable on Archicad 26 either.

What is reachable is creating geometry that already carries the colour:
`CreateSurfaces` takes an RGB, `CreateBuildingMaterials` takes a
`cutSurfaceIndex`, and `CreateWalls` takes a `buildingMaterialId`. So the
facade picture is a skin of thin walls standing 30 mm proud of the real one,
one per merged rectangle, on a layer of its own. It is native 3D — it shows in
the 3D window, in a 3D document and in a rendering — and switching the layer
off restores the model exactly.

Two smaller findings from the same session. `CreateSurfaces` answers with
attribute *ids* while a building material wants an *index*, and there is no
converting one to the other except by reading the attribute list back.
`CreateWalls` has no `layerIndex`: new walls land on the Wall tool's default
layer and have to be moved afterwards.

### D43 — A hidden layer silently refuses modification, not just deletion

D31 recorded that `DeleteElements` answers `{"success": true}` and removes
nothing when the target sits on a hidden layer. The same is true of
`SetDetailsOfElements`, which matters more than it sounds: new walls land on
the Wall tool's default layer, on the reference project that layer is hidden,
and so the move onto the tool's own layer reported success for every element
and moved none of them.

Chasing that produced a false lead worth naming. Sending `layerIndex` as a
float appeared to fix it — the int failed, `144.0` returned success — and it
had not; the layer was unchanged either way, and the float call was simply the
one made after something else had changed. The rule that catches this is the
one already in force everywhere else here: **re-read, never believe the
response.** The layers involved are therefore forced visible for the duration
of the change and put back exactly as they were, and the elements' layers are
read back before the run reports success.

### D44 — The no-sun band is spelled two ways, and reading one of them loses most of the drawing

`band_by_area` gives the no-sun band an upper bound of exactly 0.
`BandStyle` gives it `1e-9`, the tolerance below which a duration counts as
none. `_band_mask` recognised only the first, so a legend built from
`BandStyle` produced an *empty* no-sun band — silently, with every other band
correct.

On the reference facade that is 5,885 m² of wall, 83% of the elevation, and
the failure is invisible in the output: the percentages that appear all look
reasonable, and the missing band simply is not mentioned. It was caught by
totalling the bands against the surface area, which is now what the test does.
Any banding of a whole surface should carry that check.

### D45 — Surface reflectances are percentages, and a fraction renders black

``CreateSurfaces`` takes its colour as three fractions in ``[0..1]`` and its
reflectances as percentages in ``[0..100]``, truncated to whole numbers. The
two live side by side in the same object, so the natural thing — writing
``1.0`` for "full" throughout — stores ambient 1% and diffuse 0%.

The failure is a quiet one. Every band colour is stored exactly right and
reads back exactly right; the surfaces simply reflect nothing, so the coloured
model renders black and the diagram looks like a bug in the geometry rather
than in two numbers. Confirmed by probing: ``1.0`` comes back as ``1``, ``0.5``
as ``0``, ``100`` as ``100``.

Both are now sent at 100. A diagram's band has to read as its legend colour
wherever it appears rather than shading off with the angle of the face it sits
on -- that shading is a second, competing signal about sunlight in a drawing
whose entire subject is sunlight. Specular and shine stay at zero for the same
reason: a highlight reads as sun on a face that may have had none.

``DeleteAttributes`` takes ``attributesToDelete``, each entry an
``attributeType`` **and** an ``attributeId`` — not the ``attributeIds`` list
every other attribute command takes.

### D46 — Geometry built from the export must be fitted onto the project first

The first facade skin was created straight from the export's world
coordinates, and appeared beside the building and turned — the failure D30's
``PlanTransform`` exists to prevent, repeated because the massing path was
written without it. The two frames differ by the project's own rotation
(279.9 degrees here) and a shift; on this project fitting them leaves 0.175 m
of residual.

Anything created in the project from geometry computed on the export needs
that fit, not only the 2D patches that first needed it. The pairing is Zones,
because a Zone exists identically on both sides — the same room, a GlobalId in
the file and an outline in the project — and the same ``MAX_FIT_RESIDUAL_M``
guard applies: over half a metre of residual means the export is not of the
project's current state, and a wrong placement is worse than none.

The standoff that pushes the skin clear of the wall is applied in the export's
frame, *before* the transform, so it stays perpendicular to its own face.
Applied afterwards it would push every rectangle along one rotated direction.

### D47 — A 3D window answers reads with what it is showing

``GetElementsByType`` in a 3D window returned 873 of the 1,968 walls the
previous pass had drawn on the tool's own layer. The clear-out therefore
deleted 873, re-read, found none left, and reported itself finished — a
verified count that was verified against the same partial view that produced
it.

This is D40 with a sharper edge: it is not only that reads follow the current
database, it is that a 3D database's answer depends on what the view is set to
show. Any operation that has to see *all* of something forces a floor plan
first. Re-reading is still necessary and is not, on its own, sufficient.

### D48 — A wall is the only element that can be created with a material, so a flat patch is a wall lying down

Only ``CreateWalls`` takes a ``buildingMaterialId``. ``CreateSlabs``,
``CreateMeshes``, ``CreateRoofs``, ``CreateBeams`` and ``CreateColumns`` all
take a shape and no material at all -- their only route to one is
``favoriteName``, which needs a Favorite somebody made by hand. So the
horizontal half of the picture -- balcony decks, terraces, soffits, which take
more sun than any wall does -- cannot be slabs.

It can still be walls. A wall is a box: give it the rectangle's long side as
its length, its short side as the *thickness*, and 40 mm as the height, and it
lies on a deck as a coloured plate. Nothing is lost except lean, which is why
sloping faces are not panelled at all.

Grouping faces by plane had to change with it. The key was the two horizontal
components of the normal plus the plane offset, which is enough to separate
upright faces and puts a slab's top and its soffit in the same group -- both
have x and y of zero. All three components are keyed now.

### D49 — A 3D Document cannot be created, only used

There is no command in the add-on that creates a 3D Document; the nearest
names -- ``GenerateDocumentation``, ``Set3DCutPlanes`` -- do something else.
What can be done is make a *View* of one the project already has, through
``CreateViewsInViewMap``, and pin a layer combination to it.

That is worth doing rather than routing around, because the two model views
are different things. The 3D window is live: it shows the model as it is now,
turns freely, and is what somebody checks a study in. A 3D Document is a
drawing made from a 3D view, with its own pen and fill overrides and its own
dimensions, and is what goes on a sheet. An office wants both, and the tool
can supply the first outright and the second only if one exists.

### D50 — A wall shows its material only when its surface override is off, and nothing turns that off

The first coloured skin rendered uniformly grey, with every band's Surface
carrying the right colour and every band's Building Material pointing at the
right Surface -- both verified by reading them back. The cause is a third
thing: a wall's own **surface override**. With it on, the wall shows the
overriding surface and its building material's colour never appears.

Nothing in the add-on turns an override off. ``WallSettings`` is geometry
only; ``CreateWalls`` on 1.5.7 has no ``favoriteName`` field, though the
newer published schema does. What is left is the Wall tool's *defaults*, which
``CreateWalls`` inherits and ``ApplyFavoritesToElementDefaults`` can set from
a Favorite. So one Favorite, made by hand from a wall with the override
switched off, fixes every later run -- and is the only route there is.

**1.5.8 makes it a field, and the difference is whose session it touches.**
Read out of the installed ``.apx``'s own schema: every wall in ``wallsData``
now takes an optional ``favoriteName``, *"applied first, then the explicitly
given fields override them"* -- which is exactly the order this needs, because
the Favorite carries one thing (the override, off) and the band's own
``buildingMaterialId`` still has to win. ``draw_model_bands`` therefore names
the Favorite per wall when the add-on is 1.5.8 or newer, and falls back to
``apply_favorite_to_defaults`` below that. Both build the same skin. The
difference is that the defaults are shared, visible state: setting them
changes what the *next* wall a person draws by hand looks like, and it outlives
the run. A field on the request changes nothing outside it.

The check is ``has_tapir_at_least``, which answers rather than raises. A
capability with a working fallback must not become a version requirement --
somebody on 1.5.7 still gets their facade.

The existence check stays on both paths, in ``require_wall_favorite``. Neither
route refuses a name that matches nothing: the defaults route applies nothing,
and ``CreateWalls`` takes an unknown ``favoriteName`` and builds the wall
anyway. Both then produce a silently grey skin, which reads as a finding
rather than as a missing Favorite.

Worth knowing for diagnosis: ``GetFavoritePreviewImage`` renders a Favorite in
3D and returns a PNG, which is the only way from here to *see* what a created
element looks like. It is what showed the grey.

### D51 — A drawing made from a 3D source is created at a placeholder size

A Drawing placed from a plan view has its true extent as soon as the project
is saved. A Drawing placed from a 3D view or a 3D Document does not: it is
created 59 mm square and keeps that until Archicad regenerates it, which
happens when somebody opens the layout. ``UpdateDrawings`` would force it and
refuses below Archicad 27.

Two consequences. Straightening still works -- the angle is set through
``DrawingSettings`` and holds -- but tiling on the first run arranges
placeholders. And a run must therefore *not* delete and re-place the drawings
it finds, or every run resets them to the placeholder and no run ever tiles a
real size; existing drawings are reused by name and only the missing ones
placed. Open the sheet once, run again, and the arrangement uses true sizes.

Also: ``CreateLayout`` does not care that a layout of that name exists, and
will make a second. Layouts are reused by name -- unlike views, a layout
*can* be deleted, but reuse avoids needing to.


### D52 — A hidden layer is an export filter, and it fails in the wrong words

The translator exports what the current layer combination *shows*. On the
reference project, opened on a site-plan combination, all four ``06 | Zone.*``
layers were hidden and locked; the export came out at 35 MB carrying 386
walls, 92 windows, 90 slabs and **no ``IfcSpace`` at all**.

What the run then said was true and useless: ``apartment zone layers ['06 |
Zone.Units'] matched nothing``, followed by the eleven layers that *did*
export. The layer at fault is by construction absent from that list, so the
message asks the reader to notice an absence in a list of eighteen names, and
arrives only after a multi-minute export. Both diagnostics available at that
point -- the export's layers and the export's spaces -- describe the symptom.

So visibility is checked against the *live project* before the export, where
the answer is one boolean per layer. ``hidden_layers`` asks Archicad, and the
run stops in about two seconds naming the layers to switch on.

It stops rather than warns because there is nothing else it could do. Tapir
1.5.7 has no command that changes layer visibility or activates a layer
combination -- ``SetLayers``, ``SetLayerCombination``, ``ApplyLayerCombination``,
``OpenView``, ``ActivateNavigatorItem`` and ``SetCurrentWindow`` all answer
code 4010, unregistered. ``CreateLayerCombinations`` exists and is no help: a
combination that cannot be activated changes nothing. This is a hand in
Archicad, and the message says so instead of implying a flag might fix it.

A name Archicad does not recognise is deliberately *not* reported here. That
is a typo, not a hidden layer, and sending the reader to Layer Settings for a
name that is not in them is worse than silence; ``_require_matches`` already
catches it against the export, which is where the correct spellings are.

### D53 — Two sheet-building paths, and only one of them was ever fixed

The storey-plan sheet and the times-of-day sheets came out of the same run and
only one of them was right. Every correction D33 to D39 recorded had been made
in ``layout_from_views`` and none of them in ``layout_results``, so the
``Sun Study`` layout carried all three original faults at once, measured on
the reference project:

* **Positions in millimetres, sent to a field measured in metres.** Drawings
  meant for x = 140 mm sat at x = 140 m, and the sheet ran from 140 to 888
  metres across an 841 mm page.
* **The scale denominator sent as the Drawing's magnification.** ``scale`` on
  ``CreateDrawings`` is a magnification where 1.0 is 100%, so 200 asked for
  20000% and produced floor plans 187 metres wide.
* **No second pass**, so all eighteen stood at 279.9 degrees -- the project's
  own north, which is the Drawing tool's default angle.

Nobody had noticed because the run reported both sheets in the same green
line, and the one that was checked was the one that was right.

``layout_results`` is now three lines that clone the storey views, pin the
scale on them and hand them to ``layout_from_views``. A fix that lands in one
of two copies is not a fix, and the second copy is where it will be needed
next.

### D54 — A tiling has to fit the page in both directions, and a drawing that cannot has to shrink

The number of columns was chosen from the page **width** alone. Six band
diagrams 197 mm tall therefore went into one column: 1,254 mm of drawing on a
594 mm page, with four of the six off the paper and the sheet reading as
though the study had produced nothing.

Every arrangement from one column to one row is now considered, and each is
asked whether the block fits and how much of the page it wastes. What fits and
wastes least wins, and a block shaped like the page beats a strip of the same
area -- which is why six drawings that would fit in a row of six go three by
two instead.

Some drawings fit at no arrangement. A view of the coloured model arrived
1,429 x 1,256 mm on an A1, and one drawing has only one arrangement. So the
tiling also returns *what the drawings must be multiplied by* to fit, and the
sheet pass applies it. Uniformly, including to drawings that would have fitted
alone: three diagrams of one building at three times of day are read against
each other, and two at one size beside a third at another is a picture of
nothing.

The shrink is always reported, and below a quarter it is reported as a
diagnosis rather than an outcome. 14.8% is not a page slightly too small, it
is the wrong scale for the sheet, and the run says so.

### D55 — A Drawing's scale cannot be set, its magnification can, and its bounds follow neither

Three findings, and the third is the one that bites.

``SetDetailsOfElements`` accepts ``drawingScale``, answers
``{"success": true}`` and leaves the scale exactly as it was -- D43 again, in a
new place. ``ratio``, the magnification, does change. So the only handle on a
drawing that will not fit its sheet is how big it is on the paper, not what
scale it is at, which is a reason to put a study on a **no-scale** title block
rather than one that prints a figure the drawing may no longer be at.

The third: **bounds do not follow magnification.** Set a drawing to 46.9% and
46.9% is what reads back, while the bounds go on reporting the size from
before, until Archicad regenerates the drawing -- which happens when somebody
opens the layout. A pass that re-read to check its own work therefore
recomputed the full size as "bounds over magnification", got 3,050 mm from a
drawing 1,429 mm wide, and shrank it again: 46.9%, then 22.1%, halving on
every run forever.

This is the one case in this codebase where **re-reading is wrong**. What is
dependable is that bounds and magnification agree *until this code touches
them*, because Archicad drew them together; so the full size is bounds over
magnification, computed once, written once, never verified. The guard that
makes it safe is a rule rather than a measurement: a drawing already below
full size has been fitted already and is left alone, because nothing readable
distinguishes one that has regenerated from one that has not.

It costs something real. A sheet does not re-fit itself when the building
outgrows it, and the run says what magnification it left so there is a handle:
delete the drawings and let the next run place them fresh.

### D56 — A master layout is named from memory, so it is matched by its words

An office keeps dozens of masters -- seventy-one on the reference project --
punctuated every way there is: ``DA A1 - VERTICAL - No Scale`` sits three rows
from ``DA A1 - VERTICAL COVER/NO SCALE``. What a person types is "A1 no
scale".

Exact-match-or-nothing answered that by printing all seventy-one names and
stopping, which is the least useful place to put the right answer: in a wall
of text that has already scrolled past. Now the name is read as the words it
contains, every master carrying all of them is a candidate, and the one
carrying the fewest words nobody asked for wins. A tie is still a question --
two masters equally close is something only the person can settle, and either
guess puts the study on a title block meant for something else.

The one thing this cannot fix is a layout that already exists. Layouts are
reused by name, nothing in the add-on changes an existing layout's master, and
``SetLayoutSettings`` does not offer one. So a re-run with ``--master-layout``
leaves an old sheet exactly where it was, and the run now says that outright
instead of reporting the master it would have used.

### D57 — Finishing a sheet leaves you standing in it, and a Layout has no Zones

Adding the straighten-and-tile pass to the storey sheet broke every drawing
step after it. The pass has to make the layout current to work in it, and it
left it current; ``read_zones`` then read the *layout's* database, found no
Zones, and the plan transform paired 0 apartments of 10.

What the run said was "the export and the project disagree about where the
apartments are". That is D40's rule -- reads follow the current database --
arriving four steps from its cause and dressed as a geometry problem, on a
project where the export had been correct all along.

Every path that finishes a sheet now puts a floor plan back, in a ``finally``,
because the failure also happens when the sheet pass raises. The rule this
follows is the one ``ensure_model_database`` was written for: the current
database is a shared piece of the session, and anything that moves it owns
putting it back.

### D58 — A Drawing's angle field is writable, reports back, and does not turn the drawing

``straighten_and_tile`` was written with ``RotateElements``, which needs a
centre and two points on an arc to imply an angle. That was replaced with
``SetDetailsOfElements`` and a ``DrawingSettings`` ``angle`` of zero, because
the schema offers the field and one number is plainly better than an arc.

The field takes the write. It reads back as zero. The drawing does not move.

Six drawings on the reference project were reported straightened, twice, and
the sheet came out visibly crooked with Archicad's own dialog insisting the
angle was 0.00 degrees. What settles it is the ``clipPolygon``, the frame's
own corners: every one of them stood at 9.90 degrees off axis -- 279.9 modulo
a quarter turn, the Drawing tool's default angle, which is this project's
north. The office's own drawings on the same project measure 0.00.

So the angle is read from the corners and never from the field, and the turn
is ``RotateElements`` again, arc and all. This is the third member of a family
now -- ``layerIndex`` on a hidden layer (D43), ``drawingScale`` on a Drawing
(D55), and ``angle`` -- where the add-on accepts a write, stores it, returns
it, and changes nothing. Reading a value back is not evidence that it did
anything; reading the *geometry* back is.

### D59 — A layer combination cannot be activated, but it can be copied

D52 stopped the run when a layer the study needed was switched off, and said
there was nothing else it could do. Five commands had been tried --
``SetLayers``, ``SetLayerCombination``, ``ApplyLayerCombination``,
``OpenView``, ``ActivateNavigatorItem`` -- and all were unregistered.

That conclusion was right about combinations and wrong about layers. A
combination is only a set of per-layer visibilities, and those are writable:
``CreateLayers`` with ``overwriteExisting`` sets ``isHidden`` and
``isLocked``, which the facade skin already relied on to borrow a hidden
layer. What was missing was the other half -- ``GetLayerCombinations``, whose
parameter is ``attributes`` rather than the ``attributeIds`` every neighbouring
command takes, and which returns a combination's full per-layer state. Read
one, write it onto the layers, and the project is in that combination in every
way that matters to an export, without Archicad ever being asked to switch.

So the export sets its own layer state, in three steps. A base -- one of the
project's own combinations by name, usually its IFC export combination, which
is an office's own account of what belongs in an export -- or everything shown
if none is named. Then the layers the study needs are forced on, and
``--hide-layer`` forced off.

The middle step is not a refinement. On the reference project *neither*
``12 | IFC ARCH. EXPORT`` nor ``12 | IFC CONSULT. CHECK`` shows the
``06 | Zone.*`` layers, so following the office's own export settings exactly
reproduces D52: an export with no ``IfcSpace`` in it and a run reporting no
apartments. What belongs in an export and what a solar study needs are
different questions, and only the run knows the second.

Everything is snapshotted and restored in a ``finally``. Measured on a project
with 89 of 142 layers hidden and 63 locked: 94 changed for the export, all 94
back afterwards, ``06 | Zone.Units`` switched on for the export and off again
after it. The apartment figures came out identical to the run made against a
hand-selected combination, which is the check that this reproduces rather than
approximates.

The restore matters as much as the change. This tool spent three sessions
re-selecting a combination it had itself clobbered by opening its own views,
and a run that rearranges what somebody sees and leaves it that way has just
moved the problem.

Two smaller findings. ``DeleteAttributes`` will not remove a
``LayerCombination``: it validates the type name -- a wrong spelling is
refused as invalid -- and then answers ``Attribute not found`` for an id
``GetAttributesByType`` had just handed over. So combinations are reused by
name like layouts and views. And the tool records its own resolved state as a
real combination, ``SS Sun Study Export``, which changes nothing on its own
but gives the choice a name in Layer Settings that a person can inspect.

### D60 — A Text has no layer, so it lands on the office's annotation layer

``CreateTexts`` accepts a coordinate, a string, a ``height``, a ``pen``, a
``justification``, an ``angle`` and a ``floorIndex``. Probed one field at a
time: there is no ``layerIndex``, no ``penIndex``, no ``charHeight``, no
``styleName``. So a Text is created on whatever layer the Text tool defaults
to, which on the reference project is ``05 | Dims/Notes.DA`` -- one of the
office's own annotation layers for the drawing set.

That is wrong twice. The study's key is filed in somebody else's layer, and it
is outside what the next run clears, so switching the study's layer off leaves
the legend sitting on the plan with nothing to explain it.

The fix is the one the facade skin's walls already use: create, then move with
``SetDetailsOfElements``, then **read the layer back**, because that write
answers success and does nothing when either layer is hidden (D43).

Two related findings from the same probing. ``GetDetailsOfElements`` refuses a
Text outright -- ``Not yet supported element type`` -- so a text's content and
height cannot be read at all; its ``layerIndex`` and ``floorIndex`` are
readable because they sit outside ``details``. And ``Get3DBoundingBoxes``
*does* answer for a Text, which is the only way from here to find out where
one actually is. That is what located five stray probe texts at the model
origin -- which then would not delete until their layer was switched on, D31
again.

### D61 — Text height is inherited unless it is set, and the legend was spaced for a guess

The legend's rows were 1.5 m apart in model space, for a 1.0 m swatch. The
labels beside them were 1.27 m tall, because a Text created without a
``height`` takes the Text tool's default -- an office's default, set for its
own drawings, with no reason to suit a key drawn beside a plan. Consecutive
rows therefore overlapped and the legend read as one smear of text.

It is worse than a fixed error, because a text's model-space size scales with
the view: the same legend at 1:300 is half again as tall while the 1.5 m
spacing does not move.

So the height is stated rather than inherited, and the row spacing is derived
from it instead of guessed alongside it. Measured: at the stated height a
label's whole bounding box, ascender and descender included, is 0.43 m, and
rows now step by 1.5 times the taller of swatch and label.

### D62 — Moving an element off a hidden layer means borrowing that layer first

D60 moved the study's labels onto the study's layer, and every one of them
refused: ``8 of 8 legend labels would not move``. The target layer was
visible; the layer they were *created* on was not. ``05 | Dims/Notes.DA`` is
hidden on the reference project, and ``SetDetailsOfElements`` answers success
and changes nothing for an element sitting on a hidden layer -- D43, from the
other side. Every earlier instance of that trap was about the layer being
written *to*; this one is about the layer being written *from*.

So the move reads where the elements actually landed, switches those layers on
for the duration, moves, re-reads, and puts them back exactly. Which layer to
borrow is not something the caller can know: the Text tool's default is a
project's own setting, and the answer is whatever the elements report about
themselves.

Elements already on the target are left alone rather than moved to where they
are -- no write, and nothing unhidden in order to make it.

Worth knowing about the earlier runs: labels created before this fix are still
on the office's annotation layer, and the run's clean-up only ever touches its
own layer, so they are not swept up by a later run. Two hundred of them on the
reference project, findable only by ``Get3DBoundingBoxes`` because a Text will
not report its own contents.

### D63 — Layer visibility belongs to a database, not to the project

D62 left one thing unexplained: after a run the reference project's
``05 | Dims/Notes.DA`` read visible, though the borrow that unhid it restored
correctly in isolation. The conclusion recorded at the time -- that switching
views drifts the layer state and the drift needs undoing -- was wrong, and
wrong in a way that would have produced a fix for a problem nobody had.

Measured, in this order, on the reference project:

1. On a floor plan, the layer reads hidden.
2. Switch to a layout: the same layer reads **visible**. The layout's
   combination is what is being reported.
3. Write it hidden while the layout is current: the write takes effect there.
4. Switch to a floor plan and back to the layout: **visible again**. The
   combination is reapplied on every switch and the write is gone.
5. Write it *visible* while the layout is current, then switch to the floor
   plan: the floor plan reads **hidden**. The write never reached the model.

So layer visibility is not one fact the project holds. It is whatever the
current database says, each database has its own answer, and a write outside
the model is scratch that the next switch discards.

Nothing had drifted. The check that reported drift was itself run after a tour
of six layouts -- ``measure_drawings`` switches into each one -- so it read a
layout's combination and reported it as the project's. The measurement was the
bug.

Two things follow. Layer work is refused anywhere but a floor plan, rather
than answering wrongly: a snapshot taken in a layout and restored later does
not restore anything, it writes a layout's opinion over the model, which is
what ``export_state`` would have done had a database switch ever landed inside
its ``with``. And the tool has to *remember* where it is, because Archicad
will not say -- ``GetCurrentDatabase``, ``GetCurrentWindow`` and
``GetDatabases`` are all unregistered on Tapir 1.5.7 and, re-probed live,
on 1.5.8 as well, and
``GetCurrentWindowType`` answers for the window on screen, which moves
separately from the database. Every ``ChangeWindow`` in this package goes
through ``run_tapir``, so the note is taken there and cannot be bypassed by a
fifth call site.

The limit is stated rather than hidden: a person who clicks into a layout
mid-run makes the note stale, and a connection that has not moved anything
says ``None`` and is left alone.

### D64 — A layer combination is a statement about the model, so read the model

D63 said layer state belongs to a database. This is where that was already
costing something, unnoticed.

``ensure_layer_combination`` builds each sheet's combination from "the layers
as they stand", which is the right idea: everything a reader expects on a plan
stays as it is, and only the named layers are forced. But *as they stand where*
was never asked. The sheets are made in a loop, and ``layout_from_views``
leaves a layout current -- so from the second sheet onward the layers "as they
stand" were the previous sheet's layout answering with its own combination.
Each sheet inherited what the sheet before it happened to show.

The named layers were never affected: ``--hide-layer`` forces those either way,
which is why the complaint that fixed the grids and dimension layers appeared
to work. Everything the run does *not* name is what drifted, and that is most
of a project.

So the model is made current before the layers are read. That is cheap now:
``ensure_model_database`` answers from the note the connection takes on every
``ChangeWindow`` and only pays for the two reads when the tool has not moved
the database itself -- a fresh process inheriting whatever the last run left,
which is the case those reads exist for.

Two things came out of consolidating the read onto ``read_layers``, which is
what made the bug visible at all. There were four copies of "GetAttributesByType
then GetLayers" in this package. And ``LayerState`` carried only visibility and
lock, while a combination names ``isWireframe`` and ``intersectionGroupNr`` too
-- and ``CreateLayers`` writes the *whole* layer, so a field left out is a field
reset to its default. Restoring a borrowed layer had been quietly turning
wireframe off and moving the layer to intersection group 1. ``LayerState`` now
carries the whole layer, and a restore puts back what was there.

``ensure_layer_combination`` had no tests at all before this. It decides what
every drawing shows.

### D65 — The tool's output is named into the project's own numbering

``SS`` was this tool's mark on everything it made: ``SS Sun Study 09:00``,
layers called ``Sun Study.Results`` with no group at all. On the reference
project the layer groups run ``00 |`` to ``13 |``, each with a divider of its
own, and the combinations follow the same scheme. Output called ``SS`` sorts
nowhere and reads as somebody's initials.

So everything the tool creates now leads with ``14 |`` -- the next free group:
layers ``14 | Sun Study.Results`` and ``14 | Sun Study.Facade``, combinations
``14 | Sun Study 09:00``, surfaces, views and layouts the same.

The prefix could not simply be deleted, which is the part worth recording.
``remove_previous`` finds a run's own work by it, so an empty prefix matches
every view and every layout in the project and the clean-up before a rerun
would delete the practice's drawings. Replacing one marker with another keeps
that guarantee; removing it would have been the most expensive kind of tidying.

It lives in ``archicad/naming.py``, alone, because five modules need the same
string and none of them should import another to get it -- and because ``14``
is right for a project whose groups end at 13 and wrong for the next office.

The property group is deliberately *not* renamed. ``Sun Study Note`` and its
siblings are written onto Zones and already carry values on this project;
renaming them would orphan every one and leave a schedule pointing at nothing.
A property is not a layer.

### D66 — A layout cannot be created into a subset, only moved into one

Asked to file its sheets with the practice's own, in ``SHADOW DIAGRAMS`` and
``ADG DIAGRAMS`` rather than at the root of a book of 299 layouts.

``CreateLayout`` looks like it does this. Its schema accepts
``parentNavigatorItemId`` -- strict enough elsewhere to refuse a misspelt
field by name -- and answers with a database id. Measured: the sheet arrived
under the book root, not under the subset. Accepted, and ignored.

``MoveNavigatorItem`` does it, taking ``navigatorItemIdToMove`` and
``parentNavigatorItemId``, verified by reading the book back and finding the
layout at ``SAMPLE > DEVELOPMENT APPLICATION > SHADOW DIAGRAMS``. So a sheet
is made first and filed second, and the filing is read back rather than
believed -- both commands answer ``{"success": true}`` either way.

Which subset by what the sheet *is*: a clock time is a shadow diagram, and the
banded and two-hour plans are ADG diagrams. Both names are options, and a
subset that is not there is reported rather than created. The Layout Book is
the practice's structure; inventing a subset in it is a bigger decision than a
sun study gets to make on its own.

### D67 — A balcony Zone is a void, so you stand on its floor, not its top

Reported from the drawings: the sun patch on the wrong floor. On the ground
floor the balcony was bare; on the floor above, where the balcony is smaller,
a patch was drawn that overhung the apartment outline.

``_open_space_owner`` decides which flat a balcony belongs to. Nearest box is
not enough -- a balcony sits at its own apartment's floor level, which puts it
flush against the ceiling of the apartment below, so the two are exactly
equidistant and iteration order picks the winner. The disambiguator is the
level of the surface somebody stands on, and that was read as
``slab.bounds[1][2]``: the top.

Right for a slab, wrong for a Zone, and this project models balconies as
Zones. A Zone is a *void*: you stand on its floor and its top is the ceiling,
one storey up. So every balcony matched the apartment whose floor was level
with its ceiling and went up a floor. ``_floor_grid`` already makes exactly
this distinction, in a docstring twenty lines long, for exactly this reason;
the owner rule did not.

Measured before: 8 of 11 apartments had floor cells spanning 3.20 m -- one
storey height -- because the flat's own floor and a borrowed balcony floor
were in the same set. After: every apartment on one plane, all 12 balconies
level-matched, no fallbacks, the same 22,541 cells.

What made this hard to see is that nothing about it looks wrong. The patch is
drawn at the balcony's true position in plan, on a storey one above where it
belongs, and a plan of a floor with a smaller balcony is exactly where a patch
overhanging the outline is visible. Every other suspect checked out: 48 of 48
spaces matched a zone on the correct floor, all 35 zones sat within 0.0 m of
the storey they claimed, and the plan transform fitted to 0.29 m.

### D68 — The prefix is a setting, so nothing may hold it as a constant

``14 |`` leads the name of everything this tool creates (D65), and ``14`` is
right for a project whose layer groups end at 13. The next office numbers
differently, so it became ``--layer-prefix`` and a field in the window.

The interesting half is not the flag. It is that six modules held a name
*derived* from the prefix at module level -- ``DEFAULT_LAYER_NAME =
layer("Results")``, ``EXPORT_COMBINATION = named("Sun Study Export")``,
``VIEW_PREFIX``, ``ATTRIBUTE_PREFIX``, and four more in ``cli`` of which two
were Typer option defaults. Every one of those is evaluated at import, before
a command line has been read, so a run told to use another prefix would have
drawn on ``ZZ | Sun Study.Results`` while ``remove_previous`` searched for
``14 |``, and cleaned up neither. Both names look correct on their own; the
only symptom is a project that quietly accumulates every run it has ever had.

So they are functions now, called at the point of use, and the two Typer
defaults take ``None`` and resolve in the body after the prefix is set. A test
walks the package's AST and fails on any module-level call to ``layer``,
``named``, ``group`` or ``prefix``, because this is not a mistake anybody
would catch by reading -- the wrong version is shorter and looks tidier.

The prefix is process-wide state set once before any work, rather than an
argument threaded through forty call sites. That is the shape of the thing: a
run measures one project with one prefix.

An empty prefix is refused rather than accepted as a tidier name. ``remove_previous``
deletes navigator items whose name starts with it, so an empty one matches
every view and every layout in the project -- the practice's drawings deleted
by a sun study's clean-up. And a prefix *changed* between runs orphans the
last run's sheets, which is stated in the flag's help rather than worked
around: finding them would mean searching by something looser than the prefix,
which is the same danger by another route.

### D69 — A layer says where the apartments are; a name says which of them are apartments

The window was passing ``--apartment-zone-layer`` and nothing else. On the
reference project that layer, ``06 | Zone.Units``, carries 15 dwellings named
``G08``, 20 balconies named ``BY`` and the storage cupboards -- so every run
from the window assessed 41 apartments, two thirds of which have no living
room and cannot have one. It also passed no ``--open-space-zone-layer``, which
falls back to slabs named ``Balcony*``; this project has none, so no balcony
was assessed at all and every apartment was judged on its living-room limb
alone. Both failures are silent and both push the result the same way, which
is why a percentage that came out too low looked like the building's fault.

The command line has had the flags for these since D50. The window did not
offer them, and a setting that exists only on a command line does not exist
for the person the window was built for.

They are offered rather than typed, like every other project property here:
the probe reads the zones once and answers with what they are *called* on each
layer, how many carry each name, and the median floor area of each.

Which of them are dwellings is guessed from area, and the guess is shown. The
names are an office's own codes and mean nothing outside it, while a dwelling
is tens of square metres and a balcony is a few, on every project there has
ever been. The cuts are the ADG's own figures -- 30 m2, under the smallest
35 m2 studio, and a 4 m2 floor under the smallest balcony -- so a storage
cupboard is neither and is left out of both. The line under each field says
what the layer turned out to carry and what was taken, and the chooser lists
every name with its size beside it, because a guess nobody can see is the
thing this window exists to avoid.

Both of Archicad's name fields are offered. The IFC export puts one in
``Name`` and the other in ``LongName``, which is which varies by translator,
and the assessment matches either -- so a person picking from the list is
right whichever way round their project has it.

One case is refused rather than passed on: the open-space layer is only sent
when something narrows it. Naming the apartments' own layer as the open space
with no names to narrow it would take every apartment for a balcony and leave
the assessment with no apartments in it, which is a worse answer than the one
this whole entry is about.

### D70 — A windowed app cannot ask a Windows process to stop, so it leaves a note

Found by pressing Stop during an export. The button's own docstring said
"terminate, not kill: the study restores the project's layer state in a
``finally``". On Windows ``Popen.terminate()`` *is* ``TerminateProcess``,
which runs no ``finally`` at all, so the one thing this tool guarantees about
somebody else's file -- that it puts the layers back -- was being dropped in
precisely the situation the guarantee was written for. On POSIX the default
``SIGTERM`` disposition ends the process without unwinding either. The comment
described an intention that neither platform honours.

The obvious fix does not work here. The only way to *ask* a Windows process to
stop is a console control event, and the packaged app is ``--windowed``: it has
no console, so it cannot send one. Measured, with ``CREATE_NEW_PROCESS_GROUP``
and ``CTRL_BREAK_EVENT``: nothing arrived, and the run was killed at the
twenty-second deadline with the layers as the export had left them.

So the window writes a file and the run watches for it, on a thread whose only
job is to call ``interrupt_main`` when it appears. That raises
``KeyboardInterrupt`` in the main thread, which every ``with`` in this package
is already correct about -- Ctrl-C at a terminal has always been able to
interrupt a run -- and it behaves the same packaged or not, console or none.
The file is never created up front: the run stops when it *appears*, so one
lying about would stop the next run before it began.

Two things are worth knowing about the delay. A pending interrupt is delivered
between bytecodes, so it lands at the end of whatever call is in flight:
immediate during the ray-casting, one Archicad round trip during the drawing,
and not until the translator returns during the export. And a single long
``time.sleep`` swallows it entirely on Windows -- measured at 10 s of 10, against
1.01 s for a main thread doing work. The first version of the test slept, and
failed for that reason rather than for the one it was written to catch.

Terminate and then kill are still there, in that order, behind a twenty-second
deadline. A run too wedged to notice a file is worse than one stopped roughly.

### D71 — The window is grouped by what comes out of it, not by how hard a setting is

The window asked thirty questions in one column, with about half of them folded
into an *Advanced* panel at the bottom. That was the wrong cut, and it got worse
with every study added.

"Advanced" is not a property of a setting. *Facade layers* is not harder than
*Year* — it is simply the facade study's, and it sat under Advanced only because
the reference project needed it named and the next project might not. The
consequence was that no study could be read in one place: somebody setting up the
facade skin ticked a box at the top of the page, then scrolled past nine settings
belonging to two other studies to find the layer list that decides what the skin
is even measuring. A study whose inputs are scattered is a study somebody sets up
half of, and this tool's entire subject is the wrong answer nobody noticed.

So the settings are now a tab per **output** — General, Facade skin, Solar
diagrams — because that is the form the work is asked for in: a job wants the
facade skin, or it wants the solar diagrams. Apartment plans and communal open
space share one section because they are one deliverable, banding a plan by hours
of direct sun and read at the same drawing, so where their sheets are filed is
asked once for the pair.

What survives of Advanced is General, and it is a different claim. The year, the
title block, the layer combination the export starts from, the numbering the
results file themselves under and the wait a slow export needs are not advanced,
they are **shared**: every output is drawn on them, and a copy of each in three
sections would be three chances to disagree. *Also export* is there for that
reason and not because it is obscure — the facade skin and the communal study
both need the zone layers forced into the export, and neither owns the setting.

Tabs cost one thing, and it is the thing this window exists to prevent: a section
nobody opens is a section nobody knows the state of. A colleague could tick
Communal open space in March and never see it again. So it is paid for twice, in
`Window._sync`:

* a tab whose study will run carries a tick on its own label, so the state of
  every section is legible without opening any of them; and
* a line directly above Run names every study queued — *"Will run: facade skin,
  then apartment plans and sheets"* — because with the ticks spread over three
  sections there is otherwise nowhere on screen that answers "what happens if I
  press this", and this is a run of several minutes.

Both read from one list, `Window._studies`, which also carries the names `jobs`
labels the run with. A study called one thing before it runs and another in the
log is a study nobody can follow.

Two sections are declared and empty — **Shadow diagram** and **Sun eye view**.
They are here so the shape of the tool is visible and so a setting arriving later
has a decided place to land instead of being wedged into whichever section is
nearest; the clock-time subset now says in its own tooltip that it moves to
Shadow diagram when that study is built. Each says plainly that it does nothing
yet, and neither carries a tick: an empty page is honest, and a switch that does
nothing is not. They are not greyed out, because a tab that cannot be opened
cannot explain itself and reads as something broken.

Every tab scrolls on its own rather than the notebook sitting inside one
scroller. Solar diagrams is two studies and a dozen questions, each with its
explanation underneath, and is three times General; a single scroller would size
itself to the longest and leave every short tab with a bar that moves nothing.

Nothing about the command lines changed. `jobs` builds the same argv from the
same widgets, which is what the tests assert, and the saved settings are keyed on
names rather than on position for exactly this case — every field kept the name
it was saved under, so a settings file written before the sections existed opens
into them unchanged. The one key that went is `advanced_open`; `open_section`
replaces it, holding a title rather than a number so that inserting a section
cannot reopen somebody on a different page than the one they left, and a title
this build has never heard of leaves the notebook where it is. Losing which tab
was open is the cheapest thing in that file to lose.

### D72 — A shadow diagram is drawn in the plan, and its subject is attribution

The office's shadow diagrams were made from a 3D Document: model the scheme,
set the sun, place the document on a sheet, twenty-one times. That has two
costs, and only the first is about effort.

A 3D Document **cannot be created** through the add-on (D49), so every one of
them is somebody's hand work before this tool can touch it. And a rendered
shadow is *a* shadow. It cannot show the split between the shade that was
already there and the shade the proposal adds — which is the only thing a
consent authority reads the sheet for. Getting that split out of a render means
building the scheme twice, once present and once absent, and subtracting the
two by eye.

Computed, it is two ray casts and a boolean:

```
shaded_before = ~sunlit(context alone)
shaded_after  = ~sunlit(context and the proposal)

existing   = shaded_before
additional = shaded_after and not shaded_before
```

and the same subtraction against a planning envelope answers "how much of that
could have been cast by anything the controls allow here anyway". All three
come off one grid and are differenced against the same `before`, so the fills
abut rather than overlap and their areas add. That last part is tested, because
overlapping masks would put the blue on top of the grey and the sheet would
quietly claim the proposal darkened ground it did not.

**One horizontal plane, not the terrain.** Every consent authority draws to a
flat datum, and it is the only surface on which the outlines stay legible at
1:1000 — draped on a site mesh the shadow steps at every roof edge and every
contour. It is not a claim about the ground, and a sloping site really does
catch a shadow further up the hill than this says.

**The datum is project zero, and this cost a run to learn.** It defaulted to
the lowest point of the geometry, which is the bottom of the basement
excavation — 10.83 m under the street on the reference project. A plane down
there is below the whole site, so everything above shades it: measured, 24% of
the plane permanently dark and 875,156 m² of "existing shadow" on a suburban
block. Archicad's zero is conventionally the ground floor level, which is the
right default; `--shadow-datum` moves it. The related trap is the site mesh
handed in among the context, which fills the sheet solid grey the same way and
cannot be seen in the drawing — `permanently_dark_share` is reported so it
shows up as a number instead.

**One layer to an hour, and a combination to match.** Twenty-one hours on one
layer is twenty-one overlapping fills. They could go on twenty-one worksheets,
which is what the communal hourly plans do — but a worksheet carries only what
this tool draws into it, and a shadow diagram is meaningless without the
neighbourhood under it. The reference sheets show roads, boundaries and every
neighbouring building, and that context is the *model*. So each hour gets a
layer and a Layer Combination that shows it and hides its twenty siblings, and
a View pinned to that combination is the site plan with exactly one hour on it.
The combination leaves every other layer as it found it: what belongs on a site
plan is the practice's decision, already recorded in whatever combination they
draw site plans with.

**North needs no correction here, and that is worth writing down.** The export
reports a true north bearing of 0°, which looks like a lost georeference and is
not: Archicad's Survey Point model position rotates the geometry through
`IfcSite`'s placement and then writes `TrueNorth` as (0,1), because the world
coordinates really are north-aligned (see `cross_check_georeferencing`). The
command runs that cross-check before any number reaches the screen, for the
same reason `archicad-run` does.

**What was not built at the time, and now is.** The LEP envelope was
implemented in `core.shadow` and reachable from Python but not from the command
line, because extruding one wants the site boundary as a polygon and the
boundary Archicad holds is in the project frame while the shadows are computed
in the export's. D77 goes around that rather than through it: a height-limit
massing on a real project is *modelled*, not extruded at run time, so it needs
finding, not building.

### D73 — Fills carry an ID and a group, because they cannot carry each other

The drawings say how much sun something gets; the numbers behind them left the
project by a different door — a CSV, or the run's log. A figure that lives
outside the file it describes is one somebody reconciles by hand, or scales off
a drawing with a rule.

Archicad already has the machinery and it is a Schedule: list every Fill whose
ID contains `SOLAR`, group by ID, sum `Area`. `Area` is a built-in property of
a Fill and `Element ID` is a writable one, so the only missing part was the
tool filling the ID in.

```
SUN STUDY / SOLAR  / 2-3 hrs
SUN STUDY / SHADOW / JUNE 21 -9AM / ADDITIONAL
```

`SUN STUDY` first so one filter finds everything the tool drew — the group word
and not the layer prefix, because `14 |` in an element ID reads as a mistake.
Then the study, because that is the split a reader wants first and what a
schedule filters on. Then enough detail that the same schedule breaks down by
band, or by hour and by whose shadow it is.

**What is deliberately not tagged is the part that matters.** A legend swatch
or a site boundary caught by the same filter is added to the area, and nothing
on the schedule says so. The first cut of this tagged the site boundary
`SUN STUDY / SHADOW / SITE BOUNDARY` and would have added the whole site to
every shadow total. So only measured fills are tagged; legend swatches, the
site boundary and the floor footprint under a sun patch are created untagged
and pass through as `None`. Tested, because none of it is visible in the
drawing.

**Ordering is refused, not repaired.** Create commands answer in the order they
were given, and that order is the only link between a hatch and the hour it was
drawn for. On a count mismatch `stamp_in_order` writes nothing and says so: IDs
against a shifted list would put "9AM EXISTING" on a fill drawn for three in
the afternoon, and a schedule would total it without complaint. That is worse
than no ID at all.

#### Merging is impossible, and grouping is what was actually wanted

`CreateHatches` documents its own limit — *"The 2D coordinates of the hatch
outline (single contour, no holes)"* — with `additionalProperties: false`, so
no holes field can be smuggled in. (`CreateSlabs` and `CreateZones` do take
`holes`; fills do not.) Two apartments, or a shadow and the sunlit courtyard
inside it, can never be one element.

It is also the wrong goal: merged into one element the schedule loses the
per-band area, which is the number the IDs were added for. What Archicad offers
for "these belong together" is a **Group**, and `CreateGroups` (Archicad 26 and
newer) does it: one group per category, keyed on the same string the ID carries
so selection and schedule always agree. The elements stay separate, so each
keeps its own `Area`.

What *was* available was tracing instead of tiling. `series` still merged lit
cells into maximal rectangles while `penetration` had moved to traced outlines.
On a synthetic diagonal band at half-metre cells — the shape a real sun-patch
edge takes — that is 531 rectangles against 17 outlines. The lit area is now
counted off the mask rather than summed from rectangles: equivalent, and it
stays right if the shapes ever change again.

#### No contour, as far as the add-on reaches

There is no switch. `CreateHatches` exposes `contourPenIndex`, which sets
`element.hatch.contPen.penIndex`, and nothing else — no "show contour" flag. So
`BandStyle.contour_pen` defaults to `None`, meaning *draw it in the fill's own
pen*, which is invisible against the fill. A black hairline round every cell of
a patch turns a colour field into a grid of boxes, and at 1:200 the boxes are
what a reader sees.

A genuinely contour-less fill is reachable one way and it needs a person: make
a Favorite by hand with the contour switched off and pass its name, since
`CreateHatches` takes `favoriteName` and applies it before the explicit fields.
The same route D50 needed for a wall's surface override, and the only one there
is.

---

### D74 — A refusal to draw is not a report, and the last save is not a step

Both halves of this came off one colleague's run of the reference project. The
console showed ten sheets built, tiled, given tables and filed, and then:

```
CommandFailedError: Tapir command SaveProject failed: Failed to save the
project. (code -2130312308)
[the study stopped, exit code 1]
```

**The sheets were empty.** Every band drawing had been refused, one line per
band, in red, and the run had gone on regardless:

```
The export and the project disagree about where the apartments are:
0.70 m of residual, over the 0.5 m limit.
```

`_draw_zone_groups` answered `True` for *both* "Archicad refused this" and
"the plan is drawn but incomplete", and the caller appended to `made` without
consulting either. So the layer was empty and the run still built a layout, a
view, a layer combination and a table around it. Ten of them. A blank drawing
under a correct title is worse than a missing sheet, because a reader takes it
for a finding rather than for a failure.

So a refusal now answers `None` and an incomplete report answers itself. The
difference is the whole point: an incomplete plan has fills behind it and is
worth a sheet; a refused one has nothing. `_sheet_per_instant` says so and
stops when it is handed no labels at all.

**The last save is not a step, and nothing may depend on it.** The first
`SaveProject` in `_sheet_per_instant` is load-bearing — a layout is unreadable
until it exists on disk (D39) — and has been guarded since. The last one is
not: by the time it runs the fills are in the model, the sheets are made,
straightened, tabled and filed, and the file reaching disk changes nothing
that follows. It was the function's last statement, unguarded, and it took the
whole study down with it, losing every step of `massing` that came after.

Why Archicad refuses a save it accepted a minute earlier is its own business —
a dialog open in front of it, a `.pln` on a network share or under a sync
client, a lock. `-2130312308` is `APIERR_COMMANDFAILED` — *"the invoked undoable
command threw an exception"*, the generic answer out of
`ACAPI_CallUndoableCommand`, not the file-layer error this entry first called it,
and it names no cause at all. Tapir relays it without adding to it. The tool cannot fix that and should not
pretend to; what it can do is say so loudly, say that nothing is lost, and
carry on. The same guard is on the statistics sheet's final save, which had
the same shape.

`SaveProject` was later called on that same project, on Tapir 1.5.8, and
answered `{"success": true}`. So the refusal was something about the moment —
a dialog, a lock, a sync client — and not a broken command or an old add-on.
That is an argument for the guard rather than against it: a step that works
until it does not is exactly the kind nothing else may depend on.

---

### D75 — A run measures what it names, and a refusal names what disagrees

Two things from the same communal open space study, both about the run doing
work whose result nobody could use.

#### The residual now says which Zone

`0.70 m of residual, over the 0.5 m limit` and nothing else. It is a plan fit
— height is discarded before fitting (`fit_plan_transform` takes `[:, :2]`) —
and it is *relative*: rigid, so a whole export shifted or rotated fits
perfectly. What it measures is the paired Zones disagreeing with each other.
For a communal study those Zones are mostly flats borrowed to place the
drawing and none of them is the thing being measured, which is also why the
message must not say "apartments". It did.

Per-pair distances alone are not the diagnosis, which the first attempt got
wrong. A rigid fit cannot isolate an outlier: it rotates and shifts to split
the difference, so one Zone moved by 2 m leaves every other pair out too.
Measured, one 2 m outlier among five pairs reads

```
1.27, 0.62, 0.58, 0.28, 0.17 m
```

which is indistinguishable by eye from a uniformly stale export. **Refitting
without each pair does separate them.** If dropping one takes the rest inside
the limit, that Zone is the disagreement; if several removals each rescue the
fit, the export has drifted as a whole; if none does, it is badly out of step.
Three verdicts, three different remedies, and the refusal now gives one by
name. Not computed below four pairs, where dropping one leaves two, which fit
perfectly by construction and would clear whichever Zone was asked about.

#### The site surfaces are not computed for a Zone study

The same run measured 1,070,938 m² of facade and 1,946,135 m² of open ground,
every sample ray-cast at every instant of the window, to answer a question
about 828 m² of courtyard. Roughly three thousand times the area, for figures
nothing in the tool reads: `--model-bands` casts its own rays over its own
panels, and the facade and ground tables are consumed only by the report of
them.

So a run measures what it names. Name a Zone and the study is about that Zone;
name none and it is the ordinary massing run, facade and ground, unchanged.
`MassingConfig.assess_facade` / `assess_ground` are `bool | None`, `None`
following the rule, so setting either explicitly still gets both — and the
rule has one home, read through `measures_facade`, rather than one copy per
caller. The CLI states nothing.

A surface that was not assessed is `None`, never an empty band table, and
reads as `facade not assessed` rather than `0 facade samples (0.0 m2)`. The
two are different findings and only one is worth chasing: a facade measured as
zero means the subject filter matched nothing.

On the test fixture — a 93 m² zone in a 1,640 m² site, so a mild case — that
is 4.5x. On the project it came from, the ratio of areas is a thousand times
larger.

---

### D76 — Which storey to draw on and which storey to sheet are two questions

Reported from a real project: the communal fills came out on level 8, where
the Zones are not. The study's spaces were sky terraces at 98, 108 and 127 m.

The storey was chosen with `next(iter(...))` over a **set** of the measured
Zones' storey indices — so which Zone won was arbitrary, and since `str`
hashing is randomised per process, the same study could put its fills on a
different floor on the next run. Whichever won, every cell of every Zone was
then forced onto it: two thirds of that drawing was on a floor its subject is
not on.

`draw_cell_groups` already had the right path. `on_storey=None` groups the
cells by their parent and puts each on that parent Zone's own `floorInd`,
which is what the apartment diagrams have always done; the forced storey
exists for *open ground*, which belongs to no Zone and genuinely has one
level. A Zone study is not that case. So a storey is forced only when the
Zones actually share one.

**And then the sheets disappeared.** The sheet block was gated on that same
forced storey — `if sheet and on_storey is not None` — so the moment the Zones
stopped sharing a level, which is precisely the case the forcing exists to
avoid, no sheet was made at all. Silently: the fills were drawn correctly on
three storeys and nothing was ever put on paper.

They are two questions. *Where may the fills be forced?* Only onto a shared
storey. *Which plans do the sheets come from?* All of them —
`_sheet_per_instant` takes a list and makes a view per storey, so there was
never a choice to make. `_zone_storeys` answers both and is a pure function,
because the bug was in the reasoning and not in anything Archicad said.

A Zone whose storey Archicad will not report is dropped from the second answer
rather than guessed at. It still gets fills, on whatever storey is current,
which is the best available without a home for it — and the run says so
instead of making an empty sheet.


### D77 — A shadow diagram's legend is the project's to declare, not the tool's

`core.shadow` was written for three fills — existing, envelope, additional —
and three is a number taken from one sheet. Set the office's own reference
sheets side by side and it does not hold: SSDA 411 carries two rows, and SSDA
401, the Campsie SSDA set, carries six — existing neighbouring buildings,
future neighbouring context buildings, existing structures within the site, the
current LEP 2023 height limit, the Canterbury Bankstown LEP height limit, and
the proposed building envelope. The same sheet redrawn for Crows Nest swaps the
two LEP rows for a TOD height limit and a SEARs massing. Three hard-coded
constants could draw one of those and never the others, and the constants were
load-bearing in five modules: layer names, Element IDs, group keys, the style
table, the drawing order and the console table.

So `cast_shadows` takes an ordered list of sources instead, and every one of
those six spellings is read off it.

**The roles are the decision, not the count.** Making the list variable is
mechanical; deciding how its members combine is not, and getting it wrong
produces a drawing that reads perfectly and is wrong. Two roles:

A **baseline** is something that will be there — the existing neighbours, the
buildings approved next door, the structures being kept on the site. Baselines
accumulate in the order given, each charged only for the ground the earlier
ones had not already darkened. Their fills abut and their areas add to the
total shadow, which is what lets a reader add the grey rows up.

A **scenario** is something that might be — what a height limit allows, what
the SEARs massing is, what is being applied for. Each is cast against the whole
baseline and against *no other scenario*, so two scenarios overlap. That
overlap is the comparison the sheet exists to make. Differencing them against
each other would show the second one only where it beat the first, which is not
a shadow of anything, and the arithmetic would still produce a plausible
drawing — which is why the role is two flags at the command line rather than
one flag with a keyword in it. A SEARs massing silently demoted to a baseline
would charge the proposal only for what it added on top, and nobody reading the
sheet could catch it.

Cost stays linear: one ray cast per source, a baseline against the baselines
before it and a scenario against all of them. The old three-fill study is
`default_sources`, still the behaviour of a run that names nothing, and its
tests pass unchanged.

**Sources are found by Element ID, not by layer.** The six massings of a
comparison like this are one modelling exercise by one person and they do not
land on six tidy layers; they are, however, named. Prefix-matched, because
`TOD-01`, `TOD-02` and `TOD-PODIUM` are one massing and listing them
individually is a rule nobody keeps current against a model still being drawn.
An element no rule claims is reported by count and by example rather than
dropped quietly: a neighbouring building missing from the baseline is a drawing
whose proposal fill has grown to cover ground that building was already
darkening — wrong in the applicant's favour, and invisible.

**Colours are read, not chosen.** The two ramps — three greys for baselines,
three blues for scenarios — are sampled straight out of SSDA 401's own legend,
so a run reproduces the sheet the office already draws rather than approximating
it. A seventh source in either role cycles back to the lightest step, which is
visibly wrong on the sheet on purpose: the answer there is to pass a colour, not
to have the tool invent one.


### D78 — A shadow lands on what is standing, and a saved view says what stands

Settled on Crows Nest against a real SSDA set, after four wrong answers that
each looked plausible on the sheet. The pipeline below is the one that
reproduces the drawing; every step of it exists because the step before it
was not enough.

**The legend comes from saved views, not from layers.** The eight SHADOW
ANALYSIS views carry four renovation filters between them, so two scenarios
can sit on identical layers and differ only by renovation status — a
layer-derived rule merges them silently and the sheet then compares a massing
with itself. Archicad knows the answer and cannot be asked directly:
`ChangeWindow` takes a navigator item only from Archicad 27, and this is 26.
Publisher can, and applies each view's whole state when it writes it out. So
a Publisher Set of per-view IFCs is the input, one file to a legend row.

**The receiving surface is the terrain and everything standing on it.** This
was the last thing wrong and the one that mattered most. Received on terrain
alone, a shadow reaching a thirty-metre neighbour was drawn as though it
carried on to the ground *underneath* that neighbour — tens of metres too far
in plan, on a site ringed by buildings, in one direction only. Measured: the
existing-neighbour row fell 32% at 9am and 54% at 11am once roofs caught what
they actually catch.

It also cured a symptom misdiagnosed twice. All four scenarios had returned
identical areas at 9am through every run, blamed first on duplicate published
files and then on stray elements shared between the views. Neither was it: with
shadows carrying under buildings the baseline swallowed whatever each massing
did and left the same residual for all of them. On the real receiving surface
they separate at every hour.

One surface for the whole run, built from the terrain and every baseline,
rather than one per source. Per source is truer — a scenario's own roof
catches its own shadow — but each source is differenced against the ones
before it, and two masks measured on surfaces at different heights cannot be
subtracted from one another: the fills would stop tiling and the areas would
stop adding. The baselines stand in every scenario anyway.

**Terrain is a receiver and never an occluder**, claimed before any legend row
sees it. Handed in among the sources it puts every sample below the hill in
permanent shade and prints a solid grey sheet — measured at 81% of the plane.

**Ground the survey does not reach is drawn nowhere.** Off-survey samples sit
on a fallback datum abutting real terrain tens of metres away in height, and a
shadow crossing that step is an artefact of the seam.

**The frame is derived from the two norths, and geometry only checks it.**
Both frames state where north is — Archicad through its georeferencing, the
export through `IfcSite` — and the angle between those answers is the angle
between the frames, exactly. Fitting it from matched geometry instead gave
-33.12, then +0.00, then -32.37 degrees across three attempts while the true
answer, -31.60, never moved: the pairs are an element's plan centre against
its *bounding box* centre, and a box is axis-aligned in whichever frame it is
measured. They are good enough to check on, not to fit on.

**What the drawing looks like is the project's to say.** `CreateHatches` has a
contour pen and no switch to turn one off, and its background pen decides
whether a fill is opaque — and a shadow diagram is read *through*. Both are
left off the request when a Fill Favorite is named, so the Favorite's own
answer survives along with its fill type. The fill pen stays the tool's, since
it is what separates one legend row from the next.

**What was checked rather than assumed.** A 50 m cube on flat ground casts
12,700 m2 at 9am against 12,787 predicted, 6,300 at noon against 6,386, and
13,200 at 3pm against 13,282. Solar noon elevation comes out 32.75 degrees
where Sydney's winter solstice is 32.73. The shadows are long because at 9am
on 21 June the sun is 19 degrees up and a shadow runs 2.9 times the height.


### D79 — A shadow's edge is found, not approximated by the cells it was sampled on

A shadow computed from a grid can only be drawn along cell edges, so at one
metre on a 1:500 sheet its outline is a two millimetre staircase — above pen
width and plainly pixellated. A finer grid is the obvious answer and the wrong
one: cost goes with the *area* while all that is wanted is a better line, and a
staircase with smaller steps is still a staircase.

Sampling was never the limit. The occlusion test answers anywhere, not only at
lattice points, so the boundary can be **found**: it is where the answer flips,
and a flip can be bracketed and bisected. Marching squares gives the topology —
which cells the boundary crosses and in what order, which is what a mask can be
trusted for — and each crossing's position comes from bisecting the lattice edge
it sits on. Measured on a circle of radius 12.3 at one metre: every vertex within
26 microns of the true curve, and the traced area 0.1% off the analytic answer
where counting the cells is 1.2% off. It is *more* accurate than the mask, not
merely prettier.

**Not the silhouette projection this set out to be.** Extruding a massing's
silhouette and intersecting it with a plane is exact and simple; the receiver
here is terrain and every building on it (D78), so exactness there means
three-dimensional boolean geometry and polygon booleans for every difference
between sources. The flat-plane version would undo the roof-catching that moved
the areas by a third at nine in the morning. Bisection gets the same edge on the
receiver that is actually there.

**Holes are the whole difficulty**, because a fill takes one contour and none.
Two routes, and both are needed. A hole can be seamed to its outline by a cut
walked up one side and back down the other — but the cut must not cross
anything, and *which order* the holes are taken in decides whether one exists:
190 of 194 patches seamed first try, and the four that did not were the largest,
where each new cut had to dodge the slivers already made. Six orders are tried.
Where none works the patch is **sliced** instead: sweep a horizontal line, and
between two heights the interior is a run bounded left and right, carried down
as chains and closed only where the shape divides or joins. Slicing asks nothing
and cannot fail.

**What the add-on refuses, established by asking it rather than reasoning.** A
plain square, an edge one millimetre long, nodes half a millimetre apart: all
fine. A bow tie: *Failed to create new Hatch*. A contour that touches itself gets
the same answer. So self-intersection is the whole of it — not size — and every
contour is checked before it is sent, because each fault in this path first
appeared as fills that silently never drew after a run that reported success.

**Three faults worth naming**, all found by measurement after guessing produced
three wrong fixes in a row. The tell each time was a failure count that did not
move: 26 twice, 1,742 twice, 2 twice.

*The seam's sliver opens on a side*, and the wrong side sends the return leg back
across the outgoing one.

*A run can pinch to nothing and reopen* — two lobes touching at a point. It never
divides, so matching by overlap carries the piece through the pinch and the left
chain comes out on the right.

*A horizontal edge is not in the sweep* — it has no crossing — but the boundary
steps sideways along it, and a piece carried through that height recorded only
where it continued from. That cut the corner off every step: 7.15 m² across one
patch, in errors of exactly 0.5 and exactly 1.0, which at a one metre grid is a
little triangle and a little rectangle.

**Areas are unchanged and must be.** They are counted from the cell mask, which
is what tiles and what differences against the other sources; this draws a better
line around the same measurement. On the Crows Nest set every one of the 49
figures is identical to the cell-drawn run, while the fills fall from 22,951 to
2,616.

---

### D80 — A building ends at a storey, so that is what the cut names

`--exclude-above <metres>` (D30) is right about the mechanism and wrong about the
unit. The metre has to be looked up per project, and looking it up wrongly is
invisible: the elements simply stay, and the run reports a plausible number
measured through them.

That is exactly what happened on the Kogarah project. The run's own
`top of apartments 193.5 m` suggested 195 was a generous cut, and it removed
almost nothing — because the 193.5 was itself measured from parked Zones. The
real building ends at storey 20, `LIFT OVERRUN`, level **49.8 m**. Storeys 21 to
108 hold a quarter of a kilometre of parked test-fit material above it, to 338 m:
`UT1.07`, `UT2.12`, `Unit Types`, `BLD A - North Walls L4-L5`. That stack was
shading the tower it belongs to, which is why the lower floors read as sunless.

`--exclude-above-storey "LIFT OVERRUN"` names the storey instead and resolves it
through `GetStories` at run time. "Above the lift overrun" is a sentence about a
building and, as Sami put it, usually the same name in every project; 49.8 is a
number somebody has to find first. The name is matched the way a name is copied —
stripped and case-folded — and one that matches nothing is refused with the
project's own storey list, since a name that misses is normally a name from
another job. It resolves once, next to the connection, so everything downstream
still sees the metres D30 describes.

---

### D81 — Zero minutes is two findings, and only one of them has a remedy

An apartment that receives no direct sunlight is reported as one number. On
Kogarah that number was 22 of 91, and it turned out to be two unrelated
groups.

Four flats face 139 degrees. Between 09:00 and 15:00 on 21 June the sun runs
from bearing 54 through north to 306, so it is never in front of that glazing
at all — not once, before anything is asked about what stands nearby. Nothing
is shading them and nothing can stop shading them: the aspect is the answer,
and it was settled at sketch stage.

The other eighteen face 81 to 197 degrees. The sun does come round to them,
for a few of the thirteen half-hourly instants, and **every one of those is
blocked** — by the building's own fabric within 3.5 m, an `SD2.x` sliding door
across a recess, the wall opposite, the slab overhead. That is an obstruction,
and an obstruction is a design question with options.

Told as one figure the two mislead in opposite directions: the first four send
someone hunting for a shadow that is not there, and the eighteen disappear into
what looks like an unavoidable orientation problem. So `reached_by_sun` runs
the facing half of `sunlit_matrix` with no rays cast, and the sunless count
now names how many of itself the sun never reaches. Asked only of an apartment
that received nothing, because for one that got sun the answer is already
known; `None` from a caller that never ran the test stays silent rather than
becoming a finding.

### D82 — Two add-ons, because the missing commands are Archicad's and not Tapir's

The office's solar penetration diagram is made by aiming the 3D window along
the sun for one hour, making a 3D Document from it, and repeating. Neither of
those first two steps can be asked for from outside Archicad. Tapir 1.5.8's
command catalogue, read out of the installed `.apx`, holds no camera,
projection or 3D Document command, and its `ViewSettings` schema stops at the
layer combination, scale, rotation, zoom and 3D style. Archicad's own API
answers `API.Get3DProjectionInfo` with *"not found"*, code 2002.

So the gap is not Tapir being behind. It is that the JSON API has never
exposed `APIEnv_Change3DProjectionSetsID` or `APIDb_NewDatabaseID`, and only
a compiled add-on can reach them.

Three ways to close it, and the choice matters for years rather than for this
feature. **Forking Tapir** means the office runs a custom build of a tool it
otherwise gets from a release page, and every Tapir release has to be
re-merged across the five Archicad versions Tapir supports. **Waiting for an
upstream pull request** puts the delivery date in somebody else's review queue.
**A second add-on** costs one more file to install and nothing else: two
add-ons coexist, each registering its own command namespace, and the transport
is `API.ExecuteAddOnCommand` either way — which is why `run_loriini` is
`run_tapir` with one word changed.

The second add-on won, and it is deliberately three commands wide. Everything
Tapir already does keeps going through Tapir. If Tapir ever ships these, ours
is deleted and one constant changes.

Two things follow from that narrowness. The add-on holds no analysis: the menu
item starts the Python app rather than computing anything, because a second
implementation of the astronomy is a second thing to be wrong. And the sun is
handed to Archicad as a **date** rather than as angles, through
`API_SunPosition_GivenByDate`, so Archicad computes the sun from the project's
own georeferencing. Pushing our own angles in would make the two agree by
construction and destroy the only free cross-check available — the same
reasoning as the georeferencing check in [D23](#d23--archicads-north-angle-and-why-the-cross-check-compares-sums).

`API_AxonoPars::tranmat` was the one thing this could not settle from a
header, and `GetProjection` shipped as the instrument to settle it with. It
has been: read against the office's own hand-aimed sun view on the Kogarah
solar study, the matrix is what `ViewMatrix` builds, its frame is the
project's rather than true north, and `SetProjection` and
`CreateDocumentFrom3D` both hold on a live Archicad. The measurement, and the
two kit facts the first build got wrong on the way, are in `docs/addon.md`.

### D83 — The site analysis is a port, drawn where the office works, and the site lands by the project's own georeferencing

The office's `au-site-analysis` generator already answers "what is around this
address": a TypeScript pipeline over NSW open data that prints the context
analysis, the site analysis and the development summary to an A1 PDF, with a
DXF beside it for import. Asked to bring that into Loriini, three choices
were open and each was made the same way.

**Port, not wrap.** Running the TypeScript from Python -- a Node on the
workstation, a subprocess, a DXF merged by hand -- would have kept one
implementation but put Node, Chromium and `pyproj` on machines that cannot
install anything (see the workstation note in memory). The fetch layer is
small and stdlib-shaped: ArcGIS REST queries, Overpass, one Valhalla call.
So it is ported, endpoint for endpoint and rule for rule, into a `site`
layer that knows nothing about Archicad, with the transverse Mercator
arithmetic written out (Redfearn, from the GDA Technical Manual, checked
against its worked example to the millimetre) rather than imported. The
approved dependency set is unchanged.

**Draw, not import.** The generator's DXF puts everything on `SA_` layers in
MGA metres for a person to merge. Drawing the same content as native elements
into a worksheet -- through the add-on's `CreateWorksheet` (D33) and
`CreateFills` with the legend's RGB (D82), then Tapir's polylines and texts
(D60) -- means no file on disk, no merge dialog, no pen matching, and a
rerun that clears its own worksheet. The aerial is the one thing not drawn:
no command places a picture, so it is saved with world files for placing by
hand.

**The site lands where the project says it is.** A bundle is in longitude
and latitude and the project has a location and a north angle, so there is
exactly one place for every point: projected to the MGA grid, moved so the
origin's own grid position is zero, turned so true north sits where the
project's north angle puts it -- corrected for the grid's own convergence,
about one degree at Sydney, which is the difference between a survey's
north and the sun's. It is the same turn the sun eye views make in the other
direction, tested against the same figure, so a site and a sun study cannot
face different ways. A project whose location is still the Sydney preset
would put the site eleven kilometres off; the run says so, and
`--anchor site` puts the site's centre at the origin instead.

What follows from the port: the site bundle stores every OpenStreetMap
building footprint in its extent with the storeys and height OSM records,
which the sheets only label. That is the material for the next step, a
massing of the neighbours on a context layer, and the reason it is fetched
now rather than later.

### D84 — A worksheet made in the session is entered by a person, and the run waits for that

Measured on the Kogarah solar study on 11 September 2026, drawing the site
sheets. The add-on's `CreateWorksheet` was refused from its undo scope, the
same fault D82's 3D Document command had, and is fixed. Tapir's
`CreateWorksheets` then made the worksheet, and entering it was refused too:
`APIDb_ChangeCurrentDatabaseID`, the add-on's own call, answers
`APIERR_BADDATABASE` for a worksheet made in the session, so the wall D33
blamed on Tapir's `ChangeWindow` is Archicad's. A save did not lift it.

What does lift it is the one thing the API cannot do: a person opening the
worksheet. With it double-clicked in the Project Map, `GetCurrentDatabase`
reported the worksheet by id and by name, and the site sheet drew into it.

So the run no longer claims to enter a worksheet it cannot. It creates one
if there is none, tries the move once, and then polls for up to
`--wait-minutes`, saying exactly which worksheet to open. Matched by database
id rather than by name, because the add-on reports an empty name for a floor
plan and a name is a weaker key than an id anyway. A rerun in a later session
enters the existing worksheet without help, as the docs measured for the
penetration series.

The alternative -- drawing into the floor plan and asking a person to move
the elements -- was not taken. A context sheet is 6,000 elements at
1:3000 over a kilometre of the model's storey, and nothing about that is
tidier than a click.

### D85 — The context model fetches its own square, and reads the ground from an index

Asked for on 11 September 2026, after the site sheet's 34 footprints had
been modelled: "a large context, ideally 1 km radius" became 600 m and then
"make it 500 m". The site sheet's bundle covers about 300 m; the model
wants a kilometre across, and the furniture query that serves the sheet
(fifteen kinds at once, sized for 1:500) is the wrong fetch for it.

So `run_model` fetches a square of `--model-radius` metres each way: the
contours, the height-of-building control, and the footprints alone through
one Overpass query, saved as `data/model.json`. It is a `SiteBundle` with
the sheet fields empty rather than a fourth kind: the model reads the same
five fields either way, and one loader serves both. At Kogarah that is 483
footprints and 36,191 contour vertices, fetched in 38 s and modelled in 5 s.

Two things had to change for that size. The ground under each slab was
found by scanning every contour vertex for every footprint -- fine at 34,
seventeen million distances at 483 -- and is now a 40 m grid of the
vertices, the same inverse-distance-between-two-levels answer from the
cells around the point. And the mesh's level lines are thinned to 12,000
vertices, halving contours and points in turn, because Archicad
triangulates between every one and a kilometre of 2 m contours reads as
ground long before that. `--model-radius 0` keeps to the site sheet's
bundle for a small study.
