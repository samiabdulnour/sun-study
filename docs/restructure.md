# Loriini: one program, two toolsets

*Drafted 11 September 2026 for the Monday review. This is the plan the
restructure follows; what is done is marked, what is proposed is marked.*

The program began as a solar-access checker and grew a site-analysis wing,
a shadow wing and a sun-view wing, each with its own tab, its own flags
and its own words for the same things. This note fixes the words first,
then the shape.

## 1. The words

One name per thing, used in the window, the command line, the Archicad
navigator and the docs. Title case in Archicad and the window; kebab-case
on the command line.

| Toolset | Tool | What it makes | Was called |
|---|---|---|---|
| **Site tools** | **Context Analysis** | the neighbourhood sheet at 1:3000 (DA 003): zoning, transport, walking times, aerial | "context analysis" |
| | **Site Analysis** | the site sheet at 1:200–1:750 (DA 004): cadastre, contours, furniture, neighbours' heights | "site analysis" (also the tab, the tick and the job) |
| | **Summary of Controls** | the planning-controls table, drawn on a layout of its own | "development summary" |
| | **Context Model** | terrain, street blocks and neighbouring buildings in 3D, on `LORIINI` | "model the terrain and the neighbours", "context geometry 3D import" |
| **Solar tools** | **Solar Analysis** | hours of direct sun, assessed against the ADG: **apartments**, the **facade** (massing stage) or **communal open space** | "apartment plans and sheets", "facade skin", "communal open space", "solar diagrams", "sun study" |
| | **Shadow Diagram** | shadows cast hour by hour on the assessment days | "shadow diagram", "shadows" |
| | **Sun Views** | the model seen from the sun, one view and one 3D Document per hour | "sun eye views", "sun eye view", "solar penetration diagram" |

The product is **Loriini**: the window's title, the add-on's name, the
preferences folder and the executable already say so. "Solar Analysis"
stops being the product's name and becomes one tool's. The Archicad
property group stays `Sun Study` (D-something: renaming a property group
orphans the written values).

Archicad names follow the tool: `14 | Context Analysis`, `14 | Site
Analysis`, `14 | Summary of Controls`, `14 | Solar Analysis.*`, `14 | Shadow
Diagram*`, `14 | Sun Views*`. The prefix stays a setting.

Command line, one command per tool, the toolset as the group:

```
loriini site  context|site|summary|model   "ADDRESS" [options]   (any subset; all four by default)
loriini solar apartments|facade|communal   [options]
loriini shadows                            [options]
loriini sun-views                          [options]
loriini archicad ports|info|probe|selftest|rooms|objects|report|init-properties
loriini ifc info|run|rulesets              MODEL.ifc
```

The old names (`site-analysis`, `archicad-run`, `massing`, `sun-eyes`,
`archicad-*`) stay as hidden aliases for one release so the window's saved
settings and anybody's shell history keep working.

**Status:** the window and the naming words are renamed in this pass; the
command-line regrouping is proposed (the aliases are cheap, the split of
the 6,900-line `cli.py` into a package is the work).

## 2. The shape of the window

Three tabs where there were six, and nothing asked twice.

```
[ Archicad: 2614_Kogarah ... · port 19724  ▾ ] [Refresh]

┌ General ──────────┐┌ Site tools ─────────┐┌ Solar tools ─────────────┐
│ Layer prefix       ││ Address              ││ ┌ Model ─┐┌ Solar ┐┌ Shadow ┐┌ Sun views ┐
│ Sheet master       ││ Run folder           ││ export combination, also export,
│ Title block (mm)   ││ [x] Context Analysis ││ neighbouring layers, keep off,
│ Archicad wait      ││     scale            ││ ignore above, year
│ Year               ││ [x] Site Analysis    ││ ...
│                    ││ [x] Summary of Controls
│                    ││ [x] Context Model    ││
│                    ││     radius, location ││
└────────────────────┘└──────────────────────┘└──────────────────────────┘
 Will run: ...                                   [Run] [Stop] [Save as default] [Forget]
```

- **General** holds what every tool that makes a sheet needs: the layer
  prefix, the sheet master, the title-block width, the Archicad wait and
  the year. The title-block width lived in the Sun Views tab; the master in
  General; the year in General — now all three are where every layout
  reads them.
- **Site tools** is one form: the address, the run folder, four outputs
  with their own two or three fields under each.
- **Solar tools** has an inner **Model** section — the export layer
  combination, the layers forced on, the neighbours, the layers kept off,
  the height cut — because the three solar tools export the same model and
  asked for it three times. Then one section per tool: Solar Analysis
  (apartments / facade / communal, each a tick with its fields), Shadow
  Diagram, Sun Views.
- Layout filing: one **"File sheets under"** subset picker per tool rather
  than four differently-named ones ("Diagrams filed in", "Times filed in",
  "Diagrams filed in" again). The ADG/shadow split inside Solar Analysis
  stays, named for what it files.
- The tick that names a study is the first row of its section and the
  section's tab shows a ✓ when it is ticked, as now.

The attribute names behind the fields do not change, so saved settings
load as before.

**Status:** done in this pass (`src/sun_study/app/window.py`); the layout
subset pickers are renamed, not merged.

## 3. The command line

The general options, the same on every command that talks to Archicad,
with one help string each: `--port`, `--timeout`, `--layer-prefix`,
`--master-layout`, `--title-block-mm`, `--year`, `--timezone`. Today
`--port` alone has two help strings and `--master-layout` four; the sheet
scale has five flag names (`--scale`, `--site-scale`, `--model-scale`,
`--drawing-scale`, and `--scale` again meaning something else). The
regrouping in §1 is where those get one name each.

**Status:** proposed. The site command grows the tool subcommands first
(they are its `--context/--site/--summary/--model` ticks today), the
solar commands second.

## 4. Tapir to Loriini

The Python side calls 63 Tapir commands and 10 of Loriini's. Most of the
workarounds in the code exist because a Tapir command is thinner than the
element it makes: no layer at creation, no ID, no holes, no colour, and
nothing to say which database it drew into. The inventory (Themes A–P in
the session notes) sorts into three tiers.

**Done or in this pass:**

- `CreateTexts` (UTF-8, layer, anchor, ID) — every study's labels.
- `CreateFills` (RGB, holes, ID) — the site sheets; the shadow and solar
  fills still go through Tapir's `CreateHatches` and could switch.
- `CreateMesh` (levels per vertex, holes as of this build) — the terrain
  and the blocks.
- `CreateSlabs` (layer, storey, ID, holes; this build) — the neighbours,
  one pass instead of create-move-stamp.
- `SetCurrentDatabase` / `GetCurrentDatabase` — the only way to know where
  a drawing lands; a layout made in the session can be entered this way,
  which is what puts the Summary of Controls straight on its sheet.
- `PlaceFigures`, `CreateWorksheet`, `CreateDocumentFrom3D`,
  `SetProjection`, `ArrangeDrawings`, `ActivateLayerCombination`,
  `ModifyLayers`, `GetDrawings`.

**Next, in order of what they remove:**

1. `CreateHatches` → Loriini `CreateFills` in `draw.py`, `shadows.py`,
   `series.py`, `penetration.py`: removes the pen-table matching (D27),
   the contour-in-own-pen trick, the `showArea` guard, the post-creation
   `stamp_element_ids` pass and the Fill Favorite requirement. The command
   exists; this is Python work.
2. `ActivateLayerCombination` (exists, unused) in place of `layers.held_at`:
   removes the compose-write-restore machinery and the "select it by hand"
   stop before an export.
3. `CreateWalls` with layer, ID and surface → the facade skin
   (`model_bands.py`) stops building materials and moving walls.
4. A `PlaceDrawing` that takes the frame, anchor and scale at creation, so
   `sheets.py` (create, save, measure, straighten, measure, move) goes.
5. `GetElements(databaseId, types, layer)` so nothing has to stand in a
   database to read it.

**Kept on Tapir:** the navigator tree, attributes, properties,
classification, IFC export, `SaveProject`, `GetGeoLocation`. These work.

A command moves only when the Python that used the old one is deleted in
the same change, so the add-on's floor rises and the fallbacks go rather
than accumulate.

## 5. A smaller executable

`Loriini.exe` is 70 MB compressed, 223 MB unpacked. Where it goes:

| MB | What | Why it is there | Do |
|---|---|---|---|
| 106 | `ifcopenshell` | `collect_all` pulls the express rules, MVD examples and every schema file | collect the package and the geometry wrapper only; the schema files nothing imports go |
| 21 | `numpy` OpenBLAS | real | keep |
| 8 | `libcrypto` | `ssl` for the open-data fetch | keep |
| 6.3 | `rhino3dm` | `collect_submodules('sun_study')` follows `ingest/rhino.py`, a dev-only reader | exclude it |
| 3.6 | `shapely` GEOS | transitive from ifcopenshell, never imported | exclude it |
| 3.3 | `mypy`, `ast_serialize` | pulled by `pydantic.v1.mypy` | exclude it |

And the IFC reader is imported at the top of `cli.py`, so the window and
every command — the site tools, the sun views, `archicad-ports` — load
106 MB of geometry kernel to start. The import moves into the three
commands that read an IFC.

**Status:** the spec changes and the lazy import are in this pass; the
size is measured after the next build.

## 6. What stays open for Monday

- The command-line regrouping (§1, §3): do it, and how far — aliases only,
  or the package split.
- Whether the Solar Analysis facade and communal studies keep their own
  commands (`massing` with zone flags today) or become one `solar` command
  with a `--target`.
- The layer structure: everything the site tools make sits on `LORIINI`
  or five layers per sheet by instruction; the office's `03 | Site.*`
  convention is the obvious home once the tools settle.
- The office's roads mesh (blocks as holes) versus the blocks-above-terrain
  the Context Model draws now; `CreateMesh` can take the holes as of this
  build.
