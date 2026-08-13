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
| Property **value** writes | **blocked** on one project with `APIERR_NOACCESSRIGHT` |
| Layer create and lookup, element delete | works |
| `CreateHatches`, `CreateTexts` — fills and legend | **works**: 8 fills and a 7-item legend, replacing the previous run's 15 |

Two traps found the hard way, both now guarded:

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

And one Tapir bug worth knowing: **it reuses a single `err` across the loop over an
element's properties without resetting it**, so the first genuine failure makes every
later property on that element report "Failed to get property values" as well. Only the
first failure per element is a cause.

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
| IFC Space boundaries | **On** | `IfcRelSpaceBoundary` maps each window to the room it serves. Without it the tool falls back to geometric containment, which is a guess. |
| Project Location set to the real site | **Yes** | Archicad ships a city preset; exact arcminute coordinates such as `(-33,-52,0,0)` are the tell that nobody set it. |
| IFC Model position | either | Both Survey Point and Project Origin work — see the north section above. |
| Properties to export | any | The tool reads none of them. `All properties` inflated one 283 MB export to 1.7 million `IfcPropertySingleValue` entities; `Element Parameters only` exports far faster. |
