"""Drawing the study diagram onto the floor plan, against a fake Archicad.

The placement itself cannot be faked -- it is measured by reading the drawn
fills back out of a real project and testing them against the Zone outlines,
recorded in D36 and D37. What these pin is the request shape and the refusal.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from sun_study.archicad.connection import ArchicadConnection, ArchicadError
from sun_study.archicad.penetration import PlanInstant, draw_penetration, fit_to_plan
from sun_study.archicad.read import ArchicadZone
from tests.unit.test_archicad_adapter import FakeTransport, texts_sent

# One flat, four cells of floor, the near half of it in sun.
POSITIONS = np.array([[0.25, 0.25, 3.0], [0.75, 0.25, 3.0], [0.25, 0.75, 3.0], [0.75, 0.75, 3.0]])
PARENTS = ("flat-1", "flat-1", "flat-1", "flat-1")
LIT = np.array([True, True, False, False])

ZONE = ArchicadZone(
    guid="zone-1",
    name="G08",
    number="1",
    storey_index=4,
    outline=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
)
FAR_ZONE = ArchicadZone(
    guid="zone-2",
    name="G08",
    number="2",
    storey_index=4,
    outline=((10.0, 0.0), (11.0, 0.0), (11.0, 1.0), (10.0, 1.0)),
)
EXTENTS = {
    "flat-1": np.array([[0.0, 0.0, 3.0], [1.0, 1.0, 3.0]]),
    "flat-2": np.array([[10.0, 0.0, 3.0], [11.0, 1.0, 3.0]]),
}


def connect(**overrides: Any) -> tuple[ArchicadConnection, FakeTransport]:
    responses: dict[str, Any] = {
        "GetAddOnVersion": {"version": "1.5.7"},
        "GetAttributesByType": {
            "attributes": [
                {"attributeId": {"guid": "l"}, "index": 7, "name": "Solar Analysis 12:00"}
            ]
        },
        "GetLayers": {
            "layers": [{"name": "Solar Analysis 12:00", "isHidden": False, "isLocked": False}]
        },
        "GetElementsByType": {"elements": []},
        "DeleteElements": {"success": True},
        "CreateHatches": {"elements": [{"elementId": {"guid": "h"}}] * 99},
        # The add-on answers with a bare guid, not Tapir's elementId wrapper.
        "CreateFills": {"success": True, "elements": [{"guid": "h"}] * 99},
        "CreatePolylines": {"elements": [{"elementId": {"guid": "p"}}] * 99},
        "CreateTexts": {"elements": [{"elementId": {"guid": "t"}}] * 99},
        "SetDetailsOfElements": {"executionResults": [{"success": True}]},
        "GetDetailsOfElements": {"detailsOfElements": [{"layerIndex": 7}] * 99},
    }
    responses.update(overrides)
    transport = FakeTransport(responses)
    return ArchicadConnection(transport), transport


def draw(connection: ArchicadConnection, **overrides: Any):  # type: ignore[no-untyped-def]
    arguments: dict[str, Any] = {
        "instants": [PlanInstant("12:00", LIT)],
        "positions": POSITIONS,
        "parent_ids": PARENTS,
        "spacing_m": 0.5,
        "zone_by_apartment": {"flat-1": "zone-1", "flat-2": "zone-2"},
        "zones": [ZONE, FAR_ZONE],
        "export_extents": EXTENTS,
        "annotations": {"flat-1": ["Sunlit floor 0.50 m2", "P.O.S. 0.00 m2", "Not Achieved"]},
        "layer_prefix": "Solar Analysis",
    }
    arguments.update(overrides)
    return draw_penetration(connection, **arguments)


def test_a_patch_a_green_outline_and_a_label_are_all_drawn() -> None:
    connection, transport = connect()
    report = draw(connection)

    # Through the add-on, because the patch is drawn without a contour and
    # Tapir's CreateHatches has no switch for that.
    patches = transport.parameters_for("CreateFills")["fills"]
    assert patches, "the patch itself"
    assert all(p["showArea"] is False for p in patches), (
        "every cell would otherwise print its own square-metre figure"
    )
    assert all(p["floorIndex"] == 4 for p in patches), "on the storey the flat is on"
    assert all(p["contourPen"] == 0 for p in patches), (
        "no contour: against a percentage fill an edge round every cell is not the drawing"
    )

    outlines = transport.parameters_for("CreatePolylines")["polylinesData"]
    assert len(outlines) == 2, "one per matched apartment"
    assert outlines[0]["coordinates"][0] == outlines[0]["coordinates"][-1], "closed"

    texts = texts_sent(transport)
    assert texts[0]["text"].endswith("Not Achieved")
    assert report.patches and report.outlines == 2 and report.labels == 1


def test_the_patch_is_the_lit_cells_and_not_the_whole_floor() -> None:
    connection, transport = connect()
    draw(connection)

    patches = transport.parameters_for("CreateFills")["fills"]
    ys = [
        point["y"]
        for patch in patches
        for contour in patch["contours"]
        for point in contour["points"]
    ]
    assert max(ys) == pytest.approx(0.5), "the far half of the floor saw no sun"


def test_a_transform_that_does_not_fit_refuses_to_draw() -> None:
    """A patch drawn through a bad transform lands on the wrong flat and looks
    entirely plausible, so this is a refusal rather than a warning."""
    connection, _ = connect()
    wrong = {"flat-1": EXTENTS["flat-1"], "flat-2": EXTENTS["flat-1"] + 5.0}

    with pytest.raises(ArchicadError, match="disagree about where the apartments are"):
        draw(connection, export_extents=wrong)


def test_a_single_pairing_cannot_be_fitted_at_all() -> None:
    connection, _ = connect()
    with pytest.raises(ArchicadError, match="needs two"):
        draw(connection, zone_by_apartment={"flat-1": "zone-1"}, zones=[ZONE])


def test_an_apartment_with_no_zone_is_named_not_dropped() -> None:
    connection, _ = connect()
    report = draw(
        connection,
        zone_by_apartment={"flat-1": "zone-1", "flat-2": "zone-2", "flat-3": "gone"},
    )
    assert report.unmatched == ("flat-3",)
    assert not report.complete


def test_the_fit_is_reported_so_a_thin_one_is_visible() -> None:
    connection, _ = connect()
    report = draw(connection)

    assert report.fit_pairs == 2
    assert "residual" in report.describe()
    assert "confirms nothing" in report.describe(), (
        "two pairs fit perfectly by construction and must not read as verified"
    )


def test_fitting_pairs_box_centres_not_means() -> None:
    """An outline's vertices and a grid's cells are not distributed alike, so
    two means of the same room are not the same point."""
    lopsided = ArchicadZone(
        guid="zone-3",
        name="G08",
        number="3",
        # Three vertices bunched along one wall: the vertex mean sits well off
        # centre, the box centre does not.
        outline=((0.0, 0.0), (0.5, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
    )
    fitted = fit_to_plan(EXTENTS, {"flat-1": lopsided, "flat-2": FAR_ZONE}, turn_deg=0.0)

    assert fitted.rmse_m < 0.01


def test_the_export_is_turned_before_either_box_is_taken() -> None:
    """A bounding box is axis-aligned in whichever frame it is measured, so
    the same flat boxed in a north-aligned export and again in a project
    rotated away from it has two different centres -- further apart the longer
    the flat is. Measured on Kogarah, boxing each in its own frame left 0.805 m
    of residual and the drawing was refused, correctly and for a reason that
    had nothing to do with the model.
    """
    import numpy as np

    from sun_study.core.geometry import rotation_about_z

    turn = 41.0
    spin = rotation_about_z(-turn)[:2, :2]
    # A long thin flat, which is where boxing in the wrong frame hurts most.
    flat = np.array([[0.0, 0.0], [12.0, 0.0], [12.0, 2.0], [0.0, 2.0]])
    # The project has it turned; the export has it north-aligned.
    in_project = flat @ rotation_about_z(turn)[:2, :2].T
    zone = ArchicadZone(
        guid="zone-9",
        name="long",
        number="9",
        outline=tuple((float(x), float(y)) for x, y in in_project),
    )
    other = ArchicadZone(
        guid="zone-8",
        name="other",
        number="8",
        outline=tuple(
            (float(x), float(y))
            for x, y in (flat + np.array([40.0, 5.0])) @ rotation_about_z(turn)[:2, :2].T
        ),
    )
    extents = {
        "a": np.column_stack([flat, np.zeros(len(flat))]),
        "b": np.column_stack([flat + np.array([40.0, 5.0]), np.zeros(len(flat))]),
    }

    turned = fit_to_plan(extents, {"a": zone, "b": other}, turn_deg=turn)
    assert turned.rmse_m < 0.001, "boxed in one frame, the pairs agree"

    astray = fit_to_plan(extents, {"a": zone, "b": other}, turn_deg=0.0)
    assert astray.rmse_m > 0.5, "boxed in different frames, they do not"
    _ = spin


def test_a_refusal_is_stated_once_however_many_drawings_it_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bad fit refuses every drawing in the run, with the same paragraph.

    The bands, the threshold, then one per hour: on Silverwater that printed
    eleven copies of a seven-line message and buried the figures the run had
    actually produced. The refusal is one fact about the run, so it is said
    once and the repeats are acknowledged in a line.
    """
    import typer

    from sun_study import cli

    said: list[str] = []
    monkeypatch.setattr(typer, "secho", lambda message, **kwargs: said.append(str(message)))
    monkeypatch.setattr(cli, "_ALREADY_SAID", set())

    cli._say_once("the export and the project disagree")
    for _ in range(10):
        cli._say_once("the export and the project disagree")
    cli._say_once("a different problem entirely")

    assert said.count("the export and the project disagree") == 1, "said more than once"
    assert said.count("a different problem entirely") == 1, "a second refusal was swallowed"
    # The repeats are acknowledged, because how many were refused is how much
    # of the sheet is missing.
    assert sum(1 for line in said if "refused this drawing too" in line) == 10


def test_a_turned_export_places_correctly_and_an_unturned_one_does_not() -> None:
    """Why the communal plans refused on a rotated project and the flats did not.

    A bounding box is axis-aligned in whichever frame it is measured, so the
    same Zone boxed in a north-aligned export and again in a project turned
    from it has two different centres -- further apart the longer the Zone and
    the further it sits from the centre of the site. `fit_to_plan` turns the
    export before boxing, which is exactly what `turn_deg` is for.

    Silverwater stands at bearing 293.548 with its export written at 0.000:
    66.45 degrees apart. Told nothing, the fit of 1,359 Zones left a median
    residual of 39.55 m and refused every drawing. Told the angle, 0.687 m.
    """
    import numpy as np

    from sun_study.archicad.penetration import fit_to_plan
    from sun_study.archicad.read import ArchicadZone
    from sun_study.core.geometry import rotation_about_z

    turn = 66.45
    back = rotation_about_z(-turn)[:2, :2]

    # Zones spread across a site, so the error has room to grow with radius.
    centres = [(0.0, 0.0), (60.0, 10.0), (-40.0, 55.0), (120.0, -80.0), (-95.0, -30.0)]

    export_extents = {}
    zones = {}
    for index, (x, y) in enumerate(centres):
        key = f"zone-{index}"
        # A long thin Zone, which is where the boxing frame matters most.
        corners = np.array(
            [[x - 9.0, y - 1.5], [x + 9.0, y - 1.5], [x + 9.0, y + 1.5], [x - 9.0, y + 1.5]]
        )
        zones[key] = ArchicadZone(guid=key, name=key, number="", outline=tuple(map(tuple, corners)))
        # The export holds the same Zone in a frame turned the other way.
        export_extents[key] = np.column_stack([corners @ back.T, np.zeros(len(corners))])

    told = fit_to_plan(export_extents, zones, turn_deg=turn)
    assert told.rmse_m < 0.01, f"with the angle stated the fit should be exact, got {told.rmse_m}"

    not_told = fit_to_plan(export_extents, zones, turn_deg=0.0)
    assert not_told.rmse_m > 10.0, (
        "with the angle left at zero the fit must fail loudly, not quietly pass"
    )
    # And it fails the way Silverwater did: worst at the greatest radius.
    radii = [float(np.hypot(x, y)) for x, y in centres]
    worst = max(range(len(centres)), key=lambda i: not_told.per_pair_m[i])
    assert radii[worst] == max(radii), "the worst pair should be the furthest out"


def test_a_contourless_patch_goes_through_the_addon_and_a_normal_one_does_not() -> None:
    """Tapir's CreateHatches has a contour pen and no switch to turn it off.

    Against a percentage fill that matters: the contour is a solid edge round
    every cell of the grid, which is not the drawing anybody asked for. The
    add-on's CreateFills reaches the field, so a contourless patch has to go
    that way -- and a patch that wants its contour must not, because then an
    older add-on would fail at the drawing stage for no gain.
    """
    from sun_study.archicad.penetration import _as_addon_fill, _create_fills

    hatch = {
        "coordinates": [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0}, {"x": 1.0, "y": 1.0}],
        "layerIndex": 7,
        "fillPenIndex": 124,
        "fillBackgroundPenIndex": 0,
        "contourPenIndex": 124,
        "fillIndex": 484,
        "showArea": False,
        "floorInd": 8,
    }

    # Re-spelled for the add-on: the contour off, everything else carried over.
    off = _as_addon_fill(hatch, outline=False)
    assert off["contourPen"] == 0, "pen 0 is the project's no-pen, as backgroundPen 0 already is"
    assert off["fillIndex"] == 484, "the percentage fill has to survive the translation"
    assert off["layerIndex"] == 7 and off["floorIndex"] == 8
    assert off["contours"] == [{"points": hatch["coordinates"]}]
    assert "coordinates" not in off, "the add-on takes contours, not a bare coordinate list"

    on = _as_addon_fill(hatch, outline=True)
    assert on["contourPen"] == 124, "asked for a contour, it keeps the one it was given"

    # With the contour wanted, the add-on is not consulted at all.
    class TapirOnly:
        def __init__(self) -> None:
            self.asked: list[str] = []

        def run_tapir(self, command: str, parameters: object = None) -> dict[str, object]:
            self.asked.append(command)
            return {"elements": [{"elementId": {"guid": "made"}}]}

        def run_loriini(self, command: str, parameters: object = None) -> dict[str, object]:
            raise AssertionError("the add-on must not be needed for an ordinary patch")

    tapir = TapirOnly()
    made = _create_fills(tapir, [hatch], outline=True)  # type: ignore[arg-type]
    assert tapir.asked == ["CreateHatches"] and len(made) == 1


def test_the_addons_elements_are_reshaped_so_the_ids_are_actually_written() -> None:
    """The two commands answer in different shapes, and one of them stamps nothing.

    Tapir returns `{"elementId": {"guid": ...}}`; the add-on returns
    `{"guid": ...}`. `ids.stamp_element_ids` keeps only the elements carrying
    an `elementId` -- silently, with no count and no complaint -- so handed the
    add-on's shape it writes no Element IDs at all, and a Schedule totalling on
    them finds an empty drawing while the run reports every fill drawn.

    Found on the Silverwater run of 16 September 2026, which reported 332
    patch fills and stamped none of them.
    """
    from sun_study.archicad.penetration import _create_fills

    class AddOn:
        def run_loriini(self, command: str, parameters: object = None) -> dict[str, object]:
            assert command == "CreateFills"
            return {"success": True, "elements": [{"guid": "a"}, {"guid": "b"}]}

    made = _create_fills(AddOn(), [{"coordinates": []}, {"coordinates": []}], outline=False)  # type: ignore[arg-type]

    assert made == [{"elementId": {"guid": "a"}}, {"elementId": {"guid": "b"}}], (
        "every element must carry an elementId or it is dropped without a word"
    )
