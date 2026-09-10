# The Loriini add-on

Nine commands Archicad's JSON API does not have, and a menu so Loriini is
visible from inside the project a colleague is working in.

Read this before changing anything in `archicad-addon/`. The same rule applies
here as in [`archicad.md`](archicad.md): every fact below was read out of the
Archicad 26 API Development Kit's own headers or probed against a live
Archicad, and the two things that could not be settled that way are named as
open rather than guessed at.

---

## Why an add-on at all

Everything else this tool asks of Archicad goes through the
[Tapir add-on](https://github.com/ENZYME-APD/tapir-archicad-automation), and
should keep going through it. These three do not, because no command for them
exists anywhere.

The office's solar penetration diagram is made by hand like this: set the 3D
window to a parallel projection aimed along the sun's own direction for one
hour, make a 3D Document from it, repeat for every hour from 9am to 3pm, then
place the documents on a layout. The yellow glazing comes from a graphic
override rule, not from drawn geometry.

Of those steps, only the last two are reachable from outside Archicad.

| Step | Reachable today |
|---|---|
| Aim the 3D window along the sun | **no command exists** |
| Create a 3D Document | **no command exists** |
| Apply a graphic override rule | yes, `SetViewSettings` takes `graphicOverrideCombination` |
| Make a view, a layer combination, a layout | yes, all through Tapir |

Two independent checks agree on the first two rows. Tapir 1.5.8's command
catalogue, read out of the installed `TapirAddOn_AC26_Win.apx`, holds no
camera, projection or 3D Document command — and its `ViewSettings` schema
carries the layer combination, scale, rotation, zoom, 3D style and rendering
scene, and no projection. Archicad's own API, probed live, answers:

```
API.Get3DProjectionInfo  ->  {"code": 2002, "message": "Command 'API.Get3DProjectionInfo' not found"}
```

Camera elements are half-reachable and no help. `GetElementsByType` finds them,
`GetDetailsOfElements` refuses them with *"Not yet supported element type"*,
and nothing creates one. A camera is also a perspective, which a measurable
diagram must not be.

In C++ all of it is one call each. That is the whole reason this add-on exists,
and the reason it stays this small.

## What it does

### `Loriini.GetProjection`

Reads `API_3DProjectionInfo` through `APIEnv_Get3DProjectionSetsID` and reports
it, including the twelve raw numbers of `API_AxonoPars::tranmat`.

This is an instrument, not a convenience, and it is why it shipped first. See
[the open question](#the-open-question) below.

### `Loriini.SetProjection`

Takes a bearing and an altitude in degrees, builds the transformation matrix,
and writes it back through `APIEnv_Change3DProjectionSetsID`.

The sun is given as a **date**, not as angles. `API_SunAngleSettings` carries a
`sunPosOpt`, and setting it to `API_SunPosition_GivenByDate` makes Archicad
compute the sun itself from the project's own georeferencing. That is worth
preferring: it means the sun in the drawing is Archicad's, so a disagreement
with this tool's own astronomy becomes visible instead of being hidden by
writing our answer into both sides of the comparison.

Any date field left out keeps the value the project already had, so a caller
stepping through seven hours states the hour and nothing else.

### `Loriini.CreateDocumentFrom3D`

Creates a 3D Document database with `APIDb_NewDatabaseID`, then sets its
projection with `APIEnv_ChangeDocumentFrom3DSettingsID`.

The second half is the one that matters. `API_DocumentFrom3DType` carries its
own `projectionSetting`, so **a 3D Document remembers the angle and the sun it
was made at**. Without that, seven documents made in a row would all show
whatever the 3D window happens to display now, and a sheet of seven hours would
be seven copies of one hour.

The vectorial sun shadow is not part of it, though the field names suggest it
should be. `vectSunShadow` belongs to `API_3DStyle`, and a 3D style is already
reachable: Tapir's `SetViewSettings` pins one on a view by name through
`d3styleName`. Make the style once, with the shadow settings the office wants,
and every document uses it.

Both writes are wrapped in `ACAPI_CallUndoableCommand`. A run that makes seven
documents and is then abandoned must be undoable in one gesture, not deleted by
hand in the Navigator one at a time.

### `Loriini.CreateFills`

The command the diagrams are actually made of, and the one that retires the
most machinery.

`API_HatchType` carries a good deal that Tapir's `CreateHatches` has no field
for. What this command reaches and Tapir's does not:

| | Why it matters |
|---|---|
| `foregroundRGB` / `backgroundRGB` | the colour itself, not an index into somebody's pen table |
| a polygon with sub-contours | so a shadow across a courtyard has a hole in it |
| `determination` | drafting, cut or cover |
| `ltypeInd`, `penWeight` | the contour's line type and weight |
| the element ID at creation | rather than a second pass over six thousand fills |

The colour is the important one. [D27](decisions.md) exists because a pen index
means nothing outside its own pen table, so the tool measures the seven
reference band colours and then hunts for the nearest pen in the office palette
— with a one-to-one greedy assignment, a `POOR MATCH` label and a separate
indistinguishability check, all of it apparatus for a colour the element can
simply be told. On the reference project one band had no pen within 110.

`APIHatch_HasFgRGBColor` and `APIHatch_HasBkgRGBColor` are what switch the RGB
fields on. Without the flag the field is ignored in silence; with the flag and
no field the fill paints black. They are set together here and never
separately.

The sub-contour is the second. Tapir's schema says *"single contour, no holes"*
in as many words. A shadow drawn without its hole fills in the courtyard and
claims a shadow where the sky is.

`determination` is quieter but bites on a sheet. A solar diagram wants a
**drafting** fill. A cover fill belongs to a room and a cut fill to something
the section plane passes through, and either can change or vanish under a model
view option the tool never set — which is how a diagram that looked right on
screen arrives empty on a layout.

### `Loriini.GetCurrentDatabase` and `SetCurrentDatabase`

Where the tool is standing, asked rather than remembered.

[D63 and D64](decisions.md) exist because Tapir has no `GetCurrentDatabase`,
`GetCurrentWindow` or `GetDatabases`, so the Python side keeps its own note of
where it last went — and is wrong the moment anybody clicks a tab.
`APIDb_GetCurrentDatabaseID` is the call that was missing.

`SetCurrentDatabase` **reads back and compares** before reporting success. That
is not belt and braces: the wall it replaces is a command that reports success
and does not move. On Archicad 26, Tapir's `ChangeWindow` answers
`{"success": true}` and stays put for `windowType` alone, with `storyIndex`,
and with a floor plan's own `databaseId`. A replacement that trusted its own
write would be the same trap wearing a different name.

### `Loriini.CreateWorksheet`

The same wall from the other side. Tapir's `CreateWorksheets` works, and
drawing into a worksheet made in the same session fails with `-2130313110`,
before and after `RebuildView`. Creating it and entering it in one call is what
makes it drawable, which is why `makeCurrent` defaults to true.

### `Loriini.ActivateLayerCombination` and `ModifyLayers`

[D59](decisions.md) says a layer combination cannot be activated. It can:
`APIEnv_ChangeCurrLayerCombID`. The workaround it replaces rewrites every layer
in the project with `CreateLayers` and `overwriteExisting` to imitate one.

`ModifyLayers` is the other half. A layer's hidden and locked state is two bits
in an attribute header, and `ACAPI_Attribute_Modify` sets them without
recreating the layer. Only the bits the caller names are touched, which the
overwrite approach could not manage.

## What the office's own sun eye views are already set to

Read off a live project that has the whole workflow built by hand: nine saved
Axonometry views named `JUNE 21 - 9AM` through `DECEMBER 21 - 3PM`, and a 3D
Document per view beside them. Every setting on them came back through Tapir's
`GetViewSettings`, and **all of it is writable through `SetViewSettings`**:

| | |
|---|---|
| `layerCombination` | `04 \| Shadow Diagrams` |
| `graphicOverrideCombination` | `Shadow Diagrams` — this is the yellow glazing |
| `penSetName` | `00 FA Pens` |
| `modelViewOptions` | `DA Site` |
| `d3styleName` | `OpenGL Shading with Contours with Shadows`, on the views only |
| `drawingScale` | 1 on the views, 1000 on the 3D Documents |
| `structureDisplay` | `EntireStructure` |

Two things follow. The 3D Documents carry **no** `d3styleName`, which agrees
with the headers: a 3D style belongs to a view, not to a document. And the
office's *existing* views cover three instants per date, at 9am, 12pm and 3pm,
across 21 June, 21 September and 21 December. A sun eye set is hourly instead,
seven instants from 09:00 to 15:00 on 21 June, matching the assessment window
rather than the three-per-sheet layout the shadow diagrams use.

The important one is what is missing from that list. Every setting a sun eye
view needs is already reachable through Tapir except the projection itself.
That is the whole remaining job.

## The open question

`API_AxonoPars::tranmat` is a 3x4 matrix. `APIdefs_Base.h` gives the arithmetic:

```
x' = tmx[0] * x + tmx[1] * y + tmx[2]  * z + tmx[3]
y' = tmx[4] * x + tmx[5] * y + tmx[6]  * z + tmx[7]
z' = tmx[8] * x + tmx[9] * y + tmx[10] * z + tmx[11]
```

What it does **not** give is which way Archicad's rows and signs run, or how
`azimuth` and `projMod` interact with a matrix supplied from outside. The
structure's own documentation page is four lines long and was last revised in
December 2007.

So `Projection.cpp` writes its convention down and `GetProjection` exists to
check it. The calibration is: set a known angle by hand in Archicad's 3D
Projection Settings, read the matrix back, and compare it against
`ViewMatrix` for the same angle. **Until that has been done on a live
Archicad, `SetProjection` is unverified.** It is the same method
[`archicad.md`](archicad.md) records for everything else that could not be
settled from a header.

There is a second half to the same question, and it is worth 41 degrees on the
reference project. A sun bearing is a **true** bearing, and Archicad's 3D window
works in the **project's** frame, which is turned. On that project the tool
reports the project's +Y axis at true bearing 319.052, so 9am on 21 June is the
sun at 42.6 true and 83.5 in the project's own frame. The seven instants a
study draws, at the reference project's latitude:

| Hour | Altitude | True bearing | Project frame |
|---|---|---|---|
| 9:00 | 19.0 | 42.6 | 83.5 |
| 10:00 | 26.3 | 30.0 | 70.9 |
| 11:00 | 31.1 | 15.3 | 56.2 |
| 12:00 | 32.7 | 359.1 | 40.1 |
| 13:00 | 30.8 | 343.1 | 24.1 |
| 14:00 | 25.7 | 328.6 | 9.6 |
| 15:00 | 18.1 | 316.3 | 357.2 |

21 June and 09:00 to 15:00 are not a choice. They are the ruleset's own
assessment date and window, in `nsw_adg.yaml`, and the same seven hours the
shadow diagrams already default to.

Getting that wrong does not fail. It draws a complete, plausible diagram of the
building lit from the wrong side, which is precisely the failure this project
exists to avoid. `SetProjection` currently documents its bearing as clockwise
from north without saying **which** north, and the calibration has to settle
that at the same time as the matrix.

What *is* settled is the maths inside the convention. The frame is orthonormal,
right-handed and correctly oriented at every bearing and altitude tested,
including the overhead case where a bearing no longer fixes the roll and north
is put at the top of the page instead.

## The menu

`RegisterInterface` adds one item under a Loriini menu, and it starts the app
rather than running any analysis. The analysis lives in Python, where it is
tested; an add-on that duplicated it would be a second implementation nobody
could check.

The app is looked for as `Loriini.exe` **beside the .apx**, found through
`ACAPI_GetOwnLocation`. There is no fixed install path to assume: an office
without administrator rights registers its own add-on folder in the Add-On
Manager, and on these workstations that is a folder under Documents. A missing
.exe says so in a report dialog rather than doing nothing, because a menu item
that silently does nothing reads as a broken add-on.

## Building it

**Nobody in the office builds this.** Compiling an Archicad add-on needs Visual
Studio with the v142 toolset, and these workstations have no administrator
rights, so the install fails at the elevation prompt with code 1602. The
`Archicad add-on` workflow builds it on a runner instead and uploads the .apx.

The recipe is Tapir's, which is the only combination proven to build an
Archicad 26 add-on:

| | |
|---|---|
| Runner | `windows-2022` |
| Kit | `API.Development.Kit.WIN.26.3000.zip`, from Graphisoft's public releases |
| Generator | `Visual Studio 17 2022`, `-A x64` |
| Toolset | **`v142`**, not the runner's own v143 |

The kit is downloaded per build and never committed. It is a licensed
Graphisoft download and this repository is public.

`CMakeLists.txt` reads the Archicad version out of the kit's own `ACAPinc.h`
and refuses a mismatch with `AC_VERSION`. Building the 26 add-on against the 28
kit produces a file Archicad loads and then behaves strangely with, which is
much worse than a refusal.

## What the first build taught

Six failed compiles before one passed, and the fixes are worth recording
because none of them are guessable from the reference documentation.

| What broke | What it actually is |
|---|---|
| `APIEnvir.h` not found | not part of the kit. Every add-on carries its own copy in `Src`, and it is what defines `WINDOWS` |
| All nine command classes abstract | `API_AddOnCommand` has nine pure virtuals. `GetSchemaDefinitions` and `OnResponseValidationFailed` are easy to miss |
| `APIGuidToString` not found | it is in `API_Guid.hpp`, which `ACAPinc.h` does not include |
| `RSGetIndString` not found | it is in the `RS` module, reached in the examples through their own `APICommon.h` |
| `CopyName` refused an attribute | a database's name is `GS::uchar_t[256]`; an attribute's is `char[256]` |
| `vectSunShadow` not a member | it belongs to `API_3DStyle`, not to the 3D Document |
| `GS::UniString::CStr` inaccessible | the type `ToCStr` returns is private, so it can be held in `auto` and not named |

The compile errors are surfaced as **annotations** rather than left in the run
log, because a run's log needs admin rights on the repository and the person
fixing a compile error does not necessarily have them.

## Installing it

No administrator rights needed, which is the point.

1. Download the `LoriiniAddOn_AC26_Win` artifact from the workflow run.
2. Put the `.apx` in the same folder as Tapir's, e.g.
   `C:\Users\<you>\Documents\Tapir\Add-Ons\`.
3. Put `Loriini.exe` in that folder too, if the menu item should start the app.
4. In Archicad: **Options > Add-On Manager**, add that folder, restart.

`sun-study archicad-info` reports whether the add-on answered.

## Walls that stay walls

Checked against the headers and **not** fixable by any add-on. Recorded here so
nobody spends a second day on them.

**A drawing's scale cannot be set.** `API_DrawingType.drawingScale` is marked
*output only* in the C++ header, exactly as Tapir's silent refusal implies.
[D55](decisions.md) stands as written, and magnification remains the only handle
on a drawing's size.

**Graphic override combinations cannot be enumerated.** They are not attributes
on Archicad 26 — `API_AttrTypeID` has no entry for them, and the
`APIGraphicOverrides*` names in the headers are teamwork permission flags, not
accessors. A view can still be *given* a combination by name through Tapir's
`SetViewSettings`; it cannot be offered a list. Reading the name off an existing
view stays the answer.

**Morph creation.** Tapir refuses every form with `-2130313114`, and its own
schema blames Archicad 25 and 26 rather than itself. Worth retesting directly
through `ACAPI_Element_Create` once the add-on loads, but expect it to stand
until the office moves to Archicad 27.

**Access refusals.** Property values on hotlinked zones, elements on locked
layers, properties on fills. These are Archicad enforcing its own rules and
should keep failing.

## Known gaps

**The Local ID has to come from Graphisoft.** `RFIX/LoriiniAddOnFix.grc` holds
an `'MDID'` resource of two numbers, the practice's Developer ID and a Local ID
generated per add-on on the Developer Portal's Add-ons tab. Archicad validates
the pair, and an invented Local ID answers:

> This add-on cannot be validated. Please contact the distributor.

That message has no detail behind it and nothing appears in a log, so it is
worth recognising on sight. The dev kit's own FAQ lists the causes: no `MDID`
resource, one that was not compiled in, one holding demo IDs, one clashing with
another add-on, or a mistyped Developer ID. The first build hit the demo-ID case
by using the kit example's own Developer ID with a made-up Local ID.

The **Authorization Key** on that same portal page is a secret and is not in
this repository. It is what generates Local IDs. The two MDID numbers are not
secret: they ship inside every add-on binary that has ever been distributed.

**Windows only.** The menu's app launch is `ShellExecuteW` behind a `WINDOWS`
guard, and the build is Windows only. The office runs Archicad 26 on Windows.
The three commands themselves have nothing platform-specific in them.

**One Archicad version per file.** An `.apx` is compiled against one major
version. Moving the office to Archicad 27 means adding a line to the workflow
matrix and re-testing, not recompiling the same file.
