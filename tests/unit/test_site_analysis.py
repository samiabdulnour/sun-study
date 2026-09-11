"""The site sheets as elements, and the calls that put them in a worksheet.

The frame is checked against the Kogarah project's north, the same figure
``test_sun_eyes`` uses, so the two halves of the tool agree on which way is
north. The drawings are built from tiny hand-made bundles and counted; the
Archicad side is exercised through the fake transport, which records every
request so the shape of each is asserted rather than assumed.
"""

from __future__ import annotations

import math
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from sun_study.archicad import naming
from sun_study.archicad.read import GeoLocation
from sun_study.archicad.site_analysis import (
    CONTEXT_WORD,
    context_drawing,
    draw_context,
    draw_summary,
    frame_for,
    site_drawing,
    summary_drawing,
    summary_rows,
)
from sun_study.site import nsw, osm, transport
from sun_study.site.geo import Extent, extent_for, lonlat_to_mercator, lonlat_to_mga
from sun_study.site.pipeline import (
    ContextBundle,
    Institution,
    SiteBundle,
    SummaryBundle,
    describe_lots,
    load_context,
    load_site,
    lots_area_m2,
    save,
)
from tests.unit.test_archicad_adapter import Sequential, connect

KOGARAH = GeoLocation(
    latitude_deg=-33.96,
    longitude_deg=151.13,
    altitude_m=0.0,
    north_radians=math.radians(49.052),
)
SS = naming.prefix()

# A square site about 20 m across, a little east of the project location.
LON, LAT = 151.1312, -33.9601


def square(lon: float, lat: float, metres: float) -> list[tuple[float, float]]:
    dlon = metres / (111_320.0 * math.cos(math.radians(lat)))
    dlat = metres / 111_320.0
    return [
        (lon - dlon / 2, lat - dlat / 2),
        (lon + dlon / 2, lat - dlat / 2),
        (lon + dlon / 2, lat + dlat / 2),
        (lon - dlon / 2, lat + dlat / 2),
        (lon - dlon / 2, lat - dlat / 2),
    ]


def polygon(ring: list[tuple[float, float]], **properties: Any) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in ring]]},
        "properties": properties,
    }


def collection(*features: dict[str, Any]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": list(features)}


def context_bundle() -> ContextBundle:
    centre = lonlat_to_mercator(LON, LAT)
    return ContextBundle(
        address="1 Test St, Kogarah NSW",
        matched=("1 TEST STREET KOGARAH",),
        centre=centre,
        extent=extent_for(centre, 690.0, 594.0, 3000.0),
        scale=3000.0,
        site_lots=collection(polygon(square(LON, LAT, 20.0), lotidstring="1//DP1")),
        all_lots=collection(polygon(square(LON + 0.001, LAT, 20.0))),
        zoning=collection(
            polygon(square(LON, LAT, 400.0), SYM_CODE="R3"),
            polygon(square(LON + 0.005, LAT, 400.0), SYM_CODE="DM"),
        ),
        heritage=collection(polygon(square(LON - 0.002, LAT, 30.0), LAY_CLASS="Item - General")),
        roads=(
            nsw.Road("Test Street", 5, 2, tuple(square(LON, LAT + 0.001, 300.0)[:2])),
            nsw.Road("Back Lane", 7, 1, tuple(square(LON, LAT - 0.001, 300.0)[:2])),
        ),
        stations=(nsw.NamedPoint("KOGARAH", LON + 0.004, LAT + 0.002),),
        park_areas=(nsw.NamedArea("JUBILEE OVAL", LON - 0.004, LAT + 0.002, 20_000.0),),
        complexes=(nsw.NamedPoint("St George Hospital", LON, LAT + 0.003, "medical"),),
        institutions=(
            Institution("medical", "St George Hospital", (tuple(square(LON, LAT + 0.003, 100.0)),)),
        ),
        bus=osm.BusData(
            stops=(osm.Stop(LON + 0.001, LAT + 0.001, "Test St"),),
            routes=(tuple(square(LON, LAT, 500.0)[:3]),),
        ),
        isochrones=(transport.Isochrone(5, (tuple(square(LON, LAT, 600.0)),)),),
    )


def test_the_frame_turns_true_north_the_way_the_sun_eye_views_do() -> None:
    """Project +Y sits at true bearing 319.052 on the Kogarah project, so a
    point due north of the origin lands up and to the right of it, 41 degrees
    off +Y -- the same 41 the sun eye bearings turn by."""
    frame = frame_for(KOGARAH, site_centre=(KOGARAH.longitude_deg, KOGARAH.latitude_deg))
    assert frame.zone == 56
    origin = frame.project(KOGARAH.longitude_deg, KOGARAH.latitude_deg)
    assert origin == pytest.approx((0.0, 0.0), abs=1e-6)

    phi = math.radians(KOGARAH.latitude_deg)
    metres_per_degree = 111132.954 - 559.822 * math.cos(2 * phi) + 1.175 * math.cos(4 * phi)
    north = frame.project(KOGARAH.longitude_deg, KOGARAH.latitude_deg + 100.0 / metres_per_degree)
    distance = math.hypot(*north)
    assert distance == pytest.approx(100.0, rel=1e-3)
    angle_from_plus_y = math.degrees(math.atan2(north[0], north[1]))
    assert angle_from_plus_y == pytest.approx(360.0 - 319.052, abs=0.05)
    assert frame.direction(0.0) == pytest.approx(
        (north[0] / distance, north[1] / distance), abs=1e-3
    )


def test_anchoring_at_the_site_puts_its_centre_at_the_origin_and_keeps_north() -> None:
    frame = frame_for(KOGARAH, site_centre=(LON, LAT), anchor="site")
    assert frame.project(LON, LAT) == pytest.approx((0.0, 0.0), abs=1e-6)
    assert frame.plus_y_bearing_deg == pytest.approx(319.052)
    without = frame_for(None, site_centre=(LON, LAT), anchor="site")
    assert without.plus_y_bearing_deg == 0.0
    with pytest.raises(ValueError, match="geolocation"):
        frame_for(None, site_centre=(LON, LAT), anchor="location")


def test_the_context_drawing_carries_the_legends_colours_and_the_site_on_top() -> None:
    bundle = context_bundle()
    frame = frame_for(KOGARAH, site_centre=(LON, LAT), anchor="site")
    drawing = context_drawing(bundle, frame)

    zoning = [f for f in drawing.fills if f.layer == f"{SS} {CONTEXT_WORD}.Zoning"]
    assert [f.colour for f in zoning] == ["#f7c1bd"], (
        "R3 is drawn in the R3 tan; DM is not a category"
    )
    assert zoning[0].element_id == "SA ZONE R3"
    institutions = [f for f in drawing.fills if f.layer.endswith(".Institutions")]
    assert institutions[0].colour == "#f27ba9" and institutions[0].contour == "#e5237e"
    assert any(f.layer.endswith(".Heritage") for f in drawing.fills)

    site_lines = [line for line in drawing.lines if line.layer.endswith(".Site")]
    assert site_lines and site_lines[0].dashed and site_lines[0].colour == "#e30613"
    # The site is 20 m a side in the project frame, whatever the turn.
    sides = [math.dist(a, b) for a, b in pairwise(site_lines[0].points)]
    assert sides == pytest.approx([20.0] * 4, rel=0.01)

    labels = {t.text for t in drawing.texts}
    assert {
        "TEST ST",
        "BACK LN",
        "KOGARAH",
        "JUBILEE OVAL",
        "ST GEORGE HOSPITAL",
        "SITE",
        "B",
        "T",
    } <= labels
    assert any(t.text.startswith("400m") for t in drawing.texts)
    # A street gets its arrow, a lane does not: one double-headed arrow.
    road_lines = [line for line in drawing.lines if line.layer.endswith(".Road names")]
    assert len(road_lines) == 3
    # Legend rows only for what is on the map.
    legend = [t.text for t in drawing.texts if t.layer.endswith(".Legend")]
    assert "R3 MEDIUM DENSITY RESIDENTIAL" in legend and "MEDICAL" in legend
    assert "R2 LOW DENSITY RESIDENTIAL" not in legend
    assert drawing.mm(4.0) == pytest.approx(12.0), "4 mm on paper is 12 m at 1:3000"


def site_bundle() -> SiteBundle:
    centre = lonlat_to_mercator(LON, LAT)
    ring = square(LON, LAT, 20.0)
    east = LON + 0.0003
    return SiteBundle(
        address="1 Test St, Kogarah NSW",
        matched=("1 TEST STREET KOGARAH",),
        centre=centre,
        extent=extent_for(centre, 690.0, 594.0, 200.0),
        scale=200.0,
        site_lots=collection(polygon(ring, lotidstring="1//DP1")),
        site_rings=(tuple(ring[:-1]),),
        roads=(
            nsw.Road(
                "Test Street", 4, 2, ((LON - 0.001, LAT + 0.0003), (LON + 0.001, LAT + 0.0003))
            ),
        ),
        zoning=collection(polygon(square(LON, LAT, 400.0), SYM_CODE="R3")),
        contours=(
            # Just outside the west and east boundaries, four metres apart in
            # level, so the corners read about 24.2 and 27.8 and the fall shows.
            nsw.Contour(24.0, ((LON - 0.00012, LAT - 0.0004), (LON - 0.00012, LAT + 0.0004))),
            nsw.Contour(28.0, ((LON + 0.00012, LAT - 0.0004), (LON + 0.00012, LAT + 0.0004))),
        ),
        neighbours=(nsw.Neighbour("3", east, LAT),),
        furniture=osm.Furniture(
            trees=((LON, LAT + 0.0002),),
            buildings=(osm.Building(east, LAT, tuple(square(east, LAT, 10.0)), levels=2),),
            driveways=(((LON, LAT), (LON, LAT + 0.0003)),),
        ),
    )


def test_the_site_drawing_dimensions_levels_and_orients_the_site() -> None:
    frame = frame_for(KOGARAH, site_centre=(LON, LAT), anchor="site")
    drawing = site_drawing(site_bundle(), frame)
    texts = [t.text for t in drawing.texts]

    dimensions = [
        t.text
        for t in drawing.texts
        if t.layer.endswith(".Dimensions") and t.text.startswith("SB ")
    ]
    assert len(dimensions) == 4
    assert all(abs(float(t.split()[1]) - 20.0) < 0.2 for t in dimensions)
    levels = [
        t.text for t in drawing.texts if t.layer.endswith(".Levels") and t.text.startswith("RL ")
    ]
    assert len(levels) == 4
    info = next(
        t.text
        for t in drawing.texts
        if t.layer.endswith(".Dimensions") and t.text.startswith("FALL")
    )
    assert "AREA 400" in info or "AREA 399" in info or "AREA 401" in info
    assert "3\n2 STOREY\nR3 MEDIUM DENSITY RESIDENTIAL" in texts
    assert {"W\nAM", "W\nPM", "S\nAM", "S\nPM", "N", "NE SUMMER SEA BREEZE"} <= set(texts)
    assert any(f.layer.endswith(".Access") for f in drawing.fills), "a driveway becomes a triangle"
    assert any(line.layer.endswith(".Noise") for line in drawing.lines), (
        "a hierarchy-4 road is a noise source"
    )
    assert any(line.layer.endswith(".Buildings") for line in drawing.lines)

    # The north disc sits along true north from the site, at 0.42 of the field.
    north = next(t for t in drawing.texts if t.text == "N" and t.layer.endswith(".Sun path"))
    direction = frame.direction(0.0)
    radius = min(690.0, 594.0) * 0.2 * 0.42
    assert north.at[0] == pytest.approx(direction[0] * radius, abs=1.0)


def test_the_summary_table_fills_what_is_published_and_rules_up_the_rest() -> None:
    bundle = SummaryBundle(
        address="1 Test St",
        site_address="1 TEST STREET KOGARAH",
        lot_description="LOT 1 - DP 1",
        site_area_m2=1000.0,
        controls=nsw.SiteControls(
            epi_name="Georges River Local Environmental Plan 2021",
            lga_name="GEORGES RIVER",
            zone_code="R3",
            zone_purpose="Medium Density Residential",
            max_height_m=12.0,
            fsr=0.55,
            heritage=(("Cottage", "Local"),),
        ),
    )
    rows = summary_rows(bundle)
    by_label = {label: controls for kind, label, controls in rows if kind == "row"}
    assert by_label["LAND USE"] == ["R3 - Medium Density Residential"]
    assert by_label["FLOOR SPACE RATIO"] == ["0.55:1"]
    assert by_label["GROSS FLOOR AREA (sqm)"][0] == "550.0 sqm"
    assert by_label["HERITAGE"] == ["Cottage (Local)"]
    assert by_label["MIN. LOT SIZE (sqm)"] == ["No minimum lot size mapped"]
    assert by_label["SETBACK"] == [], "DCP rows are ruled up, not guessed"
    assert by_label["3D. COMMUNAL OPEN SPACE"][-1] == "= 250.0 sqm"
    assert any(label == "GEORGES RIVER LEP 2021" for kind, label, _ in rows if kind == "section")

    drawing = summary_drawing(bundle)
    assert "DEVELOPMENT SUMMARY" in [t.text for t in drawing.texts]
    assert len(drawing.lines) == len(rows) + 1, "a rule under every row and one above"


def test_the_bundles_survive_a_round_trip_through_json(tmp_path: Path) -> None:
    context = context_bundle()
    path = save(context, tmp_path / "data" / "context.json")
    back = load_context(path)
    assert back.matched == context.matched
    assert back.extent == context.extent
    assert back.roads == context.roads
    assert back.institutions == context.institutions
    assert back.bus == context.bus
    assert back.isochrones == context.isochrones

    site = site_bundle()
    again = load_site(save(site, tmp_path / "data" / "site.json"))
    assert again.site_rings == site.site_rings
    assert again.furniture == site.furniture
    assert again.contours == site.contours
    with pytest.raises(ValueError, match="not a saved site"):
        load_site(path)


def test_lot_descriptions_and_areas_follow_the_register() -> None:
    lots = collection(
        polygon(square(LON, LAT, 20.0), lotidstring="1//DP212120"),
        polygon(square(LON + 0.001, LAT, 20.0), lotidstring="2//DP212120"),
    )
    assert describe_lots(lots) == "LOT 1&2 - DP 212120"
    assert lots_area_m2(lots, 56) == pytest.approx(800.0, rel=0.01)
    assert lonlat_to_mga(LON, LAT, 56)[0] > 300_000


def navigator_tree(*worksheets: tuple[str, str]) -> dict[str, Any]:
    return {
        "navigatorItemTree": {
            "rootItem": {
                "name": "root",
                "children": [
                    {
                        "navigatorItem": {
                            "type": "WorksheetDrawingItem",
                            "name": name,
                            "navigatorItemId": {"guid": guid},
                            "children": [],
                        }
                    }
                    for name, guid in worksheets
                ],
            }
        }
    }


def test_drawing_makes_the_worksheet_through_the_add_on_and_fills_in_colour() -> None:
    """The order that makes it drawable: the add-on's CreateWorksheet, then
    the fills through CreateFills with an RGB each, then Tapir's polylines
    and texts, and the texts moved onto the study's layer."""
    connection, transport_ = connect(
        {
            "GetCurrentDatabase": {"databaseId": {"guid": "PLAN"}, "windowType": "FloorPlan"},
            "GetNavigatorItemTree": navigator_tree(),
            "CreateWorksheet": {"success": True, "databaseId": {"guid": "WS"}, "isCurrent": True},
            # One list answers every attribute kind: the fill and line type by
            # name, and every layer the context sheet draws on, as if a run
            # had made them already.
            "GetAttributesByType": {
                "attributes": [
                    {"name": "Solid Fill", "index": 1, "attributeId": {"guid": "F1"}},
                    {"name": "Dashed", "index": 2, "attributeId": {"guid": "L2"}},
                    *(
                        {"name": name, "index": 10 + i, "attributeId": {"guid": f"LAYER{i}"}}
                        for i, name in enumerate(
                            context_drawing(
                                context_bundle(),
                                frame_for(KOGARAH, site_centre=(LON, LAT), anchor="site"),
                            ).layers
                        )
                    ),
                ]
            },
            "GetLayers": {
                "layers": [{"layerAttribute": {"name": "x", "isHidden": False, "isLocked": False}}]
            },
            "GetPenTables": Sequential(
                {
                    "penTables": [
                        {
                            "penTableAttribute": {
                                "isActiveForModel": True,
                                "attributeId": {"guid": "F1"},
                            }
                        }
                    ]
                },
                {
                    "penTables": [
                        {
                            "penTableAttribute": {
                                "pens": [
                                    {
                                        "index": 5,
                                        "color": {"red": 0.9, "green": 0.05, "blue": 0.05},
                                    },
                                    {"index": 7, "color": {"red": 0.5, "green": 0.5, "blue": 0.5}},
                                ]
                            }
                        }
                    ]
                },
            ),
            "CreateLayers": {"success": True},
            "CreateFills": {"success": True, "elements": [{"guid": "H"}]},
            "CreatePolylines": {"elements": [{"elementId": {"guid": "P"}}]},
            "CreateTexts": {"success": True, "elements": [{"guid": "T"}]},
            "CreateLayerCombinations": {"executionResults": [{"success": True}]},
            "CreateViewsInViewMap": {"navigatorItems": [{"navigatorItemId": {"guid": "V"}}]},
            "SetViewSettings": {"executionResults": [{"success": True}]},
            "CreateViewMapFolder": {"navigatorItemId": {"guid": "FOLDER"}},
        }
    )
    frame = frame_for(KOGARAH, site_centre=(LON, LAT), anchor="site")
    report = draw_context(connection, context_bundle(), frame, view=False)

    commands = transport_.commands()
    assert (
        commands.index("CreateWorksheet")
        < commands.index("CreateFills")
        < commands.index("CreatePolylines")
    )
    assert transport_.parameters_for("CreateWorksheet") == {
        "name": f"{SS} {CONTEXT_WORD}",
        "makeCurrent": True,
    }

    fills = [f for call in transport_.all_parameters_for("CreateFills") for f in call["fills"]]
    zone = next(f for f in fills if f.get("elementId") == "SA ZONE R3")
    assert zone["determination"] == "drafting" and zone["showArea"] is False
    assert zone["foregroundColour"] == pytest.approx(
        {"red": 0xF7 / 255, "green": 0xC1 / 255, "blue": 0xBD / 255}
    )
    assert zone["fillIndex"] == 1
    assert len(zone["contours"]) == 1 and len(zone["contours"][0]["points"]) == 4, (
        "no closing point"
    )

    lines = [
        line
        for call in transport_.all_parameters_for("CreatePolylines")
        for line in call["polylinesData"]
    ]
    assert any(line.get("lineTypeIndex") == 2 for line in lines), "the site outline is dashed"
    site_line = next(line for line in lines if line.get("penWeightMm") == 0.9)
    assert site_line["linePenIndex"] == 5, "the site's red takes the nearest pen"
    texts = [t for call in transport_.all_parameters_for("CreateTexts") for t in call["texts"]]
    site_text = next(t for t in texts if t["text"] == "SITE")
    assert site_text["justification"] == "Center" and site_text["anchor"] == "MiddleMiddle"
    assert all("layerIndex" in t for t in texts), "the add-on's texts take their layer at creation"
    assert "SetDetailsOfElements" not in commands, "so nothing has to be moved afterwards"

    assert report.reused is False and report.fills == len(fills) and report.lines == len(lines)
    assert report.texts == len(texts)
    assert not any("pen table" in note for note in report.notes)


def test_an_existing_worksheet_is_entered_and_cleared_first() -> None:
    connection, transport_ = connect(
        {
            "GetCurrentDatabase": {"databaseId": {"guid": "PLAN"}, "windowType": "FloorPlan"},
            "GetNavigatorItemTree": navigator_tree((f"{SS} Development Summary", "NAV")),
            "GetDatabaseIdFromNavigatorItemId": {"databases": [{"databaseId": {"guid": "DB"}}]},
            "SetCurrentDatabase": {"success": True},
            "GetElementsByType": {"elements": [{"elementId": {"guid": "OLD"}}]},
            "GetDetailsOfElements": {"detailsOfElements": [{"layerIndex": 1}]},
            "DeleteElements": {"success": True},
            "GetAttributesByType": {"attributes": []},
            "CreateLayers": {"success": True},
            "GetLayers": {"layers": []},
            "CreatePolylines": {"elements": [{"elementId": {"guid": "P"}}]},
            "CreateTexts": {"elements": [{"elementId": {"guid": "T"}}]},
            "SetDetailsOfElements": {"success": True},
        }
    )
    bundle = SummaryBundle("1 Test St", "1 TEST STREET", "LOT 1 - DP 1", None, nsw.SiteControls())
    with pytest.raises(Exception, match="Created layer"):
        # No attributes at all: the layer cannot be found after creation, and
        # the run stops there rather than drawing onto layer zero.
        draw_summary(connection, bundle, view=False)
    assert transport_.parameters_for("SetCurrentDatabase") == {
        "databaseId": {"guid": "DB"},
        "windowType": "Worksheet",
    }
    commands = transport_.commands()
    assert commands.index("SetCurrentDatabase") < commands.index("DeleteElements")


def test_a_context_extent_is_the_sheet_at_its_scale() -> None:
    bundle = context_bundle()
    assert isinstance(bundle.extent, Extent)
    assert bundle.extent.width == pytest.approx(2070.0 / math.cos(math.radians(LAT)), rel=1e-6)


def test_a_refused_creation_falls_back_to_tapir_and_a_refused_entry_says_what_to_do() -> None:
    """Measured on the Kogarah solar study: the add-on's CreateWorksheet is
    refused from its undo scope, Tapir's CreateWorksheets makes the sheet,
    and neither route can enter a worksheet made in this session. The
    worksheet is then there, and the error says to open it and rerun."""
    from sun_study.archicad.site_analysis import WorksheetNotEnteredError, ensure_worksheet

    connection, transport_ = connect(
        {
            "GetCurrentDatabase": {"databaseId": {"guid": "PLAN"}, "windowType": "FloorPlan"},
            "GetNavigatorItemTree": navigator_tree(),
            "CreateWorksheet": {"error": {"code": -2130312312, "message": "Failed to create"}},
            "CreateWorksheets": {"databases": [{"databaseId": {"guid": "NEW"}}]},
            "SetCurrentDatabase": {"error": {"code": -2130313110, "message": "refused to move"}},
        }
    )
    with pytest.raises(WorksheetNotEnteredError, match="double-click"):
        ensure_worksheet(connection, f"{SS} Context Analysis", wait_s=0.0)
    assert transport_.parameters_for("CreateWorksheets") == {
        "worksheetsData": [
            {"name": f"{SS} Context Analysis", "referenceId": f"{SS} Context Analysis"}
        ]
    }
    assert transport_.all_parameters_for("SetCurrentDatabase")[0]["databaseId"] == {"guid": "NEW"}


def test_a_worksheet_already_in_front_is_drawn_into_without_a_move() -> None:
    """The way through the refusal: a person opens the worksheet, and the run
    finds itself standing in it."""
    from sun_study.archicad.site_analysis import ensure_worksheet

    connection, transport_ = connect(
        {
            "GetCurrentDatabase": {"databaseId": {"guid": "WS"}, "windowType": "Worksheet"},
            "GetNavigatorItemTree": navigator_tree((f"{SS} Context Analysis", "NAV")),
            "GetDatabaseIdFromNavigatorItemId": {"databases": [{"databaseId": {"guid": "WS"}}]},
        }
    )
    assert ensure_worksheet(connection, f"{SS} Context Analysis") == ("WS", "NAV", True)
    assert "SetCurrentDatabase" not in transport_.commands()


def test_the_run_waits_for_a_person_to_open_the_worksheet() -> None:
    """Refused twice, then in front: the third look finds it and drawing goes on."""
    from sun_study.archicad.site_analysis import ensure_worksheet

    connection, transport_ = connect(
        {
            "GetCurrentDatabase": Sequential(
                {"databaseId": {"guid": "PLAN"}, "windowType": "FloorPlan"},
                {"databaseId": {"guid": "PLAN"}, "windowType": "FloorPlan"},
                {"databaseId": {"guid": "NEW"}, "windowType": "Worksheet"},
            ),
            "GetNavigatorItemTree": navigator_tree((f"{SS} Site Analysis", "NAV")),
            "GetDatabaseIdFromNavigatorItemId": {"databases": [{"databaseId": {"guid": "NEW"}}]},
            "SetCurrentDatabase": {"error": {"code": -2130313110, "message": "refused"}},
        }
    )
    said: list[str] = []
    assert ensure_worksheet(connection, f"{SS} Site Analysis", wait_s=30.0, say=said.append) == (
        "NEW",
        "NAV",
        True,
    )
    assert said and "double-click" in said[0]
    assert transport_.commands().count("GetCurrentDatabase") == 3


def test_texts_fall_back_to_tapir_and_a_move_when_the_add_on_is_older() -> None:
    """An add-on without CreateTexts answers 'not registered'; the texts then
    go through Tapir on the Text tool's layer and are moved onto the study's."""
    from sun_study.archicad.site_analysis import Text, _Attributes, _texts

    connection, transport_ = connect(
        {
            "CreateTexts": Sequential(
                {"error": {"code": 4010, "message": "does not have the registered Add-On command"}},
                {"elements": [{"elementId": {"guid": "T"}}]},
            ),
            "GetDetailsOfElements": Sequential(
                {"detailsOfElements": [{"layerIndex": 3}]},
                {"detailsOfElements": [{"layerIndex": 7}]},
            ),
            "GetAttributesByType": {"attributes": []},
            "SetDetailsOfElements": {"success": True},
        }
    )
    texts = [Text(layer="L", text="SITE", at=(1.0, 2.0), height_mm=4.0)]
    moved = _texts(connection, texts, {"L": 7}, _Attributes(None, None, ()))
    assert moved == 1
    calls = transport_.all_parameters_for("CreateTexts")
    assert "texts" in calls[0] and "textsData" in calls[1]
    assert calls[1]["textsData"][0]["coordinate"] == {"x": 1.0, "y": 2.0, "z": 0.0}


def test_a_multipolygon_becomes_one_fill_per_part_with_its_own_holes() -> None:
    """Esri hands every ring of a feature in one list. Two parts and a hole
    in the first must not become one fill with two 'holes', which Archicad
    refuses; the closing point is dropped, since the add-on adds its own."""
    from sun_study.archicad.site_analysis import Drawing, _polygons

    big = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0), (0.0, 0.0)]
    hole = [(40.0, 40.0), (60.0, 40.0), (60.0, 60.0), (40.0, 60.0), (40.0, 40.0)]
    other = [(200.0, 0.0), (250.0, 0.0), (250.0, 50.0), (200.0, 50.0), (200.0, 0.0)]
    island = [(48.0, 48.0), (52.0, 48.0), (52.0, 52.0), (48.0, 52.0), (48.0, 48.0)]
    polygons = _polygons([hole, other, big, island])
    assert [(len(outer), len(holes)) for outer, holes in polygons] == [(4, 1), (4, 0), (4, 0)]
    assert polygons[0][1][0][0] == (40.0, 40.0)

    drawing = Drawing(3000.0, "Context Analysis")
    drawing.fill("Zoning", [big, hole, other], "#f7c1bd")
    assert len(drawing.fills) == 2
    assert drawing.fills[0].rings[0][-1] != drawing.fills[0].rings[0][0]
