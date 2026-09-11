# Site and context analysis

Give it a NSW street address; it draws the three sheets an office prepares
when a DA starts -- the **context analysis**, the **site analysis** and the
**development summary** -- into worksheets of the open Archicad project, as
native fills, polylines and texts, from open data. No site visit, no
screenshots, nothing traced.

Ported from the office's `au-site-analysis` generator, endpoint for endpoint
and rule for rule, so the two agree about where every line comes from. That
generator prints an A1 PDF and a DXF; this draws the same content where the
office works, at true ground scale, in the project's own frame.

---

## What it makes

Everything under the tool's prefix (`14 |` unless told otherwise), so it can
be found, switched off or purged as a group.

| | |
|---|---|
| `14 \| Context Analysis` worksheet | LEP zoning wash coloured by the office's categories; schools, hospitals, child care and retail coloured by what they are and labelled by name; heritage items; the rail corridor; cadastre; roads with names on a double-headed arrow (a lane gets a plain label); railway, tram and bus routes; `T`, `L` and `B` roundels for stations and stops; 5 and 10 minute walking catchments; the site on top; a legend of what is on the map |
| `14 \| Site Analysis` worksheet | the boundary with a circle at each survey corner, `SB` dimensions per edge and `RL` levels per corner from the 1 m contours; the fall arrow, `FALL` and `AREA`; contours with their levels; neighbours as number, storeys and zone; trees, poles, lamps, hydrants; driveways as access triangles; kerbside parking bands; one-way and lane arrows; noise zigzags along main roads and rail; utilities where OSM has them; OSM building footprints; the sun path with winter and summer discs; prevailing wind banners; a legend |
| `14 \| Development Summary` worksheet | the planning-controls table: address, lots, cadastral area; LEP zone, height, FSR and the GFA it allows, minimum lot size, heritage; council DCP rows ruled up empty; the ADG figures worked for this site area |
| `14 \| Context Analysis.*`, `14 \| Site Analysis.*`, `14 \| Development Summary.*` layers | one per data class, so a reader can switch off what they do not want |
| `14 \| Site Analysis` view folder | a view of each worksheet at its sheet's scale -- 1:3000 for the context sheet, whichever of 1:200 to 1:750 fits the site for the site sheet -- with a layer combination showing that drawing's layers |
| `<run folder>/data/*.json` | everything fetched, so the sheets can be redrawn without the internet, or into another project |

Texts are millimetres on paper and keep that size in any view, so the sheet
prints at its scale whatever the worksheet's own scale is. Symbols -- the
stop roundels, the sun discs, the corner circles -- are geometry sized for
the sheet's scale.

## Running it

From the window: the **Site analysis** tab, an address, the three sheets
ticked, Run. From the command line:

```
sun-study site-analysis "26-30 Campsie St, Campsie NSW 2194" --port 19723
```

Options worth knowing:

| | |
|---|---|
| `--no-context`, `--no-site`, `--no-summary` | leave a sheet out |
| `--scale 2000` | the context sheet's scale; sets how much neighbourhood is fetched |
| `--site-scale 300` | pin the site sheet's scale instead of fitting |
| `--anchor site` | put the site's centre at the project origin (see below) |
| `--aerial` | also save the SIX orthophoto as JPEG tiles with world files |
| `--fetch-only` | fetch and save; touch nothing in Archicad |
| `--from <folder>` | draw the bundles saved in that folder's `data/`; no fetch |
| `--out <folder>` | where to save; `Documents\Loriini\site-analysis\<address>` by default |

Needs the Loriini add-on beside Tapir. A worksheet can only be drawn into in
the session that made it if the add-on's own `CreateWorksheet` made it and
entered it (D33), and the fills carry the legend's own RGB through
`CreateFills` (D82).

Put `TFNSW_API_KEY=...` in the environment for authoritative bus stops; free
from opendata.transport.nsw.gov.au. Without it the stops come from
OpenStreetMap along with the routes.

## Where the site lands

A bundle is in longitude and latitude. To draw it the tool needs to know
where the project is and which way it faces, and it reads both from
**Options > Project Preferences > Project Location**, the same place the sun
studies read them:

1. every point is projected to the MGA2020 grid (zone 56 for Sydney);
2. the project origin's own grid position is subtracted, so the origin is at
   `(0, 0)`;
3. the result is turned so true north sits where the project's north angle
   says it does -- the same turn the sun eye views make in the other
   direction, checked against the same hand-worked figure in the tests.

So a site drawn into a project that is already georeferenced lands under the
model, facing the right way, and a survey merged later lands on top of it.

The run prints where the site centre landed and how far that is from the
origin. **If the project location was never set** it is Archicad's Sydney
preset, and the site lands wherever that is -- usually kilometres off. The
run warns when the location looks like a preset (`archicad-info` does the
same), and `--anchor site` puts the site's centre at the origin instead,
north-up unless the project has a north angle. That is the right choice for
a project that has nothing in it yet.

## The data

All free, none needing a key except TfNSW. Every source but the site itself
fails soft: a service that is down costs the sheet that layer and prints a
`WARN` line, and a run whose context sheet is missing its bus routes is still
a context sheet.

| need | source |
|---|---|
| geocoding | NSW Spatial `NSW_Geocoded_Addressing_Theme`, with the register's quirks handled: ranged parents, unit noise, suffixes, locality mismatches |
| lot boundary, rail corridor, property parcels | `NSW_Land_Parcel_Property_Theme` 8, 7, 12 |
| zoning, height, FSR, lot size, heritage | ePlanning `EPI_Primary_Planning_Layers` 2, 5, 1, 4, 0 |
| roads with names, hierarchy and lanes; railways | `NSW_Transport_Theme` 5, 7 |
| stations, parks, named building complexes, named areas | `NSW_Features_of_Interest_Category` and `NSW_FOI_Transport_Facilities` |
| contours, neighbour address points | `NSW_Elevation_and_Depth_Theme` 2, `NSW_Geocoded_Addressing_Theme` 1 |
| bus stops and routes, trees, hydrants, poles, lamps, one-ways, driveways, parking, utilities, building footprints | OpenStreetMap through Overpass, mirrors rotated, answers cached for a month under `%LOCALAPPDATA%\Loriini\cache\osm` |
| walking catchments | Valhalla's public server, pedestrian costing, 5 and 10 minutes |
| aerial | NSW SIX imagery export, tiled, with `.jgw` world files on the MGA grid |

The curation is the office's: zoning is the base, and a named complex is
coloured by what its name says it is -- `SCHOOL` is education, `HOSPITAL` is
medical, `CHURCH` is community -- because the register's type codes are
unreliable and its names are not. The palette is the Oatley legend's.

## What to check on a live project

None of this has run against an Archicad yet. The fake-transport tests
assert the shape of every request; these are the facts only a workstation
can settle, in the order they will be met:

1. **`CreateTexts` and `angle`.** Rotated labels -- street names, boundary
   dimensions -- pass `angle` in radians, the Archicad API's own unit, on the
   strength of D60's probe that the field exists. If they arrive turned by
   the wrong amount, it is degrees. Everything else about a text is the same
   call the shadow diagrams already make.
2. **`SetCurrentDatabase` into an existing worksheet.** A rerun finds the
   worksheet by name and enters it through the add-on before clearing it.
   The command reads back and refuses a move that did not happen, so a
   refusal is reported rather than drawn over.
3. **The fill attribute.** Fills are given the attribute named `Solid Fill`
   when the project has one; otherwise they take the Fill tool's current
   pattern and the run says so. Likewise the line type named `Dashed`.
4. **The text move.** Texts land on the Text tool's default layer and are
   moved onto the study's; the run reports how many arrived. The landing
   layer is switched on inside the worksheet first, which is where the
   elements are.
5. **The frame turn**, by eye: the site's street should run the way it does
   on the survey.

The aerial is not drawn: Archicad's JSON API has no command that places a
picture, and neither does the add-on yet. It is saved for placing by hand.

## What comes next

The site bundle already carries every OSM building footprint in the extent
with its storey count and height where recorded. The next step is a massing
of the neighbours from those -- one slab or morph per footprint at
`levels x 3.1 m`, on a context layer -- so the shadow diagrams and the sun
eye views have something to cast against before anybody models the street.
