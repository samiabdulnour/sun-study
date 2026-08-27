"""Area-weighted banding and massing-stage sampling.

The office's massing decks report a share of *square metres*, not a share of
apartments, because at massing stage there are no apartments. Two things
therefore have to be right that the per-apartment path never exercised: every
sample must carry its true area, and the bands must be weighted by that area.

Areas are checked against closed-form geometry, because a sample-counted
percentage looks exactly like an area-weighted one until the mesh stops being
uniform.
"""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from sun_study.core.analysis import (
    DEFAULT_BAND_EDGES_MINUTES,
    band_by_area,
    cumulative_minutes,
    instant_weights,
)
from sun_study.core.geometry import TriangleMesh, box, rectangle
from sun_study.core.sampling import (
    FaceSelection,
    SamplePoints,
    grid_on_rectangle,
    horizontal_grid,
    single_sample,
    triangle_samples,
)
from sun_study.ingest.ifc import IfcElement, read_ifc
from sun_study.ingest.scene import (
    MassingConfig,
    SceneConfigError,
    build_massing_scene,
    zone_floor_grid,
    zone_surface_grid,
)
from sun_study.pipeline import WEIGHTING_BY_RULESET, run_massing
from sun_study.rules.ruleset import load_ruleset

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SAMPLE = FIXTURES / "sample_building.ifc"


def flat(minutes: list[float], area: float = 1.0) -> tuple[SamplePoints, np.ndarray]:
    count = len(minutes)
    points = SamplePoints(
        np.zeros((count, 3)),
        np.tile([0.0, 0.0, 1.0], (count, 1)),
        tuple(f"s{i}" for i in range(count)),
        np.full(count, area),
    )
    return points, np.asarray(minutes, dtype=np.float64)


# ---------------------------------------------------------------------------
# Sample areas.
# ---------------------------------------------------------------------------
def test_rectangle_grid_areas_sum_to_the_rectangle() -> None:
    points = grid_on_rectangle(
        (0.0, 0.0, 0.0), (2.4, 0.0, 0.0), (0.0, 0.0, 1.8), "w", spacing_m=0.2
    )
    assert points.total_area_m2 == pytest.approx(2.4 * 1.8)
    assert np.allclose(points.areas, points.areas[0]), "a regular grid has equal cells"


def test_grid_area_uses_the_cross_product_not_the_side_lengths() -> None:
    """A sheared parallelogram's area is |u x v|, not |u| |v|.

    Multiplying the side lengths would overstate a raked facade panel.
    """
    points = grid_on_rectangle(
        (0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (1.0, 0.0, 1.0), "w", spacing_m=0.5
    )
    assert points.total_area_m2 == pytest.approx(2.0)  # |(2,0,0) x (1,0,1)| = 2
    assert points.total_area_m2 != pytest.approx(2.0 * math.sqrt(2.0))


def test_triangle_samples_areas_sum_to_the_mesh_area() -> None:
    """The property every square-metre figure depends on."""
    mesh = box((0.0, 0.0, 0.0), (3.0, 2.0, 4.0))
    expected = 2 * (3 * 2) + 2 * (3 * 4) + 2 * (2 * 4)  # 12 + 24 + 16

    points = triangle_samples(
        mesh.triangles(), ["b"] * mesh.triangle_count, spacing_m=0.25, surface_offset_m=0.0
    )
    assert points.total_area_m2 == pytest.approx(expected, rel=1e-12)


@pytest.mark.parametrize("spacing", [0.1, 0.25, 0.5, 1.0, 5.0])
def test_triangle_sample_area_is_independent_of_spacing(spacing: float) -> None:
    """Refining the grid must not change the total area, only its resolution."""
    mesh = box((0.0, 0.0, 0.0), (3.0, 2.0, 4.0))
    points = triangle_samples(
        mesh.triangles(), ["b"] * mesh.triangle_count, spacing_m=spacing, surface_offset_m=0.0
    )
    assert points.total_area_m2 == pytest.approx(52.0, rel=1e-12)


def test_a_triangle_smaller_than_a_cell_still_gets_one_sample() -> None:
    """Dropping it would quietly shrink the denominator."""
    tiny = rectangle((0.0, 0.0, 0.0), (0.01, 0.0, 0.0), (0.0, 0.0, 0.01))
    points = triangle_samples(tiny.triangles(), ["t", "t"], spacing_m=1.0)
    assert len(points) == 2
    assert points.total_area_m2 == pytest.approx(1e-4, rel=1e-9)


def test_subdivision_count_is_a_square() -> None:
    """k^2 congruent sub-triangles, so every sample stands for equal area."""
    single = rectangle((0.0, 0.0, 0.0), (4.0, 0.0, 0.0), (0.0, 0.0, 4.0))
    points = triangle_samples(single.triangles()[:1], ["t"], spacing_m=1.0)
    count = len(points)
    assert count == round(math.sqrt(count)) ** 2
    assert np.allclose(points.areas, points.areas[0])


def test_samples_lie_inside_their_triangle() -> None:
    """Centroids of sub-triangles, so nothing lands on an edge or outside."""
    triangle = np.array([[[0.0, 0.0, 0.0], [4.0, 0.0, 0.0], [0.0, 0.0, 4.0]]])
    points = triangle_samples(triangle, ["t"], spacing_m=0.5, surface_offset_m=0.0)
    x, z = points.positions[:, 0], points.positions[:, 2]
    assert np.all(x > 0.0) and np.all(z > 0.0)
    assert np.all(x + z < 4.0)


def test_face_selection_splits_walls_from_roof() -> None:
    mesh = box((0.0, 0.0, 0.0), (3.0, 2.0, 4.0))
    parents = ["b"] * mesh.triangle_count

    walls = triangle_samples(mesh.triangles(), parents, spacing_m=0.5, faces=FaceSelection.VERTICAL)
    up = triangle_samples(mesh.triangles(), parents, spacing_m=0.5, faces=FaceSelection.UPWARD)

    assert walls.total_area_m2 == pytest.approx(2 * (3 * 4) + 2 * (2 * 4))  # 40
    assert up.total_area_m2 == pytest.approx(3 * 2)  # the top only, not the base
    assert np.allclose(np.abs(walls.normals[:, 2]), 0.0, atol=1e-9)
    assert np.allclose(up.normals[:, 2], 1.0, atol=1e-9)


def test_surface_offset_pushes_along_each_face_normal() -> None:
    mesh = box((0.0, 0.0, 0.0), (2.0, 2.0, 2.0))
    points = triangle_samples(
        mesh.triangles(), ["b"] * mesh.triangle_count, spacing_m=1.0, surface_offset_m=0.1
    )
    # Every sample is outside the original box.
    inside = (
        (points.positions[:, 0] > 1e-9)
        & (points.positions[:, 0] < 2.0 - 1e-9)
        & (points.positions[:, 1] > 1e-9)
        & (points.positions[:, 1] < 2.0 - 1e-9)
        & (points.positions[:, 2] > 1e-9)
        & (points.positions[:, 2] < 2.0 - 1e-9)
    )
    assert not inside.any()


def test_single_sample_defaults_to_unit_area() -> None:
    assert single_sample((0, 0, 0), (0, 0, 1), "p").total_area_m2 == pytest.approx(1.0)
    assert single_sample((0, 0, 0), (0, 0, 1), "p", 7.5).total_area_m2 == pytest.approx(7.5)


def test_horizontal_grid_takes_the_minimum_corner_not_the_centre() -> None:
    """Pinned because it was once read as the centre.

    That put a ground grid half a site away from the building it was meant to
    cover, and the only visible symptom was a footprint mask that removed far
    too little area.
    """
    points = horizontal_grid((10.0, 20.0, 0.0), 4.0, 6.0, "g", spacing_m=1.0)
    assert points.positions[:, 0].min() == pytest.approx(10.5)
    assert points.positions[:, 0].max() == pytest.approx(13.5)
    assert points.positions[:, 1].min() == pytest.approx(20.5)
    assert points.positions[:, 1].max() == pytest.approx(25.5)
    assert points.total_area_m2 == pytest.approx(24.0)


def test_concatenate_preserves_areas() -> None:
    merged = SamplePoints.concatenate(
        [
            single_sample((0, 0, 0), (0, 0, 1), "a", 2.0),
            single_sample((1, 0, 0), (0, 0, 1), "b", 3.0),
        ]
    )
    assert merged.total_area_m2 == pytest.approx(5.0)


def test_mismatched_areas_are_rejected() -> None:
    with pytest.raises(ValueError, match="areas"):
        SamplePoints(np.zeros((3, 3)), np.zeros((3, 3)), ("a", "b", "c"), np.ones(2))


# ---------------------------------------------------------------------------
# Banding -- matching the published band scheme.
# ---------------------------------------------------------------------------
def test_bands_match_the_office_scheme() -> None:
    """Seven bands with 0hr held separate, exactly as the decks report them."""
    points, minutes = flat([0.0, 30.0, 90.0, 150.0, 210.0, 270.0, 330.0])
    result = band_by_area(points, minutes)

    assert [band.label for band in result.bands] == [
        "0hr",
        "0-1hr",
        "1-2hrs",
        "2-3hrs",
        "3-4hrs",
        "4-5hrs",
        ">5hrs",
    ]
    assert all(band.area_m2 == pytest.approx(1.0) for band in result.bands)
    assert result.total_area_m2 == pytest.approx(7.0)


def test_rollups_partition_the_total() -> None:
    """>threshold, some-but-under, and exactly zero must sum to everything."""
    points, minutes = flat([0.0, 0.0, 45.0, 119.0, 120.0, 200.0, 359.0])
    result = band_by_area(points, minutes)

    assert result.zero_m2 == pytest.approx(2.0)
    assert result.below_threshold_m2 == pytest.approx(2.0)  # 45 and 119
    assert result.at_or_above_threshold_m2 == pytest.approx(3.0)  # 120, 200, 359
    assert (
        result.zero_m2 + result.below_threshold_m2 + result.at_or_above_threshold_m2
        == pytest.approx(result.total_area_m2)
    )


def test_zero_is_held_apart_from_nearly_zero() -> None:
    """A surface receiving nothing is a different finding from one receiving
    ten minutes, and ADG criterion 3 counts only the former."""
    points, minutes = flat([0.0, 10.0])
    result = band_by_area(points, minutes)
    assert result.zero_m2 == pytest.approx(1.0)
    assert result.below_threshold_m2 == pytest.approx(1.0)


def test_exactly_the_threshold_counts_as_meeting_it() -> None:
    """ "A minimum of 2 hours" means two hours passes."""
    points, minutes = flat([120.0])
    assert band_by_area(points, minutes).at_or_above_threshold_m2 == pytest.approx(1.0)


def test_bands_are_area_weighted_not_sample_counted() -> None:
    """The distinction that makes areas worth carrying at all.

    Three samples, one of which stands for 97 m2. By count the split is 1/3
    each; by area it is 97% in one band.
    """
    points = SamplePoints(
        np.zeros((3, 3)),
        np.tile([0.0, 0.0, 1.0], (3, 1)),
        ("a", "b", "c"),
        np.array([97.0, 2.0, 1.0]),
    )
    result = band_by_area(points, np.array([300.0, 0.0, 0.0]))

    assert result.at_or_above_threshold_share == pytest.approx(0.97)
    assert result.zero_share == pytest.approx(0.03)


def test_band_shares_sum_to_one() -> None:
    rng = np.random.default_rng(4)
    count = 500
    points = SamplePoints(
        np.zeros((count, 3)),
        np.tile([0.0, 0.0, 1.0], (count, 1)),
        tuple(str(i) for i in range(count)),
        rng.uniform(0.1, 5.0, count),
    )
    result = band_by_area(points, rng.uniform(0.0, 360.0, count))
    assert sum(band.share for band in result.bands) == pytest.approx(1.0)
    assert sum(band.area_m2 for band in result.bands) == pytest.approx(result.total_area_m2)


def test_a_custom_threshold_moves_the_rollup() -> None:
    """The 3 hour criterion outside Sydney Metro uses the same machinery."""
    points, minutes = flat([150.0, 200.0])
    assert band_by_area(points, minutes, threshold_minutes=120.0).at_or_above_threshold_m2 == 2.0
    assert band_by_area(points, minutes, threshold_minutes=180.0).at_or_above_threshold_m2 == 1.0


def test_empty_input_does_not_divide_by_zero() -> None:
    result = band_by_area(SamplePoints.empty(), np.zeros(0))
    assert result.total_area_m2 == 0.0
    assert result.at_or_above_threshold_share == 0.0


def test_mismatched_lengths_are_rejected() -> None:
    points, _ = flat([0.0, 1.0])
    with pytest.raises(ValueError, match="durations"):
        band_by_area(points, np.zeros(5))


def test_unsorted_edges_are_rejected() -> None:
    points, minutes = flat([100.0])
    with pytest.raises(ValueError, match="increasing"):
        band_by_area(points, minutes, edges_minutes=(120.0, 60.0))


def test_default_edges_are_hourly() -> None:
    assert DEFAULT_BAND_EDGES_MINUTES == (60.0, 120.0, 180.0, 240.0, 300.0)


# ---------------------------------------------------------------------------
# Massing scene and the end-to-end run.
# ---------------------------------------------------------------------------
def test_massing_needs_no_zones_or_windows() -> None:
    """The whole point: this must work on a bare mass."""
    model = read_ifc(SAMPLE)
    scene = build_massing_scene(model, MassingConfig(timezone="Australia/Sydney"))
    assert len(scene.facade_samples) > 0
    assert scene.facade_samples.total_area_m2 > 0.0
    assert scene.provenance["mode"] == "massing"


def test_facade_area_matches_hand_calculation() -> None:
    """The fixture's upright surfaces, counted by hand.

    Walls 4 x (2*20*3 + 2*0.2*3) = 484.8, windows 4 x (2*2.4*1.8 + 2*0.2*1.8)
    = 37.44, balcony edges 4 x (2*3*0.25 + 2*2*0.25) = 10, slab edges
    2 x (2*20*0.25 + 2*12*0.25) = 32. That is 564.24 before the openings.

    Each of the 4 window openings then cuts a 2.4 x 1.8 hole through a 0.2
    wall: it removes the hole from both wall faces (-2 x 2.4 x 1.8 = -8.64)
    and adds two upright jambs (+2 x 1.8 x 0.2 = +0.72). The head and sill are
    horizontal, so they are not upright faces and do not count. Net -7.92 per
    window, so 564.24 - 4 x 7.92 = 532.56 m2.
    """
    model = read_ifc(SAMPLE)
    scene = build_massing_scene(model, MassingConfig(timezone="Australia/Sydney"))
    assert scene.facade_samples.total_area_m2 == pytest.approx(532.56, abs=0.01)


def test_context_is_an_occluder_but_not_in_the_denominator() -> None:
    """Otherwise the percentage describes the neighbourhood, not the scheme."""
    model = read_ifc(SAMPLE)
    scene = build_massing_scene(model, MassingConfig(timezone="Australia/Sydney"))

    assert scene.provenance["context_elements"] == 1
    context = next(e for e in model.elements if e.name.startswith("Context"))
    assert context.global_id not in set(scene.facade_samples.parent_ids)
    # ...but its triangles are still in the occluder set.
    assert scene.occluders.triangle_count == sum(e.mesh.triangle_count for e in model.occluders())


def test_ground_grid_excludes_building_footprints() -> None:
    """The masked area is the slab footprint plus the balconies overhanging it."""
    model = read_ifc(SAMPLE)
    scene = build_massing_scene(
        model, MassingConfig(timezone="Australia/Sydney", ground_spacing_m=1.0)
    )
    ground = scene.ground_samples

    # Grid extent: subject bbox (20 x 14.1) plus a 10 m margin all round.
    full = (20.0 + 20.0) * (14.1 + 20.0)
    masked = full - ground.total_area_m2
    assert 200.0 < masked < 320.0, (
        f"masked {masked:.1f} m2; expected roughly the 240 m2 slab plus 24 m2 of balcony"
    )
    assert np.allclose(ground.normals[:, 2], 1.0)


def test_ground_samples_are_not_inside_the_building() -> None:
    model = read_ifc(SAMPLE)
    scene = build_massing_scene(
        model, MassingConfig(timezone="Australia/Sydney", ground_spacing_m=1.0)
    )
    inside_slab = (np.abs(scene.ground_samples.positions[:, 0]) < 9.5) & (
        np.abs(scene.ground_samples.positions[:, 1]) < 5.5
    )
    assert not inside_slab.any(), "samples remain under the building footprint"


def test_massing_run_reports_bands_for_both_surfaces() -> None:
    result = run_massing(
        SAMPLE,
        timezone="Australia/Sydney",
        massing_config=MassingConfig(timezone="Australia/Sydney", facade_spacing_m=1.0),
    )
    assert result.threshold_minutes == 120.0
    assert result.sun_position_count == 37
    for banded in (result.facade, result.ground):
        assert len(banded.bands) == 7
        assert sum(b.share for b in banded.bands) == pytest.approx(1.0)


def test_the_south_facade_receives_nothing() -> None:
    """The southern-hemisphere tripwire, in area terms.

    Roughly half the fixture's facade faces away from the midwinter sun, so a
    large zero band is expected. A small one would mean the north handling has
    gone wrong somewhere in the massing path specifically.
    """
    result = run_massing(
        SAMPLE,
        timezone="Australia/Sydney",
        massing_config=MassingConfig(timezone="Australia/Sydney", facade_spacing_m=1.0),
    )
    assert result.facade.zero_share > 0.4, (
        f"only {result.facade.zero_share:.1%} of the facade is in permanent shade; "
        f"a Sydney box should have roughly half its envelope facing away from "
        f"the midwinter sun"
    )


def test_grid_spacing_barely_moves_the_headline_share() -> None:
    """A coarse grid is the point of massing mode, so it has to be trustworthy.

    If 1 m and 0.25 m disagreed materially, the fast setting used for
    optimisation would be reporting a different building from the fine one.
    """
    shares = []
    for spacing in (1.0, 0.25):
        result = run_massing(
            SAMPLE,
            timezone="Australia/Sydney",
            massing_config=MassingConfig(
                timezone="Australia/Sydney", facade_spacing_m=spacing, ground_spacing_m=2.0
            ),
        )
        shares.append(result.facade.at_or_above_threshold_share)

    assert abs(shares[0] - shares[1]) < 0.03, (
        f"coarse {shares[0]:.3%} vs fine {shares[1]:.3%}: the fast setting does not "
        f"agree with the accurate one"
    )


def test_massing_rejects_a_mismatched_timezone() -> None:
    with pytest.raises(ValueError, match="two different zones"):
        run_massing(
            SAMPLE,
            timezone="Australia/Sydney",
            massing_config=MassingConfig(timezone="Europe/London"),
        )


def test_empty_geometry_produces_no_samples() -> None:
    points = triangle_samples(np.zeros((0, 3, 3)), [], spacing_m=1.0)
    assert len(points) == 0
    assert points.total_area_m2 == 0.0


def test_degenerate_triangles_are_skipped() -> None:
    """Zero-area triangles appear in real exports and must not create samples."""
    collinear = np.array([[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]])
    assert len(triangle_samples(collinear, ["t"], spacing_m=0.5)) == 0


def test_triangle_samples_rejects_a_parent_id_mismatch() -> None:
    mesh = TriangleMesh.concatenate([box((0, 0, 0), (1, 1, 1))])
    with pytest.raises(ValueError, match="parent ids"):
        triangle_samples(mesh.triangles(), ["only-one"], spacing_m=1.0)


def test_the_drawing_and_the_measurement_see_the_same_elements() -> None:
    """The height cut has to apply to both, or the picture is of something else.

    ``massing_subject`` is what the facade drawing grids and what
    ``build_massing_scene`` measures. When the cut lived in the caller instead
    of here, the drawing path forgot it and coloured three sets of parked
    hotlink masters floating above the site -- fully sunlit at every hour, so
    the diagram gained facade area and the top band quadrupled.
    """
    from sun_study.ingest.scene import massing_subject

    model = read_ifc(SAMPLE)
    tall = max(
        float(element.mesh.vertices[:, 2].max())
        for element in model.occluders()
        if len(element.mesh.vertices)
    )
    config = MassingConfig(timezone="Australia/Sydney", exclude_above_m=tall / 2.0)

    reduced = massing_subject(model, config)
    scene = build_massing_scene(model, config)

    assert reduced.elements_above_cut == scene.provenance["elements_above_cut"]
    assert len(reduced.solids) == len(reduced.subject) + len(reduced.context)
    # Nothing that survived the cut lies wholly above it.
    cut = config.exclude_above_m
    assert cut is not None, "this test is about the cut, so it has to have one"
    for element in reduced.solids:
        assert float(element.mesh.vertices[:, 2].min()) <= cut


def test_the_reduction_does_not_cut_when_no_height_is_given() -> None:
    from sun_study.ingest.scene import massing_subject

    model = read_ifc(SAMPLE)
    reduced = massing_subject(model, MassingConfig(timezone="Australia/Sydney"))

    assert reduced.elements_above_cut == 0
    assert len(reduced.solids) == len(model.occluders())


# ---------------------------------------------------------------------------
# A Zone measured on its own: a playground, a courtyard, a communal terrace.
#
# The case these exist for has no apartment anywhere in it. Every other route
# to a number in this tool goes through a dwelling -- glazing marked as a
# living room, or open space owned by a flat -- and an outdoor area that
# belongs to nobody could not be measured at all.
# ---------------------------------------------------------------------------
SYDNEY = "Australia/Sydney"


def test_no_zone_surface_unless_one_is_asked_for() -> None:
    """A developed model has a Zone per room; gridding all of them by default
    would answer a question nobody asked, and quadruple an ordinary run."""
    model = read_ifc(SAMPLE)
    scene = build_massing_scene(model, MassingConfig(timezone=SYDNEY))
    assert len(scene.zone_samples) == 0
    assert scene.provenance["zones_measured"] == ()


def test_a_named_zone_is_gridded_on_its_floor() -> None:
    """8.0 x 11.6 m of floor, at the assessment height above it."""
    model = read_ifc(SAMPLE)
    scene = build_massing_scene(
        model,
        MassingConfig(
            timezone=SYDNEY,
            zone_names=("Apartment L00-A",),
            ground_spacing_m=0.5,
            zone_height_m=1.0,
        ),
    )
    assert scene.zone_samples.total_area_m2 == pytest.approx(8.0 * 11.6, abs=0.01)
    # Floor sits at z 0.25, so a 1 m assessment plane is z 1.25.
    assert np.allclose(scene.zone_samples.positions[:, 2], 1.25)
    # Open space faces the sky, whichever way the face it came from pointed.
    assert np.allclose(scene.zone_samples.normals[:, 2], 1.0)
    assert scene.provenance["zones_measured"] == ("Apartment L00-A",)


def test_the_zone_surface_needs_no_window_and_no_owner() -> None:
    """The whole point. Nothing here marks glazing or names an apartment."""
    result = run_massing(
        SAMPLE,
        timezone=SYDNEY,
        massing_config=MassingConfig(
            timezone=SYDNEY,
            zone_names=("Apartment L00-A",),
            facade_spacing_m=2.0,
            ground_spacing_m=1.0,
        ),
    )
    assert result.zone is not None
    assert sum(band.share for band in result.zone.bands) == pytest.approx(1.0)
    assert "named zones" in result.summary()


def test_a_zone_filter_that_matches_nothing_stops_the_run() -> None:
    """Matching nothing must not read as an area that gets no sun."""
    model = read_ifc(SAMPLE)
    with pytest.raises(SceneConfigError, match="No Zone matches"):
        build_massing_scene(model, MassingConfig(timezone=SYDNEY, zone_names=("Playground",)))


def test_naming_no_zone_leaves_the_result_without_one() -> None:
    result = run_massing(
        SAMPLE,
        timezone=SYDNEY,
        massing_config=MassingConfig(timezone=SYDNEY, facade_spacing_m=2.0),
    )
    assert result.zone is None
    assert "named zones" not in result.summary()


def test_a_zone_off_the_project_axes_is_not_gridded_as_its_bounding_box() -> None:
    """The failure this caught on a real project.

    A horizontal face's in-plane basis is axis-aligned, so the bounding
    rectangle of a rotated Zone is its axis-aligned bounding box. A 1,863.6 m2
    communal zone turned 79 degrees off the project axes came back as
    2,809.2 m2 -- half as big again as itself, with a share to match.
    """
    model = read_ifc(SAMPLE)
    space = next(s for s in model.of_class("IfcSpace") if s.name == "Apartment L00-A")
    turned = _rotated_about_z(space, math.radians(30.0))

    grid = zone_floor_grid(turned, "z", spacing_m=0.25, height_m=1.0)
    assert grid is not None
    box = (turned.bounds[1][0] - turned.bounds[0][0]) * (turned.bounds[1][1] - turned.bounds[0][1])
    assert box > 8.0 * 11.6 * 1.2, "the fixture rotation has to make the box bigger"
    assert grid.total_area_m2 == pytest.approx(8.0 * 11.6, rel=0.02)
    assert grid.total_area_m2 < box * 0.9


def _rotated_about_z(space: IfcElement, angle_rad: float) -> IfcElement:
    """The same Zone, turned off the project axes."""
    c, s_ = math.cos(angle_rad), math.sin(angle_rad)
    turn = np.array([[c, -s_, 0.0], [s_, c, 0.0], [0.0, 0.0, 1.0]])
    spun = TriangleMesh(space.mesh.vertices @ turn.T, space.mesh.faces)
    return replace(space, mesh=spun)


def _zone_run(start: str = "09:00") -> Any:
    rules = load_ruleset("nsw_adg")
    window = rules.assessment.model_copy(update={"window_start": start})
    return run_massing(
        SAMPLE,
        timezone=SYDNEY,
        ruleset=rules.model_copy(update={"assessment": window}),
        massing_config=MassingConfig(
            timezone=SYDNEY,
            zone_names=("Apartment L00-A",),
            facade_spacing_m=3.0,
            ground_spacing_m=2.0,
        ),
    )


def test_the_window_decides_which_hours_get_a_plan() -> None:
    """Eight from eight o'clock, seven from nine. No hour is invented."""
    from sun_study.cli import _hourly

    assert [f"{when:%H:%M}" for when, _ in _hourly(_zone_run("08:00"))] == [
        "08:00",
        "09:00",
        "10:00",
        "11:00",
        "12:00",
        "13:00",
        "14:00",
        "15:00",
    ]
    assert len(_hourly(_zone_run("09:00"))) == 7


def test_an_hourly_plan_is_the_same_data_as_the_banded_one() -> None:
    """The hourly plans come off the instants the study already cast.

    If they were cast again they could differ from the bands drawn beside
    them, and a reader comparing the two would be comparing two studies.
    """
    result = _zone_run("08:00")
    assert result.zone_sunlit is not None and result.zone_minutes is not None

    weights = instant_weights(
        result.zone_sunlit.shape[1],
        float(result.ruleset.assessment.timestep_minutes),
        WEIGHTING_BY_RULESET[result.ruleset.assessment.weighting],
    )
    assert np.allclose(cumulative_minutes(result.zone_sunlit, weights), result.zone_minutes)

    # And a sample lit at no instant is a sample with no minutes.
    assert np.allclose(result.zone_minutes[~result.zone_sunlit.any(axis=1)], 0.0)


# ---------------------------------------------------------------------------
# A Zone whose floor is not flat.
#
# Cutting a Zone against a terrain mesh gives it an underside of hundreds of
# triangles over several metres of fall. The planar sampler takes the biggest
# coplanar group and calls it the floor, which on the reference project left
# a fifth of the area unmeasured and said nothing.
# ---------------------------------------------------------------------------
def _tilted_slab(fall_m: float, size_m: float = 10.0) -> IfcElement:
    """A box whose underside runs downhill from one edge to the other."""
    x = np.array([0.0, size_m])
    y = np.array([0.0, size_m])
    top = 10.0
    # Four corners of the underside, two of them dropped by ``fall_m``.
    under = [
        (x[0], y[0], 0.0),
        (x[1], y[0], -fall_m),
        (x[1], y[1], -fall_m),
        (x[0], y[1], 0.0),
    ]
    over = [(px, py, top) for px, py, _ in under]
    vertices = np.array(under + over, dtype=np.float64)
    faces = np.array(
        [
            # Underside, wound so its normal points down.
            [0, 2, 1],
            [0, 3, 2],
            # Top, and the four sides, so the solid is closed.
            [4, 5, 6],
            [4, 6, 7],
            [0, 1, 5],
            [0, 5, 4],
            [1, 2, 6],
            [1, 6, 5],
            [2, 3, 7],
            [2, 7, 6],
            [3, 0, 4],
            [3, 4, 7],
        ],
        dtype=np.int32,
    )
    return IfcElement(
        global_id="tilted",
        ifc_class="IfcSpace",
        name="Playground",
        long_name="",
        predefined_type="",
        storey=None,
        mesh=TriangleMesh(vertices, faces),
    )


def test_a_sloping_floor_is_measured_over_its_whole_plan_area() -> None:
    """The planar sampler measures a slice; the draped one measures the lot."""
    zone = _tilted_slab(fall_m=3.0)
    draped = zone_surface_grid(zone, "z", spacing_m=0.5, height_m=1.0)
    assert draped is not None
    assert draped.total_area_m2 == pytest.approx(100.0, rel=0.05)


def test_a_sloping_floor_carries_the_slope_into_the_sample_heights() -> None:
    """Every point one metre above the ground it actually sits on."""
    zone = _tilted_slab(fall_m=3.0)
    draped = zone_surface_grid(zone, "z", spacing_m=0.5, height_m=1.0)
    assert draped is not None

    # The underside falls from z 0 at x=0 to z -3 at x=10, so a sample at x
    # should sit at 1 - 0.3x.
    expected = 1.0 - 0.3 * draped.positions[:, 0]
    assert np.allclose(draped.positions[:, 2], expected, atol=0.02)
    # Facing the sky, whatever the fall.
    assert np.allclose(draped.normals, np.array([0.0, 0.0, 1.0]))


def test_an_area_on_a_slope_is_quoted_as_its_plan_area() -> None:
    """A playground on a ramp is not more playground for being tilted.

    It also has to match the drawing, which is a plan.
    """
    flat = zone_surface_grid(_tilted_slab(fall_m=0.0), "z", spacing_m=0.5, height_m=1.0)
    steep = zone_surface_grid(_tilted_slab(fall_m=8.0), "z", spacing_m=0.5, height_m=1.0)
    assert flat is not None and steep is not None
    assert steep.total_area_m2 == pytest.approx(flat.total_area_m2, rel=0.02)


def test_a_flat_zone_reads_the_same_either_way() -> None:
    """The change must not move a number on a level site."""
    model = read_ifc(SAMPLE)
    space = next(s for s in model.of_class("IfcSpace") if s.name == "Apartment L00-A")
    planar = zone_floor_grid(space, "z", spacing_m=0.5, height_m=1.0)
    draped = zone_surface_grid(space, "z", spacing_m=0.5, height_m=1.0)
    assert planar is not None and draped is not None
    assert draped.total_area_m2 == pytest.approx(planar.total_area_m2, rel=0.02)
    assert np.allclose(draped.positions[:, 2], planar.positions[0, 2], atol=0.01)
