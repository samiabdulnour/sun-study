"""Putting the shadows into Archicad: one layer to an hour, and a combination.

What is worth testing here is not that a hatch was sent -- it is the three
things that decide whether the sheets are readable and whether a rerun is
safe: the fills land on the right hour's layer, each hour's combination shows
that layer and hides its siblings while leaving the rest of the project alone,
and a second run replaces its own work instead of stacking on it.

The transport is the same fake the rest of the adapter tests use, so a test
reads as the exchange it expects.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import pytest

from sun_study.archicad import naming
from sun_study.archicad.connection import ArchicadConnection, ArchicadError
from sun_study.archicad.shadows import (
    combination_name,
    draw_shadow_series,
    layer_name,
    shared_layer_name,
)
from sun_study.core.geometry import box, prism
from sun_study.core.shadow import (
    ADDITIONAL,
    EXISTING,
    cast_shadows,
    default_sources,
    ground_plane_grid,
)

WEST_45 = np.array([[-1.0, 0.0, 1.0]]) / np.sqrt(2.0)

#: The project's own layers, as ``GetAttributesByType`` reports them: id,
#: index and name, and no visibility. Two of them, one shown and one hidden,
#: because the thing worth proving about a combination is that it leaves both
#: exactly as it found them.
PROJECT_LAYERS = [
    ("wall", "01 | Wall.External", 11, False),
    ("dims", "05 | Dims/Notes.DA", 12, True),
]


class ShadowTransport:
    """A project that already has the tool's layers, answering by parameter.

    Purpose-built rather than scripted by command name, because reading layers
    takes *two* commands and ``GetAttributesByType`` is asked for Layers and
    for LayerCombinations in the same run. A fake that cannot tell those apart
    answers the layer lookup with the combination list and the drawing fails
    several steps later on a layer it was told does not exist -- which is a
    good deal harder to read than this class.
    """

    def __init__(self, labels: list[str], stale: int = 0) -> None:
        rows = list(PROJECT_LAYERS)
        for position, label in enumerate(labels):
            rows.append((f"shadow-{label}", layer_name(label), 100 + position, False))
        rows.append(("legend", shared_layer_name(), 200, False))
        self.layers = rows
        #: Fills left by an earlier run, spread over the tool's own layers so
        #: a rerun has something real to clear.
        self.stale = [
            {"elementId": {"guid": f"old-{index}-{layer}"}, "layerIndex": layer}
            for layer in [100 + n for n in range(len(labels))]
            for index in range(stale)
        ]
        self.sent: list[dict[str, Any]] = []
        #: Every Element ID written, in the order it was written.
        self.stamped: list[dict[str, Any]] = []
        #: Every group asked for, so a test can check what shares one.
        self.groups: list[dict[str, Any]] = []

    # -- the transport contract --------------------------------------------
    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(payload)
        command = payload["command"]
        if command != "API.ExecuteAddOnCommand":
            return {"succeeded": True, "result": {}}
        name = payload["parameters"]["addOnCommandId"]["commandName"]
        given = payload["parameters"].get("addOnCommandParameters") or {}
        return {
            "succeeded": True,
            "result": {"addOnCommandResponse": self._answer(name, given)},
        }

    def _answer(self, command: str, given: dict[str, Any]) -> dict[str, Any]:
        if command == "GetAddOnVersion":
            return {"version": "1.5.7"}
        if command == "GetAttributesByType":
            if given.get("attributeType") == "Layer":
                return {
                    "attributes": [
                        {"attributeId": {"guid": guid}, "name": name, "index": index}
                        for guid, name, index, _ in self.layers
                    ]
                }
            return {"attributes": []}  # no layer combination of ours exists yet
        if command == "GetLayers":
            asked = [entry["attributeId"]["guid"] for entry in given["attributeIds"]]
            by_guid = {guid: (name, hidden) for guid, name, _, hidden in self.layers}
            return {
                "layers": [
                    {"name": by_guid[guid][0], "isHidden": by_guid[guid][1], "isLocked": False}
                    for guid in asked
                ]
            }
        if command == "GetElementsByType":
            return {"elements": self.stale if given.get("elementType") == "Hatch" else []}
        if command == "GetDetailsOfElements":
            return {
                "detailsOfElements": [
                    {"layerIndex": element["layerIndex"]} for element in given["elements"]
                ]
            }
        if command == "DeleteElements":
            return {"executionResults": [{"success": True} for _ in given["elements"]]}
        if command == "CreateHatches":
            return {"elements": [{"elementId": {"guid": "fill"}} for _ in given["hatchesData"]]}
        if command in ("CreateLayers", "CreateLayerCombinations"):
            return {"attributeIds": []}
        if command == "CreateGroups":
            groups = given["elementGroups"]
            self.groups.extend(groups)
            return {"groupGuids": [{"groupId": {"guid": f"group-{n}"}} for n in range(len(groups))]}
        if command == "GetAllProperties":
            # Both of them, because the project really carries both and only
            # one of them can be written to.
            return {
                "properties": [
                    {
                        "propertyId": {"guid": "prop-element-id"},
                        "propertyGroupName": "General Parameters",
                        "propertyName": "Element ID",
                        "propertyIsEditable": True,
                        "propertyValueType": "string",
                    },
                    {
                        "propertyId": {"guid": "prop-hotlink-id"},
                        "propertyGroupName": "General Parameters",
                        "propertyName": "Hotlink and Element ID",
                        "propertyIsEditable": False,
                        "propertyValueType": "string",
                    },
                ]
            }
        if command == "GetFavoritesByType":
            # The project's Fill Favorites. An unknown name is refused rather
            # than drawn with the tool's own settings, so the list matters.
            return {"favorites": ["General_Fill", "LORIINI"]}
        if command == "SetPropertyValuesOfElements":
            self.stamped.extend(given["elementPropertyValues"])
            return {"executionResults": [{"success": True} for _ in given["elementPropertyValues"]]}
        raise AssertionError(f"unscripted command {command!r}")

    # -- reading it back ----------------------------------------------------
    def all_parameters_for(self, command: str) -> list[dict[str, Any]]:
        return [
            dict(payload["parameters"]["addOnCommandParameters"])
            for payload in self.sent
            if payload["command"] == "API.ExecuteAddOnCommand"
            and payload["parameters"]["addOnCommandId"]["commandName"] == command
        ]


def series(labels: list[str] | None = None, envelope: bool = True):  # type: ignore[no-untyped-def]
    labels = labels or ["9AM", "12PM"]
    grid = ground_plane_grid(
        (np.array([-40.0, -5.0, 0.0]), np.array([10.0, 5.0, 20.0])),
        datum_m=0.0,
        spacing_m=2.0,
        margin_m=40.0,
    )
    return cast_shadows(
        grid,
        sources=default_sources(
            box((-40.0, -5.0, 0.0), (-30.0, 5.0, 10.0)),
            box((0.0, -5.0, 0.0), (10.0, 5.0, 20.0)),
            prism([(0, -5), (10, -5), (10, 5), (0, 5)], 0.0, 9.5) if envelope else None,
        ),
        sun_vectors=np.repeat(WEST_45, len(labels), axis=0),
        moments=[dt.datetime(2024, 6, 21, 9 + i, tzinfo=dt.UTC) for i in range(len(labels))],
        labels=labels,
        spacing_m=2.0,
    )


def connect(labels: list[str], stale: int = 0):  # type: ignore[no-untyped-def]
    transport = ShadowTransport(labels, stale)
    return ArchicadConnection(transport), transport


def hatches(transport: ShadowTransport) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for call in transport.all_parameters_for("CreateHatches"):
        out.extend(call["hatchesData"])
    return out


# -- the fills -------------------------------------------------------------


def test_each_hour_is_drawn_on_its_own_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Twenty-one hours on one layer is twenty-one overlapping fills and one
    unreadable plan. The layer is the only thing separating them."""
    connection, transport = connect(["9AM", "12PM"])

    report = draw_shadow_series(connection, series(["9AM", "12PM"]))

    assert report.layers == (layer_name("9AM"), layer_name("12PM"))
    drawn = {hatch["layerIndex"] for hatch in hatches(transport)}
    assert len(drawn) >= 2, "the two hours did not land on different layers"


def test_a_shadow_fill_never_prints_its_own_area_across_the_plan() -> None:
    """A Fill inherits the Fill tool's default, and on a real project that
    default has Show Area Text on -- which puts a square-metre figure on every
    rectangle of a tiled shadow, and a shadow with a courtyard in it is
    tiled."""
    connection, transport = connect(["9AM"])

    draw_shadow_series(connection, series(["9AM"]))

    assert all(hatch["showArea"] is False for hatch in hatches(transport))


def test_a_plan_fill_carries_its_storey_and_a_worksheet_fill_does_not() -> None:
    """Tapir's own tracker records a floor index silently destroying elements
    in a database that has no storeys."""
    connection, transport = connect(["9AM"])
    draw_shadow_series(connection, series(["9AM"]), storey_index=2)
    assert all(hatch["floorInd"] == 2 for hatch in hatches(transport))

    connection, transport = connect(["9AM"])
    draw_shadow_series(connection, series(["9AM"]), storey_index=None)
    assert all("floorInd" not in hatch for hatch in hatches(transport))


def test_the_existing_shadow_is_drawn_under_what_the_proposal_adds() -> None:
    """Back to front: the existing shadow is the ground the argument is read
    against, and the proposal's own addition is the thing being looked at."""
    from sun_study.archicad.shadows import drawing_order, styles_for

    drawn = series(["9AM"])
    order = drawing_order(drawn.sources)
    assert order.index(EXISTING) < order.index(ADDITIONAL)

    connection, transport = connect(["9AM"])
    draw_shadow_series(connection, drawn)

    pens = [hatch["fillPenIndex"] for hatch in hatches(transport)]
    styles = styles_for(drawn.sources)
    assert pens.index(styles[EXISTING].fill_pen) < pens.index(styles[ADDITIONAL].fill_pen)


def test_the_site_boundary_is_drawn_once_not_once_an_hour() -> None:
    """It is identical at every instant. Twenty-one copies is twenty-one
    things to correct when somebody changes a pen."""
    connection, transport = connect(["9AM", "12PM", "3PM"])

    draw_shadow_series(
        connection,
        series(["9AM", "12PM", "3PM"]),
        boundary=((0.0, -5.0), (10.0, -5.0), (10.0, 5.0), (0.0, 5.0)),
        boundary_pen=7,
    )

    on_seven = [hatch for hatch in hatches(transport) if hatch["fillPenIndex"] == 7]
    assert len(on_seven) == 1


# -- the combinations ------------------------------------------------------


def test_an_hours_combination_shows_that_hour_and_hides_the_others() -> None:
    """What makes a View of the plan a drawing of one hour."""
    labels = ["9AM", "12PM", "3PM"]
    connection, transport = connect(labels)

    draw_shadow_series(connection, series(labels))

    written = transport.all_parameters_for("CreateLayerCombinations")
    assert len(written) == len(labels)
    nine = written[0]["layerCombinationDataArray"][0]
    hidden = {entry["attributeId"]["guid"]: entry["isHidden"] for entry in nine["layers"]}
    assert hidden["shadow-9AM"] is False, "the hour being drawn"
    assert hidden["shadow-12PM"] is True and hidden["shadow-3PM"] is True, "its siblings"
    assert hidden["legend"] is False, "the legend and boundary are shared"


def test_a_combination_leaves_the_rest_of_the_project_as_it_found_it() -> None:
    """What belongs on a site plan is the practice's decision, already recorded
    in whatever combination they draw site plans with. A combination built from
    this tool's opinion of somebody else's drawing would be worse than none."""
    connection, transport = connect(["9AM"])

    draw_shadow_series(connection, series(["9AM"]))

    entry = transport.all_parameters_for("CreateLayerCombinations")[0]
    states = {
        row["attributeId"]["guid"]: row["isHidden"]
        for row in entry["layerCombinationDataArray"][0]["layers"]
    }
    assert states["wall"] is False, "was shown, stays shown"
    assert states["dims"] is True, "was hidden, stays hidden"


def test_the_combinations_are_named_for_the_hour_they_show() -> None:
    connection, _ = connect(["9AM"])

    report = draw_shadow_series(connection, series(["9AM"]))

    assert report.combinations == (combination_name("9AM"),)
    assert naming.prefix() in report.combinations[0]


# -- rerunning -------------------------------------------------------------


def test_a_rerun_clears_its_own_fills_before_drawing_again() -> None:
    """Otherwise the second run stacks a set of fills on the first and every
    shadow quietly doubles in contour weight."""
    connection, _ = connect(["9AM", "12PM"], stale=3)

    report = draw_shadow_series(connection, series(["9AM", "12PM"]))

    assert report.cleared == 6, "three fills off each of the two layers"


# -- what the run says out loud --------------------------------------------


def test_an_hour_with_the_sun_down_is_named_rather_than_drawn_black() -> None:
    grid = ground_plane_grid(
        (np.array([-40.0, -5.0, 0.0]), np.array([10.0, 5.0, 20.0])),
        datum_m=0.0,
        spacing_m=4.0,
        margin_m=20.0,
    )
    night = cast_shadows(
        grid,
        sources=default_sources(
            box((-40.0, -5.0, 0.0), (-30.0, 5.0, 10.0)),
            box((0.0, -5.0, 0.0), (10.0, 5.0, 20.0)),
        ),
        sun_vectors=np.array([[-1.0, 0.0, -0.5]]),
        moments=[dt.datetime(2024, 6, 21, 17, tzinfo=dt.UTC)],
        labels=["5PM"],
        spacing_m=4.0,
    )
    connection, _ = connect(["5PM"])

    report = draw_shadow_series(connection, night)

    assert report.below_horizon == ("5PM",)
    assert "below the horizon" in report.describe()


def test_terrain_in_the_context_is_a_warning_and_not_a_silent_grey_sheet() -> None:
    """It cannot be seen in the drawing: a solid grey sheet is exactly what a
    dense city at 9am looks like to somebody who was not there."""
    grid = ground_plane_grid(
        (np.array([-40.0, -5.0, 0.0]), np.array([10.0, 5.0, 20.0])),
        datum_m=0.0,
        spacing_m=4.0,
        margin_m=20.0,
    )
    under_a_lid = cast_shadows(
        grid,
        sources=default_sources(
            box((-300.0, -300.0, 5.0), (300.0, 300.0, 6.0)),
            box((0.0, -5.0, 0.0), (10.0, 5.0, 20.0)),
        ),
        sun_vectors=WEST_45,
        moments=[dt.datetime(2024, 6, 21, 12, tzinfo=dt.UTC)],
        labels=["12PM"],
        spacing_m=4.0,
    )
    connection, _ = connect(["12PM"])

    report = draw_shadow_series(connection, under_a_lid)

    assert "WARNING" in report.describe()
    assert "site mesh" in report.describe()


def test_the_areas_are_reported_so_nobody_scales_them_off_the_drawing() -> None:
    connection, _ = connect(["9AM"])

    report = draw_shadow_series(connection, series(["9AM"]))

    assert report.areas_m2["9AM"][EXISTING] > 0.0
    assert report.areas_m2["9AM"][ADDITIONAL] > 0.0
    assert "m2" in report.describe()


# -- the legend a six-row sheet needs -------------------------------------


def test_baselines_are_grey_and_scenarios_are_blue_in_the_sheets_own_colours() -> None:
    """The ramps are sampled out of SSDA 401's legend, so a run reproduces the
    sheet rather than approximating it: what will be there recedes in grey,
    what is being argued about comes forward in blue."""
    from sun_study.archicad.shadows import BASELINE_RAMP, SCENARIO_RAMP, styles_for
    from sun_study.core.shadow import BASELINE, SCENARIO, SourceSpec

    styles = styles_for(
        [
            SourceSpec("existing", "Existing neighbouring buildings", BASELINE),
            SourceSpec("future", "Future neighbouring context buildings", BASELINE),
            SourceSpec("on site", "Existing structures within the site", BASELINE),
            SourceSpec("tod", "TOD", SCENARIO),
            SourceSpec("sears", "SEARs", SCENARIO),
            SourceSpec("proposed", "Proposed building envelope", SCENARIO),
        ]
    )

    assert [styles[key].rgb for key in ("existing", "future", "on site")] == list(BASELINE_RAMP)
    assert [styles[key].rgb for key in ("tod", "sears", "proposed")] == list(SCENARIO_RAMP)
    # The legend row is the sheet's words, upper-cased the way a title block is.
    assert styles["on site"].label == "EXISTING STRUCTURES WITHIN THE SITE"
    # One pen each, so a pen table can be matched row by row.
    assert len({style.fill_pen for style in styles.values()}) == 6


def test_every_baseline_is_drawn_under_every_scenario() -> None:
    """Scenarios genuinely overlap, so this decides what a reader sees and not
    merely which hairline wins on a shared edge."""
    from sun_study.archicad.shadows import drawing_order
    from sun_study.core.shadow import BASELINE, SCENARIO, SourceSpec

    order = drawing_order(
        [
            SourceSpec("tod", "TOD", SCENARIO),
            SourceSpec("existing", "Existing", BASELINE),
            SourceSpec("proposed", "Proposed", SCENARIO),
            SourceSpec("future", "Future", BASELINE),
        ]
    )

    assert order == ("existing", "future", "tod", "proposed")


# -- the frame the fills are drawn in --------------------------------------


def test_every_ring_is_moved_into_the_project_frame() -> None:
    """The bug this exists to end. A shadow is computed where the exporter put
    the geometry and drawn where Archicad keeps it, and with a Survey Point
    export those differ by the site's north angle -- 31.5 degrees on the Crows
    Nest model. Drawn uncorrected, every fill is the right shape in the wrong
    place at the wrong angle, and the sheet looks perfectly plausible."""
    import numpy as np

    from sun_study.core.geometry import PlanTransform, rotation_about_z

    turn = PlanTransform(
        rotation=rotation_about_z(-31.516)[:2, :2],
        offset=np.array([12.0, -5.0]),
        rmse_m=0.0,
    )
    connection, transport = connect(["9AM"])
    drawn = series(["9AM"])
    draw_shadow_series(connection, drawn, transform=turn)
    turned = [h["coordinates"] for h in hatches(transport)]

    connection, transport = connect(["9AM"])
    draw_shadow_series(connection, drawn)
    plain = [h["coordinates"] for h in hatches(transport)]

    assert len(turned) == len(plain) and turned, "the same fills, moved"
    # Every one of them, not merely the first: a half-corrected drawing still
    # tiles and still looks like a shadow while sitting off the model.
    for moved, original in zip(turned, plain, strict=True):
        source = np.array([[p["x"], p["y"]] for p in original])
        assert np.allclose(np.array([[p["x"], p["y"]] for p in moved]), turn.apply(source))


def test_no_transform_leaves_the_coordinates_alone() -> None:
    """A project that cannot be joined back to its export loses the correction
    and is told so; it must not lose the drawing, or silently gain a rotation
    nobody fitted."""
    connection, transport = connect(["9AM"])
    draw_shadow_series(connection, series(["9AM"]), transform=None)

    assert hatches(transport), "still drawn"


def test_a_favourite_replaces_the_contour_rather_than_fighting_it() -> None:
    """CreateHatches has a contour pen and no switch to turn the contour off,
    so a contour-less fill can only come from a Favorite. A Favorite's
    settings are applied first and the explicit fields over the top, so naming
    a contour pen alongside it would put back the very thing it exists to
    remove."""
    connection, transport = connect(["9AM"])
    draw_shadow_series(connection, series(["9AM"]), favourite="LORIINI")
    drawn = hatches(transport)

    assert drawn
    assert all(h.get("favoriteName") == "LORIINI" for h in drawn)
    assert all("contourPenIndex" not in h for h in drawn), "the Favorite decides it"
    # And the background, which decides whether the fill is opaque. A shadow
    # diagram is read *through*: the roads, the boundaries and the neighbours
    # are underneath it, and an opaque fill is a grey rectangle over them.
    assert all("fillBackgroundPenIndex" not in h for h in drawn)
    # The pen still does separate one legend row from the next; that is not
    # the Favorite's business.
    assert len({h["fillPenIndex"] for h in drawn}) > 1


def test_without_a_favourite_the_contour_is_hidden_in_the_fills_own_pen() -> None:
    """The nearest thing to contour-less this add-on reaches unaided."""
    connection, transport = connect(["9AM"])
    draw_shadow_series(connection, series(["9AM"]))
    drawn = hatches(transport)

    assert drawn
    assert all("favoriteName" not in h for h in drawn)
    assert all(h["contourPenIndex"] == h["fillPenIndex"] for h in drawn)
    assert all("fillBackgroundPenIndex" in h for h in drawn), "no Favorite to ask"


def test_a_favourite_the_project_does_not_have_is_refused() -> None:
    """CreateHatches takes an unknown favoriteName and draws the hatch anyway,
    with the tool's own settings and a contour round every cell. That is a
    plan of boxes rather than a shadow, and it reads as a drawing defect
    rather than as a missing Favorite."""
    connection, _ = connect(["9AM"])

    with pytest.raises(ArchicadError, match="No Fill favorite"):
        draw_shadow_series(connection, series(["9AM"]), favourite="Nope")
