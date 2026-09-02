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

and the same subtraction a third time, against a planning envelope, answers
"how much of that could have been cast by anything the controls allow here
anyway" -- which is the question the applicant is arguing and the objector is
disputing. All three come off one grid, so the three fills tile without a seam
and their areas add up.

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
from dataclasses import dataclass

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
    "ENVELOPE",
    "EXISTING",
    "ShadowInstant",
    "ShadowSeries",
    "cast_shadows",
    "ground_plane_grid",
]

#: The three fills, in the order they are drawn and legended. Keys rather than
#: an enum because they are also layer-name fragments, dictionary keys in the
#: report and column headings in the CSV, and a StrEnum that has to be
#: ``str()``-ed at every one of those is a worse deal than three constants.
EXISTING = "existing"
ADDITIONAL = "additional"
ENVELOPE = "envelope"

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
class ShadowInstant:
    """One moment, and the three shadow fills at it.

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
    context: TriangleMesh,
    proposal: TriangleMesh,
    envelope: TriangleMesh | None = None,
    sun_vectors: FloatArray,
    moments: Sequence[dt.datetime],
    labels: Sequence[str],
    spacing_m: float,
) -> ShadowSeries:
    """Trace the three shadow fills at every instant.

    ``context`` is everything that already stands -- neighbours, and any part
    of the site not being applied for. ``proposal`` is the scheme. ``envelope``
    is the planning control made solid, usually the site boundary extruded to
    a height limit; ``None`` leaves that category empty and draws no pink.

    ``sun_vectors`` must be unit vectors towards the sun **in the model
    frame**, one row per moment, exactly as ``analysis.sunlit_matrix`` wants
    them. Passing ENU vectors against a rotated model is the easiest way to
    produce a shadow diagram that is confidently wrong by the site's north
    angle, and nothing downstream can detect it.

    Three casts, not four: the envelope is compared against the same
    ``shaded_before`` as the proposal, so the pink and the blue are answers to
    the same question about the same land and can be read against each other.
    """
    if len(moments) != len(labels):
        raise ValueError(f"{len(moments)} moments but {len(labels)} labels")
    directions = np.ascontiguousarray(sun_vectors, dtype=np.float64)
    if directions.ndim != 2 or directions.shape[1] != 3:
        raise ValueError(f"sun_vectors must have shape (n, 3), got {directions.shape}")
    if len(directions) != len(moments):
        raise ValueError(f"{len(directions)} sun vectors for {len(moments)} moments")

    cell_area = spacing_m * spacing_m
    above_horizon = directions[:, 2] > 0.0

    before = _shaded(grid, Occluder(context), directions)
    after = _shaded(grid, Occluder(TriangleMesh.concatenate([context, proposal])), directions)
    with_envelope = (
        _shaded(grid, Occluder(TriangleMesh.concatenate([context, envelope])), directions)
        if envelope is not None and envelope.triangle_count
        else np.zeros_like(before)
    )

    instants: list[ShadowInstant] = []
    for index, (moment, label) in enumerate(zip(moments, labels, strict=True)):
        down = not bool(above_horizon[index])
        masks = {
            EXISTING: before[:, index],
            # Both differenced against the same "before", so the fills abut
            # instead of overlapping and their areas add.
            ADDITIONAL: after[:, index] & ~before[:, index],
            ENVELOPE: with_envelope[:, index] & ~before[:, index],
        }
        if down:
            masks = {key: np.zeros_like(mask) for key, mask in masks.items()}
        instants.append(
            ShadowInstant(
                moment=moment,
                label=label,
                regions={
                    key: tuple(drawable_contours(grid.positions, mask, spacing_m))
                    for key, mask in masks.items()
                },
                areas_m2={key: float(mask.sum()) * cell_area for key, mask in masks.items()},
                below_horizon=down,
            )
        )

    lit_ever = (~before).any(axis=1) if before.size else np.zeros(len(grid), dtype=bool)
    always_dark = float((~lit_ever).mean()) if len(grid) else 0.0
    return ShadowSeries(
        instants=tuple(instants),
        spacing_m=spacing_m,
        datum_m=float(grid.positions[:, 2].min()) if len(grid) else 0.0,
        sample_count=len(grid),
        permanently_dark_share=always_dark,
    )
