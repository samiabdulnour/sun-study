"""Matching room labels to the apartments containing them.

The room codes and counts here are the ones measured on a real project --
``L/D`` for living/dining, ``K``, ``B1``..``B3``, ``EN``, ``LY`` -- and the
hotlink masters at 64 m, 158 m and 280 m are the reason the storey test exists.
"""

from __future__ import annotations

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
