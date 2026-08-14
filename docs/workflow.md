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
    --write --draw
```

**Check the zones-per-layer breakdown before trusting the layer names above.** A project
can carry apartments on `06 | Zone.SEPP 65`, a duplicate set on `06 | Zone.Units`, and
area take-off on `10 | Calc.*` — all of them Zones, and in one real file all named
`RESI`. `archicad-info` counts them per layer and warns when two `06 | Zone.*` layers
hold the same number, which is what a duplicated set looks like. Passing both would
count every apartment twice.

`--livable-suffix` matches windows **and** doors, so balcony sliders count. The layer
names must be typed exactly as Archicad holds them — a filter that matches nothing stops
the run and prints the layers the file does contain, rather than reporting a building
with no apartments.

`--draw` puts the answer on the floor plan: one coloured fill per apartment, taken from
the Zone's own outline and placed on the Zone's own storey, plus a legend. It all goes on
one layer (`Sun Study.Results` by default), and re-running deletes the previous set
before drawing the new one.

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
