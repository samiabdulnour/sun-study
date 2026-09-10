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
from sun_study.archicad.read import GeoLocation
from sun_study.archicad.sun_eyes import (
    DOCUMENT_SCALE,
    SunEyeSettings,
    make_sun_eye_views,
    sun_eyes,
)
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
        layer_combination=f"{SS} Sun Eye Views",
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
    settings = SunEyeSettings(layer_combination=f"{SS} Sun Eye Views")

    made = make_sun_eye_views(connection, eyes, settings=settings)

    assert [view.name for _, view, _ in made] == [
        f"{SS} Sun Eye 21 Jun 09:00",
        f"{SS} Sun Eye 21 Jun 15:00",
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
