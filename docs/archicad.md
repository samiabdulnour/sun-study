# Talking to Archicad

Everything in `sun_study/archicad/` is written against the protocol as it actually is,
read out of the Tapir add-on's own sources. This page records **what was verified,
against which version, and where each fact came from**, plus the two facts that could
not be verified without an Archicad and the checklist that settles them.

Read this before changing anything in that package. Fabricating a plausible-looking
command is the fastest way to lose a day here: the failure mode is a request Archicad
politely refuses, or worse, one it accepts and misinterprets.

---

## Versions

| | |
|---|---|
| Archicad | **26** (Windows), the office's version |
| Tapir add-on | **1.5.7**, from `archicad-addon/Sources/AddOnVersion.hpp` |
| Minimum add-on | **1.5.1** — `GetElementsByIFCIds` is the binding constraint |
| Port | `19723` on `127.0.0.1` |

**AC26 is supported.** `TapirAddOn_AC26_Win.apx` is a published release artefact, and
`AddOnMain.cpp` registers every command unconditionally — there is no per-command
version gating. Guards named `ServerMainVers_2600 / 2700 / 2800` do exist, in
`CommandBase.cpp` and `IssueCommands.cpp`, but they are API-compatibility shims around
enum and container changes between SDK versions, not command exclusions.

Install from <https://github.com/ENZYME-APD/tapir-archicad-automation/releases>.

`ArchicadConnection.require_tapir()` checks the version at the handshake, so an old
add-on fails as a version problem rather than as an unexplained error on whichever
command happens to need a newer build.

---

## The protocol

Archicad 24+ listens on `http://127.0.0.1:19723` and takes:

```json
{"command": "API.<Name>", "parameters": {...}}
```

Tapir's commands are reached through one official command:

```json
{"command": "API.ExecuteAddOnCommand",
 "parameters": {"addOnCommandId": {"commandNamespace": "TapirCommand",
                                   "commandName": "<Name>"},
                "addOnCommandParameters": {...}}}
```

Verified from two independent places in the Tapir repository:
`builtin-scripts/aclib/__init__.py` and `sandbox/python-package/src/tapir_py/core.py`.

### Errors arrive at two levels, and both must be checked

The outer response carries `succeeded` and may carry `error`. The **inner** Tapir
response, under `result.addOnCommandResponse`, may carry its own `error` while the
outer call reports success.

Tapir's own reference client prints both and returns `None`, so a caller that does not
inspect the return value sees a silent failure. `run_tapir` raises on both.

A third case is subtler still and is documented under [write-back](#write-back) below:
some commands report per-item outcomes *inside* a successful response, and one of those
outcomes is an empty object.

---

## Commands used

Every one of these was read from `docs/archicad-addon/command_definitions.js` (the
generated schema catalogue) and cross-read against its C++ implementation. The "since"
column is the add-on version the command first appeared in.

| Command | Since | Kind | Used for |
|---|---|---|---|
| `GetAddOnVersion` | 0.1.0 | Tapir | Handshake and version gate |
| `GetProjectInfo` | 0.1.0 | Tapir | Naming the run in the header |
| `GetGeoLocation` | 1.1.6 | Tapir | Cross-checking the export's georeferencing |
| `GetElementsByType` | 1.0.7 | Tapir | Finding the Zones |
| `GetDetailsOfElements` | 1.0.7 | Tapir | Zone name, number and storey |
| `GetClassificationsOfElements` | 1.0.7 | Tapir | Property availability |
| `GetAllClassificationSystems` | — | **Official** | The system ids the above needs |
| `GetElementsByIFCIds` | 1.5.1 | Tapir | IFC GlobalId → Archicad GUID |
| `GetIFCIdsOfElements` | 1.5.1 | Tapir | The same join, inverted, for diagnostics |
| `IFCFileOperation` | 1.2.6 | Tapir | Exporting the model to analyse |
| `GetAllProperties` | 1.1.3 | Tapir | Resolving property names to identifiers |
| `CreateSurfaces` | 1.2.2 | Tapir | One Surface per band, in the reference colours |
| `CreateBuildingMaterials` | 1.0.1 | Tapir | Carries a band's Surface to a wall, via `cutSurfaceIndex` |
| `CreateWalls` | 1.4.0 | Tapir | The facade skin: one thin wall per merged rectangle |
| `CreateViewsInViewMap` | 1.1.7 | Tapir | Views of the 3D window and of a 3D Document |
| `GetNavigatorItemTree` | 1.1.7 | Tapir | Finding the 3D window and the project's 3D Documents |
| `SetDetailsOfElements` | 1.0.7 | Tapir | Moving new walls onto the tool's own layer |
| `GetAttributesByType` | 1.0.7 | Tapir | Surface and layer indices, which creation does not return |
| `GetLayers` | 1.0.7 | Tapir | Whether a layer is hidden — the difference between a change and a no-op |
| `CreatePropertyGroups` | 1.0.7 | Tapir | `sun-study init-properties` |
| `CreatePropertyDefinitions` | 1.0.9 | Tapir | `sun-study init-properties` |
| `SetPropertyValuesOfElements` | 1.0.6 | Tapir | Writing the results |
| `GetAttributesByType` | 1.1.3 | Tapir | Finding the results layer and the pen tables |
| `GetLayers` | 1.5.4 | Tapir | Whether the results layer is hidden or locked |
| `CreateLayers` | 1.5.4 | Tapir | Creating the results layer |
| `GetPenTables` | 1.5.4 | Tapir | Matching band colours to the office's own pens |
| `CreateHatches` | 1.5.7 | Tapir | The apartment fills and legend swatches |
| `CreateTexts` | 1.5.7 | Tapir | The legend labels |
| `DeleteElements` | 1.0.7 | Tapir | Clearing the previous run |
| `GetNavigatorItemTree` | 1.1.7 | Tapir | The storeys to place, and the masters to place them on |
| `CloneProjectMapItemToViewMap` | 1.1.7 | Tapir | A View per storey, because a Drawing is placed from one |
| `CreateLayout` | 1.4.0 | Tapir | The sheet |
| `CreateDrawings` | 1.4.0 | Tapir | The linked plan on the sheet, at a stated scale |
| `GetLayoutSettings` | 1.1.7 | Tapir | The page size, so the drawings land on the paper |

`GetAllClassificationSystems` is one of **Archicad's own** commands, not a Tapir one.
Tapir has no getter for classification systems — only for an element's classification
*within named systems* — and that command requires the system identifiers up front.

### Shapes worth writing down

`GetGeoLocation` → `projectLocation.{longitude, latitude, altitude, north}`, where
**`north` is in radians**, plus a `surveyPoint` block.

`SetPropertyValuesOfElements` takes a **display string**, not a typed value:

```json
{"elementPropertyValues": [
  {"elementId": {"guid": "..."},
   "propertyId": {"guid": "..."},
   "propertyValue": {"value": "2.35"}}]}
```

`CreatePropertyDefinitions` requires `availability` — a list of **classification
items**, because Archicad makes a custom property available to an element through its
classification, not its type. An unclassified Zone cannot receive one.

`IFCFileOperation` takes `method` (`save` | `merge` | `open`), `ifcFilePath` and an
optional `fileType` (`ifc` | `ifcxml` | `ifczip` | `ifcxmlzip`).

### Two identifiers for one element, and only one of them matches

`GetElementsByIFCIds` is asked in the spelling the *export* wrote and answers in the
spelling *Archicad* keeps, and on AC26 those are not the same string:

| | |
|---|---|
| What an IFC export writes | `0UHJKXnLzA2OlQcIwF4FFe` — 22 characters, base64 |
| What `GetIFCIdsOfElements` returns | `1E453521-C55F-4A09-8BDA-992E8F10F3E8` |
| What `GetElementsByIFCIds` matched | the second only |

They are the same 128 bits: an `IfcRoot.GlobalId` is that GUID compressed. Asked with the
compressed form, all fifteen of the reference project's apartment Zones came back
unmatched — which reads as an export that has drifted away from the project rather than
as two spellings of one number. The drawing step then reported "10 assessed apartments had
no zone to draw" over a project where every one of them was still there.

`read.elements_by_ifc_ids` offers both forms in one request and maps the answer back onto
whichever the caller asked with. `expanded_ifc_guid` does the conversion.

### `CreateLayout` needs a master, whatever its schema says

The published `inputScheme` requires only `layoutName`. The implementation refuses that
with `APIERR_BADPARS` (-2130313112), *"Either masterLayoutName or masterNavigatorItemId
must be provided"*.

Masters are read from the Layout Book — `GetNavigatorItemTree` with
`navigatorMapId: "LayoutBook"`, where they arrive as `MasterLayoutItem` under a
`MasterFolderItem`. That map's enum is `PublicViewMap | ProjectMap | LayoutBook |
PublisherSets`: there is no `ViewMap`, and asking for one is a schema violation rather
than an empty tree.

Which master is a judgement, and never a silent one. The reference project keeps **67**,
and they are not interchangeable — a title block sized for A3 puts a 1:200 plan off the
page. `layout.choose_master` prefers one whose name states the scale being drawn
("A1 - VERTICAL 1:200", "DA A1 - VERTICAL 1:200"), falls back to the first in the book,
and `--master-layout` settles it by hand. The run always prints what it used.

`GetLayoutSettings` then gives the page size, and the drawings are tiled inside it. The
first implementation placed them in a row 420 mm apart, which for six storeys is 2.5 m of
paper: five of the six sat outside the A1, and the sheet read as empty.

### The active database, and what a selection quietly does to an export

Two pieces of Archicad state that no command parameter mentions decide where a
drawing lands and what an export contains. Both were found the hard way, live.

**A selection empties the export.** With even one element selected, the
office translator exports an `IfcSite` and an `IfcBuilding` and nothing else —
**5.8 kB against 86 MB**. The run then fails much later, in the scene filter, as
"apartment zone layers matched nothing", which sends the reader to check a layer
name that was correct. This tool creates the trap itself: `CreateHatches` leaves
its last fill selected, so `--draw` on one run silently empties the export of the
next. `read.clear_selection` runs before every export and reports what it cleared.

**Element creation follows the current *database*, which `ChangeWindow` can
move.** `CreateHatches` takes `layerIndex` and `floorInd` but no database, so it
draws into whatever database is current — that part of [D28](decisions.md) is
right. What D28 says next, that nothing can change it, is **wrong**:
`ChangeWindow` (1.3.1) takes `{"databaseId": {...}, "windowType": "..."}` and
calls `ACAPI_Database_ChangeCurrentDatabase`, which is exactly the call element
creation follows.

Measured on AC26 with Tapir 1.5.7, drawing one fill with the worksheet `CLIENT`
active:

| | |
|---|---|
| Hatches visible before, on the floor plan | 1648 |
| Hatches visible after `ChangeWindow` to the worksheet | 3570 |
| After creating one fill | 3571 |
| Back on the floor plan | 1648, and the new fill is **not** among them |

So fills *can* be drawn into a worksheet. Three cautions, all measured:

- **On AC26 the `navigatorItemId` form is rejected** — *"navigatorItemId requires
  Archicad 27 or later; use databaseId instead"*. Get the id from
  `GetDatabaseIdFromNavigatorItemId`, or from `CreateWorksheets` directly.
- **A worksheet created in this session cannot be activated in it.**
  `CreateWorksheets` succeeds and returns a database id, the navigator lists the
  item, `GetDatabaseIdFromNavigatorItemId` returns the same id — and
  `ChangeWindow` still fails with `-2130313110`, *"Failed to change current
  database"*, before and after `RebuildView`. Existing worksheets activate
  first time. So a worksheet target has to already exist.
- **`GetCurrentWindowType` still reports `FloorPlan` afterwards.** The database
  moved; the visible window did not. Do not use it to confirm the switch — check
  `ChangeWindow`'s own `{"success": true}`, and remember the state is global and
  outlives the command.

### A fill cannot carry a property, and a label can only carry text

Asked directly, twice, on a fill created for the purpose:

| Attempt | Result |
|---|---|
| `SetPropertyValuesOfElements` on an unclassified fill | `-2130312908` *Failed to set property value for element* |
| `SetClassificationsOfElements` on that fill, using the item its Zone carries | `-2130312907` *Failed to set classification item for element* |
| `SetPropertyValuesOfElements` after that | refused again; `GetClassificationsOfElements` still returns `[]` |

Archicad grants a custom property through a classification and will not classify
a Hatch, so **the numbers cannot live in the fill**. Nothing in Tapir's schema
forbids it — the refusal comes from Archicad itself.

`CreateLabels` (1.2.5) is the other half of the same question, and it splits:

| Shape | Result |
|---|---|
| `begCoordinate` + `midCoordinate` + `endCoordinate`, static `text` | **works** |
| `parentElementId` (a Zone) + `text` | `-2130312912` *Failed to create new Label* |
| `parentElementId` + coordinates + text | `-2130312912` |

The add-on's own example places a live property label with
`<PROPERTY-{guid}>` autotext in `text` alongside `parentElementId`, and the
element-creation base class deliberately suppresses autotext resolution so the
token is stored rather than frozen. It fails here anyway: the text branch runs
only when the Label tool's *current default* is a text-class label, and this
project's is not. Until that is settled, annotation is **static text on a leader**
— which is what the office's own reference drawing uses.

### One place the schema and the implementation disagree

`GetClassificationsOfElements`' published schema wraps each entry in a
`classificationId` key (`ClassificationIdArrayItem`). Its implementation, at
`ClassificationCommands.cpp:95`, pushes the inner object directly:

```cpp
classificationIds (GS::ObjectState (
    "classificationSystemId", GS::ObjectState ("guid", APIGuidToString (systemGuid)),
    "classificationItemId",   GS::ObjectState ("guid", APIGuidToString (item.guid))));
```

`read._classification_item_guid` accepts **both** shapes rather than betting on which
one a given build sends. `test_classifications_accept_both_shapes_and_drop_the_null_guid`
pins that.

The same function drops the null GUID `00000000-...-000000000000`, which is how
Archicad reports "unclassified" — it comes back as a well-formed identifier rather than
as an error, and offering it as a property availability would look like it had worked.

---

## Write-back

### An empty execution result is a failure

`SetPropertyValuesOfElements` returns `executionResults`, one slot per requested value.
Tapir allocates the slots up front, then fills them in a loop over the properties
**Archicad actually returned** for that element:

```cpp
GSErrCode err = ACAPI_Element_GetPropertyValuesByGuid (elemGuid, properties, propertyValues);
for (API_Property& propertyValue : propertyValues) { ... }
```

A property that is not *available* for the element — the usual cause being an
unclassified Zone — is never returned, so its slot stays a default-constructed empty
object. It is neither `{"success": true}` nor `{"success": false, ...}`.

Reading `{}` as success would report a full write of a project where nothing landed.
`write._execution_problem` treats it as a failure and names the likely cause.

### Numbers are safe; booleans are not

Tapir pins the display-string conversion itself. `PropertyConversionUtils` in
`PropertyCommands.cpp` hard-codes:

| | |
|---|---|
| Decimal delimiter | `.` |
| Thousand separator | space |
| Length / area / volume | metre, m², m³ |
| Angle | decimal degrees |

So `"2.35"` on a `number` property means 2.35 regardless of the project's unit
preferences. That is verified, not assumed.

Nothing states what string a `boolean` property parses from, so the pass/fail columns
are `string` properties holding `Yes`/`No`. See [decision D21](decisions.md).

### What gets created

One property group, **`Sun Study`**, with nine properties — see `APARTMENT_PROPERTIES`
in `archicad/write.py`. Creating them is a separate command, `sun-study
init-properties`, rather than something a results run does silently, because property
definitions are part of the project file and on a Teamwork job that changes what
everybody sees.

`init-properties` is idempotent: it reads the catalogue, creates only what is missing,
and re-reads to pick up the new identifiers. It deliberately does **not** widen the
availability of properties that already exist — that would silently overwrite a
definition a colleague may have edited.

---

## North: the convention, and the frame trap

**`placeInfo.north` is the angle of true north measured counter-clockwise from the
project +X axis, in radians.** Tapir passes it through undocumented; this was derived
from a real AC26 export, three independent numbers agreeing to 1.6 × 10⁻⁵ degrees. The
bearing of project +Y is `degrees(north) − 90`, so an untouched project reports π/2,
not 0. See [decision D23](decisions.md) for the measurements.

**The trap is that Archicad's angle is in the project frame and an IFC export need not
be.** Archicad's `IFC Model position` option decides where the rotation goes:

| Model position | `IfcSite` placement | `TrueNorth` |
|---|---|---|
| Survey Point | carries the rotation | `(0,1)` — world coords are already north-aligned |
| Project Origin | unrotated | carries the rotation |

Both are correct and self-consistent, and the analysis is unaffected either way because
it reads world coordinates with `use-world-coords = True`. But a comparison against
anything *outside* the file has to add the two together:

```
project +Y bearing  ==  TrueNorth bearing  -  site placement rotation
```

`cross_check_georeferencing` uses that identity, which holds under both options without
needing to know which was used. An earlier version compared against `TrueNorth` alone
and rejected a perfectly good Survey Point export — 49° against 0°. If you change this
code, that is the mistake to avoid.

---

## Manual test checklist

Run this at a Windows workstation with Archicad 26 and Tapir 1.5.1+. None of it can run
in CI. Record the outcome and the add-on version in the pull request.

Before starting, in Archicad:

- **Options ▸ Work Environment ▸ Model Compare and JSON Interface** — the JSON interface
  must be enabled.
- **Options ▸ Project Preferences ▸ Project Location** — latitude, longitude and North
  Direction must all be set. Set North to something clearly non-zero (30° is ideal, it
  matches the test fixture) so the cross-check has something to check.
- The apartment Zones must be **classified**. Any classification system will do.
- The active IFC translator must export site location, true north, Zones (as
  `IfcSpace`), and windows.

Then:

1. **Connection.** `sun-study archicad-info`
   Expect: add-on version, project name, location line, zone count, either "all N zones
   are classified" or a named list of the unclassified ones, and the distinct zone
   names. *Read the zone names before anything else* — they tell you what to pass to
   `--living-room`, and whether zones are placed per room or per unit. **Done once on
   an AC26 project: everything above returned correctly.**

2. **Nothing running.** Quit Archicad, repeat step 1.
   Expect: exit code 2 and a message naming the JSON Interface setting. No traceback.

3. **Property creation.** `sun-study init-properties`
   Expect: nine properties listed. Check them in **Options ▸ Property Manager** under
   the `Sun Study` group, with the descriptions filled in and availability covering the
   Zones' classifications.

4. **Idempotency.** Run step 3 again.
   Expect: the same nine listed, no duplicates in the Property Manager, no error.

5. **Export and assess.**
   `sun-study archicad-run --timezone Australia/Sydney --ifc-out check.ifc`
   Expect: "georeferencing cross-check passed", then the usual per-apartment output.
   *If the cross-check fails, read the message: it distinguishes a moved site from a
   flipped north sense, and the latter is the expected way [D23](decisions.md) resolves.*
   Open `check.ifc` and confirm it is the current model, not a stale file.

6. **Stale export guard.** Re-run step 5 with the same `--ifc-out` while Archicad is
   shut. Expect a refusal to analyse an unmodified file.

7. **Write-back.** `sun-study archicad-run --timezone Australia/Sydney --write`
   Expect: "wrote N property values across M zones", exit code 0.
   In Archicad, select a Zone and check the `Sun Study` values in the Info Box or
   Property Manager. *Confirm the hours read as numbers, not as text* — a number
   property that failed to parse shows as undefined.

8. **The schedule.** Build a Zone schedule with the `Sun Study` columns.
   This is the actual deliverable: an ADG table as a native Archicad schedule.
   Confirm the hour columns sort numerically and `Meets Minimum` filters.

9. **Unavailable property.** Unclassify one Zone, re-run step 7.
   Expect: a partial write reported with that zone named, exit code 3 — *not* a silent
   success. This is the `{}` execution result described above.

10. **Undo.** Confirm the write-back is a single undo step (Tapir wraps it in
    `ACAPI_CallUndoableCommand`), and that undoing it removes the values.

## Verified against a live Archicad 26

Run on two real projects. What has actually been observed working, as opposed to
machine-checked against a fake transport:

| | |
|---|---|
| Connection, version handshake, project info | works |
| `GetGeoLocation`, zones, zone details, classifications | works |
| Property group create and lookup | works, **once definitions carry a `defaultValue`** |
| Property **value** writes | **partly blocked**: on one project 2 of 8 zones took every value and 6 refused every value; on another the refusing zones were traced to the locked layer `10 \| Calc.GFA` |
| `GetDetailsOfElements` layer index, `GetHotlinks` — why a write was refused | **works**: named a locked layer as the cause |
| `SetDetailsOfElements` with a surface, on a Wall | **refused**: a wall has no settable surface. Only Objects report one at all |
| `CreateMorphs`, box and explicit body | **refused** on Archicad 26: `Failed to create morph` for every shape tried |
| `CreateSlabs` / `CreateMeshes` / `CreateRoofs` with a material | **refused**: no `buildingMaterialId` field. Only `CreateWalls` has one |
| Creating a 3D Document | **not offered**: no command exists. A View of an existing one can be made |
| `CreateWalls` with a `layerIndex` | **refused**: schema has no such field. New walls land on the tool's default layer |
| `SetDetailsOfElements` with a `drawingScale`, on a Drawing | **refused silently**: answers `{"success": true}` and leaves the scale as it was |
| `SetDetailsOfElements` with a `ratio`, on a Drawing | **works**: the magnification is the only handle on a drawing's size on the page |
| A Drawing's `bounds` after its `ratio` changes | **stale**: they keep the old size until Archicad regenerates the drawing, which happens when somebody opens the layout ([D55](decisions.md)) |
| `SetMasterLayout`, `DeleteLayouts` | **not offered**: unregistered. An existing layout keeps the master it was made on |
| `SetLayoutSettings`, `DeleteNavigatorItems` | exist, and `SetLayoutSettings` carries no master field |
| Layer create and lookup, element delete | works |
| `CreateHatches`, `CreateTexts` — fills and legend | **works**: 8 fills and a 7-item legend, replacing the previous run's 15 |
| `GetElementsByIFCIds` — the results-to-Zones join | **works, once both spellings are offered**: 0 of 15 matched on the export's own GlobalIds, 15 of 15 on the expanded GUIDs |
| `clear_selection` before an export | **required**: with one element selected the export is 5.8 kB instead of 86 MB |
| Drawing into an existing Worksheet via `ChangeWindow` | **works**: fill landed in `CLIENT`, absent from the floor plan |
| Drawing into a Worksheet created in the same session | **fails**: `-2130313110`, before and after `RebuildView` |
| Properties or classifications on a Fill | **refused by Archicad**: `-2130312908` / `-2130312907` |
| `CreateLabels` with coordinates and static text | **works** |
| `CreateLabels` with `parentElementId` (live property autotext) | **fails**: `-2130312912`, the Label tool default is not text-class |
| Property values onto hotlinked Zones | **refused**: 6 of 10 apartments are hotlink instances, read-only in the host |
| `CreateWorksheets` then `ChangeWindow` then `CreateHatches` | **works on an existing worksheet**: 6396 fills and 19 captions landed in `Solar Penetration Outlines`, none of them on the floor plan |
| Leaving a worksheet, programmatically | **cannot be done on AC26**: `windowType` alone, with `storyIndex`, and with a floor plan's `databaseId` all answer `{"success": true}` and change nothing |
| An IFC export taken with a worksheet in front | **unaffected**: 86 MB, the whole project. The window is cosmetic; only the *selection* empties an export |
| The layout chain — navigator tree, clone, `CreateLayout`, `CreateDrawings` | **works, once a master is supplied**: 6 linked plans at 1:200 on `DA A1 - VERTICAL 1:200`, tiled on the 841 × 594 sheet `GetLayoutSettings` reported |
| `GetPenTables` — reading the office palette | **works**: 255 pens, one band matched exactly, one had no pen within 110 |

**The write refusal is per element, not per request.** The decisive observation: in one
run the same payload, the same nine properties and the same classification landed on two
zones and was refused by six. That rules out the request, the property definitions and
the classification all at once, and leaves a property of those six elements — which is
what `diagnose_write_access` now goes and asks about, since `APIERR_NOACCESSRIGHT` names
three causes (locked element, locked layer, hotlinked module) without saying which.

Three traps found the hard way, all now guarded:

**A property definition with no `defaultValue` is rejected** with `APIERR_BADVALUE`
(-2130313104). The schema does not require the field, but Tapir then sets the variant
status to null and Archicad refuses the definition. A probe that varied name, type,
availability and `isEditable` failed on every variation, because the missing default was
what they all shared.

**`GetLayers` requires `attributeIds`**, so it cannot enumerate — it has to be told which
layers to describe. Calling it bare is a schema violation and Archicad rejects the whole
command with code 4002. Use `GetAttributesByType` with `attributeType: "Layer"`.

That is a family, not a one-off: every `Get<Attribute>s` command added in 1.5.4 —
`GetPenTables`, `GetFills`, `GetSurfaces`, `GetProfiles`, `GetComposites` and the rest —
takes the same required `attributeIds`. `GetAttributesByType` is the only enumerator, and
it reports just id, index and name, so reading any detail is always two calls.
`GetPenTables` also accepts `fields`, which is worth using: each pen table carries 255
pens, and finding out which of several tables is active should not pull all of them.

**A modal dialog blocks the entire API.** Leave Object Settings — or any modal window —
open in Archicad and every command fails with `Invalid program status` (code 4001), which
reads like a fault in the tool rather than a window that needs closing. Archicad names the
dialog in the message, so the tool keeps that and adds the fix.

**A zone name is not an identifier.** One project holds 1341 zones, of which the eight
sampled were all called `RESI` with no number. Collapsing failures by display name then
reported seven refusing elements as "56 failures over 1 elements", and a list of six
zones with holes read as "RESI, RESI, RESI, RESI, RESI". Reports now collapse on the
element GUID, and `disambiguated` tags only the colliding names with a GUID fragment.

**Excluding the hotlink layers is not always possible.** The advice above — switch the
masters off in the export — assumes they have layers of their own. On the reference project
they do not: masters and real building share `01 | Wall.External`,
`01 | Wall.Unit Internal` and the rest, so no layer combination separates them. What does
separate them is height, and `--exclude-above` is the knob. See [D30](decisions.md).

**A zone's layer is what says whether it is an apartment.** The same project's zones sat
on `10 | Calc.GFA` — area take-off, not housing — and were locked because a calculation
layer normally is. `ArchicadZone` carries `layer_index` and the self-test reports the
layers its sample came from, because a run against the wrong zones otherwise looks
exactly like a run against the right ones.

**Archicad gives each running instance its own port.** The JSON API is on 19723 only for
the *first* Archicad open on the machine; a second lands on 19724, and so on up to 19742
(Tapir's own client pins the range at `range(19723, 19743)`). Switching projects by
opening the new one alongside the old therefore moves it off the default port, and once
the first instance is closed the default answers nothing at all — `WinError 10061`,
"actively refused". That reads as "Archicad is not running" and sends people to the Work
Environment setting, which was never off. `sun-study archicad-ports` lists every live
instance with its open project, and a refused connection now scans and says where
Archicad actually is.

The scan is real network I/O and runs **only** when the CLI has decided to tell a human.
An earlier version did it inside the transport's error path, which made every failed
call pay for a twenty-port scan and took the test suite from 14s to 146s.

**A pen table can put two bands on one pen.** Matching each band to its nearest pen
independently is the obvious implementation and it is wrong: on a real office table the
3–4 and 4–5 hour bands both took pen 124, because their reference colours are 30 apart
and the palette held one amber nearest to both. The assignment is one-to-one now, and a
separate check reports bands whose *different* pens are still near-identical in colour
([D27](decisions.md)).

And one Tapir bug worth knowing: **it reuses a single `err` across the loop over an
element's properties without resetting it**, so the first genuine failure makes every
later property on that element report "Failed to get property values" as well. Only the
first failure per element is a cause. Confirmed live: 48 reported failures over 6
elements, so 6 causes and 42 echoes.

### Known unknowns to settle while you are there

- Whether a `boolean` property accepts a display string, and which one
  ([D21](decisions.md)). Create one by hand in the Property Manager and try
  `"true"` / `"Yes"` / `"1"` through `SetPropertyValuesOfElements`.
- Whether `GetClassificationsOfElements` sends the wrapped or the bare shape on AC26.

The `north` convention ([D23](decisions.md)) is **settled** and no longer needs a step.

## Translator settings the analysis needs

Read off a real AC26 export that turned out to be missing two of them. Check these in
the translator you export with, under `File ▸ Interoperability ▸ IFC ▸ IFC Translators`:

| Setting | Needed | Why |
|---|---|---|
| Zones in the export filter | **Yes** | Zones become `IfcSpace`. Without them there are no apartments at all, and `run` reports zero. |
| The active layer combination | **shows the zone layers** | The translator exports what the combination *shows*, so a hidden `06 \| Zone.*` layer is an export filter in its own right — the reference project produced 386 walls, 92 windows and no `IfcSpace` from a site-plan combination. `archicad-run` now checks this before exporting ([D52](decisions.md)). Nothing in the add-on can switch it; it is a hand in Archicad. |
| IFC Space boundaries | **On** | `IfcRelSpaceBoundary` maps each window to the room it serves. Without it the tool falls back to geometric containment, which is a guess. |
| Project Location set to the real site | **Yes** | Archicad ships a city preset; exact arcminute coordinates such as `(-33,-52,0,0)` are the tell that nobody set it. |
| IFC Model position | either | Both Survey Point and Project Origin work — see the north section above. |
| Properties to export | any | The tool reads none of them. `All properties` inflated one 283 MB export to 1.7 million `IfcPropertySingleValue` entities; `Element Parameters only` exports far faster. |
