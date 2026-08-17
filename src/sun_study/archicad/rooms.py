"""Matching room labels to the apartments that contain them.

In this practice's projects a Zone is a whole apartment unit and the rooms
inside it are not Zones at all -- they are GDL label objects carrying a short
room code in ``room_txt``: ``L/D`` for living/dining, ``K`` for kitchen, ``B1``
for a bedroom. ADG 4A-1 is about living rooms, so that code is the only thing
in the model that separates the room the standard cares about from the rooms it
does not.

The join is geometric: a label belongs to the apartment whose Zone outline
contains its placement point, on the same storey. There is no relationship in
the model to follow, because a label is annotation -- it is not owned by the
Zone and does not reference it.

Why the storey test is not optional
-----------------------------------
These labels live inside hotlinked unit-type modules whose *masters* are parked
far above the building -- one real project has three such sets, at roughly 64 m,
158 m and 280 m, on storeys with no apartments on them at all. A master's label
sits at the same X and Y as the placed instance it came from, so a plain
point-in-polygon test matches it to an apartment perfectly and silently, and the
apartment ends up with two or four copies of every room. Comparing storeys is
what tells the copy from the original.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sun_study.archicad.read import ArchicadZone, LibraryObject

__all__ = [
    "LIVING_ROOM_CODES",
    "ROOM_LABEL_PART",
    "ROOM_NAME_PARAMETER",
    "ROOM_VOCABULARY",
    "RoomLabel",
    "RoomMatch",
    "is_living_room",
    "match_rooms",
    "point_in_polygon",
    "room_labels",
    "unknown_codes",
]

#: The library part carrying a room's name and size in the reference office
#: library. Matched as a substring, because the trailing number is a library
#: version that moves.
ROOM_LABEL_PART = "Room Name and Size Label"

#: Where that part keeps the room code. Measured, not guessed: of 329 placed
#: labels in the reference project, 299 carried a value here.
ROOM_NAME_PARAMETER = "room_txt"

#: The office's own room codes, read off a real project rather than invented.
#: Every code observed there is here, so an unrecognised one means the
#: vocabulary has grown and is worth looking at -- see ``unknown_codes``.
#:
#: Deliberately *not* exhaustive by guesswork. ``S`` and ``BP`` are left
#: unmapped because they are genuinely ambiguous: ``S`` could be study, store
#: or sitting room, and only the first and last would matter to ADG 4A-1.
#: Guessing either way moves the headline percentage on a coin toss.
ROOM_VOCABULARY: dict[str, str] = {
    "L/D": "living/dining",
    "L": "living",
    "LIV": "living",
    "LIVING": "living",
    "LD": "living/dining",
    "L/K/D": "living/kitchen/dining",
    "K": "kitchen",
    "K/D": "kitchen/dining",
    "D": "dining",
    "B": "bedroom",
    "B1": "bedroom",
    "B2": "bedroom",
    "B3": "bedroom",
    "B4": "bedroom",
    "BED": "bedroom",
    "EN": "ensuite",
    "BATH": "bathroom",
    "WC": "toilet",
    "LY": "laundry",
    "ST": "store",
    "UT": "utility",
    "MULTI ROOM": "multi-purpose",
    "BALC": "balcony",
    "TER": "terrace",
}

#: Which of those are living rooms for ADG 4A-1. A room counts if living space
#: is any part of what it is: an ``L/K/D`` is still where people sit.
LIVING_ROOM_CODES: tuple[str, ...] = tuple(
    code for code, kind in ROOM_VOCABULARY.items() if "living" in kind
)


def is_living_room(code: str, *, extra: Sequence[str] = ()) -> bool:
    """Whether a room code names a living room for ADG 4A-1.

    ``extra`` adds codes for a project whose vocabulary differs. It never
    *removes* one, because the built-in set was measured rather than assumed
    and a caller silencing it would do so invisibly.
    """
    wanted = {item.strip().upper() for item in extra}
    return code.strip().upper() in set(LIVING_ROOM_CODES) | wanted


def unknown_codes(labels: Sequence[RoomLabel]) -> tuple[str, ...]:
    """Codes not in the vocabulary, most common first.

    Worth printing every run. A new code is either a room type nobody has
    classified yet or a typo, and both are silent: an unrecognised living room
    is simply not assessed, and nothing else in the output says so.
    """
    counted: dict[str, int] = {}
    for label in labels:
        if label.code not in ROOM_VOCABULARY:
            counted[label.code] = counted.get(label.code, 0) + 1
    return tuple(code for code, _ in sorted(counted.items(), key=lambda pair: -pair[1]))


@dataclass(frozen=True)
class RoomLabel:
    """One room, as the model actually records it: a code at a point."""

    guid: str
    code: str
    """``L/D``, ``K``, ``B1`` and so on. Upper-cased and stripped."""
    x: float
    y: float
    storey_index: int | None

    @property
    def point(self) -> tuple[float, float]:
        return (self.x, self.y)


def room_labels(
    objects: Sequence[LibraryObject],
    *,
    part: str = ROOM_LABEL_PART,
    parameter: str = ROOM_NAME_PARAMETER,
) -> tuple[RoomLabel, ...]:
    """The room labels among a set of library objects, with their codes.

    Objects whose code is blank are dropped rather than kept as unnamed rooms.
    In the reference project 30 of 329 were blank, and a room with no name
    cannot be classified as a living room or as anything else -- carrying it
    would only inflate the counts.
    """
    wanted = part.casefold()
    found: list[RoomLabel] = []
    for item in objects:
        if wanted not in item.library_part.casefold():
            continue
        code = (item.parameter(parameter) or "").strip().upper()
        if not code:
            continue
        found.append(
            RoomLabel(
                guid=item.guid,
                code=code,
                x=item.origin[0],
                y=item.origin[1],
                storey_index=item.storey_index,
            )
        )
    return tuple(found)


def point_in_polygon(point: tuple[float, float], polygon: Sequence[tuple[float, float]]) -> bool:
    """Ray casting, counting crossings to the right of the point.

    Half-open on purpose -- ``(y0 > y) != (y1 > y)`` counts a vertex once
    rather than twice -- so a label sitting exactly on a shared wall between
    two apartments lands in one of them rather than in both or in neither.
    """
    if len(polygon) < 3:
        return False
    x, y = point
    inside = False
    previous_x, previous_y = polygon[-1]
    for current_x, current_y in polygon:
        if (current_y > y) != (previous_y > y):
            span = previous_y - current_y
            if span != 0.0:
                crossing_x = current_x + (y - current_y) / span * (previous_x - current_x)
                if x < crossing_x:
                    inside = not inside
        previous_x, previous_y = current_x, current_y
    return inside


@dataclass(frozen=True)
class RoomMatch:
    """Which rooms each apartment turned out to contain."""

    by_zone: dict[str, tuple[RoomLabel, ...]]
    """Zone GUID -> the room labels inside it."""
    unplaced: tuple[RoomLabel, ...]
    """Labels inside no zone -- hotlink masters, or rooms outside a unit."""
    zones_without_rooms: tuple[str, ...]
    """Apartments that contain no room label at all."""

    @property
    def matched(self) -> int:
        return sum(len(rooms) for rooms in self.by_zone.values())

    def codes(self) -> dict[str, int]:
        """How many of each room code were matched, most common first."""
        return _tally(room for rooms in self.by_zone.values() for room in rooms)

    def unplaced_codes(self) -> dict[str, int]:
        """The same for labels that landed in no apartment.

        Comparing the two tallies is what catches a room type that exists in
        the project but never reaches an apartment. On the reference project
        29 labels read ``L/D`` and *none* of them matched, which says the
        living rooms are somewhere the join is not looking rather than that
        the building has no living rooms.
        """
        return _tally(self.unplaced)

    def missing_kinds(self) -> tuple[str, ...]:
        """Codes present only among the unplaced labels, most common first.

        A code that exists in the project and never lands in an apartment is
        the signature of a join that is finding some rooms and missing a whole
        category of others.
        """
        placed = set(self.codes())
        return tuple(code for code in self.unplaced_codes() if code not in placed)

    def describe(self) -> str:
        placed = len(self.by_zone)
        total = placed + len(self.zones_without_rooms)
        lines = [f"matched {self.matched} room labels into {placed} of {total} apartments"]
        if self.by_zone:
            mix = ", ".join(f"{code} x{count}" for code, count in list(self.codes().items())[:12])
            lines.append(f"  rooms found: {mix}")
            per = sorted(len(rooms) for rooms in self.by_zone.values())
            lines.append(
                f"  rooms per apartment: min {per[0]}, median {per[len(per) // 2]}, max {per[-1]}"
            )
        if self.unplaced:
            lines.append(
                f"  {len(self.unplaced)} labels fell inside no apartment on their own "
                f"storey. Hotlink masters sit at the same X and Y as the units they "
                f"came from, so this is where they land -- and where they must land, "
                f"or every apartment would count its rooms twice."
            )
            missing = self.missing_kinds()
            if missing:
                tally = self.unplaced_codes()
                shown = ", ".join(f"{code} x{tally[code]}" for code in missing[:10])
                lines.append(
                    f"  but {len(missing)} room codes appear ONLY among those: {shown}. "
                    f"A code the project has and no apartment contains means the join "
                    f"is missing a whole category of room, not just the masters."
                )
        if self.zones_without_rooms:
            lines.append(
                f"  {len(self.zones_without_rooms)} apartments contain no room label, "
                f"so nothing can say which part of them is the living room"
            )
        return "\n".join(lines)


def match_rooms(zones: Sequence[ArchicadZone], labels: Sequence[RoomLabel]) -> RoomMatch:
    """Put each room label in the apartment whose outline contains it.

    Both the point-in-polygon test *and* the storey have to agree. A hotlink
    master's label shares its X and Y with the placed instance, so position
    alone matches it to a real apartment -- confidently, and wrongly.
    """
    by_storey: dict[int | None, list[ArchicadZone]] = {}
    for zone in zones:
        by_storey.setdefault(zone.storey_index, []).append(zone)

    found: dict[str, list[RoomLabel]] = {}
    unplaced: list[RoomLabel] = []
    for label in labels:
        candidates = by_storey.get(label.storey_index, ())
        home = next(
            (zone for zone in candidates if point_in_polygon(label.point, zone.outline)),
            None,
        )
        if home is None:
            unplaced.append(label)
        else:
            found.setdefault(home.guid, []).append(label)

    return RoomMatch(
        by_zone={guid: tuple(rooms) for guid, rooms in found.items()},
        unplaced=tuple(unplaced),
        zones_without_rooms=tuple(zone.guid for zone in zones if zone.guid not in found),
    )


def _tally(labels: Iterable[RoomLabel]) -> dict[str, int]:
    """Room codes counted, most common first."""
    counted: dict[str, int] = {}
    for label in labels:
        counted[label.code] = counted.get(label.code, 0) + 1
    return dict(sorted(counted.items(), key=lambda pair: -pair[1]))
