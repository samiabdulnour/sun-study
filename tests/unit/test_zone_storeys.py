"""Which storey a communal Zone's fills are drawn on.

Found on a real project. Three communal open spaces sat at 98 m, 108 m and
127 m up a tower -- sky terraces, one per level -- and every fill for all three
landed on one plan, because the caller took the first measured Zone's storey
and named it for the lot. Two thirds of the drawing was on a floor the space
is not on, and the sheets still looked right.

A Zone knows its own storey. The only question is whether the drawing asks.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from sun_study.archicad.connection import ArchicadConnection
from sun_study.archicad.draw import BandStyle
from sun_study.archicad.penetration import CellGroup, draw_cell_groups
from sun_study.archicad.read import ArchicadZone

#: Two terraces, one above the other, each a 2 m square of cells. Square so
#: the fitted frame is exact and the residual guard cannot be what fails.
LOWER = ArchicadZone(
    guid="guid-lower",
    name="COMMUNAL OPEN SPACE",
    number="",
    storey_index=8,
    outline=((0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)),
)
UPPER = ArchicadZone(
    guid="guid-upper",
    name="COMMUNAL OPEN SPACE",
    number="",
    storey_index=14,
    outline=((10.0, 0.0), (12.0, 0.0), (12.0, 2.0), (10.0, 2.0)),
)


class DrawingTransport:
    """Answers everything ``draw_cell_groups`` asks, and keeps the hatches."""

    def __init__(self) -> None:
        self.hatches: list[dict[str, Any]] = []

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["command"] != "API.ExecuteAddOnCommand":
            return {"succeeded": True, "result": {}}
        name = payload["parameters"]["addOnCommandId"]["commandName"]
        given = payload["parameters"].get("addOnCommandParameters") or {}
        return {"succeeded": True, "result": {"addOnCommandResponse": self._answer(name, given)}}

    def _answer(self, command: str, given: dict[str, Any]) -> dict[str, Any]:
        if command == "GetAddOnVersion":
            return {"version": "1.5.7"}
        if command == "GetAttributesByType":
            if given.get("attributeType") == "Layer":
                return {
                    "attributes": [{"attributeId": {"guid": "L"}, "name": "14 | X", "index": 7}]
                }
            return {"attributes": []}
        if command == "GetLayers":
            return {"layers": [{"name": "14 | X", "isHidden": False, "isLocked": False}]}
        if command == "CreateHatches":
            self.hatches.extend(given["hatchesData"])
            return {"elements": [{"elementId": {"guid": f"h{n}"}} for n in given["hatchesData"]]}
        if command == "GetElementsByType":
            return {"elements": []}
        if command == "CreateTexts":
            return {"elements": [{"guid": "t"} for _ in (given.get("texts") or given["textsData"])]}
        if command == "GetAllProperties":
            return {"properties": []}
        if command in ("CreateGroups",):
            return {"groupGuids": []}
        if command in ("CreateLayers", "ChangeSelectionOfElements", "MoveElements"):
            return {"attributeIds": [], "executionResults": []}
        return {}


def draw(on_storey: int | None) -> list[dict[str, Any]]:
    """Draw one band over both terraces and hand back the hatches."""
    transport = DrawingTransport()
    connection = ArchicadConnection(transport)

    # Four cells per terrace, at the Zones' own coordinates, so the fitted
    # frame is the identity and only the storey is under test.
    positions = np.array(
        [[0.5, 0.5, 0.0], [1.5, 0.5, 0.0], [10.5, 0.5, 0.0], [11.5, 0.5, 0.0]],
        dtype=np.float64,
    )
    parents = ["ifc-lower", "ifc-lower", "ifc-upper", "ifc-upper"]

    draw_cell_groups(
        connection,
        groups=[
            CellGroup(
                label="in sun",
                mask=np.ones(4, dtype=bool),
                style=BandStyle("in sun", float("inf"), fill_pen=1),
                area_m2=4.0,
                share=1.0,
            )
        ],
        positions=positions,
        parent_ids=parents,
        spacing_m=1.0,
        zone_by_apartment={"ifc-lower": "guid-lower", "ifc-upper": "guid-upper"},
        zones=[LOWER, UPPER],
        export_extents={
            "ifc-lower": np.array([[0.0, 0.0, 0.0], [2.0, 2.0, 0.0]]),
            "ifc-upper": np.array([[10.0, 0.0, 0.0], [12.0, 2.0, 0.0]]),
        },
        layer_name="14 | X",
        on_storey=on_storey,
    )
    return transport.hatches


def within(hatch: dict[str, Any], x0: float, x1: float) -> bool:
    """Whether every corner of a hatch lies in that band of x.

    The legend swatches are drawn beside the plan and carry a storey of their
    own, so "has a floorInd" does not separate a patch from a legend key. Where
    the shape *is* does: the two terraces are 8 m apart and the legend is
    outside both.
    """
    return all(x0 <= corner["x"] <= x1 for corner in hatch["coordinates"])


def test_each_zone_is_drawn_on_its_own_storey() -> None:
    """The bug. Two terraces on different levels, and both sets of fills went
    to whichever storey the first one happened to be on."""
    hatches = draw(on_storey=None)
    lower = [h for h in hatches if within(h, 0.0, 2.0)]
    upper = [h for h in hatches if within(h, 10.0, 12.0)]

    assert lower and upper, "both terraces drew something"
    assert {h["floorInd"] for h in lower} == {8}, "the lower terrace is on 8"
    assert {h["floorInd"] for h in upper} == {14}, "and the upper one on 14"


def test_the_cells_of_one_zone_do_not_stray_onto_another_storey() -> None:
    """The old behaviour put every fill on one storey, so the count of
    distinct storeys was 1. Asserting it is now 2 would also pass if the two
    terraces had simply swapped, which is why each is checked by name."""
    hatches = draw(on_storey=None)
    patches = [h for h in hatches if within(h, 0.0, 2.0) or within(h, 10.0, 12.0)]

    assert len(patches) >= 2
    for hatch in patches:
        expected = 8 if within(hatch, 0.0, 2.0) else 14
        assert hatch["floorInd"] == expected


def test_a_named_storey_still_puts_everything_on_it() -> None:
    """The other path is not a mistake -- open ground belongs to no Zone and
    the storey it appears on is the one whose level it sits at. It just must
    not be chosen for Zones that know their own."""
    hatches = draw(on_storey=3)
    patches = [h for h in hatches if within(h, 0.0, 2.0) or within(h, 10.0, 12.0)]

    assert patches
    assert {h["floorInd"] for h in patches} == {3}


# -- choosing the storey, which is where the bug actually was --------------


def test_zones_sharing_a_storey_are_forced_onto_it() -> None:
    from sun_study.cli import _zone_storeys

    assert _zone_storeys([8, 8, 8]) == (8, [8])


def test_zones_on_different_storeys_are_left_to_answer_for_themselves() -> None:
    """``None`` selects the per-Zone path in ``draw_cell_groups``. The old rule
    took the first Zone's storey and named it for all three."""
    shared, storeys = _levels([8, 14, 20])

    assert shared is None, "no single storey may be forced"
    assert storeys == [8, 14, 20], "and every one of them gets a sheet"


def test_a_storey_archicad_would_not_report_decides_nothing() -> None:
    """A Zone that cannot say where it is must not answer for Zones that can,
    and it cannot contribute a plan that does not exist."""
    assert _levels([None]) == (None, [])
    assert _levels([8, None]) == (None, [8])


def test_the_sheets_follow_every_storey_not_just_the_shared_one() -> None:
    """These were tied together once, so the moment the Zones stopped sharing
    a storey -- the case the first answer exists for -- the fills drew on three
    levels and nothing was put on paper."""
    shared, storeys = _levels([14, 8])

    assert shared is None
    assert storeys == [8, 14], "sorted, so the sheets come out in level order"


def _levels(levels: list[int | None]) -> tuple[int | None, list[int]]:
    from sun_study.cli import _zone_storeys

    return _zone_storeys(levels)


# -- one sheet per storey, carrying every hour -----------------------------


def test_the_clock_times_and_the_whole_day_plans_are_grouped_differently() -> None:
    """A time is one of a series and is read against the others; the banded
    plan and the threshold plan each answer a question on their own and carry
    a legend of their own. Splitting them is what lets the seven hours share
    one A1 without the other two joining them."""
    from sun_study.cli import _CLOCK

    made = ["Communal", "Communal 2h", "Communal 09:00", "Communal 12:00", "Communal 15:00"]
    times = [name for name in made if _CLOCK.search(name)]
    whole_day = [name for name in made if not _CLOCK.search(name)]

    assert times == ["Communal 09:00", "Communal 12:00", "Communal 15:00"]
    assert whole_day == ["Communal", "Communal 2h"]


def test_a_sheet_is_named_for_the_storey_it_shows() -> None:
    """The practice's own name for that level, because that is what a reader
    looking for it will look for. The index is an Archicad detail that matches
    no label on any other drawing."""
    from sun_study.archicad.layout import NavigatorItem
    from sun_study.cli import _storey_label

    named = NavigatorItem(identifier="x", name="LEVEL 08", kind="StoryItem", prefix="8")
    assert _storey_label(named, 8) == "LEVEL 08"


def test_a_storey_with_no_name_falls_back_to_its_index() -> None:
    from sun_study.archicad.layout import NavigatorItem
    from sun_study.cli import _storey_label

    blank = NavigatorItem(identifier="x", name="   ", kind="StoryItem", prefix="8")
    assert _storey_label(blank, 8) == "Level 8"
    assert _storey_label(None, 14) == "Level 14"
