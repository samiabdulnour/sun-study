"""Drawing the per-instant series into a Worksheet, against a fake Archicad.

What these pin is the shape of the requests and the honesty of the report.
The behaviour that cannot be faked -- whether Archicad actually moves the
current database -- is measured in ``docs/archicad.md`` and recorded in D33.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from sun_study.archicad.connection import ArchicadConnection, ArchicadError
from sun_study.archicad.series import (
    PatchRow,
    activate,
    clear_database,
    draw_patch_series,
    find_worksheet,
    restore_after,
)
from tests.unit.test_archicad_adapter import FakeTransport, Sequential

WORKSHEET_TREE = {
    "navigatorItemTree": {
        "type": "ProjectItem",
        "name": "SAMPLE",
        "navigatorItemId": {"guid": "root"},
        "prefix": "",
        "children": [
            {
                "navigatorItem": {
                    "type": "FolderItem",
                    "name": "Worksheets",
                    "navigatorItemId": {"guid": "folder"},
                    "prefix": "",
                    "children": [
                        {
                            "navigatorItem": {
                                "type": "WorksheetDrawingItem",
                                "name": "Solar Penetration Outlines",
                                "navigatorItemId": {"guid": "ws-1"},
                                "prefix": "",
                            }
                        },
                        {
                            "navigatorItem": {
                                "type": "WorksheetDrawingItem",
                                "name": "CLIENT",
                                "navigatorItemId": {"guid": "ws-2"},
                                "prefix": "",
                            }
                        },
                    ],
                }
            }
        ],
    }
}


def connect(responses: dict[str, Any]) -> tuple[ArchicadConnection, FakeTransport]:
    transport = FakeTransport(
        {
            "GetAddOnVersion": {"version": "1.5.7"},
            "GetNavigatorItemTree": WORKSHEET_TREE,
            "GetDatabaseIdFromNavigatorItemId": {"databases": [{"databaseId": {"guid": "db-1"}}]},
            "ChangeWindow": {"success": True},
            **responses,
        }
    )
    return ArchicadConnection(transport), transport


def drawing_responses(**overrides: Any) -> dict[str, Any]:
    responses: dict[str, Any] = {
        "GetAttributesByType": {
            "attributes": [{"attributeId": {"guid": "l"}, "index": 7, "name": "Sun Study"}]
        },
        "GetLayers": {"layers": [{"name": "Sun Study", "isHidden": False, "isLocked": False}]},
        "GetElementsByType": {"elements": []},
        "GetDetailsOfElements": {"detailsOfElements": [{"layerIndex": 7}] * 4},
        "DeleteElements": {"success": True},
        "CreateHatches": {"elements": [{"elementId": {"guid": "h"}}] * 4000},
        "CreateTexts": {"elements": [{"elementId": {"guid": "t"}}] * 400},
    }
    responses.update(overrides)
    return responses


# A two-by-two grid of cells, one per level, so a row split is observable.
POSITIONS = np.array(
    [
        [0.0, 0.0, 3.0],
        [0.25, 0.0, 3.0],
        [0.0, 0.0, 6.0],
        [0.25, 0.0, 6.0],
    ]
)
SUNLIT = np.array([[True, False], [True, False], [False, True], [False, True]])


def test_a_worksheet_is_found_past_spacing_and_case() -> None:
    connection, _ = connect({})
    found = find_worksheet(connection, "  solar  penetration OUTLINES ")

    assert found.name == "Solar Penetration Outlines"
    assert found.database_id == "db-1"


def test_a_missing_worksheet_lists_the_ones_that_exist() -> None:
    """And says why the tool will not simply make one: a worksheet created
    through the API cannot be activated in the same session."""
    connection, _ = connect({})
    with pytest.raises(ArchicadError, match="Solar Penetration Outlines"):
        find_worksheet(connection, "Sun Study")


def test_activation_that_does_not_take_is_an_error_not_a_shrug() -> None:
    """Drawing after a failed switch would put the whole series on the floor
    plan of whatever storey happened to be open."""
    connection, _ = connect({"ChangeWindow": {"success": False}})
    with pytest.raises(ArchicadError, match="cannot be activated"):
        activate(connection, "db-1", "Worksheet")


def test_the_series_is_one_row_per_level_and_one_column_per_instant() -> None:
    connection, transport = connect(drawing_responses())
    rows = [
        PatchRow("RL 6.0", np.array([False, False, True, True])),
        PatchRow("RL 3.0", np.array([True, True, False, False])),
    ]

    report = draw_patch_series(
        connection,
        worksheet=find_worksheet(connection, "Solar Penetration Outlines"),
        positions=POSITIONS,
        sunlit=SUNLIT,
        times=["09:00", "15:00"],
        spacing_m=0.25,
        layer_name="Sun Study",
        rows=rows,
    )

    assert report.tiles == 4, "two levels by two instants"
    hatches = [
        h for call in transport.all_parameters_for("CreateHatches") for h in call["hatchesData"]
    ]
    assert all(h["layerIndex"] == 7 for h in hatches)
    assert all("floorInd" not in h for h in hatches), (
        "a worksheet has no storeys, and a floor index in one has destroyed elements before"
    )
    assert all(len(h["coordinates"]) == 4 for h in hatches), "rectangles, unclosed"
    assert all(h["showArea"] is False for h in hatches), (
        "a Fill inherits the tool default, and with Show Area Text on that is "
        "one square-metre figure printed across every cell of the patch"
    )

    texts = [t for call in transport.all_parameters_for("CreateTexts") for t in call["textsData"]]
    assert {t["text"] for t in texts} == {"09:00", "15:00", "RL 6.0", "RL 3.0"}


def test_the_lit_area_reported_is_summed_across_levels() -> None:
    """One tile per level means the same plan position appears once per level,
    and a merge over all of them at once would count that area once."""
    connection, _ = connect(drawing_responses())
    rows = [
        PatchRow("upper", np.array([False, False, True, True])),
        PatchRow("lower", np.array([True, True, False, False])),
    ]

    report = draw_patch_series(
        connection,
        worksheet=find_worksheet(connection, "Solar Penetration Outlines"),
        positions=POSITIONS,
        sunlit=SUNLIT,
        times=["09:00", "15:00"],
        spacing_m=0.25,
        layer_name="Sun Study",
        rows=rows,
    )

    # Two cells lit at each instant, each 0.25 m square, one level at a time.
    assert report.lit_area_m2 == pytest.approx((0.125, 0.125))


def test_without_rows_everything_is_drawn_as_one_tile_per_instant() -> None:
    connection, _ = connect(drawing_responses())
    report = draw_patch_series(
        connection,
        worksheet=find_worksheet(connection, "Solar Penetration Outlines"),
        positions=POSITIONS,
        sunlit=SUNLIT,
        times=["09:00", "15:00"],
        spacing_m=0.25,
        layer_name="Sun Study",
    )
    assert report.tiles == 2


def test_the_worksheet_is_emptied_first_and_the_run_says_so() -> None:
    """CreateTexts takes no layer, so a caption cannot be deleted by one and
    the whole database has to go. A person needs telling before they put
    anything of their own in there."""
    connection, transport = connect(
        drawing_responses(
            GetElementsByType=Sequential(
                {"elements": [{"elementId": {"guid": "old-hatch"}}]},
                {"elements": [{"elementId": {"guid": "old-text"}}]},
                {"elements": []},
            )
        )
    )

    report = draw_patch_series(
        connection,
        worksheet=find_worksheet(connection, "Solar Penetration Outlines"),
        positions=POSITIONS,
        sunlit=SUNLIT,
        times=["09:00"],
        spacing_m=0.25,
        layer_name="Sun Study",
    )

    assert report.cleared == 2
    deleted = transport.parameters_for("DeleteElements")["elements"]
    assert {e["elementId"]["guid"] for e in deleted} == {"old-hatch", "old-text"}
    assert "regenerated in full" in report.describe()


def test_drawing_no_instants_at_all_is_refused() -> None:
    connection, _ = connect(drawing_responses())
    with pytest.raises(ArchicadError, match="No instants"):
        draw_patch_series(
            connection,
            worksheet=find_worksheet(connection, "Solar Penetration Outlines"),
            positions=POSITIONS,
            sunlit=SUNLIT,
            times=[],
            spacing_m=0.25,
            layer_name="Sun Study",
        )


def test_clearing_a_database_takes_hatches_and_texts_together() -> None:
    connection, transport = connect(
        {
            "GetElementsByType": Sequential(
                {"elements": [{"elementId": {"guid": "a"}}, {"elementId": {"guid": "b"}}]},
                {"elements": [{"elementId": {"guid": "c"}}]},
                {"elements": []},
            ),
            "GetDetailsOfElements": {"detailsOfElements": [{"layerIndex": 7}] * 3},
            "GetAttributesByType": {
                "attributes": [{"attributeId": {"guid": "l"}, "index": 7, "name": "Sun Study"}]
            },
            "GetLayers": {
                "layers": [{"attributeId": {"guid": "l"}, "isHidden": False, "isLocked": False}]
            },
            "DeleteElements": {"success": True},
        }
    )
    deleted, left = clear_database(connection)
    assert (deleted, left) == (3, 0)
    assert transport.commands().count("DeleteElements") == 1, "one request, not one per element"


def test_a_restore_that_archicad_ignores_is_reported_as_such() -> None:
    """AC26 answers 'success' and leaves the worksheet on screen. Believing it
    would have the run tell people they are somewhere they are not."""
    connection, _ = connect({"GetCurrentWindowType": {"currentWindowType": "Worksheet"}})
    assert restore_after(connection, "db-1") is False

    connection, _ = connect({"GetCurrentWindowType": {"currentWindowType": "FloorPlan"}})
    assert restore_after(connection, "db-1") is True


# ---------------------------------------------------------------------------
# Sheet placement. A drawing's size comes from its view's extent at the
# drawing's scale, because a layout made in this session cannot be measured.
# ---------------------------------------------------------------------------
def test_drawings_are_placed_in_metres_not_millimetres() -> None:
    """A Drawing's position is in metres while the page is described in
    millimetres. Mixing them put a drawing meant for x = 200 mm at x = 200 m,
    a quarter of a kilometre off a sheet 0.841 m wide."""
    from sun_study.archicad.layout import LayoutSheet, sheet_positions

    sheet = LayoutSheet(width_mm=841.0, height_mm=594.0)
    # 53 x 37 m at 1:200 is 0.266 x 0.185 m on the sheet.
    positions = sheet_positions(sheet, 6, (0.266, 0.185))

    assert len(positions) == 6
    assert all(0 < x < 0.841 and 0 < y < 0.594 for x, y in positions), (
        "every centre must land inside a sheet measured in metres"
    )
    assert len({round(y, 4) for _, y in positions}) == 2, "six of that size make two rows"
    assert len({round(x, 4) for x, _ in positions}) == 3, "three across"


def test_a_block_of_drawings_is_centred_on_the_sheet() -> None:
    from sun_study.archicad.layout import LayoutSheet, sheet_positions

    sheet = LayoutSheet(width_mm=841.0, height_mm=594.0)
    positions = sheet_positions(sheet, 2, (0.266, 0.185))

    middle = sum(x for x, _ in positions) / len(positions)
    assert middle == pytest.approx(0.841 / 2, abs=0.001), (
        "two small drawings must not bunch into a corner of an A1"
    )


def test_no_drawings_is_no_positions() -> None:
    from sun_study.archicad.layout import LayoutSheet, sheet_positions

    assert sheet_positions(LayoutSheet(841.0, 594.0), 0, (0.1, 0.1)) == []


def test_elements_left_behind_by_a_refused_delete_are_reported() -> None:
    """Archicad answers ``success`` and deletes nothing when the elements are
    on a hidden layer, which is the ordinary case for a layer this tool made.
    A worksheet on the reference project reached 15,889 fills that way, each
    run reporting that it had cleared the last."""
    connection, _ = connect(
        {
            # Three found, and three still there afterwards: nothing went.
            "GetElementsByType": {"elements": [{"elementId": {"guid": "a"}}]},
            "GetDetailsOfElements": {"detailsOfElements": [{"layerIndex": 7}]},
            "GetAttributesByType": {
                "attributes": [{"attributeId": {"guid": "l"}, "index": 7, "name": "Sun Study"}]
            },
            "GetLayers": {
                "layers": [{"attributeId": {"guid": "l"}, "isHidden": False, "isLocked": False}]
            },
            "DeleteElements": {"success": True},
        }
    )
    deleted, left = clear_database(connection)

    assert deleted == 0, "nothing actually went"
    assert left == 3, "and the run must say so rather than claim a clean sheet"
