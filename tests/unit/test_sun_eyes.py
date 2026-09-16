"""Sun eye views: the frame turn, the settings, and the order of the calls.

The numbers are the ones measured on the Kogarah solar study on 10 September
2026 and recorded in ``docs/addon.md``: a project whose +Y sits at true
bearing 319.052, and the office's own hand-aimed 9am view read back at
83.372 in that frame against 83.51 for the sun this tool computes.
"""

from __future__ import annotations

import datetime as dt
import math

import pytest

from sun_study.archicad import naming
from sun_study.archicad.layout import LayoutSheet
from sun_study.archicad.read import GeoLocation
from sun_study.archicad.sun_eyes import (
    DOCUMENT_SCALE,
    SunEyeSettings,
    make_sun_eye_views,
    sheet_cells_for,
    sheet_groups,
    sheet_positions_for,
    sun_eyes,
)
from sun_study.archicad.views import StoreyView
from tests.unit.test_archicad_adapter import connect

KOGARAH = GeoLocation(
    latitude_deg=-33.866667,
    longitude_deg=151.216667,
    altitude_m=0.0,
    north_radians=math.radians(49.052),
)
MIDWINTER = dt.date(2026, 6, 21)
SS = naming.prefix()


def test_seven_hours_in_the_project_frame_match_the_recorded_table() -> None:
    eyes = sun_eyes(KOGARAH, date=MIDWINTER, hours=range(9, 16), timezone="Australia/Sydney")
    assert [eye.when.hour for eye in eyes] == [9, 10, 11, 12, 13, 14, 15]

    # docs/addon.md, "The seven instants a study draws".
    expected = [
        (19.0, 42.6, 83.5),
        (26.3, 30.0, 70.9),
        (31.1, 15.3, 56.2),
        (32.7, 359.1, 40.1),
        (30.8, 343.1, 24.1),
        (25.7, 328.6, 9.6),
        (18.1, 316.3, 357.2),
    ]
    for eye, (altitude, true_bearing, project_bearing) in zip(eyes, expected, strict=True):
        assert eye.altitude_deg == pytest.approx(altitude, abs=0.1)
        assert eye.true_bearing_deg == pytest.approx(true_bearing, abs=0.1)
        assert eye.project_bearing_deg == pytest.approx(project_bearing, abs=0.1)


def test_the_turn_is_the_projects_north_angle() -> None:
    eyes = sun_eyes(KOGARAH, date=MIDWINTER, hours=[9], timezone="Australia/Sydney")
    turn = (eyes[0].project_bearing_deg - eyes[0].true_bearing_deg) % 360.0
    # +Y at 319.052 true, so a true bearing gains 360 - 319.052 in the frame.
    assert turn == pytest.approx(360.0 - 319.052, abs=1e-6)


def test_hours_with_the_sun_down_are_left_out() -> None:
    eyes = sun_eyes(KOGARAH, date=MIDWINTER, hours=[3, 12, 22], timezone="Australia/Sydney")
    assert [eye.when.hour for eye in eyes] == [12]


def test_the_sun_is_handed_to_archicad_as_a_date() -> None:
    (eye,) = sun_eyes(KOGARAH, date=MIDWINTER, hours=[9], timezone="Australia/Sydney")
    assert eye.sun_for_archicad() == {
        "year": 2026,
        "month": 6,
        "day": 21,
        "hour": 9,
        "minute": 0,
        "second": 0,
        "summerTime": False,
    }
    assert eye.label == "21 Jun 09:00"
    assert eye.stamp == "0900"


def test_view_settings_differ_between_window_and_document() -> None:
    settings = SunEyeSettings(
        layer_combination=f"{SS} Sun Views",
        renovation_filter_guid="38FD3426-70C5-9C4D-ACA5-877D4A623A36",
        pen_set="00 FA Pens",
    )
    of_window = settings.for_view(of_document=False)
    of_document = settings.for_view(of_document=True)

    assert of_window["graphicOverrideCombination"] == "Sun Eye Views"
    assert of_window["renovationFilterGuid"] == {"guid": "38FD3426-70C5-9C4D-ACA5-877D4A623A36"}
    assert of_window["drawingScale"] == 1
    assert of_window["d3styleName"] == "OpenGL Shading with Contours with Shadows"

    # A 3D style belongs to a view of the window, not to a document.
    assert "d3styleName" not in of_document
    assert of_document["drawingScale"] == int(DOCUMENT_SCALE)
    assert of_document["layerCombination"] == of_window["layerCombination"]


def test_each_view_is_aimed_before_it_is_saved() -> None:
    connection, transport = connect(
        {
            "GetNavigatorItemTree": {
                "navigatorItemTree": {"rootItem": {"name": "root", "children": []}}
            },
            "CreateViewMapFolder": {"navigatorItemId": {"guid": "FOLDER"}},
            "SetProjection": {"success": True},
            "CreateViewsInViewMap": {"navigatorItems": [{"navigatorItemId": {"guid": "VIEW"}}]},
            "SetViewSettings": {"executionResults": [{"success": True}, {"success": True}]},
        }
    )
    # The Project Map, asked separately by three_d_sources through the same
    # command: the fake answers the same tree, so give it a 3D window item.
    transport.responses["GetNavigatorItemTree"] = {
        "navigatorItemTree": {
            "rootItem": {
                "name": "root",
                "children": [
                    {
                        "navigatorItem": {
                            "name": "Generic Axonometry",
                            "type": "AxonometryItem",
                            "navigatorItemId": {"guid": "AXO"},
                        }
                    }
                ],
            }
        }
    }
    eyes = sun_eyes(KOGARAH, date=MIDWINTER, hours=[9, 15], timezone="Australia/Sydney")
    settings = SunEyeSettings(layer_combination=f"{SS} Sun Views")

    made = make_sun_eye_views(connection, eyes, settings=settings)

    assert [view.name for _, view, _ in made] == [
        f"{SS} Sun View 21 Jun 09:00",
        f"{SS} Sun View 21 Jun 15:00",
    ]
    assert [reused for _, _, reused in made] == [False, False]

    aims = transport.all_parameters_for("SetProjection")
    assert [round(a["viewAzimuth"], 1) for a in aims] == [83.5, 357.2]
    assert [a["sun"]["hour"] for a in aims] == [9, 15]

    # Aim, save, aim, save -- never two aims and then two saves.
    order = [c for c in transport.commands() if c in ("SetProjection", "CreateViewsInViewMap")]
    assert order == ["SetProjection", "CreateViewsInViewMap"] * 2

    saved = transport.all_parameters_for("CreateViewsInViewMap")
    assert all(s["viewsData"][0]["navigatorItemId"] == {"guid": "AXO"} for s in saved)
    assert all(s["viewsData"][0]["parentNavigatorItemId"] == {"guid": "FOLDER"} for s in saved)

    pinned = transport.parameters_for("SetViewSettings")["navigatorItemIdsWithViewSettings"]
    assert [p["viewSettings"]["graphicOverrideCombination"] for p in pinned] == [
        "Sun Eye Views"
    ] * 2


def test_seven_documents_become_a_morning_sheet_and_an_afternoon_sheet() -> None:
    eyes = sun_eyes(KOGARAH, date=MIDWINTER, hours=range(9, 16), timezone="Australia/Sydney")
    made = [(eye, StoreyView(0, f"doc {eye.stamp}", f"ID{eye.stamp}"), False) for eye in eyes]

    groups = sheet_groups(made, sheets="two")

    assert [name for name, _ in groups] == [
        f"{SS} Sun Views 09:00-12:00",
        f"{SS} Sun Views 13:00-15:00",
    ]
    assert [[i for i, _ in views] for _, views in groups] == [
        ["ID0900", "ID1000", "ID1100", "ID1200"],
        ["ID1300", "ID1400", "ID1500"],
    ]


def test_one_sheet_keeps_the_plain_name_and_a_sheet_each_is_named_for_its_hour() -> None:
    eyes = sun_eyes(KOGARAH, date=MIDWINTER, hours=[9, 12], timezone="Australia/Sydney")
    made = [(eye, StoreyView(0, "doc", f"ID{eye.stamp}"), False) for eye in eyes]
    assert [name for name, _ in sheet_groups(made, sheets="one")] == [f"{SS} Sun Views"]
    assert [name for name, _ in sheet_groups(made, sheets="each")] == [
        f"{SS} Sun Views 09:00",
        f"{SS} Sun Views 12:00",
    ]
    # Two drawings in two: one each, named for the hour, not "09:00-09:00".
    assert [name for name, _ in sheet_groups(made, sheets="two")] == [
        f"{SS} Sun Views 09:00",
        f"{SS} Sun Views 12:00",
    ]


def test_nine_hours_in_two_is_five_and_four() -> None:
    """8am to 4pm, which some councils ask for: the first sheet takes the
    larger half, and both are laid out on a grid of five."""
    eyes = sun_eyes(KOGARAH, date=MIDWINTER, hours=range(8, 17), timezone="Australia/Sydney")
    made = [(eye, StoreyView(0, f"doc {eye.stamp}", f"ID{eye.stamp}"), False) for eye in eyes]
    groups = sheet_groups(made, sheets="two")
    assert [name for name, _ in groups] == [
        f"{SS} Sun Views 08:00-12:00",
        f"{SS} Sun Views 13:00-16:00",
    ]
    assert [len(views) for _, views in groups] == [5, 4]


B1 = LayoutSheet(width_mm=1000.0, height_mm=707.0)


def test_four_drawings_sit_two_by_two_clear_of_the_title_block() -> None:
    positions = sheet_positions_for(B1, 4, title_block_mm=100.0)
    # 900 mm of usable width in two 450 mm columns; rows read from the top,
    # and layout y runs upward, so the first row is the higher one.
    assert [(round(x * 1000), round(y * 1000)) for x, y in positions] == [
        (225, 530),
        (675, 530),
        (225, 177),
        (675, 177),
    ]


def test_a_short_sheet_keeps_the_full_sheets_grid() -> None:
    # Three afternoon drawings take the first three cells of the same
    # two-by-two the morning sheet uses; the grid is sized for a full sheet.
    full = sheet_cells_for(B1, 4, title_block_mm=100.0)
    assert full[:3] == sheet_cells_for(B1, 4, title_block_mm=100.0)[:3]
    assert len(sheet_cells_for(B1, 3, title_block_mm=100.0)) == 3


def test_cells_are_the_grid_less_a_gap_for_the_title() -> None:
    cells = sheet_cells_for(B1, 4, title_block_mm=100.0, gap_mm=20.0)
    x0, y0, x1, y1 = (round(v * 1000) for v in cells[0])
    # First cell: top-left, 450 x 353.5 mm, inset 20 mm on every side.
    assert (x0, y0, x1, y1) == (20, 374, 430, 687)
    # Every cell the same size.
    sizes = {(round((c[2] - c[0]) * 1000), round((c[3] - c[1]) * 1000)) for c in cells}
    assert sizes == {(410, 314)}


def test_the_storey_filter_comes_off_before_any_view_is_made() -> None:
    """A saved 3D view keeps the filter it was made under, so a window left on
    one storey bakes half a model into every sun view -- the context is homed
    on the storey nearest zero and the building stands on the ground storey
    and above, and one storey can never show both (D98)."""
    from sun_study.archicad.sun_eyes import show_every_storey

    connection, transport = connect({"Set3DFilter": {"success": True, "allStories": True}})

    assert show_every_storey(connection) == "", "nothing to report when it comes off"
    assert transport.parameters_for("Set3DFilter") == {"allStories": True}


def test_an_older_add_on_without_the_command_is_said_and_not_fatal() -> None:
    """The views were made this way until the command existed, and they are
    still worth making. What is not acceptable is making them quietly: a view
    carrying half the model looks exactly like one carrying all of it."""
    from sun_study.archicad.sun_eyes import show_every_storey

    connection, _ = connect(
        {
            "Set3DFilter": {
                "error": {"code": 4010, "message": "does not have the registered Add-On command"}
            }
        }
    )

    trouble = show_every_storey(connection)
    assert "no Set3DFilter" in trouble
    assert "half the model" in trouble


def test_a_filter_that_will_not_come_off_is_reported_rather_than_assumed() -> None:
    """Read back rather than believed, for the same reason the add-on reads it
    back: a refused field reported as set is a sun view that is wrong and says
    it is right."""
    from sun_study.archicad.sun_eyes import show_every_storey

    connection, _ = connect({"Set3DFilter": {"success": True, "allStories": False}})

    assert "would not come off" in show_every_storey(connection)


def test_the_documents_already_there_have_their_own_filter_taken_off() -> None:
    """Setting the window is not enough. API_DocumentFrom3DType carries an
    API_3DFilterAndCutSettings of its own, so a document made while the window
    was filtered goes on converting those storeys for ever -- and it can be
    neither re-aimed nor deleted through the API, so mending it in place is
    the only mend there is (D98)."""
    from sun_study.archicad.sun_eyes import mend_document_filters

    tree = {
        "navigatorItemTree": {
            "name": "root",
            "children": [
                {
                    "navigatorItem": {
                        "type": "DocumentFrom3DItem",
                        "name": f"{naming.prefix()} Sun View Document 21 Jun 09:00",
                        "navigatorItemId": {"guid": "DOC"},
                        "children": [],
                    }
                }
            ],
        }
    }
    connection, transport = connect(
        {
            "GetNavigatorItemTree": tree,
            "GetDatabaseIdFromNavigatorItemId": {"databases": [{"databaseId": {"guid": "DB"}}]},
            "Set3DFilter": {"success": True, "allStories": True},
        }
    )

    mended, looked, said = mend_document_filters(connection)

    assert (mended, looked, said) == (1, 1, "")
    sent = transport.parameters_for("Set3DFilter")
    assert sent["allStories"] is True
    assert sent["databaseId"] == {"guid": "DB"}, "the document, not the window"


def test_a_document_that_keeps_its_filter_is_counted_and_said() -> None:
    """A refused document is a sun view that shows part of the model and looks
    exactly like one that shows all of it."""
    from sun_study.archicad.sun_eyes import mend_document_filters

    tree = {
        "navigatorItemTree": {
            "name": "root",
            "children": [
                {
                    "navigatorItem": {
                        "type": "DocumentFrom3DItem",
                        "name": f"{naming.prefix()} Sun View Document 21 Jun 09:00",
                        "navigatorItemId": {"guid": "DOC"},
                        "children": [],
                    }
                }
            ],
        }
    }
    connection, _ = connect(
        {
            "GetNavigatorItemTree": tree,
            "GetDatabaseIdFromNavigatorItemId": {"databases": [{"databaseId": {"guid": "DB"}}]},
            "Set3DFilter": {"success": True, "allStories": False},
        }
    )

    mended, looked, said = mend_document_filters(connection)

    assert (mended, looked) == (0, 1)
    assert "kept their storey filter" in said


def test_the_documents_just_made_are_mended_after_they_are_made(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A document carries its own copy of the storey filter from birth (D98).

    `mend_document_filters` ran once, before `make_sun_eye_documents` had made
    anything, so it only ever reached documents left by earlier runs. Whatever
    filter `CreateDocumentFrom3D` gave the new ones stayed on them -- and those
    are the databases `make_sun_eye_sheets` places, so the sheet came out with
    half the model in it and said nothing. Reported 16 September 2026: views
    that come out filtered to the 0.AHD storey however the window is set.

    A document can be neither re-aimed nor deleted through the API, so mending
    in place afterwards is the only mend there is.

    The order is the test, not the call: a mend that runs only before the
    documents exist passes any "was it mended" assertion and ships the same
    wrong sheet.
    """
    import datetime as dt

    from typer.testing import CliRunner

    from sun_study import cli
    from sun_study.archicad.read import GeoLocation
    from sun_study.archicad.sun_eyes import SunEye

    order: list[str] = []

    class Connection:
        def run_tapir(self, command: str, parameters: object = None) -> dict[str, object]:
            if command == "GetCurrentWindowType":
                return {"currentWindowType": "FloorPlan"}
            return {}

    def mend(_connection: object) -> tuple[int, int, str]:
        order.append("mend")
        return 1, 1, ""

    def documents(*args: object, **kwargs: object) -> list[object]:
        order.append("make_documents")
        return []

    noon = dt.datetime(2026, 6, 21, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=10)))
    eye = SunEye(when=noon, true_bearing_deg=0.0, project_bearing_deg=0.0, altitude_deg=32.0)

    monkeypatch.setattr(cli, "_connect", lambda *a, **k: Connection())
    monkeypatch.setattr(cli, "show_every_storey", lambda *a, **k: "")
    monkeypatch.setattr(cli, "mend_document_filters", mend)
    monkeypatch.setattr(cli, "remove_previous", lambda *a, **k: (0, 0))
    monkeypatch.setattr(
        cli, "read_geo_location", lambda *a, **k: GeoLocation(-33.9, 151.2, 37.0, 0.0)
    )
    monkeypatch.setattr(cli, "sun_eyes", lambda *a, **k: [eye])
    monkeypatch.setattr(cli, "sun_eye_layer_combination", lambda *a, **k: ("combo", []))
    monkeypatch.setattr(cli, "planned_renovation_filter", lambda *a, **k: None)
    monkeypatch.setattr(cli, "make_sun_eye_documents", documents)
    monkeypatch.setattr(cli, "make_sun_eye_views", lambda *a, **k: [])
    monkeypatch.setattr(cli, "make_sun_eye_sheets", lambda *a, **k: (0, 0))

    CliRunner().invoke(cli.app, ["sun-views", "--port", "1"])

    assert order.count("mend") == 2, f"mended {order.count('mend')} times, not before and after"
    assert order.index("make_documents") < order.index("mend", 1), (
        "the documents this run made were never mended; only earlier runs' were"
    )
