"""Matching room labels to the apartments containing them.

The room codes and counts here are the ones measured on a real project --
``L/D`` for living/dining, ``K``, ``B1``..``B3``, ``EN``, ``LY`` -- and the
hotlink masters at 64 m, 158 m and 280 m are the reason the storey test exists.
"""

from __future__ import annotations

import math

import pytest

from sun_study.archicad.read import ArchicadZone, LibraryObject
from sun_study.archicad.rooms import (
    RoomLabel,
    match_rooms,
    point_in_polygon,
    room_labels,
)

SQUARE = ((0.0, 0.0), (10.0, 0.0), (10.0, 8.0), (0.0, 8.0))


def _zone(
    guid: str, storey: int, outline: tuple[tuple[float, float], ...] = SQUARE
) -> ArchicadZone:
    return ArchicadZone(guid=guid, name="RESI", number="", storey_index=storey, outline=outline)


def _label(code: str, x: float, y: float, storey: int, guid: str = "") -> RoomLabel:
    return RoomLabel(guid=guid or f"{code}-{x}-{y}", code=code, x=x, y=y, storey_index=storey)


def _object(part: str, code: str, x: float, y: float, z: float, storey: int) -> LibraryObject:
    return LibraryObject(
        guid=f"{part}-{code}-{z}",
        library_part=part,
        origin=(x, y, z),
        storey_index=storey,
        parameters=(("room_txt", code), ("label_type", "Room Name and Dimension")),
    )


# -- reading the codes off the objects ------------------------------------


def test_the_room_code_is_read_and_normalised() -> None:
    found = room_labels([_object("Room Name and Size Label 19", " l/d ", 1.0, 2.0, 30.0, 9)])
    assert len(found) == 1
    assert found[0].code == "L/D", "stripped and upper-cased, so 'l/d' and 'L/D' agree"


def test_a_blank_code_is_dropped_rather_than_kept_as_a_room() -> None:
    """30 of 329 labels in the reference project carried no code. A room with
    no name cannot be classified as a living room or as anything else."""
    found = room_labels(
        [
            _object("Room Name and Size Label 19", "", 1.0, 2.0, 30.0, 9),
            _object("Room Name and Size Label 19", "L/D", 3.0, 2.0, 30.0, 9),
        ]
    )
    assert [item.code for item in found] == ["L/D"]


def test_other_library_parts_are_ignored() -> None:
    """The project holds 652 trees and 128 joinery objects too."""
    found = room_labels(
        [
            _object("Tree Plan 26", "L/D", 1.0, 2.0, 30.0, 9),
            _object("Room Name and Size Label 19", "K", 3.0, 2.0, 30.0, 9),
        ]
    )
    assert [item.code for item in found] == ["K"]


def test_the_library_part_is_matched_as_a_substring() -> None:
    """The trailing number is a library version and it moves."""
    assert room_labels([_object("Room Name and Size Label 21", "B1", 1.0, 1.0, 0.0, 9)])


# -- point in polygon ------------------------------------------------------


def test_a_point_inside_and_outside_a_square() -> None:
    assert point_in_polygon((5.0, 4.0), SQUARE)
    assert not point_in_polygon((15.0, 4.0), SQUARE)
    assert not point_in_polygon((5.0, 9.0), SQUARE)


def test_a_point_in_the_notch_of_an_l_shape_is_outside() -> None:
    """Apartments wrap lift cores, so a bounding box would be wrong."""
    ell = ((0.0, 0.0), (10.0, 0.0), (10.0, 4.0), (4.0, 4.0), (4.0, 8.0), (0.0, 8.0))
    assert point_in_polygon((2.0, 6.0), ell), "the upright arm is inside"
    assert not point_in_polygon((7.0, 6.0), ell), "the notch is not"


def test_a_label_on_a_shared_wall_lands_in_exactly_one_apartment() -> None:
    """Two apartments meeting at x=10. A label exactly on the line must go to
    one of them: in both is a double count, in neither is a lost room."""
    left = ((0.0, 0.0), (10.0, 0.0), (10.0, 8.0), (0.0, 8.0))
    right = ((10.0, 0.0), (20.0, 0.0), (20.0, 8.0), (10.0, 8.0))
    on_the_line = (10.0, 4.0)
    assert point_in_polygon(on_the_line, left) != point_in_polygon(on_the_line, right)


def test_a_degenerate_outline_contains_nothing() -> None:
    assert not point_in_polygon((0.0, 0.0), ())
    assert not point_in_polygon((0.0, 0.0), ((1.0, 1.0), (2.0, 2.0)))


# -- the join --------------------------------------------------------------


def test_rooms_land_in_the_apartment_that_contains_them() -> None:
    zones = [_zone("apt-1", storey=9)]
    labels = [_label("L/D", 2.0, 2.0, 9), _label("K", 6.0, 2.0, 9), _label("B1", 6.0, 6.0, 9)]

    match = match_rooms(zones, labels)

    assert [room.code for room in match.by_zone["apt-1"]] == ["L/D", "K", "B1"]
    assert match.matched == 3
    assert match.unplaced == ()
    assert match.zones_without_rooms == ()


def test_a_hotlink_master_does_not_double_count_the_rooms() -> None:
    """The trap this whole module is shaped around.

    A master's label sits at the *same X and Y* as the placed instance it came
    from, just on a parked storey. Position alone matches it to a real
    apartment -- confidently and wrongly -- and every apartment ends up with
    two of every room.
    """
    zones = [_zone("apt-1", storey=9)]
    placed = _label("L/D", 2.0, 2.0, storey=9, guid="placed")
    master = _label("L/D", 2.0, 2.0, storey=38, guid="master")

    match = match_rooms(zones, [placed, master])

    assert [room.guid for room in match.by_zone["apt-1"]] == ["placed"]
    assert [room.guid for room in match.unplaced] == ["master"]
    assert match.matched == 1, "one living room, not two"


def test_labels_outside_every_apartment_are_reported_not_dropped() -> None:
    match = match_rooms([_zone("apt-1", storey=9)], [_label("ST", 50.0, 50.0, 9)])

    assert match.matched == 0
    assert len(match.unplaced) == 1
    assert "fell inside no apartment" in match.describe()


def test_an_apartment_with_no_labels_is_named() -> None:
    """It cannot be assessed against a standard about living rooms."""
    zones = [
        _zone("apt-1", storey=9),
        _zone("apt-2", storey=9, outline=((20.0, 0.0), (30.0, 0.0), (30.0, 8.0), (20.0, 8.0))),
    ]
    match = match_rooms(zones, [_label("L/D", 2.0, 2.0, 9)])

    assert match.zones_without_rooms == ("apt-2",)
    assert "1 apartments contain no room label" in match.describe()


def test_the_room_mix_is_reported_for_checking() -> None:
    """Which code means 'living room' is a practice's own convention, and
    getting it wrong changes the headline percentage silently."""
    zones = [_zone("apt-1", storey=9)]
    labels = [_label("L/D", 2.0, 2.0, 9), _label("B1", 4.0, 2.0, 9), _label("B2", 6.0, 2.0, 9)]

    match = match_rooms(zones, labels)

    assert match.codes() == {"L/D": 1, "B1": 1, "B2": 1}
    assert "L/D x1" in match.describe()


def test_two_apartments_on_one_storey_keep_their_own_rooms() -> None:
    zones = [
        _zone("apt-1", storey=9),
        _zone("apt-2", storey=9, outline=((20.0, 0.0), (30.0, 0.0), (30.0, 8.0), (20.0, 8.0))),
    ]
    labels = [_label("L/D", 2.0, 2.0, 9), _label("L/D", 22.0, 2.0, 9)]

    match = match_rooms(zones, labels)

    assert len(match.by_zone["apt-1"]) == 1
    assert len(match.by_zone["apt-2"]) == 1


def test_the_same_position_on_a_different_storey_is_a_different_apartment() -> None:
    """Apartments stack. Without the storey test, a label would match the
    apartment directly below as readily as its own."""
    zones = [_zone("apt-lower", storey=9), _zone("apt-upper", storey=10)]
    match = match_rooms(zones, [_label("L/D", 2.0, 2.0, storey=10)])

    assert "apt-upper" in match.by_zone
    assert "apt-lower" not in match.by_zone


# -- the room vocabulary ---------------------------------------------------


def test_the_office_codes_are_built_in_rather_than_configured() -> None:
    """Every code observed on the reference project is classified, so a run
    needs no --living to know what a living room is."""
    from sun_study.archicad.rooms import ROOM_VOCABULARY

    for code in ("L/D", "K", "B1", "B2", "B3", "B", "EN", "LY", "ST", "UT", "MULTI ROOM"):
        assert code in ROOM_VOCABULARY, code


def test_living_dining_is_a_living_room_and_a_bedroom_is_not() -> None:
    from sun_study.archicad.rooms import is_living_room

    assert is_living_room("L/D")
    assert is_living_room("l/d"), "codes are compared case-insensitively"
    assert is_living_room("L/K/D"), "living space is part of what it is"
    assert not is_living_room("B1")
    assert not is_living_room("K")
    assert not is_living_room("EN")


def test_an_unclassified_code_is_not_guessed_at() -> None:
    """Nobody has said what BP is, so nothing pretends to know. Guessing a
    room type either way moves the headline percentage on a coin toss."""
    from sun_study.archicad.rooms import ROOM_VOCABULARY, is_living_room

    assert "BP" not in ROOM_VOCABULARY
    assert not is_living_room("BP")
    assert is_living_room("BP", extra=["BP"]), "but a project can say so explicitly"


def test_an_extra_code_adds_to_the_built_in_set_rather_than_replacing_it() -> None:
    """A caller silencing the measured vocabulary would do so invisibly."""
    from sun_study.archicad.rooms import is_living_room

    assert is_living_room("LOUNGE", extra=["LOUNGE"])
    assert is_living_room("L/D", extra=["LOUNGE"]), "the built-in set still applies"


def test_unrecognised_codes_are_reported_not_ignored() -> None:
    """An unrecognised living room is simply not assessed, and nothing else in
    the output would say so."""
    from sun_study.archicad.rooms import unknown_codes

    found = unknown_codes(
        [
            _label("L/D", 1.0, 1.0, 9),
            _label("BP", 2.0, 1.0, 9),
            _label("BP", 3.0, 1.0, 9),
            _label("XYZ", 4.0, 1.0, 9),
        ]
    )
    assert found == ("BP", "XYZ"), "most common first, and known codes are absent"


# -- catching a room type that never reaches an apartment ------------------


def test_a_code_present_only_among_unplaced_labels_is_called_out() -> None:
    """Observed on the reference project: 29 labels read L/D and *none* of
    them matched, which says the living rooms are somewhere the join is not
    looking -- not that the building has no living rooms."""
    zones = [_zone("apt-1", storey=9)]
    labels = [
        _label("B1", 2.0, 2.0, storey=9),
        _label("L/D", 2.0, 4.0, storey=38),
        _label("L/D", 4.0, 4.0, storey=38),
    ]

    match = match_rooms(zones, labels)

    assert match.missing_kinds() == ("L/D",)
    assert match.unplaced_codes() == {"L/D": 2}
    described = match.describe()
    assert "appear ONLY among those" in described
    assert "L/D x2" in described


def test_a_code_present_in_both_places_is_not_called_out() -> None:
    """Masters duplicate every room, so most codes appear on both sides. Only
    the ones that never land anywhere are a signal."""
    zones = [_zone("apt-1", storey=9)]
    labels = [_label("L/D", 2.0, 2.0, storey=9), _label("L/D", 2.0, 2.0, storey=38)]

    match = match_rooms(zones, labels)
    assert match.missing_kinds() == ()
    assert "appear ONLY among those" not in match.describe()


def test_study_and_storage_are_told_apart() -> None:
    """Confirmed by the practice, and the obvious reading is backwards: ST is
    study and S is storage. A study is habitable, storage is not."""
    from sun_study.archicad.rooms import ROOM_VOCABULARY, is_living_room

    assert ROOM_VOCABULARY["ST"] == "study"
    assert ROOM_VOCABULARY["S"] == "storage"
    assert not is_living_room("ST"), "habitable, but ADG 4A-1 is about living rooms"
    assert not is_living_room("S")


# -- master, or a placed room the join lost? -------------------------------


def test_a_miss_on_a_storey_with_no_apartments_is_just_a_master() -> None:
    """Landing nowhere is what a parked master is supposed to do."""
    zones = [_zone("apt-1", storey=9)]
    match = match_rooms(zones, [_label("L/D", 2.0, 2.0, storey=38)])

    assert len(match.unplaced) == 1
    assert match.missed_on_live_storeys == ()
    assert "sit on storeys that DO carry apartments" not in match.describe()


def test_a_miss_on_a_storey_that_has_apartments_is_a_lost_room() -> None:
    """The distinction that matters. A label on an apartment storey that still
    matches no outline is a placed room falling outside its zone -- missing
    from the assessment rather than correctly excluded from it."""
    zones = [_zone("apt-1", storey=9)]
    outside_the_outline = _label("L/D", 50.0, 50.0, storey=9)
    master = _label("L/D", 2.0, 2.0, storey=38)

    match = match_rooms(zones, [outside_the_outline, master])

    assert len(match.unplaced) == 2, "both missed"
    assert [room.guid for room in match.missed_on_live_storeys] == [outside_the_outline.guid]
    described = match.describe()
    assert "sit on storeys that DO carry apartments" in described
    assert "L/D x1" in described


def test_the_lost_rooms_are_reported_by_code() -> None:
    """Which room type is going missing is the whole diagnostic: all the small
    rooms matching and every living room missing points somewhere specific."""
    zones = [_zone("apt-1", storey=9)]
    labels = [
        _label("B1", 2.0, 2.0, storey=9),
        _label("L/D", 50.0, 50.0, storey=9),
        _label("L/D", 60.0, 50.0, storey=9),
    ]

    match = match_rooms(zones, labels)
    assert "L/D x2" in match.describe()
    assert match.codes() == {"B1": 1}, "only the matched rooms count as found"


# -- the tolerance ---------------------------------------------------------


def test_distance_is_zero_inside_and_the_gap_outside() -> None:
    from sun_study.archicad.rooms import distance_to_polygon

    assert distance_to_polygon((5.0, 4.0), SQUARE) == 0.0
    assert distance_to_polygon((12.0, 4.0), SQUARE) == pytest.approx(2.0)
    assert distance_to_polygon((5.0, -3.0), SQUARE) == pytest.approx(3.0)
    # Past a corner, so the nearest point is the vertex rather than an edge.
    assert distance_to_polygon((13.0, 12.0), SQUARE) == pytest.approx(5.0)


def test_distance_is_to_the_edge_not_the_centre() -> None:
    """An apartment wrapping a lift core must not appear closer than it is."""
    from sun_study.archicad.rooms import distance_to_polygon

    ell = ((0.0, 0.0), (10.0, 0.0), (10.0, 4.0), (4.0, 4.0), (4.0, 8.0), (0.0, 8.0))
    assert distance_to_polygon((6.0, 5.0), ell) == pytest.approx(1.0), "1 m above the notch edge"


def test_a_label_just_outside_is_matched_and_the_reach_is_reported() -> None:
    """The bug this fixes: on a real project every living room on an apartment
    storey fell outside its zone while the bedrooms around it matched."""
    zones = [_zone("apt-1", storey=9)]
    outside = _label("L/D", 10.4, 4.0, storey=9)

    match = match_rooms(zones, [outside], tolerance_m=1.5)

    assert [room.code for room in match.by_zone["apt-1"]] == ["L/D"]
    assert match.unplaced == ()
    assert len(match.reached_for) == 1
    assert match.reached_for[0][1] == pytest.approx(0.4)
    described = match.describe()
    assert "matched within the 1.5 m tolerance" in described
    assert "L/D x1" in described


def test_a_label_beyond_the_tolerance_still_misses() -> None:
    zones = [_zone("apt-1", storey=9)]
    match = match_rooms(zones, [_label("L/D", 20.0, 4.0, storey=9)], tolerance_m=1.5)

    assert match.by_zone == {}
    assert len(match.unplaced) == 1


def test_a_contained_label_is_not_counted_as_reached_for() -> None:
    """Only the judgement calls are reported, or the report is noise."""
    zones = [_zone("apt-1", storey=9)]
    match = match_rooms(zones, [_label("L/D", 5.0, 4.0, storey=9)], tolerance_m=1.5)

    assert match.matched == 1
    assert match.reached_for == ()
    assert "tolerance" not in match.describe()


def test_the_tolerance_picks_the_nearest_apartment_not_the_first() -> None:
    """Between two apartments, a label belongs to the one it is closest to."""
    near = _zone("apt-near", storey=9, outline=((11.0, 0.0), (20.0, 0.0), (20.0, 8.0), (11.0, 8.0)))
    far = _zone("apt-far", storey=9)
    label = _label("L/D", 10.7, 4.0, storey=9)

    match = match_rooms([far, near], [label], tolerance_m=1.5)

    assert "apt-near" in match.by_zone, "0.3 m away, against 0.7 m for the other"
    assert "apt-far" not in match.by_zone


def test_zero_tolerance_requires_containment() -> None:
    zones = [_zone("apt-1", storey=9)]
    match = match_rooms(zones, [_label("L/D", 10.1, 4.0, storey=9)], tolerance_m=0.0)

    assert match.by_zone == {}
    assert len(match.missed_on_live_storeys) == 1


def test_the_tolerance_never_reaches_across_a_storey() -> None:
    """A master directly above an apartment is 0 m away in plan."""
    zones = [_zone("apt-1", storey=9)]
    match = match_rooms(zones, [_label("L/D", 5.0, 4.0, storey=38)], tolerance_m=1.5)

    assert match.by_zone == {}
    assert len(match.unplaced) == 1


# -- a zone covering more than one unit ------------------------------------


def test_two_living_rooms_in_one_apartment_are_reported() -> None:
    """A flat has one living room. Two means the zone covers two units, and
    every count downstream is then wrong while still reading as plausible."""
    zones = [_zone("apt-1", storey=9)]
    labels = [_label("L/D", 2.0, 2.0, 9), _label("L/D", 8.0, 6.0, 9), _label("B1", 5.0, 2.0, 9)]

    match = match_rooms(zones, labels)

    assert match.duplicated == (("apt-1", "L/D", 2),)
    described = match.describe()
    assert "more than one of a room that should be unique" in described
    assert "L/D x2" in described


def test_several_bedrooms_are_normal_and_not_flagged() -> None:
    """B1, B2 and B3 are already distinct codes, so three bedrooms in a
    three-bedroom flat is not a fault. B is the bathroom, and one of those
    alongside them is the normal arrangement."""
    zones = [_zone("apt-1", storey=9)]
    labels = [
        _label("B", 2.0, 2.0, 9),
        _label("B1", 4.0, 2.0, 9),
        _label("B2", 6.0, 2.0, 9),
        _label("B3", 8.0, 2.0, 9),
        _label("L/D", 5.0, 6.0, 9),
    ]

    match = match_rooms(zones, labels)
    assert match.duplicated == ()
    assert "should be unique" not in match.describe()


def test_b_is_the_bathroom_not_a_bedroom() -> None:
    """Read off a typical floor plan: the bedrooms are B1 3.0x3.6, B2 and B3
    3.0x3.0 with beds drawn in, while B is 1.8x3.1 beside the ensuite with
    sanitary fittings. The letter alone gets this backwards, and a bedroom is
    habitable where a bathroom is not."""
    from sun_study.archicad.rooms import ROOM_VOCABULARY

    assert ROOM_VOCABULARY["B"] == "bathroom"
    assert ROOM_VOCABULARY["B1"] == "bedroom"


def test_two_bathrooms_in_one_flat_are_flagged() -> None:
    """One bathroom per dwelling, beside its ensuite."""
    zones = [_zone("apt-1", storey=9)]
    labels = [_label("B", 2.0, 2.0, 9), _label("B", 6.0, 2.0, 9)]

    assert match_rooms(zones, labels).duplicated == (("apt-1", "B", 2),)


def test_a_tidy_apartment_reports_no_duplicates() -> None:
    zones = [_zone("apt-1", storey=9)]
    labels = [_label("L/D", 2.0, 2.0, 9), _label("K", 4.0, 2.0, 9), _label("EN", 6.0, 2.0, 9)]

    assert match_rooms(zones, labels).duplicated == ()


def test_the_measured_tolerance_cannot_reach_into_a_neighbouring_room() -> None:
    """The reaches observed on a real project were 0.00 m to 0.30 m: labels
    sitting exactly on the outline, not dragged past it. The default has to
    cover that and stop well short of a room's width."""
    from sun_study.archicad.rooms import DEFAULT_TOLERANCE_M

    assert DEFAULT_TOLERANCE_M >= 0.3, "must cover the worst case measured"
    assert DEFAULT_TOLERANCE_M < 1.0, "must not span a habitable room"


def test_a_label_exactly_on_the_outline_is_matched() -> None:
    """The actual cause: a point on an edge is neither in nor out by a strict
    test, and the median reach measured was 0.00 m."""
    zones = [_zone("apt-1", storey=9)]
    on_the_edge = _label("L/D", 10.0, 4.0, storey=9)

    match = match_rooms(zones, [on_the_edge])
    assert match.matched == 1
    assert match.by_zone["apt-1"][0].code == "L/D"


# -- overlapping zones -----------------------------------------------------


def test_polygon_area_ignores_winding_direction() -> None:
    """Only ever compared, never reported -- but a clockwise outline must not
    sort as smaller than every other zone in the project."""
    from sun_study.archicad.rooms import polygon_area

    clockwise = ((0.0, 0.0), (0.0, 8.0), (10.0, 8.0), (10.0, 0.0))
    assert polygon_area(SQUARE) == pytest.approx(80.0)
    assert polygon_area(clockwise) == pytest.approx(80.0)
    assert polygon_area(((0.0, 0.0), (1.0, 1.0))) == 0.0


def test_a_label_inside_two_zones_goes_to_the_smaller_one() -> None:
    """The bug this fixes. Zones overlap -- a floor-wide zone drawn over the
    unit zones inside it -- and both contain the label at distance 0. Breaking
    that tie by list order put 30 rooms and four kitchens in one apartment
    while leaving 18 apartments empty.
    """
    whole_floor = _zone(
        "floor", storey=9, outline=((0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0))
    )
    unit = _zone("apt-1", storey=9)

    # The floor zone is listed first, so list order would hand it the label.
    match = match_rooms([whole_floor, unit], [_label("L/D", 5.0, 4.0, storey=9)])

    assert "apt-1" in match.by_zone, "the smallest containing zone is the most specific"
    assert "floor" not in match.by_zone


def test_the_overlap_itself_is_reported() -> None:
    """Picking the smaller zone is a repair, not a fix: the layer still holds
    two kinds of zone, and a run reading both measures some apartments twice."""
    whole_floor = _zone(
        "floor", storey=9, outline=((0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0))
    )
    unit = _zone("apt-1", storey=9)

    match = match_rooms([whole_floor, unit], [_label("L/D", 5.0, 4.0, storey=9)])

    assert len(match.inside_several) == 1
    assert "fell inside more than one apartment" in match.describe()


def test_a_label_in_exactly_one_zone_is_not_called_an_overlap() -> None:
    zones = [
        _zone("apt-1", storey=9),
        _zone("apt-2", storey=9, outline=((20.0, 0.0), (30.0, 0.0), (30.0, 8.0), (20.0, 8.0))),
    ]
    match = match_rooms(zones, [_label("L/D", 5.0, 4.0, storey=9)])

    assert match.inside_several == ()
    assert "more than one apartment" not in match.describe()


def test_the_smaller_zone_wins_regardless_of_list_order() -> None:
    """Whichever way Archicad happens to list them."""
    big = _zone("big", storey=9, outline=((0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)))
    small = _zone("small", storey=9)
    label = _label("K", 5.0, 4.0, storey=9)

    assert "small" in match_rooms([big, small], [label]).by_zone
    assert "small" in match_rooms([small, big], [label]).by_zone


def test_a_nearer_zone_still_beats_a_smaller_distant_one() -> None:
    """Area only breaks ties. Distance is still the first thing that matters."""
    near_big = _zone("near", storey=9, outline=((0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)))
    far_small = _zone(
        "far", storey=9, outline=((100.0, 0.0), (101.0, 0.0), (101.0, 1.0), (100.0, 1.0))
    )

    match = match_rooms([far_small, near_big], [_label("K", 5.0, 4.0, storey=9)])
    assert "near" in match.by_zone


def test_a_problem_zone_is_named_by_its_storey() -> None:
    """A project where every zone is called the same thing and carries no
    number gives a reader a GUID, which locates nothing in Archicad. A storey
    number opens the right plan."""
    zones = [_zone("apt-1", storey=7)]
    labels = [_label("K", 2.0, 2.0, 7), _label("K", 6.0, 2.0, 7)]

    described = match_rooms(zones, labels).describe()
    assert "storey 7: K x2" in described


def test_a_zone_with_no_storey_says_so_rather_than_guessing() -> None:
    from sun_study.archicad.read import ArchicadZone

    zone = ArchicadZone(guid="apt-1", name="RESI", number="", storey_index=None, outline=SQUARE)
    labels = [
        RoomLabel(guid="a", code="K", x=2.0, y=2.0, storey_index=None),
        RoomLabel(guid="b", code="K", x=6.0, y=2.0, storey_index=None),
    ]

    assert "storey unknown: K x2" in match_rooms([zone], labels).describe()


# -- the room's own floor rectangle ----------------------------------------


def _sized(
    code: str, x: float, y: float, w: float, d: float, storey: int = 9, angle: float = 0.0
) -> RoomLabel:
    return RoomLabel(
        guid=f"{code}-{x}",
        code=code,
        x=x,
        y=y,
        storey_index=storey,
        width_m=w,
        depth_m=d,
        angle=angle,
    )


def test_the_label_object_carries_the_rooms_size() -> None:
    """A label object is stretched to the room it names and prints those
    figures: one reported as 3.0 x 3.0 displays 'B3 3.0 x 3.0' on the plan."""
    from sun_study.archicad.read import LibraryObject

    found = room_labels(
        [
            LibraryObject(
                guid="o1",
                library_part="Room Name and Size Label 19",
                origin=(10.0, 4.0, 30.0),
                dimensions=(5.7, 4.0, 0.0),
                storey_index=9,
                parameters=(("room_txt", "L/D"),),
            )
        ]
    )
    assert found[0].width_m == pytest.approx(5.7)
    assert found[0].depth_m == pytest.approx(4.0)
    assert found[0].area_m2 == pytest.approx(22.8), "the L/D on the reference plan"


def test_the_footprint_is_a_rectangle_from_a_corner() -> None:
    """The origin is a corner, not the centre -- which is why these labels sit
    exactly on a zone outline and why matching them needed a tolerance."""
    room = _sized("L/D", 10.0, 4.0, 5.7, 4.0)
    flattened = [value for corner in room.footprint() for value in corner]
    assert flattened == pytest.approx([10.0, 4.0, 15.7, 4.0, 15.7, 8.0, 10.0, 8.0])


def test_a_rotated_room_lands_on_itself() -> None:
    """A quarter turn puts the far corner up and left of the origin."""
    room = _sized("L/D", 0.0, 0.0, 4.0, 2.0, angle=math.pi / 2)
    corners = room.footprint()
    assert corners[1] == pytest.approx((0.0, 4.0), abs=1e-9)
    assert corners[3] == pytest.approx((-2.0, 0.0), abs=1e-9)
    from sun_study.archicad.rooms import polygon_area

    assert polygon_area(corners) == pytest.approx(8.0), "rotation preserves area"


def test_a_room_with_no_size_has_no_footprint() -> None:
    """So a caller samples nothing rather than a degenerate rectangle."""
    assert _sized("L/D", 1.0, 1.0, 0.0, 0.0).footprint() == ()
    assert _sized("L/D", 1.0, 1.0, 3.0, 0.0).footprint() == ()


def test_the_largest_room_is_reported_for_checking_against_a_plan() -> None:
    """The only way to know the geometry is right rather than self-consistent."""
    zones = [_zone("apt-1", storey=9)]
    labels = [_sized("L/D", 1.0, 1.0, 5.7, 4.0), _sized("B1", 6.0, 1.0, 3.0, 3.6)]

    described = match_rooms(zones, labels).describe()
    assert "L/D the largest at 5.7 x 4.0 m (22.8 m2)" in described


def test_rooms_without_a_size_are_counted() -> None:
    zones = [_zone("apt-1", storey=9)]
    labels = [_sized("L/D", 1.0, 1.0, 5.7, 4.0), _label("K", 6.0, 1.0, 9)]

    assert "1 rooms report no size" in match_rooms(zones, labels).describe()
