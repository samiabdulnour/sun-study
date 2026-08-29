# Graphisoft Developer Program — application text

Answers prepared for the Graphisoft Developer Program application form. Kept in the
repository so the description of the add-on and the description in the README cannot
drift apart, and so a later renewal starts from what was actually said.

Everything here is factual as of the state of the tool on this branch. Where the tool
does not yet do something, it says so — the reason for applying is precisely the set of
things the JSON automation interface cannot reach.

---

## "Please describe the Add-on you develop."

> What kind of functionality you want to enhance in ARCHICAD, or develop to ARCHICAD
> additionally? (i.e. export function for 3D model viewer on mobile platform / adding
> custom parameters to model elements / recolouring point cloud objects etc.)

### Full answer

I develop a solar-access analysis add-on for ARCHICAD, aimed at Australian residential
projects assessed under the NSW Apartment Design Guide (SEPP 65).

**The gap it fills.** ARCHICAD already carries everything the assessment needs — the
apartment Zones, the balconies, the living-room glazing, the overhangs, the 3D site
context, and the project's location and true north. What it cannot do is answer the
question a development application turns on: how many hours of direct sunlight each
apartment's living-room glazing and private open space receive on 21 June between 09:00
and 15:00. Today that number is produced outside the model, in Rhino/Grasshopper or by a
consultant, and typed back into ARCHICAD by hand — so it is stale the moment the massing
moves. The add-on computes it from the ARCHICAD model itself and writes the answer back
into the project.

**Functionality, in three parts:**

1. *Analysis.* Geometric direct-sunlight hours per Zone. Sun positions are pure astronomy
   from the project's own latitude, longitude and north (no weather file, no radiation or
   illuminance modelling), ray-cast against the building, its overhangs and the
   neighbouring context. A second mode works before apartments exist, banding facade area
   and open ground by sunlight hours — the metric a massing study iterates against.

2. *Custom parameters on model elements.* Results are written onto the apartment Zones as
   custom property values: the measured hours for living room and open space, the verdict,
   the ruleset and its version, and the timestamp of the run. The compliance table then
   becomes a native ARCHICAD Zone schedule with its own audit trail, and the practice's
   existing graphic overrides colour the SEPP 65 diagram from it. Nothing downstream of
   the property changes.

3. *Native documentation.* The study draws itself inside the project rather than arriving
   as an imported picture: one coloured fill per apartment, on that apartment's own storey
   and on a layer of the add-on's own, with band colours matched against the project's own
   pen table, plus a legend, and a Layout carrying one linked Drawing per storey. A re-run
   deletes its own previous output and redraws.

**Where it is today, and what I need the SDK for.** The tool exists, is written against
ARCHICAD 26 on Windows, and runs: a packaged desktop application driving the JSON
automation interface, with model geometry travelling out by IFC export. That architecture
has taken it as far as it goes, and two things now push it towards a real Add-On:

- **Reading geometry through the API rather than round-tripping IFC.** The export takes
  minutes on a real project, and its result depends on translator options, the active
  layer combination and even the current selection — settings that change the analysed
  model quietly rather than failing.
- **Element creation and drawing control the automation interface does not reach.**
  Setting a Drawing's scale; creating Morphs or Meshes that carry a building material;
  classifying a 2D fill so it can hold a property value; property labels bound to their
  Zone; activating a layer combination. Each of these currently has a workaround, a
  limitation the tool has to report to the user, or both.

I am an individual developer, working with an architectural practice in Sydney as the
first user of the tool.

### Short answer, if the field is tight

A solar-access analysis add-on for Australian residential projects. It computes geometric
direct-sunlight hours for each apartment's living-room glazing and private open space
from the ARCHICAD model's own geometry, location and true north, assesses them against the
NSW Apartment Design Guide, and writes the result back onto the apartment Zones as custom
property values — so the compliance table is a native ARCHICAD schedule rather than a
hand-typed document. It also draws the study natively: a colour-banded fill per apartment
on its storey, a legend, and a Layout of linked plans. It works today over the JSON
automation interface with geometry exported as IFC; I am applying for the SDK to read
model geometry directly and to reach element creation and drawing settings that the
automation interface does not expose.

---

## Supporting facts, if the form asks for more

| | |
|---|---|
| Application name | Loriini (the analysis it writes into a project is named "Sun Study") |
| Target version | ARCHICAD 26, Windows |
| Current integration | JSON automation interface (port 19723), via the Tapir add-on 1.5.1+ |
| Geometry path | IFC export from the open project, read with IfcOpenShell |
| Implementation | Python 3.11+, numpy; packaged as a single Windows executable |
| Written back as | A `Sun Study` property group of nine Zone properties |
| Also created in the project | Layers, Surfaces, Building Materials, Walls, Hatches, Texts, Views, Layouts and Drawings |
| Standard assessed | NSW Apartment Design Guide 4A-1, as a versioned data file with a citation per threshold |
| Validation | Sun positions checked against NREL/TP-560-34302 and NOAA's published spreadsheet; geometry against six closed-form analytic cases; whole-tool comparison against a Grasshopper/Ladybug chain agreeing to 0.19 percentage points |
| Status | Prototype. Not validated for DA submission — design-stage iteration only |
| Licence | Source-available; copyright Sami Abdulnour |

## What was hardest, and why it argues for a real Add-On

`docs/archicad.md` records what the automation interface would and would not do, measured
against a live ARCHICAD 26 rather than assumed from a schema. The short version, and the
best evidence for this application:

- A Drawing's scale field accepts a write, answers `{"success": true}`, and leaves the
  scale as it was. Magnification is the only real handle.
- `CreateMorphs` is refused on AC26 for every shape tried, and only `CreateWalls` accepts
  a building material — so a flat coloured patch has to be modelled as a thin wall lying
  down.
- ARCHICAD grants a custom property through a classification and will not classify a
  Hatch, so the measured numbers cannot live in the fill that shows them.
- A layer combination cannot be activated; it can only be read and copied layer by layer.
- One element left selected reduces the IFC export from 86 MB to 5.8 kB, with no error.

None of these are faults in the automation interface — they are the edge of what a
JSON command set is meant to cover. They are the work an Add-On would do properly.
