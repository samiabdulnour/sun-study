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

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sun_study.archicad.read import ArchicadZone, LibraryObject

__all__ = [
    "DEFAULT_TOLERANCE_M",
    "LIVING_ROOM_CODES",
    "ROOM_LABEL_PART",
    "ROOM_NAME_PARAMETER",
    "ROOM_VOCABULARY",
    "UNIQUE_ROOM_CODES",
    "RoomLabel",
    "RoomMatch",
    "distance_to_polygon",
    "is_living_room",
    "match_rooms",
    "point_in_polygon",
    "polygon_area",
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
#: ``ST`` is **study** and ``S`` is **storage**, confirmed by the practice.
#: Worth stating because the obvious reading is the other way round -- an
#: earlier version of this table had ``ST`` as "store" -- and the two differ in
#: kind: a study is habitable, storage is not.
#:
#: Neither is a living room, so for ADG 4A-1 the correction changes nothing
#: today. It would matter the moment a ruleset asks about habitable rooms.
#:
#: ``BP`` is still unmapped, because nobody has said what it is. An
#: unrecognised code is reported rather than guessed at.
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
    # B is the *bathroom*, not a bedroom. Read off a typical floor plan: the
    # bedrooms are B1 3.0x3.6, B2 and B3 3.0x3.0 with beds drawn in, while B
    # is 1.8x3.1 beside the ensuite with sanitary fittings. Guessing from the
    # letter alone gets this backwards, and it matters -- a bedroom is
    # habitable and a bathroom is not.
    "B": "bathroom",
    "B1": "bedroom",
    "B2": "bedroom",
    "B3": "bedroom",
    "B4": "bedroom",
    "BED": "bedroom",
    "EN": "ensuite",
    "BATH": "bathroom",
    "WC": "toilet",
    "LY": "laundry",
    "ST": "study",
    "S": "storage",
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


#: How far outside a zone outline a room label may sit and still belong to it.
#:
#: Measured, then measured again. On a real project strict containment lost 70
#: placed labels including every one of the 14 living rooms, while matching the
#: bedrooms and ensuites around them. The first guess at why -- annotation
#: dragged past the wall to where it reads well -- was wrong, and a 1.5 m
#: tolerance was set to cover it.
#:
#: What the reaches actually measured was **0.00 m to 0.30 m, median 0.00 m**.
#: The labels are not dragged anywhere: they sit *exactly on* the outline, and
#: a point on an edge is neither in nor out by a strict test. This is coincident
#: geometry, not loose draughting.
#:
#: So the tolerance is 0.5 m: comfortably past the worst case observed, and
#: small enough that a label cannot reach into a neighbouring room at all,
#: since no habitable room is a metre wide. A tolerance that spans a real room
#: would eventually attach a bedroom to the flat next door and never say so.
DEFAULT_TOLERANCE_M = 0.5


def polygon_area(polygon: Sequence[tuple[float, float]]) -> float:
    """Enclosed area by the shoelace formula, sign discarded.

    Only ever compared, never reported, so winding direction does not matter
    -- but taking the absolute value means a clockwise outline does not sort
    as smaller than every other zone in the project.
    """
    if len(polygon) < 3:
        return 0.0
    total = 0.0
    previous_x, previous_y = polygon[-1]
    for x, y in polygon:
        total += previous_x * y - x * previous_y
        previous_x, previous_y = x, y
    return abs(total) / 2.0


def distance_to_polygon(
    point: tuple[float, float], polygon: Sequence[tuple[float, float]]
) -> float:
    """How far a point lies outside a polygon. Zero when it is inside.

    Used to attach a label to the apartment it most plausibly belongs to when
    it sits outside them all. The distance is to the nearest *edge*, so an
    apartment wrapping a lift core does not appear closer than it is.
    """
    if len(polygon) < 3:
        return float("inf")
    if point_in_polygon(point, polygon):
        return 0.0

    x, y = point
    best = float("inf")
    previous = polygon[-1]
    for current in polygon:
        best = min(best, _distance_to_segment(x, y, previous, current))
        previous = current
    return best


def _distance_to_segment(
    x: float, y: float, start: tuple[float, float], end: tuple[float, float]
) -> float:
    start_x, start_y = start
    end_x, end_y = end
    run_x, run_y = end_x - start_x, end_y - start_y
    length_squared = run_x * run_x + run_y * run_y
    if length_squared == 0.0:
        return math.hypot(x - start_x, y - start_y)
    # Clamped so the nearest point stays on the segment rather than on the
    # infinite line through it.
    along = max(0.0, min(1.0, ((x - start_x) * run_x + (y - start_y) * run_y) / length_squared))
    return math.hypot(x - (start_x + along * run_x), y - (start_y + along * run_y))


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
    missed_on_live_storeys: tuple[RoomLabel, ...] = ()
    """Unplaced labels sharing a storey with apartments.

    The distinction that matters. A label on a storey with no apartments on it
    is a hotlink master, parked above the building, and belongs nowhere. A
    label on a storey that *does* carry apartments is a placed room that fell
    outside every outline -- a geometry problem, and a room silently missing
    from the assessment.
    """
    duplicated: tuple[tuple[str, str, int], ...] = ()
    """``(zone guid, room code, how many)`` where one apartment holds several.

    A flat has one living room and one kitchen. Two of either means the zone
    is covering more than one unit, and every count downstream is then wrong
    in a way that still reads as plausible -- an apartment reported as having
    sunlight in "its" living room when the label belongs next door.
    """
    inside_several: tuple[RoomLabel, ...] = ()
    """Labels that fell inside more than one apartment outline at once.

    Zones overlap in real projects -- a floor-wide or multi-unit zone drawn
    over the unit zones inside it. Each such label went to the *smallest*
    containing zone, which is the most specific one, but the overlap itself is
    worth knowing: it means the layer holds two kinds of zone, and a run that
    reads both is measuring some apartments twice.
    """
    reached_for: tuple[tuple[RoomLabel, float], ...] = ()
    """Labels matched by the tolerance rather than by containment, and by how far.

    Every one of these is a judgement call the tool made on a person's behalf,
    so all of them are reported. A handful at a few centimetres is annotation
    sitting on a wall line; a lot of them near the limit means the tolerance is
    papering over a mismatch between the zones and the rooms.
    """
    tolerance_m: float = 0.0

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
        if self.reached_for:
            gaps = sorted(gap for _, gap in self.reached_for)
            mix = ", ".join(
                f"{code} x{count}"
                for code, count in list(_tally(label for label, _ in self.reached_for).items())[:8]
            )
            lines.append(
                f"  {len(self.reached_for)} of those sat outside their apartment and were "
                f"matched within the {self.tolerance_m:g} m tolerance: {mix}"
            )
            lines.append(
                f"    by {gaps[0]:.2f} m to {gaps[-1]:.2f} m, median {gaps[len(gaps) // 2]:.2f} m"
            )
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
        if self.missed_on_live_storeys:
            mix = ", ".join(
                f"{code} x{count}"
                for code, count in list(_tally(self.missed_on_live_storeys).items())[:10]
            )
            lines.append(
                f"  {len(self.missed_on_live_storeys)} of them sit on storeys that DO "
                f"carry apartments: {mix}. Those are not masters -- they are placed "
                f"rooms falling outside every zone outline, so they are missing from "
                f"the assessment rather than correctly excluded from it."
            )
        if self.zones_without_rooms:
            lines.append(
                f"  {len(self.zones_without_rooms)} apartments contain no room label, "
                f"so nothing can say which part of them is the living room"
            )
        if self.inside_several:
            lines.append(
                f"  {len(self.inside_several)} labels fell inside more than one apartment "
                f"at once and went to the smallest containing one. Overlapping zones mean "
                f"the layer holds two kinds -- unit zones and something larger over them -- "
                f"and a run reading both measures some apartments twice."
            )
        if self.duplicated:
            worst = sorted(self.duplicated, key=lambda item: -item[2])[:6]
            mix = ", ".join(f"{code} x{count}" for _, code, count in worst)
            affected = len({guid for guid, _, _ in self.duplicated})
            lines.append(
                f"  {affected} apartments hold more than one of a room that should be "
                f"unique: {mix}. A flat has one living room and one kitchen, so those "
                f"zones are covering more than one unit -- either the outline spans "
                f"several flats, or the apartments are on a layer this run did not read."
            )
        return "\n".join(lines)


def match_rooms(
    zones: Sequence[ArchicadZone],
    labels: Sequence[RoomLabel],
    *,
    tolerance_m: float = DEFAULT_TOLERANCE_M,
) -> RoomMatch:
    """Put each room label in the apartment it belongs to.

    Containment first, then the nearest apartment within ``tolerance_m``. Both
    steps require the same storey. A hotlink master's label shares its X and Y
    with the placed instance it came from, so position alone matches it to a
    real apartment -- confidently, and wrongly.

    The tolerance exists because a label is annotation, dragged to wherever it
    reads well on the drawing. Strict containment lost every living room on a
    real project while matching the bedrooms around them. Set ``tolerance_m``
    to zero to require containment.
    """
    by_storey: dict[int | None, list[ArchicadZone]] = {}
    for zone in zones:
        by_storey.setdefault(zone.storey_index, []).append(zone)

    found: dict[str, list[RoomLabel]] = {}
    unplaced: list[RoomLabel] = []
    reached: list[tuple[RoomLabel, float]] = []
    overlapped: list[RoomLabel] = []
    for label in labels:
        candidates = by_storey.get(label.storey_index, ())
        if not candidates:
            unplaced.append(label)
            continue

        # Nearest wins, and among equally near ones the *smallest*.
        #
        # Zones overlap: a floor-wide or multi-unit zone sits over the unit
        # zones inside it, and a label inside both is at distance 0.0 from
        # each. Breaking that tie by list order -- which is what min() does on
        # its own -- hands every label to whichever zone Archicad happened to
        # list first. On a real project that put 30 rooms and four kitchens in
        # one apartment while leaving 18 apartments empty. The smallest
        # containing zone is the most specific one, which is the unit.
        home, gap, _ = min(
            (
                (zone, distance_to_polygon(label.point, zone.outline), polygon_area(zone.outline))
                for zone in candidates
            ),
            key=lambda item: (item[1], item[2]),
        )
        if gap > tolerance_m:
            unplaced.append(label)
            continue
        if gap > 0.0:
            reached.append((label, gap))
        if (
            gap == 0.0
            and sum(1 for zone in candidates if point_in_polygon(label.point, zone.outline)) > 1
        ):
            overlapped.append(label)
        found.setdefault(home.guid, []).append(label)

    # Separating the two kinds of miss is the whole diagnostic. Landing on a
    # storey with no apartments is what a master is *supposed* to do; landing
    # on a storey that has apartments and still matching none of them is a
    # placed room the join lost.
    live = {zone.storey_index for zone in zones}
    return RoomMatch(
        by_zone={guid: tuple(rooms) for guid, rooms in found.items()},
        unplaced=tuple(unplaced),
        zones_without_rooms=tuple(zone.guid for zone in zones if zone.guid not in found),
        missed_on_live_storeys=tuple(label for label in unplaced if label.storey_index in live),
        reached_for=tuple(reached),
        tolerance_m=tolerance_m,
        inside_several=tuple(overlapped),
        duplicated=_duplicates(found),
    )


#: Rooms a flat has exactly one of, so two of any of them means the zone spans
#: more than one dwelling. Bedrooms are absent on purpose: B1, B2 and B3 are
#: already distinct codes. ``B`` is here because it is the *bathroom* -- see
#: the note in ``ROOM_VOCABULARY`` -- and a flat has one, beside its ensuite.
UNIQUE_ROOM_CODES = frozenset(
    {"L/D", "L", "LIV", "LIVING", "LD", "L/K/D", "K", "K/D", "LY", "EN", "B"}
)


def _duplicates(found: dict[str, list[RoomLabel]]) -> tuple[tuple[str, str, int], ...]:
    """Apartments holding several of a room they should have one of."""
    repeated: list[tuple[str, str, int]] = []
    for guid, rooms in found.items():
        for code, count in _tally(rooms).items():
            if count > 1 and code in UNIQUE_ROOM_CODES:
                repeated.append((guid, code, count))
    return tuple(repeated)


def _tally(labels: Iterable[RoomLabel]) -> dict[str, int]:
    """Room codes counted, most common first."""
    counted: dict[str, int] = {}
    for label in labels:
        counted[label.code] = counted.get(label.code, 0) + 1
    return dict(sorted(counted.items(), key=lambda pair: -pair[1]))
