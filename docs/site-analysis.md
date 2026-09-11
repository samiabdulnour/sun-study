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

## What the first live run found

Run on the Kogarah solar study, 18-24 Princes Highway, on 11 September 2026.
The fetch, the frame turn and the drawing all held; two walls and one fault
came up, and each is recorded in `docs/addon.md`.

**Entering the worksheet.** Archicad refuses to make a worksheet created in
the same session current, whichever add-on asks -- Tapir's `ChangeWindow`,
the add-on's `SetCurrentDatabase`, both `APIERR_BADDATABASE`. Opening the
worksheet by hand is what makes it current, and the run is built around
that: it creates the worksheet, prints *waiting for '14 | Context Analysis'
to become current*, and polls every two seconds for `--wait-minutes` (five by
default). Double-click the worksheet under Worksheets in the Project Map and
leave it as the front tab; the run draws the moment it sees it. On a later
session the worksheet already exists and is entered without help. The
add-on's own `CreateWorksheet` was refused outright by the first build, from
an undo scope it should not have had; that is fixed for the next build and
the Tapir route stands as the fallback.

**One letter per label.** Every text made through Tapir 1.5.8 showed its
first letter: the Text tool's default box in that project wraps, and Tapir
hands the default through. The add-on now has its own `CreateTexts`, with a
non-breaking box, the layer, the anchor and the pen set at creation; the run
uses it when the installed add-on has it and falls back to Tapir's, and the
layer move, otherwise. Until the new build is installed, setting the Text
tool's default to non-breaking in the project and rerunning is the
workaround.

**One letter, the real cause.** With the add-on's own command the labels
were still one letter, and a one-character and a ten-character text came
back the same width: the content was cut, not wrapped. Archicad 26's text
memo is a byte string (`char **textContent` in its header), and both Tapir
1.5.8 and the first build wrote UTF-16, whose second byte is a NUL. The
command writes UTF-8 now.

**The orthophoto.** No command in Archicad's API or Tapir places a picture,
so the add-on grew `PlaceFigures`: the tile as base64 inside the request,
its box in metres and its turn from the frame. With `--aerial` the tiles
are fetched into `data/aerial-context/` and `data/aerial-site/`, and each
sheet draws them under everything else; the zoning and institution washes
then use the project's `50%` fill with a clear background so the photo
shows through, the way the office's sheets have it. Without that fill
attribute the wash is solid and the run says so.

**Still to confirm by eye:** the `angle` of rotated labels is sent in
radians; if street names and boundary dimensions arrive turned wrong, it is
degrees. Fills were given the `Solid Fill` attribute (index 12 on that
project) and dashed lines the `Dashed` line type (14); lines took the nearest
pen of the active table, `00 FA Pens`, and the views are pinned to its
`- Site Analysis` sibling, which the office keeps for these sheets, and to
No Overrides.

## What comes next

The site bundle already carries every OSM building footprint in the extent
with its storey count and height where recorded. The next step is a massing
of the neighbours from those -- one slab or morph per footprint at
`levels x 3.1 m`, on a context layer -- so the shadow diagrams and the sun
eye views have something to cast against before anybody models the street.
