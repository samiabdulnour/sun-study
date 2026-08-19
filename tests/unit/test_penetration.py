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
from tests.unit.test_archicad_adapter import FakeTransport

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
            "attributes": [{"attributeId": {"guid": "l"}, "index": 7, "name": "Sun Study 12:00"}]
        },
        "GetLayers": {
            "layers": [{"name": "Sun Study 12:00", "isHidden": False, "isLocked": False}]
        },
        "GetElementsByType": {"elements": []},
        "DeleteElements": {"success": True},
        "CreateHatches": {"elements": [{"elementId": {"guid": "h"}}] * 99},
        "CreatePolylines": {"elements": [{"elementId": {"guid": "p"}}] * 99},
        "CreateTexts": {"elements": [{"elementId": {"guid": "t"}}] * 99},
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
        "layer_prefix": "Sun Study",
    }
    arguments.update(overrides)
    return draw_penetration(connection, **arguments)


def test_a_patch_a_green_outline_and_a_label_are_all_drawn() -> None:
    connection, transport = connect()
    report = draw(connection)

    hatches = transport.parameters_for("CreateHatches")["hatchesData"]
    assert hatches, "the patch itself"
    assert all(h["showArea"] is False for h in hatches), (
        "every cell would otherwise print its own square-metre figure"
    )
    assert all(h["floorInd"] == 4 for h in hatches), "on the storey the flat is on"

    outlines = transport.parameters_for("CreatePolylines")["polylinesData"]
    assert len(outlines) == 2, "one per matched apartment"
    assert outlines[0]["coordinates"][0] == outlines[0]["coordinates"][-1], "closed"

    texts = transport.parameters_for("CreateTexts")["textsData"]
    assert texts[0]["text"].endswith("Not Achieved")
    assert report.patches and report.outlines == 2 and report.labels == 1


def test_the_patch_is_the_lit_cells_and_not_the_whole_floor() -> None:
    connection, transport = connect()
    draw(connection)

    hatches = transport.parameters_for("CreateHatches")["hatchesData"]
    ys = [point["y"] for hatch in hatches for point in hatch["coordinates"]]
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
    fitted = fit_to_plan(EXTENTS, {"flat-1": lopsided, "flat-2": FAR_ZONE})

    assert fitted.rmse_m < 0.01
