# The Loriini add-on

Eleven commands Archicad's JSON API does not have, and a menu so Loriini is
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
[the convention](#the-convention-settled) below, and the measurement that settled it.

It answers `APIERR_BADDATABASE` (-2130313110) when the front window and the
current database disagree -- a 3D Document on screen with the database moved
to a floor plan by `ensure_model_database`, say. Neither half of the add-on
can put them back together (`SetCurrentDatabase` reports the move refused),
so a command that needs the projection asks for a floor plan tab in front and
does not move the database itself.

### `Loriini.SetProjection`

Takes a bearing and an altitude in degrees, builds the transformation matrix,
and writes it back through `APIEnv_Change3DProjectionSetsID`. The bearing is
in the **project's frame**, clockwise from the project's own +Y axis, not a
true bearing; see [the convention](#the-convention-settled) for why and by
how much that differs.

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

Neither write is undoable, and the first build's attempt to make them so is
why it failed. The kit documents both `APIDb_NewDatabaseID` and
`APIEnv_ChangeDocumentFrom3DSettingsID` as *non-undoable data structure
modifiers*, and the first answers `APIERR_REFUSEDCMD` from inside an undo
scope: wrapped in `ACAPI_CallUndoableCommand`, the live call came back
`-2130312312`, which is that code. So seven documents made by a run and then
abandoned are deleted the way any other is, by hand in the Navigator. The names
a run gives them are what make that tolerable.

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

### `Loriini.ArrangeDrawings`

What a placed Drawing is, once Tapir has placed it. `CreateDrawings` puts a
Drawing of a 3D Document on a layout **clipped to a placeholder frame about
59 mm square, anchored by its bottom-left corner, and set to manual update**,
and Tapir can then change only its magnification. Opening the layout does not
help: the content regenerates inside the same 59 mm clip. A sheet of seven sun
eye views was seven stamps, with the fourth in the wrong corner because layout
coordinates run upward from the bottom-left and the tiling had assumed the
opposite.

`API_DrawingType` carries all of it as plain fields, and this command writes
them back with `ACAPI_Element_Change`, all in one undo step. Given a
`layoutDatabaseId` it makes that layout's database current for the call and
puts the previous one back after.

Freeing the frame was tried first and is the wrong answer: a drawing of a 3D
Document is the **whole site model** projected, and freed it swamps the sheet.
The office's diagram is a window around the building. So the frame stays a
clip, sized to the cell the drawing sits in, and the drawing is placed by its
**own origin** -- `useOwnOrigoAsAnchor` -- which for a 3D Document is the
projected model origin, and on a site modelled around its origin that is the
building. The placeholder frame Tapir leaves is centred there too; it is only
59 mm wide. "Almost there, just expand it", as the practice put it.

`GetDrawings` beside it reads every Drawing on a layout back -- position,
anchor, frame, bounds and the content box from
`APIDb_GetFullDrawingContentBoxID` -- because a frame written in the wrong
coordinate space is a drawing off the page, and this is how that is seen
rather than guessed.

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

What the first live run taught, on the Kogarah solar study on 11 September
2026. The first build wrapped `APIDb_NewDatabaseID` in an undo scope, and it
answered `APIERR_REFUSEDCMD` (-2130312312), exactly as the 3D Document
command had; the scope is gone. Tapir's own `CreateWorksheets` then made the
worksheet (it wants `name` and `referenceId`), and `SetCurrentDatabase` --
`APIDb_ChangeCurrentDatabaseID`, the add-on's own call -- was refused with
`APIERR_BADDATABASE` (-2130313110) for it, so the refusal Tapir's
`ChangeWindow` meets is Archicad's, not Tapir's. **A worksheet made in the
session cannot be entered from outside by any route.** What does make it
current is a person opening it: with the worksheet double-clicked in the
Project Map, `GetCurrentDatabase` reported its id, its type and its name, and
6,000 elements later drew into it. So the Python side creates the worksheet,
asks for it to be opened, and polls until it is (D84).

### `Loriini.CreateTexts`

Texts with the box, the layer and the anchor Tapir's `CreateTexts` cannot set.

Measured on the same run: the 81 labels of a site sheet, made through Tapir
1.5.8, each showed its first letter. `Get3DBoundingBoxes` on a ten-character
probe explained it -- 3.2 m wide and 5.5 m tall, ten lines of one character.
Tapir takes the Text tool's defaults and this project's default box wraps at
a width narrower than a character. Tapir's command has no field for the box,
`ModifyTexts` is not in 1.5.8, and `SetDetailsOfElements` refuses a Text
until 1.5.9. So the add-on carries the command: a non-breaking box unless a
`width` is asked for, the layer at creation (which retires the move D60 and
D62 describe), the anchor by name, the angle in radians, and the element ID
in the same call. Content is one paragraph and one run, the shape Tapir
builds, because a memo without paragraphs reads back with no style.

The probe also settled the unit of `height` for a worksheet: millimetres on
paper at the worksheet's own scale, 0.35 m per 3.5 mm in a fresh worksheet
at 1:100. A text keeps its paper size in any view, so a label sized for the
sheet prints right at the view's scale whatever the worksheet's is.

And the same afternoon, the reason the box was not the whole story: with the
new command installed the labels were *still* one letter, and a
one-character and a ten-character probe came back the same width from both
commands. The content was cut, not wrapped. Archicad 26's
`API_ElementMemo::textContent` is `char **` -- a byte string, read out of
the kit's own `APIdefs_Elements.h` -- and both Tapir 1.5.8 and the first
build of this command wrote UTF-16 with `GS::ucscpy`, whose second byte is
the NUL that ends a C string. Tapir's newest source switches to a
`GS::UniString` for Archicad 28, where the field's type changed, and keeps
the UTF-16 copy for 26. This command writes UTF-8.

### `Loriini.PlaceFigures`

A picture on the drawing, for the orthophoto under a site sheet. No command
anywhere places a Figure: not the JSON API, not Tapir. The image comes
inside the request as base64, because an add-on cannot be pointed at a path
on the machine; the box is the size in metres, `usePixelSize` off, with
`rotAngle` about the anchor and `storageFormat` from the file type. The
kit's own `Element_Test` example is the model: `API_PictureID`, the defaults
read with no memo, the file's bytes in `memo.pictHdl`.

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

## The convention, settled

`API_AxonoPars::tranmat` is a 3x4 matrix. `APIdefs_Base.h` gives the arithmetic:

```
x' = tmx[0] * x + tmx[1] * y + tmx[2]  * z + tmx[3]
y' = tmx[4] * x + tmx[5] * y + tmx[6]  * z + tmx[7]
z' = tmx[8] * x + tmx[9] * y + tmx[10] * z + tmx[11]
```

What it does **not** give is which way Archicad's rows and signs run, or how
`azimuth` and `projMod` interact with a matrix supplied from outside. The
structure's own documentation page is four lines long and was last revised in
December 2007. So `Projection.cpp` writes its convention down and
`GetProjection` exists to check it against a view aimed by hand.

That check was made on 10 September 2026 against the Kogarah solar study,
with the office's own `JUNE 21 - 9AM` view open in the 3D window. What came
back:

```
tranmat rows:
  -0.11543   0.99332   0.00000   0
  -0.32337  -0.03758   0.94553   0
   0.93921   0.10914   0.32554   0
orthonormal, determinant 1, invtranmat is the transpose
azimuth 6.628      projMod 15
sun     azimuth 6.492  altitude 18.979  given by date, 2017-06-21 09:00
```

Read against `ViewMatrix`, every part of the convention holds:

| | |
|---|---|
| Rows are `right`, `up`, `eye`, in that order | the third row is a unit vector at altitude 18.998, the first is horizontal |
| No transposition, no sign flip | the third row decodes to bearing 83.372 clockwise from project +Y; its negation and the columns decode to nothing meaningful |
| The frame is the **project's** | the tool's sun for that instant is true bearing 42.564, which is 83.512 in a project whose +Y sits at 319.052. The hand-aimed view is 0.14 degrees off it |
| `projMod` 15 is `API_Projection_FreeAx` | the preset whose matrix is its own, in `APIdefs_Elements.h` |
| `azimuth` is degrees **anticlockwise from +X** | 90 - 83.372 = 6.628, exactly as reported. Not radians, and not a bearing |
| Archicad's sun uses the same convention | 90 - 6.492 = 83.508 in the project frame, which is true 42.56: this tool's astronomy and Archicad's agree to 0.01 degrees in both axes |

So the bearing `SetProjection` takes is in the project frame, and a caller
holding a true bearing turns it first: `project = true - (270 + north)`, with
`north` the project's north angle in degrees, as `GetGeoLocation` reports it.
On the reference project that turn is 41 degrees, and getting it wrong does not
fail. It draws a complete, plausible diagram of the building lit from the wrong
side, which is precisely the failure this project exists to avoid. The seven
instants a study draws, at the reference project's latitude:

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

### What the first live write taught

The same session then wrote the exact 9am direction back through
`SetProjection`, and the call answered `success` while the matrix read back
afterwards was the one from before it. The cause is the second parameter of
`APIEnv_Change3DProjectionSetsID`. The header describes it as *"switch only
axono or persp"*, which the first build read as "touch only the axonometric
half". The kit's own documentation page says the opposite: with it set, *only
the `isPersp` field is considered* and every other parameter is ignored. The
parameter is now left out, and `Aim` fills `projMod` and `azimuth` alongside
the matrix so the dialog agrees with what the window shows.

Retested the same afternoon, with the 3D window starting from a perspective.
`SetProjection` for 21 June 09:00 at 83.512 and 18.977 came back through
`GetProjection` as a parallel projection whose third row decodes to exactly
83.512 and 18.977, with `azimuth` 6.488, `projMod` 15 and the sun given by
date. **`SetProjection` is verified**, on both the matrix and the frame.

### What a saved view and a 3D Document keep

The two routes to a sun eye set were then tried on the same project, and
both hold the projection they were made with.

**A view of the 3D window keeps it.** With the window aimed at 09:00 by
`SetProjection`, Tapir's `CreateViewsInViewMap` from the `Generic Axonometry`
item made a view; the window was then aimed at 15:00 and a second view made.
Opening the first by hand swung the window back to bearing 83.506, altitude
18.977, sun 21 June 2026 09:00 by date -- the add-on's numbers, not the
83.372 of the office's hand-aimed view beside it. So a saved Axonometry view
carries its own projection *and* its own sun date, and every other setting on
it is Tapir's to write.

**A 3D Document keeps it too.** `CreateDocumentFrom3D` for 12:00, once out of
the undo scope, made a document that opened as a noon sun eye view, confirmed
by eye. It appears in the Project Map like any other, and a view of it takes
the same settings as the office's own.

**The settings a sun eye view wants** are not the shadow diagrams'. Read off
the office's Solar Penetration Diagrams, and corrected by the practice:

| | |
|---|---|
| `graphicOverrideCombination` | `Sun Eye Views` -- the yellow glazing for this diagram, distinct from `Shadow Diagrams` |
| `renovationFilterGuid` | the project's planned filter, the one 749 of its 1,043 views carry |
| `layerCombination` | `04 \| Shadow Diagrams` with **every layer that carries a zone hidden**, thirteen of them on the reference project, measured from the zones rather than listed |
| `modelViewOptions` | `DA General Arrangement` |
| `d3styleName` | `OpenGL Shading with Contours with Shadows` on the views; none on the documents |

Zones matter because they are bodies in 3D: left visible they sit inside the
glazing the diagram is meant to show through. Tapir cannot name a renovation
filter, only carry its GUID, so the planned filter is found by reading it off
an existing view rather than by name.

**Nothing made before a close survives an unsaved close.** Views, folders and
layer combinations made through the API are ordinary project changes, and the
first calibration set vanished with a reopen. Save before restarting Archicad
for a new add-on build.

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

**The Local ID comes from Graphisoft, and cannot be invented.**
`RFIX/LoriiniAddOnFix.grc` holds an `'MDID'` resource of two numbers, the
practice's Developer ID and a Local ID generated per add-on on the Developer
Portal's Add-ons tab. Archicad validates the pair, and an invented Local ID
answers:

> This add-on cannot be validated. Please contact the distributor.

That message has no detail behind it and nothing appears in a log, so it is
worth recognising on sight. The dev kit's own FAQ lists the causes: no `MDID`
resource, one that was not compiled in, one holding demo IDs, one clashing with
another add-on, or a mistyped Developer ID. The first build hit the demo-ID case
by using the kit example's own Developer ID with a made-up Local ID. Swapping in
the practice's real Developer ID while keeping a made-up Local ID made it worse
rather than better: Archicad then rejected the file as not an add-on at all. The
pair is checked, and neither half can be guessed.

Keep the resource file bare. A prose comment above the resource compiled
without complaint and produced a binary Archicad would not open, which is why
the build now searches the finished `.apx` for the Developer ID's four bytes
before it will publish an artifact.

The **Authorization Key** on that same portal page is a secret and is not in
this repository. It is what generates Local IDs. The two MDID numbers are not
secret: they ship inside every add-on binary that has ever been distributed.

**Windows only.** The menu's app launch is `ShellExecuteW` behind a `WINDOWS`
guard, and the build is Windows only. The office runs Archicad 26 on Windows.
The three commands themselves have nothing platform-specific in them.

**One Archicad version per file.** An `.apx` is compiled against one major
version. Moving the office to Archicad 27 means adding a line to the workflow
matrix and re-testing, not recompiling the same file.
