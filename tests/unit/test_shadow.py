"""Shadows on a plane, and the attribution the drawing exists to make.

The arithmetic is checked against geometry whose answer can be worked out on
paper. With the sun due west at 45 degrees, a building's shadow runs due east
and is exactly as long as the building is tall, so a 10 m cube covers its own
10 m footprint plus 10 m of ground: 200 m2 at 10 m wide, and no library needed
to know it.

That is worth testing at this length because the failure mode is not a crash.
A shadow attributed to the wrong cause is a drawing that reads perfectly, goes
to a consent authority, and is wrong about the only thing anybody asked.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from sun_study.core.geometry import TriangleMesh, box, prism, triangulate
from sun_study.core.shadow import (
    ADDITIONAL,
    BASELINE,
    ENVELOPE,
    EXISTING,
    SCENARIO,
    ShadowSource,
    cast_shadows,
    default_sources,
    ground_plane_grid,
)

#: Due west, 45 degrees up. Shadows run due east, one metre per metre of
#: height, which is what makes every figure below arithmetic rather than a
#: regression baseline.
WEST_45 = np.array([[-1.0, 0.0, 1.0]]) / np.sqrt(2.0)

NOON = dt.datetime(2024, 6, 21, 12, 0, tzinfo=dt.UTC)


def plane(spacing: float = 1.0) -> object:
    """A grid wide enough that nothing in these tests reaches its edge."""
    return ground_plane_grid(
        (np.array([-40.0, -5.0, 0.0]), np.array([10.0, 5.0, 20.0])),
        datum_m=0.0,
        spacing_m=spacing,
        margin_m=60.0,
    )


def cast(context: TriangleMesh, proposal: TriangleMesh, envelope: TriangleMesh | None = None):  # type: ignore[no-untyped-def]
    return cast_shadows(
        plane(),
        sources=default_sources(context, proposal, envelope),
        sun_vectors=WEST_45,
        moments=[NOON],
        labels=["12PM"],
        spacing_m=1.0,
    ).instants[0]


# -- the split ------------------------------------------------------------


def test_the_proposal_is_charged_only_for_what_it_darkened() -> None:
    """The whole argument of the drawing. A 20 m proposal east of a 10 m
    neighbour covers its own footprint and 20 m of ground -- 300 m2 -- and the
    neighbour's 200 m2 stays the neighbour's."""
    neighbour = box((-40.0, -5.0, 0.0), (-30.0, 5.0, 10.0))
    proposal = box((0.0, -5.0, 0.0), (10.0, 5.0, 20.0))

    instant = cast(neighbour, proposal)

    assert instant.areas_m2[EXISTING] == pytest.approx(200.0)
    assert instant.areas_m2[ADDITIONAL] == pytest.approx(300.0)


def test_a_proposal_standing_in_an_existing_shadow_adds_nothing() -> None:
    """The case the subtraction is for. Ground already dark at this hour is
    not darkened again, and a scheme tucked inside a taller neighbour's shadow
    must be charged nothing at all -- not charged twice, and not credited."""
    tower = box((-40.0, -5.0, 0.0), (-30.0, 5.0, 60.0))
    tucked = box((-20.0, -5.0, 0.0), (-15.0, 5.0, 4.0))

    instant = cast(tower, tucked)

    assert instant.areas_m2[ADDITIONAL] == pytest.approx(0.0)
    assert instant.regions[ADDITIONAL] == ()


def test_the_two_fills_abut_and_never_overlap() -> None:
    """Both are differenced against the same 'before', so the areas add. Drawn
    from masks that overlapped, the blue would sit on top of the grey and the
    sheet would quietly claim the proposal darkened ground it did not."""
    neighbour = box((-25.0, -5.0, 0.0), (-15.0, 5.0, 10.0))
    proposal = box((0.0, -5.0, 0.0), (10.0, 5.0, 20.0))

    instant = cast(neighbour, proposal)
    both = cast(TriangleMesh.empty(), TriangleMesh.concatenate([neighbour, proposal]))

    assert instant.areas_m2[EXISTING] + instant.areas_m2[ADDITIONAL] == pytest.approx(
        both.areas_m2[ADDITIONAL]
    )


def test_the_envelope_is_measured_against_the_same_ground_as_the_proposal() -> None:
    """Pink and blue answer the same question about the same land, so they can
    be read against each other: this is what the controls would have allowed,
    that is what is asked for."""
    neighbour = box((-40.0, -5.0, 0.0), (-30.0, 5.0, 10.0))
    proposal = box((0.0, -5.0, 0.0), (10.0, 5.0, 20.0))
    allowed = prism([(0, -5), (10, -5), (10, 5), (0, 5)], 0.0, 9.5)

    instant = cast(neighbour, proposal, allowed)

    # 10 m of footprint plus 9.5 m of shadow, at 10 m wide, on a 1 m grid.
    assert instant.areas_m2[ENVELOPE] == pytest.approx(190.0)
    assert instant.areas_m2[ENVELOPE] < instant.areas_m2[ADDITIONAL], "the scheme exceeds it"


def test_no_envelope_draws_no_third_fill() -> None:
    """A project with no height control named should get grey and blue and no
    pink at all -- not a pink of zero area, which would still put a layer in
    the project, a row in the legend and a column in the schedule for a
    category the project does not have."""
    instant = cast(
        box((-40.0, -5.0, 0.0), (-30.0, 5.0, 10.0)), box((0.0, -5.0, 0.0), (10.0, 5.0, 20.0))
    )

    assert ENVELOPE not in instant.regions
    assert ENVELOPE not in instant.areas_m2


# -- the sun being down ---------------------------------------------------


def test_an_hour_before_sunrise_draws_nothing_rather_than_everything() -> None:
    """With the sun down every ray is refused and every sample reads shaded.
    Drawn, that is a sheet showing the whole suburb black at an hour nobody
    asked about -- so it is flagged and drawn empty instead."""
    series = cast_shadows(
        plane(),
        sources=default_sources(
            box((-40.0, -5.0, 0.0), (-30.0, 5.0, 10.0)),
            box((0.0, -5.0, 0.0), (10.0, 5.0, 20.0)),
        ),
        sun_vectors=np.array([[-1.0, 0.0, -0.5]]),
        moments=[NOON],
        labels=["6PM"],
        spacing_m=1.0,
    )
    instant = series.instants[0]

    assert instant.below_horizon is True
    assert all(area == pytest.approx(0.0) for area in instant.areas_m2.values())
    assert all(regions == () for regions in instant.regions.values())


# -- what the caller is told ----------------------------------------------


def test_terrain_handed_in_as_context_shows_up_as_a_number() -> None:
    """A site mesh among the occluders puts every sample below the hill in
    permanent shade and fills the sheet solid grey. It cannot be detected from
    the drawing, so it is reported as a share of the grid instead."""
    lid = box((-200.0, -200.0, 5.0), (200.0, 200.0, 6.0))

    series = cast_shadows(
        plane(),
        sources=default_sources(lid, box((0.0, -5.0, 0.0), (10.0, 5.0, 20.0))),
        sun_vectors=WEST_45,
        moments=[NOON],
        labels=["12PM"],
        spacing_m=1.0,
    )

    assert series.permanently_dark_share == pytest.approx(1.0)
    assert "100% always dark" in series.describe()


def test_the_caption_is_the_words_the_reference_sheets_use() -> None:
    instant = cast(
        box((-40.0, -5.0, 0.0), (-30.0, 5.0, 10.0)), box((0.0, -5.0, 0.0), (10.0, 5.0, 20.0))
    )

    assert instant.caption == "JUNE 21 -12PM"


def test_a_label_for_every_moment_or_none_at_all() -> None:
    with pytest.raises(ValueError, match="2 moments but 1 labels"):
        cast_shadows(
            plane(),
            sources=default_sources(TriangleMesh.empty(), box((0.0, -5.0, 0.0), (10.0, 5.0, 20.0))),
            sun_vectors=np.vstack([WEST_45, WEST_45]),
            moments=[NOON, NOON],
            labels=["12PM"],
            spacing_m=1.0,
        )


# -- the envelope solid ----------------------------------------------------


def test_a_site_boundary_is_triangulated_whatever_shape_the_land_is() -> None:
    """A battleaxe block, a splayed corner and a right-of-way cut out of one
    side are ordinary. A triangle fan from vertex zero is wrong on all three,
    so the cap is ear-clipped and its area has to match the polygon's."""
    battleaxe = [(0, 0), (20, 0), (20, 6), (8, 6), (8, 20), (0, 20)]
    ring = np.asarray(battleaxe, dtype=np.float64)

    faces = triangulate(battleaxe)

    def area(triangle: np.ndarray) -> float:
        a, b, c = triangle
        return abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])) / 2.0

    polygon = (
        abs(
            np.dot(ring[:, 0], np.roll(ring[:, 1], -1))
            - np.dot(ring[:, 1], np.roll(ring[:, 0], -1))
        )
        / 2.0
    )
    assert sum(area(ring[list(face)]) for face in faces) == pytest.approx(polygon)


def test_a_clockwise_boundary_triangulates_the_same_as_a_counter_clockwise_one() -> None:
    """Survey data arrives wound either way and nobody checks which."""
    assert len(triangulate([(0, 0), (10, 0), (10, 10), (0, 10)])) == len(
        triangulate([(0, 0), (0, 10), (10, 10), (10, 0)])
    )


def test_a_repeated_first_point_is_dropped_rather_than_refused() -> None:
    """Half the world writes a closed ring with its first point repeated."""
    closed = prism([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)], 0.0, 9.5)
    open_ring = prism([(0, 0), (10, 0), (10, 10), (0, 10)], 0.0, 9.5)

    assert closed.triangle_count == open_ring.triangle_count


def test_an_envelope_is_closed_on_top() -> None:
    """A ray from a sample *inside* the footprint leaves through the cap. An
    open-topped prism would let that sample see the sun straight through the
    roof of the thing shading it."""
    from sun_study.core.occlusion import Occluder

    solid = prism([(0, 0), (10, 0), (10, 10), (0, 10)], 0.0, 9.5)
    straight_up = np.array([[0.0, 0.0, 1.0]])

    assert Occluder(solid).any_hit(np.array([[5.0, 5.0, 0.0]]), straight_up)[0]


def test_a_prism_needs_three_points_and_a_height() -> None:
    with pytest.raises(ValueError, match="at least 3 distinct points"):
        prism([(0, 0), (10, 0)], 0.0, 9.5)
    with pytest.raises(ValueError, match="must be above"):
        prism([(0, 0), (10, 0), (10, 10)], 9.5, 9.5)


# -- how the fills are drawn -----------------------------------------------


def test_a_fill_has_no_visible_contour_by_default() -> None:
    """``CreateHatches`` exposes a contour pen and no way to switch the contour
    off, so the nearest thing is drawing it in the fill's own pen. A black
    hairline round every cell of a patch turns a colour field into a grid of
    boxes, and at 1:200 the boxes are what a reader sees."""
    from sun_study.archicad.draw import BandStyle

    assert BandStyle("2-3 hrs", 180.0, fill_pen=93).outline_pen == 93


def test_a_contour_can_still_be_asked_for_explicitly() -> None:
    from sun_study.archicad.draw import BandStyle

    assert BandStyle("2-3 hrs", 180.0, fill_pen=93, contour_pen=1).outline_pen == 1


def test_tracing_beats_tiling_wherever_a_patch_has_a_diagonal_edge() -> None:
    """The merge that was actually available. A sun patch edge is a stepped
    diagonal, and tiling it into rectangles costs one fill per step."""
    from sun_study.core.patches import drawable_contours, merge_lit_cells

    side = 40
    columns, rows = np.meshgrid(np.arange(side), np.arange(side), indexing="xy")
    positions = np.column_stack([columns.ravel() * 0.5, rows.ravel() * 0.5, np.zeros(side * side)])
    diagonal = (columns.ravel() + rows.ravel()) % 7 < 3

    tiled = merge_lit_cells(positions, diagonal, 0.5)
    traced = drawable_contours(positions, diagonal, 0.5)

    assert len(traced) * 10 < len(tiled), "tracing must be an order of magnitude fewer"


# -- many sources ---------------------------------------------------------
#
# The six-row sheet. SSDA 401 attributes its shadow to existing neighbours,
# future neighbours, structures kept on the site, two height limits and the
# proposed envelope, and the arithmetic that separates them is the whole
# argument the sheet makes. Every figure below is worked out on paper from the
# same due-west 45-degree sun the rest of this file uses.


def cast_sources(*sources: ShadowSource):  # type: ignore[no-untyped-def]
    return cast_shadows(
        plane(),
        sources=list(sources),
        sun_vectors=WEST_45,
        moments=[NOON],
        labels=["12PM"],
        spacing_m=1.0,
    ).instants[0]


def baseline(key: str, x0: float, x1: float, height: float) -> ShadowSource:
    return ShadowSource(key, key.title(), box((x0, -5.0, 0.0), (x1, 5.0, height)), BASELINE)


def scenario(key: str, x0: float, x1: float, height: float) -> ShadowSource:
    return ShadowSource(key, key.title(), box((x0, -5.0, 0.0), (x1, 5.0, height)), SCENARIO)


def test_baselines_are_charged_only_for_ground_the_earlier_ones_left_lit() -> None:
    """Existing neighbours, then future neighbours. Each 10 m cube covers its
    own footprint plus 10 m of shadow -- 200 m2 -- but the second one is
    standing 5 m into the first one's shadow, so 150 m2 of its 200 was already
    dark and it is charged for the 50 m2 it actually darkened."""
    instant = cast_sources(
        baseline("existing", -40.0, -30.0, 10.0),
        baseline("future", -35.0, -25.0, 10.0),
    )

    assert instant.areas_m2["existing"] == pytest.approx(200.0)
    assert instant.areas_m2["future"] == pytest.approx(50.0)


def test_baseline_fills_tile_so_their_areas_add_to_the_whole_shadow() -> None:
    """The property that lets a reader add the grey rows up. Two baselines
    covering x -40 to -15 between them, and no square metre counted twice."""
    instant = cast_sources(
        baseline("existing", -40.0, -30.0, 10.0),
        baseline("future", -35.0, -25.0, 10.0),
    )

    total = instant.areas_m2["existing"] + instant.areas_m2["future"]
    assert total == pytest.approx(250.0)


def test_two_scenarios_overlap_because_they_are_rival_answers() -> None:
    """A height limit and the scheme are alternatives on the same land, and
    neither is subtracted from the other. Both 10 m wide: the 10 m massing
    covers 200 m2 and the 20 m one covers 300 m2, each measured whole."""
    instant = cast_sources(
        baseline("existing", -40.0, -30.0, 10.0),
        scenario("tod", 0.0, 10.0, 10.0),
        scenario("sears", 0.0, 10.0, 20.0),
    )

    assert instant.areas_m2["tod"] == pytest.approx(200.0)
    assert instant.areas_m2["sears"] == pytest.approx(300.0)


def test_a_scenario_is_charged_against_every_baseline_not_merely_the_first() -> None:
    """The bug this is here to catch. A scheme standing entirely inside the
    combined shadow of the existing *and* the future context adds nothing; a
    version that differenced against the first baseline alone would charge it
    100 m2 for ground the future context had already darkened."""
    instant = cast_sources(
        baseline("existing", -40.0, -30.0, 10.0),
        baseline("future", -20.0, -10.0, 10.0),
        scenario("proposed", -30.0, -20.0, 10.0),
    )

    assert instant.areas_m2["proposed"] == pytest.approx(0.0)
    assert instant.regions["proposed"] == ()


def test_the_six_row_sheet_comes_back_with_six_rows_in_the_order_given() -> None:
    """SSDA 401's legend, end to end. The order is the sheet's argument and it
    survives the round trip, because the layer, the Element ID and the legend
    row are all read off it."""
    series = cast_shadows(
        plane(),
        sources=[
            baseline("existing neighbours", -40.0, -35.0, 10.0),
            baseline("future context", -34.0, -30.0, 10.0),
            baseline("existing on site", -29.0, -25.0, 8.0),
            scenario("tod", 0.0, 10.0, 10.0),
            scenario("sears", 0.0, 10.0, 15.0),
            scenario("proposed envelope", 0.0, 10.0, 20.0),
        ],
        sun_vectors=WEST_45,
        moments=[NOON],
        labels=["12PM"],
        spacing_m=1.0,
    )

    assert [source.key for source in series.sources] == [
        "existing neighbours",
        "future context",
        "existing on site",
        "tod",
        "sears",
        "proposed envelope",
    ]
    assert [source.role for source in series.sources] == [BASELINE] * 3 + [SCENARIO] * 3
    assert set(series.instants[0].areas_m2) == {source.key for source in series.sources}


def test_two_sources_may_not_share_a_key() -> None:
    """The key is the layer fragment and the Element ID segment. Two rows
    sharing one would draw the second over the first and total them together
    in the schedule, silently."""
    with pytest.raises(ValueError, match="share a key"):
        cast_sources(baseline("tod", -40.0, -30.0, 10.0), scenario("tod", 0.0, 10.0, 10.0))


def test_a_source_with_no_geometry_is_not_a_reason_to_fail() -> None:
    """'This project has no future context' is an ordinary answer. The row
    stays in the legend, its fill is empty, and nothing is cast for it."""
    instant = cast_sources(
        baseline("existing", -40.0, -30.0, 10.0),
        ShadowSource("future", "Future", TriangleMesh.empty(), BASELINE),
        scenario("proposed", 0.0, 10.0, 20.0),
    )

    assert instant.areas_m2["future"] == pytest.approx(0.0)
    assert instant.regions["future"] == ()
    assert instant.areas_m2["proposed"] == pytest.approx(300.0)


def test_an_unknown_role_is_refused_rather_than_treated_as_a_baseline() -> None:
    """Silently defaulting would put a SEARs massing in the baseline and
    charge the proposal only for what it added on top of it."""
    with pytest.raises(ValueError, match="Unknown source role"):
        cast_sources(ShadowSource("odd", "Odd", box((0, -5, 0), (10, 5, 10)), "comparator"))
