"""What the controls would let a neighbour build, worked out on paper first.

Every case is a rectangle whose setbacks and areas can be checked by hand,
because the point of the module is that a designer can trust the number
written on the sheet: six metres in at four storeys, nine above that, and
storeys off the top when the floor-space ratio says so.
"""

from __future__ import annotations

import pytest

from sun_study.site.envelope import (
    ADG_SEPARATION,
    DEFAULT_ZONES,
    Lot,
    envelope_of,
    envelopes,
    inset,
    street_edges,
)
from sun_study.site.geo import ring_area

#: A 30 m by 40 m lot with the street along its south edge, and its three
#: neighbours: one either side and one behind.
LOT = ((0.0, 0.0), (30.0, 0.0), (30.0, 40.0), (0.0, 40.0))
WEST = ((-30.0, 0.0), (0.0, 0.0), (0.0, 40.0), (-30.0, 40.0))
EAST = ((30.0, 0.0), (60.0, 0.0), (60.0, 40.0), (30.0, 40.0))
BEHIND = ((0.0, 40.0), (30.0, 40.0), (30.0, 80.0), (0.0, 80.0))
NEIGHBOURS = (WEST, EAST, BEHIND)


def test_the_separation_table_is_the_adgs() -> None:
    """Part 3F: 12, 18 and 24 m between habitable rooms, half of that to a
    boundary, by the three height bands."""
    assert [(b.top_m, b.storeys_to, b.between_m, b.boundary_m) for b in ADG_SEPARATION] == [
        (12.0, 4, 12.0, 6.0),
        (25.0, 8, 18.0, 9.0),
        (None, None, 24.0, 12.0),
    ]


def test_the_edge_no_other_lot_shares_is_the_street() -> None:
    """The cadastre is complete where the road centrelines are not, so a
    street frontage is found by what is missing, not by what is drawn."""
    assert street_edges(LOT, NEIGHBOURS) == [True, False, False, False]
    corner = street_edges(LOT, (WEST, BEHIND))
    assert corner == [True, True, False, False], "a corner lot fronts two streets"


def test_a_rectangle_set_back_is_the_rectangle_the_setbacks_leave() -> None:
    (ring,) = inset(LOT, [6.0, 6.0, 6.0, 6.0])
    assert len(ring) == 4, "the corners are recovered, not chamfered"
    corners = sorted((round(x, 1), round(y, 1)) for x, y in ring)
    for corner, expected in zip(corners, [(6, 6), (6, 34), (24, 6), (24, 34)], strict=True):
        assert corner == pytest.approx(expected, abs=0.02)
    assert ring_area(ring) == pytest.approx(18.0 * 28.0, rel=0.002)

    (ring,) = inset(LOT, [6.0, 9.0, 9.0, 9.0])
    assert ring_area(ring) == pytest.approx(12.0 * 25.0, rel=0.002), "the street edge keeps its own"


def test_setbacks_that_meet_in_the_middle_leave_nothing() -> None:
    assert inset(LOT, [16.0, 16.0, 16.0, 16.0]) == []


def test_a_clockwise_ring_is_set_back_inward_all_the_same() -> None:
    """The cadastre does not promise a winding; the inset must not turn a
    clockwise lot inside out and grow it."""
    clockwise = tuple(reversed(LOT))
    (ring,) = inset(clockwise, [6.0] * 4)
    assert ring_area(ring) == pytest.approx(18.0 * 28.0, rel=0.002)


def test_a_battleaxe_keeps_its_head_and_loses_its_handle() -> None:
    """The handle is 4 m wide under a 6 m setback, so it goes; the head is
    30 m square and keeps an 18 m square. A half-plane clip would have
    lost the head to the handle's edge, which is why the inset is traced."""
    battleaxe = (
        (0.0, 0.0),
        (4.0, 0.0),
        (4.0, 30.0),
        (30.0, 30.0),
        (30.0, 60.0),
        (0.0, 60.0),
    )
    (ring,) = inset(battleaxe, [6.0] * 6)
    assert ring_area(ring) == pytest.approx(18.0 * 18.0, rel=0.005)
    assert min(y for _, y in ring) == pytest.approx(36.0, abs=0.02)


def test_an_l_shaped_lot_keeps_its_arm_with_an_arc_at_the_corner() -> None:
    """The reflex corner is set back along an arc, as a real setback is,
    so the area lies between the two rectangles a corner cut would give."""
    ell = ((0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (15.0, 20.0), (15.0, 40.0), (0.0, 40.0))
    (ring,) = inset(ell, [6.0] * 6)
    square_corner = 18.0 * 8.0 + 3.0 * 20.0
    assert square_corner < ring_area(ring) < square_corner + 36.0


def test_a_dumbbell_is_two_pieces() -> None:
    """Two heads on a 4 m neck under a 6 m setback: two envelopes on one lot."""
    dumbbell = (
        (0.0, 0.0),
        (30.0, 0.0),
        (30.0, 30.0),
        (17.0, 30.0),
        (17.0, 50.0),
        (30.0, 50.0),
        (30.0, 80.0),
        (0.0, 80.0),
        (0.0, 50.0),
        (13.0, 50.0),
        (13.0, 30.0),
        (0.0, 30.0),
    )
    pieces = inset(dumbbell, [6.0] * 12)
    assert len(pieces) == 2
    assert all(ring_area(piece) == pytest.approx(18.0 * 18.0, rel=0.005) for piece in pieces)


def test_the_envelope_steps_at_twelve_and_twenty_five_metres() -> None:
    """A 25 m control is eight storeys at 3.1 m: a lower tier at 6 m in for
    the first four, an upper tier at 9 m in for the next four, and no third
    tier because there is no ninth storey. The step is on the storey line
    at 12.4 m, not at the band's 12 m: the fourth storey is in the band."""
    lot = Lot("1//DP1", LOT, "R4", 25.0, None)
    built = envelope_of(lot, NEIGHBOURS)
    assert built is not None
    assert built.storeys == 8 and built.height_m == pytest.approx(24.8)
    assert built.binding == "height" and built.street_edges == 1
    assert [(t.bottom_m, round(t.top_m, 6), t.boundary_m) for t in built.tiers] == [
        (0.0, 12.4, 6.0),
        (12.4, 24.8, 9.0),
    ]
    assert built.tiers[0].area_m2 == pytest.approx(18.0 * 28.0, rel=0.002)
    assert built.tiers[1].area_m2 == pytest.approx(12.0 * 25.0, rel=0.002)
    assert built.footprint == built.tiers[0].rings[0]


def test_a_tall_control_reaches_the_third_band() -> None:
    lot = Lot("1//DP1", LOT, "B4", 40.0, None)
    built = envelope_of(lot, NEIGHBOURS)
    assert built is not None
    assert built.storeys == 12 and built.height_m == pytest.approx(37.2)
    assert [t.boundary_m for t in built.tiers] == [6.0, 9.0, 12.0]
    assert built.tiers[2].area_m2 == pytest.approx(6.0 * 22.0, rel=0.005)


def test_the_floor_space_ratio_takes_storeys_off_the_top() -> None:
    """1,200 sqm at 1:1 is 1,200 sqm of floor. Two storeys of the 504 sqm
    lower tier is 1,008; a third would be 1,512. So two, and the envelope
    says the ratio bound it, not the height."""
    lot = Lot("1//DP1", LOT, "R4", 25.0, 1.0)
    built = envelope_of(lot, NEIGHBOURS)
    assert built is not None
    assert built.storeys == 2 and built.height_m == pytest.approx(6.2)
    assert built.binding == "fsr"
    assert built.gfa_m2 == pytest.approx(1008.0, rel=0.002)
    assert built.allowed_gfa_m2 == pytest.approx(1200.0)
    assert len(built.tiers) == 1 and built.tiers[0].top_m == pytest.approx(6.2)
    assert built.caption == "FUTURE 2 STOREY\n6.2 m\nFSR 1:1 BINDS"


def test_a_generous_ratio_leaves_the_height_to_bind() -> None:
    lot = Lot("1//DP1", LOT, "R4", 25.0, 3.0)
    built = envelope_of(lot, NEIGHBOURS)
    assert built is not None
    assert built.storeys == 8 and built.binding == "height"
    assert built.caption == "FUTURE 8 STOREY\n24.8 m\nFSR 3:1"


def test_no_height_control_is_no_envelope() -> None:
    assert envelope_of(Lot("1//DP1", LOT, "R4", None, 1.0), NEIGHBOURS) is None
    assert envelope_of(Lot("1//DP1", LOT, "R4", 2.0, 1.0), NEIGHBOURS) is None


def test_the_lots_around_the_site_are_chosen_by_reach_zone_and_control() -> None:
    """The site's own lot is the proposal's; a lot zoned for houses keeps
    its house; a lot with no control cannot be read; a lot beyond reach is
    not context; a lot too small once set back is nothing."""
    far = ((500.0, 0.0), (530.0, 0.0), (530.0, 40.0), (500.0, 40.0))
    tiny = ((60.0, 0.0), (70.0, 0.0), (70.0, 40.0), (60.0, 40.0))
    lots = [
        Lot("site", BEHIND, "R4", 25.0, None),
        Lot("east", EAST, "R4", 25.0, None),
        Lot("west", WEST, "R2", 9.0, 0.5),
        Lot("front", LOT, "R3", None, None),
        Lot("far", far, "R4", 25.0, None),
        Lot("tiny", tiny, "R4", 25.0, None),
    ]
    report = envelopes(lots, [BEHIND], reach_m=100.0)
    assert [e.lot.identifier for e in report.envelopes] == ["east"]
    assert (report.on_site, report.other_zones, report.no_height) == (1, 1, 1)
    assert (report.beyond_reach, report.too_small) == (1, 1)
    assert "1 envelopes" in report.describe()
    assert "1 zoned for houses" in report.describe()


def test_the_permitted_zones_are_the_ones_a_flat_building_may_stand_in() -> None:
    assert "R2" not in DEFAULT_ZONES and "R4" in DEFAULT_ZONES and "MU1" in DEFAULT_ZONES
    report = envelopes([Lot("west", WEST, "R2", 25.0, None)], [BEHIND], zones=("R2",))
    assert len(report.envelopes) == 1, "a council that allows it says so"
