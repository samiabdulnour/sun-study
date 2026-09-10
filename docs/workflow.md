# Workflow: ADG solar access from an Archicad model

Written for a practice that already follows the FUSE Archicad Manual (QA01) and its
Layer and Combo Matrix. **Almost nothing here is new convention.** The manual already
says where apartments live, what shades, and where a solar-access verdict is recorded;
this document connects those to the tool and names the four gaps that need filling.

---

## The idea

Three conventions carry everything the analysis needs.

| Question | Answered by | Source |
|---|---|---|
| Which apartments are being assessed? | `06 \| Zone.SEPP 65` zones | Layer matrix 87 — *"unit zone duplicate, used to schedule SEPP 65 + Diagrams"* |
| Which glazing counts as living-room? | `_L` suffix on the window/door ID | Office convention |
| Which is private open space? | `06 \| Zone.Balcony` zones | Layer matrix 85 |
| What casts shadow? | Building, plus `01 \| Floor Overhang.Shadow Calcs` and `03 \| Site Contexts.3D` | Layer matrix 13, 39 |
| Where on earth is it? | Project Location | — |
| Where does the answer go? | `Daylight` Y/N on the zone | Manual §5.10.2, p151 |

The last row is the important one. The manual already ends with a human ticking
`Daylight` = Y or N per apartment, which drives the graphic override that colours the
SEPP 65 diagram and fills the schedule. **This tool replaces the ticking, not the
drawing.** Everything downstream of that property is unchanged.

---

## Step 0 — four additions to the office template

Done once, not per project.

**1. An IFC translator named `Solar Study Export`.** The general-purpose translator is
wrong for this in both directions: the export used to build this workflow was 283 MB and
omitted Zones entirely, while carrying 1.7 million property values the tool never reads.
Settings are in [Step 2](#step-2--export).

**2. A layer combination `06 | SEPP 65 Export`.** Drives the translator's element filter.
Contents in Step 2.

**3. The `Sun Study` property group.** Created by `sun-study init-properties`, but adding
it to the template means every new project has the schedule columns already. Nine
properties: the measured hours, the verdict, and the provenance.

**4. `_L` written into the manual.** It is currently office knowledge, not documentation.
It is also the single convention that most changes the compliance percentage, so it
belongs in §5.10 beside the `Daylight` instruction.

> **`_L` marks habitable rooms — rooms that require sunlight — not living rooms
> specifically.** Confirmed by the practice. It is a room-level marker, unrelated to the
> unit-level `Liveable` and `Adaptable/Livable` properties, which record the accessible
> housing standard.
>
> **ADG 4A-1 is about living rooms**, so the two are not the same question. An apartment
> whose bedroom faces north and whose living room faces south passes on the bedroom's
> sun if they are pooled. Every run prints an openings-per-apartment spread for exactly
> this reason: one or two per apartment is a living-room set, three or four in a
> two-bedroom apartment is a habitable-room set. Narrowing to living rooms needs a
> second marker the model does not carry yet — see [D24](decisions.md).

---

## Step 1 — modelling checklist

Per project, all of it already in the manual except where noted.

- [ ] **Project Location** set to the real site coordinates and north.
      Exact arcminute values such as `-33° 52' 00"` are the Sydney preset, not a site.
      `archicad-info` now warns when both coordinates land on a whole arcminute; one
      real project was still pointing at the Sydney CBD preset, 11 km from its site.
- [ ] **Unit zones** on `06 | Zone.Units`, as usual.
- [ ] **SEPP 65 zones** on `06 | Zone.SEPP 65` — one per apartment, per the manual.
- [ ] **Zone Number = the unit number** on those SEPP 65 zones, and Zone Name = the type.
      *This is a change.* Today Zone Number holds the type (`2B`, `1B`), which means 60
      apartments share one label and a compliance schedule cannot be read by a human.
      The `Apartment ID` stamp field is also still at its `<Apartment ID>` default.
- [ ] **Balcony zones** on `06 | Zone.Balcony`.
- [ ] **`_L` suffix** on the ID of every living-room window and sliding door.
      Doors matter — in the reference model 110 of 252 livable openings were `SD2.x_L`
      sliding doors, which for an apartment is usually *the* living-room glazing.
- [ ] **Overhangs** modelled on `01 | Floor Overhang.Shadow Calcs`.
- [ ] **Context** on `03 | Site Contexts.3D`.
- [ ] **Zones classified.** Any classification system will do; Archicad attaches custom
      properties through classification, so an unclassified zone silently refuses to
      accept a result.

---

## Step 2 — export

One IFC containing geometry **and** zones. Two separate exports cannot be combined —
the analysis needs the windows and the zones in the same coordinate system.

### Layer combination `06 | SEPP 65 Export`

| Include | Why |
|---|---|
| `01 \| Wall.External` | The facade, and the thing that shades it |
| `01 \| Floor.Structural`, `01 \| Floor.Finish` | Slabs and their edges |
| `01 \| Floor Overhang.Shadow Calcs` | Exists for exactly this |
| `01 \| Balustrade` | Solid balustrades shade the storey below |
| `01 \| Columns`, `01 \| Beams`, `01 \| Core`, `01 \| Roof.Structural` | Self-shading |
| `01 \| Wall.Unit Internal` | Cheap, and keeps space boundaries intact |
| `03 \| Site Contexts.3D` | Neighbouring buildings |
| `06 \| Zone.SEPP 65` | The apartments |
| `06 \| Zone.Balcony` | Private open space |

| Exclude | Why |
|---|---|
| **`13 \| HLinks.Unit Types`** | Masters are placed on the AHD level. They would export as real geometry at the datum, shading the building from below and inflating facade area. |
| `10 \| Calc.*` | GBA/GFA/NLA/Landscape/Storage zones would be counted as apartments |
| `06 \| Zone.Internal`, `06 \| Zone.Units` | Duplicate the SEPP 65 zones |
| `02 \| Furniture.*`, `02 \| Joinery` | Indoors, shades nothing |
| `05 \| Dims/Notes.*`, `00 \| *` | Annotation and working layers |

### Translator settings

| Setting | Value | Why |
|---|---|---|
| Elements to export | Filtered elements → `06 \| SEPP 65 Export` | |
| **IFC Space boundaries** | **On** | Maps each window to the room it serves. Without it the tool falls back to geometric containment, which is a guess. |
| Space containment | On | |
| Element Classifications | On | Carries the `Daylight` classification |
| **Properties to export** | **Element Parameters only** | `All properties` produced 1.7 M `IfcPropertySingleValue` entities and a 283 MB file. None are read. |
| Convert IFC Annotations / 2D elements | Off | Bulk, no geometry value |
| Convert 2D symbols of Doors and Windows | Off | As above |
| Convert Grid elements | Off | As above |
| IFC Model position | either | Survey Point and Project Origin both work; the tool reconciles them |
| Partial Structure Display | Entire Model | |

---

## Step 3 — run

If more than one Archicad is open, check which port your project is on first — each
instance gets its own, and the default 19723 belongs to whichever started first:

```powershell
sun-study archicad-ports
```

With the project open in Archicad and the Tapir add-on installed:

```powershell
uv run sun-study archicad-info --properties     # connection, zones per layer, names, classification
uv run sun-study init-properties                # once per project, or ships in the template

uv run sun-study archicad-run --timezone Australia/Sydney `
    --livable-suffix "_L" `
    --apartment-zone-layer "06 | Zone.SEPP 65" `
    --open-space-zone-layer "06 | Zone.Balcony" `
    --write --draw --sheet
```

**Check the zones-per-layer breakdown before trusting the layer names above.** A project
can carry apartments on `06 | Zone.SEPP 65`, a duplicate set on `06 | Zone.Units`, and
area take-off on `10 | Calc.*` — all of them Zones, and in one real file all named
`RESI`. `archicad-info` counts them per layer and warns when two `06 | Zone.*` layers
hold the same number, which is what a duplicated set looks like. Passing both would
count every apartment twice.

Where one layer carries dwellings *and* balconies — 15 units named `G08` beside 20
balconies named `BY`, which is the ordinary case — add `--apartment-zone-name` and
`--open-space-zone-name`. Without them every zone on the layer is an apartment, and a
balcony assessed as one is a flat with no living room: it fails, silently, and takes the
percentage down with it. The window offers both, read from the project.

`--livable-suffix` matches windows **and** doors, so balcony sliders count. The layer
names must be typed exactly as Archicad holds them — a filter that matches nothing stops
the run and prints the layers the file does contain, rather than reporting a building
with no apartments.

`--draw` puts the answer on the floor plan: one coloured fill per apartment, taken from
the Zone's own outline and placed on the Zone's own storey, plus a legend. It all goes on
one layer (`14 | Sun Study.Results` by default), and re-running deletes the previous set
before drawing the new one.

`--layer-prefix` is the `14 |` in that name, and leads every layer, layer combination,
surface, view and layout the run creates, so the output files itself inside the office's
own numbering — `14` because the reference project's layer groups end at `13`. It is
also how a rerun finds its own sheets to replace, so a run given a different prefix from
the last one leaves the last one's behind to be deleted by hand, and an empty one is
refused rather than accepted.

Colours are **pen indices**, because that is what `CreateHatches` takes — so the run
reads the project's own pen table and gives each band the pen closest to the reference
study's colour for it. Nothing to configure, and the mapping is printed:

```
  matched band colours against 255 pens in the project's pen table:
    0 hrs    rgb(8, 48, 107)   -> pen 91   exact
    2-3 hrs  rgb(230, 238, 156)-> pen 94   close (off by 21)
```

**Read the labels.** `POOR MATCH` means the pen table has no pen near that colour, and
the band landed on whatever was least far away. A separate warning names any two bands
whose pens are near-identical in colour — different pen numbers, same look on paper, so
the boundary between them cannot be read. Override any band with `--pen`, repeatable,
applied after matching so the others keep their matches:

```powershell
--pen "2-3 hrs=42" --pen "5+ hrs=47"
```

An override naming a band that does not exist is an error, not a no-op — `--pen
"2-3 hours=42"`, with "hours" rather than "hrs", would otherwise draw the defaults and
look entirely correct.

Two limits it reports rather than hides: an apartment wrapping a lift core is drawn over
the void, because a hatch is a single contour; and curved zone edges become straight
segments between their nodes.

`--sheet` adds the last step of the manual's own sequence: a Layout carrying one Drawing
per storey that has fills, at 1:200. The Drawings are **linked**, so re-running the study
updates the plan and the sheet follows — the reason this goes through the View Map rather
than drawing into a worksheet directly, which Archicad's API cannot target anyway.
It never deletes: a re-run makes a new Layout rather than rebuilding one that may carry a
title block and a revision history. And it is never fatal — the fills are already in the
project, so a sheet that could not be made is reported and the run finishes.

`archicad-run` exports through the translator, reads the IFC, cross-checks the
georeferencing against Archicad's own answer, assesses, and writes back. Nothing is
printed before the cross-check passes, so a lost or re-interpreted north stops the run
rather than colouring a diagram wrongly.

For massing studies, before apartments exist, `sun-study massing` reports facade and
ground area banded by sunlight hours. That needs no zones and no `_L` — it is the metric
a massing optimisation maximises, and it is not the ADG criterion. The output says so.

---

## Step 4 — the result

Two things land on every SEPP 65 zone.

**`Daylight` = `Y` / `N`** — the existing property from manual §5.10.2. The graphic
override colours the diagram from it and the schedule template reads it. Nothing
downstream changes.

**The `Sun Study` group** — the audit trail, because a bare Y/N cannot be checked:

| Property | Example |
|---|---|
| Living Room Sunlight (h) | `2.35` |
| Private Open Space Sunlight (h) | `3.33` |
| Governing Sunlight (h) | `2.35` |
| Meets Minimum | `Yes` |
| No Direct Sunlight | `No` |
| Counted in Compliance | `Yes` |
| Sun Study Ruleset | `nsw_adg@1.0.0 / sydney_metro (2h cumulative)` |
| Sun Study Run | `2026-08-13 07:41 UTC` |

**Sun Study Run is the one to look at.** A value older than the last massing change is
stale, and a stale number looks exactly like a current one.

---

## Step 5 — before quoting a figure

- **Read the run header.** Site, north bearing, ruleset version, continuity setting and
  the `_L` interpretation are all echoed before any number.
- **Check the apartment count** against the accommodation schedule. If it disagrees, the
  layer combination is wrong.
- **Check `Sun Study Run`** against the date of the last design change.
- **Do not submit it.** This tool is for design iteration. A DA submission rests on a
  consultant's report, and the disclaimer prints on every run for that reason.

---

## Shadow diagrams

A different study from the one above, sharing the export machinery and almost
nothing else. Settled against a real SSDA set on Crows Nest; see
[D78](decisions.md) for why each step is there.

**Once per project.** Make a view per legend row under one folder — the legend
is whatever the argument needs, six rows on one job and two on another — plus
one for the terrain. Put them in a Publisher Set publishing **IFC** to a
folder. Views rather than layers because a view carries its renovation filter,
and two scenarios can share layers and differ only by that.

Make a Fill Favorite with the contour off and the fill type you want. A
contour-less fill cannot be reached any other way.

**Every run.**

```
sun-study shadows --port 19723   --shadow-from-views <folder the Publisher Set wrote>   --shadow-date 06-21 --shadow-hour 9,10,11,12,13,14,15   --shadow-grid 1 --shadow-datum 91 --shadow-storey 6   --shadow-favourite "<your Fill Favorite>"   --shadow-terrain  "TERAIN"   --shadow-source   "EXISTING NEIGHBOURING BUILDINGS"   --shadow-source   "FUTURE NEIGHBOURING BUILDINGS"   --shadow-source   "EXISTING STRUCTURES WITHIN THE SITE"   --shadow-scenario "TOD - scenario 2"   --shadow-scenario "TOD + 20% - scenario 3"   --shadow-scenario "SEARS - scenario 5"   --shadow-scenario "Proposed"   --draw --sheet
```

Publish the set first; the run reads what Publisher wrote and does not export.
About twenty minutes to publish, seven to run.

The same study is on the **Shadow diagram** tab of `Loriini.exe`. Point
*Published views* at the folder and the three lists below it are ticked from
the IFCs actually in it, rather than typed. There is deliberately no layer
route on that page: a layer combination cannot reproduce a view that differs
by renovation filter, which is the ordinary case here, and offering both would
offer a way to be quietly wrong.

`--shadow-source` is something that will be there and accumulates.
`--shadow-scenario` is something that might be, cast against every source and
against no other scenario, so two of them overlap — which is the comparison.
`--shadow-edge` is how close a drawn outline is brought to the true shadow
boundary, in metres; the edge is found by bisecting against the geometry rather
than followed along cell edges, so it runs at whatever angle the sun makes
instead of as a staircase. `0` draws on cell edges, which is what every version
before [D79](decisions.md) did. `--shadow-datum` is where ground is taken to be
beyond the survey, and
`--shadow-storey` is the storey the fills are drawn on, which is the site plan
rather than a datum storey: on this project storey 0 is `AHD` and GROUND is 6.

**Three lines of the output are worth reading every time.**

*`draped onto N terrain and M building triangles; X% ... left undrawn`* — the
share of the grid with no survey under it, which is drawn nowhere. A large
share is not an error, but it is how much of the sheet is missing.

*`drawing frame: rotated ... checked against N elements (typical miss ...)`* —
under a metre is bounding-box noise. A warning here means the fills are not on
the model, and nothing about the sheet will show it.

*`N solid(s) match no --shadow-source ... and cast nothing`* — listed by layer.
Interior fabric belongs in that list; a *context* layer in it is a legend row
that will be missing from the sheet.

**What will still be wrong, and only the model can fix it.** A massing that is
both future context and a scenario is tested against itself and reads as
nothing. An approved-but-unbuilt tower counted among the existing neighbours
dominates the baseline and absorbs the site's own shadow. Both are a view
showing more than its name says.

## Sun eye views

The solar penetration diagram: the model seen from the sun, so every pane the
sun reaches is a pane you can see. The office makes it by aiming the 3D window
by hand for each hour, which lands within a few tenths of a degree; this aims
it exactly, and each view carries Archicad's own sun for its instant. Needs
the Loriini add-on beside Tapir -- see [addon.md](addon.md) -- because the
projection is the one thing Tapir has no command for.

**Once per project.** Nothing. The override, the renovation filter and the
layer combination it starts from are all read off the project.

**Every run.** Click a floor plan tab first; the command refuses to move the
database under another window, because Archicad then refuses the projection.

```
sun-study sun-eyes --port 19724 --pen-set "00 FA Pens"
```

What it makes, all under the tool's prefix:

| | |
|---|---|
| `14 \| Sun Eye Views` | one view of the 3D window per hour, 9am to 3pm on 21 June, aimed along the sun |
| `14 \| Sun Eye 21 Jun 09:00` ... | seven 3D Documents in the Project Map, each with its own projection and sun |
| `14 \| Sun Eye Documents` | a view of each document at 1:200 |
| `14 \| Sun Eye Views 09:00-12:00`, `13:00-15:00` layouts | the documents on two sheets, a morning and an afternoon, four to a sheet |
| `14 \| Sun Eye Views` layer combination | `04 \| Shadow Diagrams` with every zone-carrying layer hidden |

Every view is pinned to the `Sun Eye Views` graphic override, the planned
renovation filter, `DA General Arrangement` model view options and the pen set
given. Each of those is an option if a project differs; the date and hours come
from the ruleset's assessment window.

A drawing placed from a 3D Document starts at a placeholder size, about 50 mm
square, and takes its real size when Archicad updates it. Open the layout; if
the drawings are still small, select them and Update. `--scale` and
`--per-sheet` change the size and the split.

Two things to know. **Save before closing**: views, documents and combinations
made through the API are ordinary project changes, and an unsaved close loses
them. And **nothing here can be deleted or re-aimed by the tool** -- a second
run keeps what is already there under its names and fills only the gaps, so
to redo an hour, delete its view and document in the Navigator first.

## What this still needs

Honest status, so nobody plans around something that does not exist.

**Built** — `--livable-suffix` across windows and doors (D24), layer-based selection of
apartment zones, open-space zones and context (D25), zone-based private open space
gridded on the floor rather than the ceiling, and `archicad-info --properties`.

**Still to build:**

| | |
|---|---|
| Writing the existing `Daylight` property | Needs the exact group and property name from `archicad-info --properties`, run on a project built from the template. Guessing it would create a second column that looks right and drives nothing. |
| Curtain-wall glazing | See question 3 below — no proposal yet |

**Open questions for the practice:**

1. **What does `_L` mean** — living room, or habitable room? (Step 0.)
2. **Will SEPP 65 zones carry unit numbers?** Without them the Archicad schedule still
   works, but exported CSV/JSON cannot be read by a human.
3. **Curtain wall.** In the reference model the facade was 33 curtain walls and 5,553
   members against 219 windows, and the `_L` convention is not used on curtain wall
   panels. Where a living room is glazed by curtain wall rather than a window, there is
   currently no way to mark it.

**Not solvable by convention:** the tool measures geometric direct sunlight. It is not a
daylight, illuminance or energy model, and it does not replace a consultant.
