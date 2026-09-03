"""Shadows cast onto a plane, and whose fault each part of one is.

A shadow diagram is not a picture of shade. It is an argument about
*attribution*: this much of the neighbourhood was already dark at ten in the
morning, and this much more is dark because of the thing being applied for.
A consent authority reads the second number, not the first, and a drawing that
merges them answers a question nobody asked.

That attribution is the whole reason for computing shadows rather than
photographing them off a 3D Document. Getting the split out of a rendered
model means building the scheme twice -- once present, once absent -- lining
the two images up and subtracting them by eye or in Photoshop. Here it is two
ray casts and a boolean:

    shaded_before = ~sunlit(context alone)
    shaded_after  = ~sunlit(context and the proposal)

    existing   = shaded_before
    additional = shaded_after and not shaded_before

and the same subtraction against a planning envelope answers "how much of that
could have been cast by anything the controls allow here anyway" -- which is
the question the applicant is arguing and the objector is disputing.

How many subtractions there are is the project's business, not this module's.
A sheet's legend is however many rows it took to make the argument: two on one
of the office's jobs, six on the Campsie SSDA set, four on a site where the
comparison is a TOD height limit against a SEARs massing. So what comes in is
an ordered list of *sources*, each either a BASELINE -- something that will be
there, charged only for the ground the baselines before it had not already
darkened -- or a SCENARIO, something that might be, charged against the whole
baseline and against no other scenario.

That distinction is the one thing here worth being careful about. Baselines
tile: their fills abut and their areas add to the total shadow. Scenarios
deliberately do not, because two of them are rival answers about the same
land, and a drawing that differenced them against each other would show the
second one only where it beat the first -- which is not a shadow of anything.
Everything comes off one grid either way, so any two fills that do abut, abut
exactly.

The plane
---------
Shadows land on one horizontal plane, which is the convention every consent
authority draws to and the only surface on which the outlines stay legible at
1:1000. It is not a claim about the ground: a sloping site really does catch a
shadow further up the hill than a flat datum says. See D72 for why that trade
was taken, and ``datum_m`` for how to move it.

The grid is the whole rectangle, footprints included. A sample under a
building is shaded by that building and belongs in the shadow, which is how a
shadow diagram reads -- the grey field in a reference sheet covers the
neighbours as well as the ground between them. Deleting footprints, the way
the massing study's open-ground grid does, would punch a hole in every shadow
exactly where a building stands.

What is *not* an occluder here
------------------------------
Terrain. If the site mesh is handed in among the context, every sample on the
flat datum that happens to sit below the hill is permanently dark, and the
drawing fills solid grey. ``permanently_dark_share`` is reported so that shows
up as a number in the run rather than as a sheet somebody prints.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, replace

import numpy as np
import numpy.typing as npt

from sun_study.core.analysis import sunlit_matrix
from sun_study.core.geometry import TriangleMesh
from sun_study.core.occlusion import Occluder
from sun_study.core.patches import Ring, drawable_contours
from sun_study.core.sampling import SamplePoints, horizontal_grid

FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]

__all__ = [
    "ADDITIONAL",
    "BASELINE",
    "ENVELOPE",
    "EXISTING",
    "SCENARIO",
    "Draped",
    "ShadowInstant",
    "ShadowSeries",
    "ShadowSource",
    "SourceSpec",
    "cast_shadows",
    "default_sources",
    "drape_onto_terrain",
    "ground_plane_grid",
]

#: The default three fills, in the order they are drawn and legended. Keys
#: rather than an enum because they are also layer-name fragments, dictionary
#: keys in the report and column headings in the CSV, and a StrEnum that has
#: to be ``str()``-ed at every one of those is a worse deal than three
#: constants.
#:
#: They are a *default*, not the vocabulary. A real sheet's legend is however
#: many rows that project argued its case in: the Campsie SSDA set carries six
#: -- existing neighbours, future neighbours, existing structures on the site,
#: two LEP height limits and the proposed envelope -- where another job in the
#: same office carries two. Three hard-coded categories could draw the second
#: sheet and never the first, so what the engine actually takes is a list.
EXISTING = "existing"
ADDITIONAL = "additional"
ENVELOPE = "envelope"

#: What a source is *for*, which is the whole of how its fill is computed.
#:
#: A baseline is a thing that will be there: the existing neighbours, the
#: buildings already approved next door, the structures on the site being kept.
#: Baselines accumulate in the order given, each charged only for the ground
#: the ones before it had not already darkened, so their fills abut and their
#: areas add.
#:
#: A scenario is a thing that *might* be there, and every scenario is an
#: answer to the same question about the same land -- what a height limit
#: allows, what the SEARs massing is, what is being applied for. Each is cast
#: against the whole baseline and none against another, so two scenarios
#: overlap on purpose: that overlap is the comparison the sheet exists to
#: make, and differencing them against each other would destroy it.
BASELINE = "baseline"
SCENARIO = "scenario"

#: How far above the datum the samples sit. A point exactly on a plane that
#: coincides with a ground slab starts its ray inside that slab and reads as
#: permanently dark -- the same 50 mm the communal study learned to use, and
#: for the same reason.
DEFAULT_DATUM_OFFSET_M = 0.05

#: How far past the subject's own footprint to grid. A shadow at nine in the
#: morning on 21 June in Sydney runs roughly twice the building's height, so
#: 150 m covers a 70 m tower and most of what it reaches. Too small and the
#: shadow is *clipped at the edge of the grid* with a straight line that looks
#: like a real boundary, which is the failure worth being generous against.
DEFAULT_MARGIN_M = 150.0


@dataclass(frozen=True)
class SourceSpec:
    """A source stripped of its geometry: what the drawing end needs.

    The mesh is the heaviest thing in a shadow run and the drawing end has no
    use for it -- it wants to know what the categories were called, what order
    they go in and which of them is a scenario, so the layers, the legend and
    the console table can all be built from one list instead of three that
    drift. Carried on the series for exactly that reason.
    """

    key: str
    """Dictionary key, layer-name fragment and Element ID segment."""

    label: str
    """What the legend row says. The sheet's words, not the tool's."""

    role: str = BASELINE


@dataclass(frozen=True)
class ShadowSource:
    """One thing that casts a shadow, and what its shadow means."""

    key: str
    label: str
    mesh: TriangleMesh
    role: str = BASELINE

    @property
    def spec(self) -> SourceSpec:
        return SourceSpec(key=self.key, label=self.label, role=self.role)


def default_sources(
    context: TriangleMesh,
    proposal: TriangleMesh,
    envelope: TriangleMesh | None = None,
) -> tuple[ShadowSource, ...]:
    """The three-fill study, as a source list.

    What a run with nothing named produces, and the shape every sheet in the
    office's simpler set is drawn in: what already stands, what the controls
    allowed anyway, and what the proposal adds on top of both being read
    against the same ground.
    """
    sources = [
        ShadowSource(EXISTING, "Shadow cast by existing buildings", context, BASELINE),
    ]
    if envelope is not None and envelope.triangle_count:
        sources.append(
            ShadowSource(ENVELOPE, "Shadow cast by the planning envelope", envelope, SCENARIO)
        )
    sources.append(
        ShadowSource(ADDITIONAL, "Additional shadow cast by proposed building", proposal, SCENARIO)
    )
    return tuple(sources)


@dataclass(frozen=True)
class ShadowInstant:
    """One moment, and one shadow fill per source at it.

    ``regions`` holds the rings to draw per category, already reduced to
    what a single-contour fill can carry: outlines where the shadow is solid,
    tiled rectangles where it has a courtyard in it. That reduction belongs
    here rather than beside the drawing command, because a shadow with a hole
    filled in is not a drawing defect -- it is a claim that a sunlit courtyard
    was dark. A category with no shadow at all has an empty tuple, which is
    drawn as nothing rather than skipped, so the layer for that hour still
    exists and its view still opens.

    ``below_horizon`` is the one case where the outlines are deliberately
    empty and the areas are not zero-because-nothing-was-shaded. With the sun
    down, every ray is refused before it is cast and *everything* reads as
    shaded; drawn, that is a sheet showing the whole suburb in shadow at an
    hour the study should not have been asked about. So it is flagged, drawn
    empty, and said out loud by the caller.
    """

    moment: dt.datetime
    label: str
    regions: dict[str, tuple[Ring, ...]]
    areas_m2: dict[str, float]
    below_horizon: bool = False

    @property
    def caption(self) -> str:
        """What goes under the drawing, in the reference sheets' own words."""
        return f"{self.moment.strftime('%B %d').upper()} -{self.label}"


@dataclass(frozen=True)
class ShadowSeries:
    """Every instant asked for, and what the grid they were measured on was."""

    instants: tuple[ShadowInstant, ...]
    sources: tuple[SourceSpec, ...]
    """The categories, in drawing order, back to front.

    On the series rather than rederived at each end, because the layer a fill
    lands on, the Element ID it is stamped with, the legend row it is
    explained by and the column it is totalled in are four spellings of one
    list, and four places to keep them in step is three too many.
    """

    spacing_m: float
    datum_m: float
    sample_count: int
    permanently_dark_share: float
    """Fraction of the grid that was shaded at *every* instant, context alone.

    A number worth printing. On an open site it is the footprints of the
    neighbours and little else, so a few per cent; anything approaching one
    means the terrain was handed in as an occluder and the drawing is solid
    grey. See the module docstring.
    """

    def describe(self) -> str:
        dark = f"{self.permanently_dark_share * 100:.0f}%"
        return (
            f"{len(self.instants)} instants on a {self.spacing_m:g} m grid at "
            f"{self.datum_m:g} m, {self.sample_count} samples, {dark} always dark"
        )


def ground_plane_grid(
    bounds: tuple[FloatArray, FloatArray],
    *,
    datum_m: float,
    spacing_m: float,
    margin_m: float = DEFAULT_MARGIN_M,
    offset_m: float = DEFAULT_DATUM_OFFSET_M,
) -> SamplePoints:
    """An upward-facing grid over the site and everything a shadow can reach.

    ``bounds`` is the (min, max) corner pair of everything being drawn -- the
    subject and its context together, not the subject alone, since the point
    of the margin is to catch a shadow leaving the site and the point of the
    context bounds is that the drawing shows the neighbours it lands on.

    Every cell is kept, footprints included; see the module docstring.
    """
    lower, upper = np.asarray(bounds[0], dtype=np.float64), np.asarray(bounds[1], dtype=np.float64)
    return horizontal_grid(
        (float(lower[0]) - margin_m, float(lower[1]) - margin_m, datum_m),
        float(upper[0] - lower[0]) + 2.0 * margin_m,
        float(upper[1] - lower[1]) + 2.0 * margin_m,
        "shadow-plane",
        height_m=offset_m,
        spacing_m=spacing_m,
    )


@dataclass(frozen=True)
class Draped:
    """A grid moved onto the ground, and which of it found ground at all."""

    grid: SamplePoints
    on_terrain: BoolArray
    """Per sample. False where the survey does not reach, and those samples
    are on the fallback datum rather than on anything real."""

    @property
    def off_terrain_share(self) -> float:
        return float(1.0 - self.on_terrain.mean()) if len(self.on_terrain) else 0.0


def drape_onto_terrain(
    grid: SamplePoints,
    terrain: TriangleMesh,
    *,
    offset_m: float = DEFAULT_DATUM_OFFSET_M,
) -> Draped:
    """Move every sample down or up onto the terrain surface beneath it.

    The flat datum is a convention, not a claim about the ground (D72), and on
    a site that falls it is wrong in a way that matters: a shadow really does
    reach further up a hill than a level plane says, and the drawing that
    matters most -- how far the proposal's shadow climbs towards the
    neighbours -- is exactly the one the flat plane understates. Crows Nest
    falls about ninety metres across the model.

    Terrain is a heightfield, not a solid, so this projects each triangle into
    plan and interpolates its z rather than casting a ray: an Archicad Mesh is
    a single-valued surface, and the barycentric answer is exact where a ray
    would be an approximation with a tolerance to tune. Where two triangles
    cover one sample -- a fold, or two meshes overlapping -- the *highest*
    wins, because that is the surface a shadow would land on.

    Samples outside the terrain's own footprint keep the datum they came in
    with. That is a real case rather than an error: the grid runs a margin
    past everything so a shadow leaving the site is not clipped, and the
    survey rarely reaches that far. It is reported as ``off_terrain`` so a
    caller can say how much of the drawing is still flat.

    Returns the draped grid and which samples found ground. Where a caller
    clips to the survey, that flag is the clip.
    """
    positions = np.array(grid.positions, dtype=np.float64, copy=True)
    none_found = np.zeros(len(positions), dtype=bool)
    if not terrain.triangle_count or not len(positions):
        return Draped(grid=grid, on_terrain=none_found)

    # The grid is regular and axis-aligned, so a triangle's plan bounding box
    # maps straight to a block of cells. Walking 33,000 triangles and touching
    # only the cells each one covers is linear in the terrain; testing every
    # sample against every triangle would be 2.8 x 10^10 tests.
    xs = np.unique(positions[:, 0])
    ys = np.unique(positions[:, 1])
    if len(xs) < 2 or len(ys) < 2:
        return Draped(grid=grid, on_terrain=none_found)
    step_x, step_y = xs[1] - xs[0], ys[1] - ys[0]
    x0, y0 = xs[0], ys[0]
    nx, ny = len(xs), len(ys)

    # Where each sample sits in that lattice, so a cell index can be turned
    # back into the row of ``positions`` it belongs to.
    col = np.rint((positions[:, 0] - x0) / step_x).astype(np.int64)
    row = np.rint((positions[:, 1] - y0) / step_y).astype(np.int64)
    into = np.full((nx, ny), -1, dtype=np.int64)
    into[col, row] = np.arange(len(positions))

    height = np.full((nx, ny), -np.inf, dtype=np.float64)
    corners = terrain.vertices[terrain.faces]

    # Near-vertical faces cannot catch a shadow, and they are what a "terrain"
    # made of solids is full of: site context extruded from datum zero up to
    # the ground, roads modelled as slabs, each with a skirt joining its base
    # to its top. Interpolating a skirt puts a sample somewhere down the side
    # of the block instead of on the ground above it.
    #
    # Kept on |nz| rather than nz, so both faces of a closed solid survive:
    # winding is not dependable in an IFC export, and the *highest* surface
    # wins below anyway, which picks the top of a solid without needing to
    # know which way its normals were written. Degenerate triangles have no
    # normal and no plan area, and drop out here rather than dividing by zero
    # further down.
    normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    # About six degrees off vertical: enough to drop a skirt that is not quite
    # plumb, shallow enough to keep a steep bank that really is ground.
    steep_enough = lengths > 0.0
    steep_enough &= np.abs(normals[:, 2]) / np.maximum(lengths, 1e-12) > 0.1
    corners = corners[steep_enough]
    if not len(corners):
        return Draped(grid=grid, on_terrain=none_found)

    a, b, c = corners[:, 0], corners[:, 1], corners[:, 2]

    lo_x = np.floor((np.minimum(np.minimum(a[:, 0], b[:, 0]), c[:, 0]) - x0) / step_x)
    hi_x = np.ceil((np.maximum(np.maximum(a[:, 0], b[:, 0]), c[:, 0]) - x0) / step_x)
    lo_y = np.floor((np.minimum(np.minimum(a[:, 1], b[:, 1]), c[:, 1]) - y0) / step_y)
    hi_y = np.ceil((np.maximum(np.maximum(a[:, 1], b[:, 1]), c[:, 1]) - y0) / step_y)

    for index in range(len(corners)):
        i0, i1 = int(max(lo_x[index], 0)), int(min(hi_x[index], nx - 1))
        j0, j1 = int(max(lo_y[index], 0)), int(min(hi_y[index], ny - 1))
        if i0 > i1 or j0 > j1:
            continue
        gx = x0 + np.arange(i0, i1 + 1) * step_x
        gy = y0 + np.arange(j0, j1 + 1) * step_y
        px, py = np.meshgrid(gx, gy, indexing="ij")

        # Barycentric coordinates in plan. A degenerate triangle -- one seen
        # edge-on, which a vertical cliff in a mesh really is -- has zero area
        # in plan and is skipped rather than dividing by it.
        ax, ay = a[index, 0], a[index, 1]
        v0x, v0y = b[index, 0] - ax, b[index, 1] - ay
        v1x, v1y = c[index, 0] - ax, c[index, 1] - ay
        denominator = v0x * v1y - v1x * v0y
        if abs(denominator) < 1e-12:
            continue
        wx, wy = px - ax, py - ay
        u = (wx * v1y - v1x * wy) / denominator
        v = (v0x * wy - wx * v0y) / denominator
        inside = (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1.0 + 1e-9)
        if not inside.any():
            continue
        z = a[index, 2] + u * (b[index, 2] - a[index, 2]) + v * (c[index, 2] - a[index, 2])
        block = height[i0 : i1 + 1, j0 : j1 + 1]
        np.maximum(block, np.where(inside, z, -np.inf), out=block)

    found = np.isfinite(height) & (into >= 0)
    rows = into[found]
    positions[rows, 2] = height[found] + offset_m
    on_terrain = np.zeros(len(positions), dtype=bool)
    on_terrain[rows] = True
    return Draped(grid=replace(grid, positions=positions), on_terrain=on_terrain)


def _shaded(grid: SamplePoints, occluder: Occluder, sun_vectors: FloatArray) -> BoolArray:
    """Which samples are *not* in the sun, per instant.

    Written here rather than taken as ``~sunlit_matrix`` so the below-horizon
    case can be told apart from the shaded one. ``sunlit_matrix`` answers
    "false" to both, which is correct for counting sunlight hours and wrong
    for drawing a shadow: at an hour before sunrise it would fill the sheet.
    Telling the two apart is the caller's job, on the sun vector's own +Z.
    """
    return ~sunlit_matrix(grid, occluder, sun_vectors)


def cast_shadows(
    grid: SamplePoints,
    *,
    sources: Sequence[ShadowSource],
    sun_vectors: FloatArray,
    moments: Sequence[dt.datetime],
    labels: Sequence[str],
    spacing_m: float,
    receiving: BoolArray | None = None,
) -> ShadowSeries:
    """Trace one shadow fill per source at every instant.

    ``receiving`` marks the samples that are real ground. Where a study is
    draped onto a survey that does not cover the whole grid, the rest sit on a
    fallback datum abutting terrain that may be tens of metres higher or
    lower, and a shadow crossing that step is an artefact of the seam rather
    than anything on the site. Masked out here rather than dropped from the
    grid, so the lattice stays regular and the contours still tile.

    ``sources`` is the legend, in the order it is drawn and read, back to
    front. Its two roles are what the arithmetic turns on -- see ``BASELINE``
    and ``SCENARIO`` -- and they are not interchangeable: a scenario demoted
    to a baseline would charge the SEARs massing only for what the TOD
    massing had not already darkened, which is a comparison between two
    alternatives that were never going to stand at the same time.

    ``sun_vectors`` must be unit vectors towards the sun **in the model
    frame**, one row per moment, exactly as ``analysis.sunlit_matrix`` wants
    them. Passing ENU vectors against a rotated model is the easiest way to
    produce a shadow diagram that is confidently wrong by the site's north
    angle, and nothing downstream can detect it.

    One ray cast per source, and no more. A baseline is cast against the
    baselines before it, a scenario against all of them; nothing is cast
    twice, so the cost is linear in the legend rather than in its powerset.
    """
    if len(moments) != len(labels):
        raise ValueError(f"{len(moments)} moments but {len(labels)} labels")
    directions = np.ascontiguousarray(sun_vectors, dtype=np.float64)
    if directions.ndim != 2 or directions.shape[1] != 3:
        raise ValueError(f"sun_vectors must have shape (n, 3), got {directions.shape}")
    if len(directions) != len(moments):
        raise ValueError(f"{len(directions)} sun vectors for {len(moments)} moments")
    if not sources:
        raise ValueError("No shadow sources, so there is nothing to draw.")
    seen = [source.key for source in sources]
    if len(set(seen)) != len(seen):
        raise ValueError(f"Two shadow sources share a key: {sorted(seen)}")
    unknown = {source.role for source in sources} - {BASELINE, SCENARIO}
    if unknown:
        raise ValueError(f"Unknown source role(s) {sorted(unknown)}")

    cell_area = spacing_m * spacing_m
    above_horizon = directions[:, 2] > 0.0
    empty = np.zeros((len(grid), len(moments)), dtype=bool)
    ground = (
        np.ones(len(grid), dtype=bool) if receiving is None else np.asarray(receiving, dtype=bool)
    )
    if len(ground) != len(grid):
        raise ValueError(f"{len(ground)} receiving flags for {len(grid)} samples")

    # Baselines first, cumulatively. ``standing`` is the mesh of everything
    # that will be there; ``before`` is the ground it has already darkened,
    # and it is the datum every scenario is then charged against.
    masks: dict[str, BoolArray] = {}
    standing: list[TriangleMesh] = []
    before = empty
    for source in sources:
        if source.role != BASELINE:
            continue
        standing.append(source.mesh)
        # A baseline with no geometry darkens nothing. Worth short-circuiting
        # rather than casting: an empty source is what "the project has no
        # future context" looks like, and it should cost nothing and draw
        # nothing rather than raise.
        shaded = (
            _shaded(grid, Occluder(TriangleMesh.concatenate(standing)), directions)
            if any(mesh.triangle_count for mesh in standing)
            else empty
        )
        masks[source.key] = shaded & ~before
        before = shaded

    baseline = TriangleMesh.concatenate(standing) if standing else TriangleMesh.empty()

    # Then every scenario against that same datum, and never against each
    # other. Two scenarios overlapping is the point: see ``SCENARIO``.
    for source in sources:
        if source.role != SCENARIO:
            continue
        after = (
            _shaded(grid, Occluder(TriangleMesh.concatenate([baseline, source.mesh])), directions)
            if source.mesh.triangle_count
            else before
        )
        masks[source.key] = after & ~before

    instants: list[ShadowInstant] = []
    for index, (moment, label) in enumerate(zip(moments, labels, strict=True)):
        down = not bool(above_horizon[index])
        at_this_hour = {
            source.key: (
                np.zeros(len(grid), dtype=bool) if down else masks[source.key][:, index] & ground
            )
            for source in sources
        }
        instants.append(
            ShadowInstant(
                moment=moment,
                label=label,
                regions={
                    key: tuple(drawable_contours(grid.positions, mask, spacing_m))
                    for key, mask in at_this_hour.items()
                },
                areas_m2={key: float(mask.sum()) * cell_area for key, mask in at_this_hour.items()},
                below_horizon=down,
            )
        )

    # Over the ground that exists, not over the whole rectangle: samples off
    # the survey are drawn nowhere, so counting them as permanently dark would
    # raise an alarm about a region the drawing does not make a claim about.
    lit_ever = (~before).any(axis=1) if before.size else np.zeros(len(grid), dtype=bool)
    always_dark = float((~lit_ever[ground]).mean()) if ground.any() else 0.0
    return ShadowSeries(
        instants=tuple(instants),
        sources=tuple(source.spec for source in sources),
        spacing_m=spacing_m,
        datum_m=float(grid.positions[:, 2].min()) if len(grid) else 0.0,
        sample_count=len(grid),
        permanently_dark_share=always_dark,
    )
