"""How a series of drawings is dealt onto layouts, the same way for every tool.

Three modes, one rule each, and the one thing that matters about the second
sheet of a split: it is laid out on the first sheet's grid, so the two read
alike.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from sun_study.archicad import naming
from sun_study.archicad.layout import LayoutSheet, remove_stale_layouts, tile_positions
from sun_study.archicad.shadows import sheet_name
from sun_study.archicad.sheeting import (
    DEFAULT_SHEET_MODE,
    cells_for,
    parse_sheet_mode,
    sheet_chunks,
    sheet_title,
)
from sun_study.archicad.sheets import TableRow
from sun_study.cli import _sheet_table
from tests.unit.test_archicad_adapter import connect


def test_the_default_is_a_morning_and_an_afternoon() -> None:
    assert DEFAULT_SHEET_MODE == "two"
    assert parse_sheet_mode(None) == "two" and parse_sheet_mode("  ") == "two"


@pytest.mark.parametrize(
    ("said", "mode"),
    [
        ("one", "one"),
        ("1", "one"),
        ("all", "one"),
        ("Two", "two"),
        ("2", "two"),
        ("half", "two"),
        ("each", "each"),
        ("per drawing", "each"),
        ("EVERY", "each"),
    ],
)
def test_every_way_people_say_a_mode_reaches_the_same_word(said: str, mode: str) -> None:
    assert parse_sheet_mode(said) == mode


def test_a_mode_that_is_none_of_those_is_refused_with_the_three_words() -> None:
    with pytest.raises(ValueError, match="one, two or each"):
        parse_sheet_mode("three")


def test_seven_drawings_are_one_sheet_or_four_and_three_or_seven() -> None:
    assert sheet_chunks(7, "one") == [(0, 7)]
    assert sheet_chunks(7, "two") == [(0, 4), (4, 7)]
    assert sheet_chunks(7, "each") == [(i, i + 1) for i in range(7)]


def test_nine_drawings_split_five_and_four_and_the_grid_is_five() -> None:
    """8am to 4pm. The first sheet takes the larger half; both are laid out
    as a sheet of five, so the second has one empty cell."""
    chunks = sheet_chunks(9, "two")
    assert chunks == [(0, 5), (5, 9)]
    assert cells_for(chunks) == 5


def test_fewer_than_two_drawings_is_one_sheet_whatever_the_mode() -> None:
    assert sheet_chunks(1, "two") == [(0, 1)]
    assert sheet_chunks(1, "each") == [(0, 1)]
    assert sheet_chunks(0, "one") == [] and cells_for([]) == 0


def test_a_sheet_is_titled_by_the_hours_on_it() -> None:
    assert sheet_title(["09:00", "10:00", "11:00", "12:00"]) == "09:00-12:00"
    assert sheet_title(["15:00"]) == "15:00"
    assert sheet_title([]) == ""


def test_a_short_sheet_takes_the_first_cells_of_the_fuller_sheets_grid() -> None:
    """Three drawings laid out as four sit in the two-by-two's first three
    cells, at the two-by-two's size, not in a row of three at another."""
    sheet = LayoutSheet(841.0, 594.0)
    tile = (0.2, 0.15)
    full = tile_positions(sheet, 4, tile)
    short = tile_positions(sheet, 3, tile)
    assert full.columns == 2 and full.rows == 2
    assert short.columns == 3, "on its own, three would be a row"
    assert full.positions[:3] != short.positions[:3]
    # Which is why the builders ask for the grid of the fuller sheet.


def test_shadow_sheets_are_named_for_the_day_the_part_or_the_hour() -> None:
    naming.set_prefix("14 |")
    assert sheet_name("JUNE 21", 1, 1) == "14 | Shadow Diagrams - JUNE 21"
    assert sheet_name("JUNE 21", 2, 2, ["1PM", "2PM", "3PM"]) == (
        "14 | Shadow Diagrams - JUNE 21 (2 of 2)"
    )
    assert sheet_name("JUNE 21", 3, 7, ["11AM"]) == "14 | Shadow Diagrams - JUNE 21 11AM"


def test_a_sheet_of_several_instants_stacks_their_tables_under_headings() -> None:
    """The figures are per instant. On a sheet carrying three, three tables
    would overwrite each other, so they are one table with a heading each."""
    tables: dict[str, Sequence[TableRow]] = {
        "09:00": [TableRow("lit", 10.0, 0.5)],
        "12:00": [TableRow("lit", 20.0, 0.9)],
        "15:00": [],
    }
    titles = {"09:00": "Sun at 09:00", "12:00": "Sun at 12:00"}
    rows, title = _sheet_table(["09:00", "12:00", "15:00"], tables, titles, None, "", "09:00-15:00")
    assert title == "09:00-15:00"
    assert rows is not None
    assert [(r.label, r.heading) for r in rows] == [
        ("Sun at 09:00", True),
        ("lit", False),
        ("Sun at 12:00", True),
        ("lit", False),
    ]
    # One instant keeps its own table and title, as before.
    rows, title = _sheet_table(["12:00"], tables, titles, None, "", "12:00")
    assert title == "Sun at 12:00" and rows == tables["12:00"]
    # The whole-day table wins wherever it is given.
    day = [TableRow("day", 1.0, 1.0)]
    assert _sheet_table(["09:00", "12:00"], tables, titles, day, "The day", "x") == (
        day,
        "The day",
    )


def test_a_sheet_from_an_earlier_mode_is_removed_and_the_current_ones_kept() -> None:
    """A split run left ``(1 of 2)`` and ``(2 of 2)``; this run makes one
    sheet. The two go, the one stays, another tool's sheet is not touched."""

    def item(name: str, guid: str) -> dict[str, object]:
        return {
            "navigatorItem": {
                "name": name,
                "type": "LayoutItem",
                "navigatorItemId": {"guid": guid},
            }
        }

    connection, transport = connect(
        {
            "GetNavigatorItemTree": {
                "navigatorItemTree": {
                    "type": "BookItem",
                    "name": "Layout Book",
                    "children": [
                        item("14 | Shadow Diagrams - JUNE 21 (1 of 2)", "A"),
                        item("14 | Shadow Diagrams - JUNE 21 (2 of 2)", "B"),
                        item("14 | Shadow Diagrams - JUNE 21", "C"),
                        item("14 | Sun Views", "D"),
                    ],
                }
            },
            "DeleteNavigatorItems": {"success": True},
        }
    )
    gone = remove_stale_layouts(
        connection, "14 | Shadow Diagrams - ", ["14 | Shadow Diagrams - JUNE 21"]
    )
    assert gone == [
        "14 | Shadow Diagrams - JUNE 21 (1 of 2)",
        "14 | Shadow Diagrams - JUNE 21 (2 of 2)",
    ]
    deleted = transport.parameters_for("DeleteNavigatorItems")["navigatorItemIds"]
    assert [d["navigatorItemId"]["guid"] for d in deleted] == ["A", "B"]
