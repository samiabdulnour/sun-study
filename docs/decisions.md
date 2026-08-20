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
