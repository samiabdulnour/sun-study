"""The Archicad adapter, exercised through a fake transport.

There is no Archicad in CI, and there never will be, so what a machine can
check here is bounded: the *shape* of every request this tool sends, and its
handling of every response shape the add-on can return. That bound is the
reason the adapter is kept thin and free of analysis logic.

What these tests deliberately cannot prove is that Archicad accepts the
requests -- for that there is the checklist in ``docs/archicad.md``, to be run
by a human at a workstation. Every request shape asserted here was read out of
Tapir's own sources rather than recalled, and the source is named in the test.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from sun_study.archicad.connection import (
    MINIMUM_TAPIR_VERSION,
    TAPIR_NAMESPACE,
    ArchicadConnection,
    ArchicadError,
    CommandFailedError,
    HttpTransport,
    TapirUnavailableError,
)
from sun_study.archicad.read import (
    GeoreferencingMismatchError,
    classification_items_of,
    cross_check_georeferencing,
    elements_by_ifc_ids,
    export_ifc,
    ifc_ids_of_elements,
    north_bearing_deg,
    project_info,
    read_geo_location,
    zones,
)
from sun_study.archicad.write import (
    APARTMENT_PROPERTIES,
    PROPERTY_GROUP_NAME,
    existing_properties,
    init_properties,
    write_assessment,
)
from sun_study.ingest.ifc import read_ifc
from sun_study.rules.assessment import ApartmentResult, BuildingAssessment
from sun_study.rules.ruleset import Continuity

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_building.ifc"


class FakeTransport:
    """Answers from a script, and records exactly what it was asked.

    Keyed by command name -- for Tapir calls, the *inner* command name -- so a
    test reads as a list of the exchanges it expects rather than as a queue
    whose order has to be maintained by hand.
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.sent: list[dict[str, Any]] = []

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(payload)
        command = payload["command"]
        if command == "API.ExecuteAddOnCommand":
            inner = payload["parameters"]["addOnCommandId"]["commandName"]
            if inner not in self.responses:
                raise AssertionError(f"unscripted Tapir command {inner!r}")
            return {"succeeded": True, "result": {"addOnCommandResponse": self.responses[inner]}}
        if command not in self.responses:
            raise AssertionError(f"unscripted official command {command!r}")
        return {"succeeded": True, "result": self.responses[command]}

    def parameters_for(self, command: str) -> dict[str, Any]:
        """The add-on parameters of the one call to ``command``."""
        matches = [
            payload["parameters"]["addOnCommandParameters"]
            for payload in self.sent
            if payload["command"] == "API.ExecuteAddOnCommand"
            and payload["parameters"]["addOnCommandId"]["commandName"] == command
        ]
        assert len(matches) == 1, f"expected one {command} call, got {len(matches)}"
        return dict(matches[0])

    def commands(self) -> list[str]:
        return [
            payload["parameters"]["addOnCommandId"]["commandName"]
            if payload["command"] == "API.ExecuteAddOnCommand"
            else payload["command"]
            for payload in self.sent
        ]


def connect(responses: dict[str, Any]) -> tuple[ArchicadConnection, FakeTransport]:
    transport = FakeTransport({"GetAddOnVersion": {"version": "1.5.7"}, **responses})
    return ArchicadConnection(transport), transport


# -- the protocol envelope ------------------------------------------------
# Verified against builtin-scripts/aclib/__init__.py and
# sandbox/python-package/src/tapir_py/core.py in the Tapir repository.


def test_tapir_calls_are_wrapped_in_execute_add_on_command() -> None:
    connection, transport = connect({"GetProjectInfo": {"isUntitled": False}})
    connection.run_tapir("GetProjectInfo", {"a": 1})

    assert transport.sent == [
        {
            "command": "API.ExecuteAddOnCommand",
            "parameters": {
                "addOnCommandId": {
                    "commandNamespace": TAPIR_NAMESPACE,
                    "commandName": "GetProjectInfo",
                },
                "addOnCommandParameters": {"a": 1},
            },
        }
    ]


def test_official_commands_are_not_wrapped() -> None:
    connection, transport = connect({"API.GetAllClassificationSystems": {"a": 1}})
    connection.run_official("API.GetAllClassificationSystems")

    assert transport.sent == [{"command": "API.GetAllClassificationSystems", "parameters": {}}]


def test_outer_failure_is_raised() -> None:
    class Failing:
        def send(self, payload: dict[str, Any]) -> dict[str, Any]:
            return {"succeeded": False, "error": {"code": 7, "message": "no project open"}}

    connection = ArchicadConnection(Failing())
    with pytest.raises(CommandFailedError, match="no project open"):
        connection.run_official("API.GetAllClassificationSystems")


def test_inner_failure_is_raised_even_though_the_outer_call_succeeded() -> None:
    """The dangerous case: Tapir's own client prints this and returns None.

    A caller that only checks ``succeeded`` reads the error object as data.
    """

    class InnerFailure:
        def send(self, payload: dict[str, Any]) -> dict[str, Any]:
            return {
                "succeeded": True,
                "result": {
                    "addOnCommandResponse": {"error": {"code": 3, "message": "bad parameters"}}
                },
            }

    connection = ArchicadConnection(InnerFailure())
    with pytest.raises(CommandFailedError, match="bad parameters"):
        connection.run_tapir("GetProjectInfo")


def test_missing_add_on_response_names_the_add_on() -> None:
    class NoAddOn:
        def send(self, payload: dict[str, Any]) -> dict[str, Any]:
            return {"succeeded": True, "result": {}}

    connection = ArchicadConnection(NoAddOn())
    with pytest.raises(TapirUnavailableError, match="Tapir add-on is probably not installed"):
        connection.run_tapir("GetProjectInfo")


def test_old_tapir_is_rejected_with_the_reason() -> None:
    transport = FakeTransport({"GetAddOnVersion": {"version": "1.4.0"}})
    connection = ArchicadConnection(transport)
    with pytest.raises(TapirUnavailableError, match="GetElementsByIFCIds"):
        connection.require_tapir()


def test_minimum_version_matches_the_newest_command_used() -> None:
    """``GetElementsByIFCIds`` arrived in 1.5.1 and is the binding constraint.

    If a newer command is adopted, this constant has to move with it, or a
    workstation on an older add-on fails on that command rather than at the
    handshake.
    """
    assert MINIMUM_TAPIR_VERSION == (1, 5, 1)


def test_version_is_read_once_and_cached() -> None:
    connection, transport = connect({})
    assert connection.require_tapir() == "1.5.7"
    assert connection.require_tapir() == "1.5.7"
    assert transport.commands() == ["GetAddOnVersion"]


def test_unreachable_archicad_names_the_setting_to_check() -> None:
    transport = HttpTransport(host="http://127.0.0.1", port=1, timeout_seconds=0.5)
    with pytest.raises(ArchicadError, match="JSON"):
        transport.send({"command": "API.GetAllClassificationSystems", "parameters": {}})


# -- georeferencing -------------------------------------------------------


def test_geo_location_keeps_the_raw_radians_and_derives_the_bearing() -> None:
    connection, _ = connect(
        {
            "GetGeoLocation": {
                "projectLocation": {
                    "latitude": -33.8373,
                    "longitude": 151.0436,
                    "altitude": 12.5,
                    "north": 0.5,
                },
                "surveyPoint": {},
            }
        }
    )
    location = read_geo_location(connection)

    assert location.latitude_deg == pytest.approx(-33.8373)
    assert location.north_radians == pytest.approx(0.5)
    assert location.project_north_bearing_deg == pytest.approx(math.degrees(0.5) - 90.0 + 360.0)


def test_archicads_default_north_means_project_y_is_true_north() -> None:
    """The check that makes the 90 degree offset believable rather than fitted.

    ``placeInfo.north`` measures true north counter-clockwise from project +X,
    so a project nobody has rotated reports pi/2, not 0 -- and pi/2 has to come
    out as a bearing of zero. An offset of 0 would make an untouched project
    report 90 degrees of rotation, which no Archicad user has ever seen.
    """
    assert north_bearing_deg(math.pi / 2) == pytest.approx(0.0)


def test_north_bearing_is_wrapped_into_zero_to_360() -> None:
    assert north_bearing_deg(0.0) == pytest.approx(270.0)
    assert north_bearing_deg(math.pi) == pytest.approx(90.0)
    assert north_bearing_deg(3 * math.pi) == pytest.approx(90.0)


def test_geo_location_without_a_project_location_says_where_to_set_it() -> None:
    connection, _ = connect({"GetGeoLocation": {"surveyPoint": {}}})
    with pytest.raises(ArchicadError, match="Project Location"):
        read_geo_location(connection)


def _fixture_model() -> Any:
    return read_ifc(FIXTURE)


def _located(model: Any, *, north_deg: float, latitude_offset: float = 0.0) -> Any:
    connection, _ = connect(
        {
            "GetGeoLocation": {
                "projectLocation": {
                    "latitude": model.latitude_deg + latitude_offset,
                    "longitude": model.longitude_deg,
                    "altitude": 0.0,
                    "north": math.radians(north_deg),
                },
                "surveyPoint": {},
            }
        }
    )
    return read_geo_location(connection)


def _archicad_north_for(bearing_deg: float) -> float:
    """The ``placeInfo.north`` Archicad would report for a project +Y bearing."""
    return bearing_deg + 90.0


def test_cross_check_passes_when_both_sources_agree() -> None:
    model = _fixture_model()
    north = _archicad_north_for(model.true_north_bearing_deg)
    cross_check_georeferencing(_located(model, north_deg=north), model)


def test_cross_check_catches_a_moved_site() -> None:
    model = _fixture_model()
    north = _archicad_north_for(model.true_north_bearing_deg)
    with pytest.raises(GeoreferencingMismatchError, match="latitude"):
        cross_check_georeferencing(_located(model, north_deg=north, latitude_offset=1.0), model)


def test_cross_check_catches_a_rotated_export() -> None:
    """The failure it exists for: a north the export lost or re-interpreted.

    The fixture's 30 degree north makes the direction visible; a sign error
    would land on 330 rather than 30 and pass a test built on zero.
    """
    model = _fixture_model()
    assert model.true_north_bearing_deg % 360.0 == pytest.approx(30.0), (
        "the fixture is authored with true north 30 degrees off project north; "
        "this test needs a non-zero north to have any discriminating power"
    )

    wrong = _archicad_north_for(model.true_north_bearing_deg) + 25.0
    with pytest.raises(GeoreferencingMismatchError, match="true north"):
        cross_check_georeferencing(_located(model, north_deg=wrong), model)


def test_cross_check_names_a_flipped_convention_rather_than_just_failing() -> None:
    """If the 90 degree offset is wrong for some build, say so, don't just stop."""
    model = _fixture_model()
    mirrored = _archicad_north_for(-model.true_north_bearing_deg)
    with pytest.raises(GeoreferencingMismatchError, match="NORTH_ANGLE_OFFSET_DEG"):
        cross_check_georeferencing(_located(model, north_deg=mirrored), model)


# The numbers below are measured, not invented: they come from an Archicad 26
# export of a real nine-storey project, cross-read three ways. See D23.
REFERENCE_ARCHICAD_NORTH_RAD = 0.856118
REFERENCE_SITE_ROTATION_DEG = 40.948
REFERENCE_IFC_TRUE_NORTH_DEG = 0.0


def test_a_survey_point_export_is_not_reported_as_a_mismatch() -> None:
    """The false positive this cross-check shipped with, pinned so it stays fixed.

    Archicad's "Survey Point" model position rotates the geometry through
    ``IfcSite``'s placement and then writes ``TrueNorth`` as ``(0,1)``, because
    the world coordinates it produces really are north-aligned. Comparing
    Archicad's project-frame angle against ``TrueNorth`` alone rejected that
    export -- 49 degrees against 0 -- on a file that was entirely correct.

    Only the sum is comparable, and the sum balances.
    """
    model = replace(
        _fixture_model(),
        true_north_bearing_deg=REFERENCE_IFC_TRUE_NORTH_DEG,
        site_rotation_deg=REFERENCE_SITE_ROTATION_DEG,
    )
    location = _located(model, north_deg=math.degrees(REFERENCE_ARCHICAD_NORTH_RAD))

    # Both routes must land on the same project +Y bearing.
    assert location.project_north_bearing_deg == pytest.approx(319.052, abs=1e-3)
    cross_check_georeferencing(location, model)


def test_the_two_export_modes_are_indistinguishable_to_the_cross_check() -> None:
    """Whichever half of the file carries the angle, the sum is the same.

    That is what lets this work without detecting the export mode, which is
    not recorded anywhere a reader could rely on.
    """
    base = _fixture_model()
    survey_point = replace(
        base, true_north_bearing_deg=0.0, site_rotation_deg=REFERENCE_SITE_ROTATION_DEG
    )
    project_origin = replace(
        base, true_north_bearing_deg=-REFERENCE_SITE_ROTATION_DEG, site_rotation_deg=0.0
    )
    location = _located(base, north_deg=math.degrees(REFERENCE_ARCHICAD_NORTH_RAD))

    cross_check_georeferencing(location, survey_point)
    cross_check_georeferencing(location, project_origin)


# -- elements -------------------------------------------------------------


def test_project_info_reads_the_fields_it_needs() -> None:
    connection, _ = connect(
        {
            "GetProjectInfo": {
                "isUntitled": False,
                "isTeamwork": True,
                "projectName": "Tower",
                "projectPath": "//bimcloud/Tower.pln",
            }
        }
    )
    info = project_info(connection)
    assert info.name == "Tower"
    assert info.is_teamwork
    assert "Teamwork" in info.describe()


def test_zones_are_requested_by_type_and_joined_to_their_details() -> None:
    connection, transport = connect(
        {
            "GetElementsByType": {
                "elements": [
                    {"elementId": {"guid": "guid-b"}},
                    {"elementId": {"guid": "guid-a"}},
                ]
            },
            "GetDetailsOfElements": {
                "detailsOfElements": [
                    {
                        "type": "Zone",
                        "id": "2",
                        "floorIndex": 1,
                        "layerIndex": 3,
                        "drawIndex": 0,
                        "details": {"name": "Living", "numberStr": "G02"},
                    },
                    {
                        "type": "Zone",
                        "id": "1",
                        "floorIndex": 0,
                        "layerIndex": 3,
                        "drawIndex": 0,
                        "details": {"name": "Studio", "numberStr": "G01"},
                    },
                ]
            },
        }
    )
    found = zones(connection)

    assert transport.parameters_for("GetElementsByType") == {"elementType": "Zone"}
    assert transport.parameters_for("GetDetailsOfElements") == {
        "elements": [{"elementId": {"guid": "guid-b"}}, {"elementId": {"guid": "guid-a"}}]
    }
    assert [zone.label for zone in found] == ["G01 Studio", "G02 Living"]
    assert found[0].guid == "guid-a"
    assert found[0].storey_index == 0


def test_zones_refuse_a_details_list_of_the_wrong_length() -> None:
    """Tapir returns details positionally, so a short list silently misaligns."""
    connection, _ = connect(
        {
            "GetElementsByType": {
                "elements": [{"elementId": {"guid": "a"}}, {"elementId": {"guid": "b"}}]
            },
            "GetDetailsOfElements": {"detailsOfElements": [{"details": {}}]},
        }
    )
    with pytest.raises(ArchicadError, match="parallel"):
        zones(connection)


def test_no_zones_short_circuits_before_asking_for_details() -> None:
    connection, transport = connect({"GetElementsByType": {"elements": []}})
    assert zones(connection) == ()
    assert transport.commands() == ["GetElementsByType"]


def test_elements_by_ifc_ids_reports_missing_and_ambiguous_matches() -> None:
    connection, transport = connect(
        {
            "GetElementsByIFCIds": {
                "elementsByIFCIds": [
                    {"ifcId": "one", "elements": [{"elementId": {"guid": "x"}}]},
                    {
                        "ifcId": "many",
                        "elements": [
                            {"elementId": {"guid": "y"}},
                            {"elementId": {"guid": "z"}},
                        ],
                    },
                ]
            }
        }
    )
    mapping = elements_by_ifc_ids(connection, ["one", "many", "none"])

    assert transport.parameters_for("GetElementsByIFCIds") == {"ifcIds": ["one", "many", "none"]}
    assert mapping == {"one": ["x"], "many": ["y", "z"], "none": []}


def test_ifc_ids_of_elements_skips_errors_and_falls_back_to_the_external_id() -> None:
    connection, _ = connect(
        {
            "GetIFCIdsOfElements": {
                "elementIFCIds": [
                    {"elementId": {"guid": "a"}, "ifcId": "IFC-A", "externalIFCId": ""},
                    {"error": {"code": 1, "message": "gone"}},
                    {"elementId": {"guid": "c"}, "ifcId": "", "externalIFCId": "EXT-C"},
                ]
            }
        }
    )
    assert ifc_ids_of_elements(connection, ["a", "b", "c"]) == {"a": "IFC-A", "c": "EXT-C"}


def test_classifications_accept_both_shapes_and_drop_the_null_guid() -> None:
    """Tapir's schema wraps each entry; ``ClassificationCommands.cpp`` does not.

    Both are accepted rather than betting on which one a build sends, and the
    null GUID -- how Archicad reports "unclassified" -- is not a real item.
    """
    connection, transport = connect(
        {
            "API.GetAllClassificationSystems": {
                "classificationSystems": [{"classificationSystemId": {"guid": "sys-1"}}]
            },
            "GetClassificationsOfElements": {
                "elementClassifications": [
                    {
                        "classificationIds": [
                            {
                                "classificationId": {
                                    "classificationSystemId": {"guid": "sys-1"},
                                    "classificationItemId": {"guid": "item-wrapped"},
                                }
                            }
                        ]
                    },
                    {
                        "classificationIds": [
                            {
                                "classificationSystemId": {"guid": "sys-1"},
                                "classificationItemId": {"guid": "item-bare"},
                            }
                        ]
                    },
                    {
                        "classificationIds": [
                            {
                                "classificationSystemId": {"guid": "sys-1"},
                                "classificationItemId": {
                                    "guid": "00000000-0000-0000-0000-000000000000"
                                },
                            }
                        ]
                    },
                ]
            },
        }
    )
    found = classification_items_of(connection, ["a", "b", "unclassified"])

    assert transport.parameters_for("GetClassificationsOfElements")["classificationSystemIds"] == [
        {"classificationSystemId": {"guid": "sys-1"}}
    ]
    assert found == {"a": {"item-wrapped"}, "b": {"item-bare"}}


def test_classification_systems_must_exist() -> None:
    connection, _ = connect({"API.GetAllClassificationSystems": {"classificationSystems": []}})
    with pytest.raises(ArchicadError, match="Classification Manager"):
        classification_items_of(connection, ["a"])


# -- IFC export -----------------------------------------------------------


def test_export_ifc_sends_the_save_operation_and_returns_the_path(tmp_path: Path) -> None:
    target = tmp_path / "export" / "model.ifc"

    class WritingTransport(FakeTransport):
        def send(self, payload: dict[str, Any]) -> dict[str, Any]:
            if payload["command"] == "API.ExecuteAddOnCommand":
                name = payload["parameters"]["addOnCommandId"]["commandName"]
                if name == "IFCFileOperation":
                    target.write_text("ISO-10303-21;")
            return super().send(payload)

    transport = WritingTransport({"IFCFileOperation": {"success": True}})
    connection = ArchicadConnection(transport)

    assert export_ifc(connection, target) == target.resolve()
    assert transport.parameters_for("IFCFileOperation") == {
        "method": "save",
        "ifcFilePath": str(target.resolve()),
        "fileType": "ifc",
    }


def test_export_ifc_refuses_to_report_success_when_no_file_appeared(tmp_path: Path) -> None:
    connection, _ = connect({"IFCFileOperation": {"success": True}})
    with pytest.raises(ArchicadError, match="does not"):
        export_ifc(connection, tmp_path / "missing.ifc")


def test_export_ifc_rejects_a_stale_file(tmp_path: Path) -> None:
    """Analysing yesterday's export is the quiet failure this catches."""
    target = tmp_path / "stale.ifc"
    target.write_text("ISO-10303-21;")

    connection, _ = connect({"IFCFileOperation": {"success": True}})
    with pytest.raises(ArchicadError, match="was not modified"):
        export_ifc(connection, target)


# -- properties -----------------------------------------------------------


def _property_catalogue(names: list[str]) -> dict[str, Any]:
    return {
        "properties": [
            {
                "propertyId": {"guid": f"prop-{index}"},
                "propertyType": "Custom",
                "propertyGroupName": PROPERTY_GROUP_NAME,
                "propertyName": name,
                "propertyCollectionType": "Single",
                "propertyValueType": "String",
                "propertyMeasureType": "Default",
                "propertyIsEditable": True,
                "isExpressionBased": False,
            }
            for index, name in enumerate(names)
        ]
        + [
            {
                "propertyId": {"guid": "other"},
                "propertyType": "StaticBuiltIn",
                "propertyGroupName": "General",
                "propertyName": "Living Room Sunlight (h)",
                "propertyCollectionType": "Single",
                "propertyValueType": "String",
                "propertyMeasureType": "Default",
                "propertyIsEditable": True,
                "isExpressionBased": False,
            }
        ]
    }


def test_existing_properties_ignores_identically_named_properties_elsewhere() -> None:
    connection, _ = connect({"GetAllProperties": _property_catalogue(["Living Room Sunlight (h)"])})
    assert existing_properties(connection) == {"Living Room Sunlight (h)": "prop-0"}


class CreatingTransport(FakeTransport):
    """A project where ``CreatePropertyDefinitions`` actually creates things.

    ``init_properties`` reads the catalogue, creates what is missing, then
    reads the catalogue again to pick up the new identifiers. A static fake
    would let a version that never re-read still pass.
    """

    def __init__(self, present: list[str], all_names: list[str]) -> None:
        super().__init__(
            {
                "GetAddOnVersion": {"version": "1.5.7"},
                "GetAllProperties": _property_catalogue(present),
                "CreatePropertyGroups": {"propertyGroupIds": [{"propertyGroupId": {"guid": "g"}}]},
                "CreatePropertyDefinitions": {
                    "propertyIds": [
                        {"propertyId": {"guid": f"new-{i}"}}
                        for i in range(len(all_names) - len(present))
                    ]
                },
            }
        )
        self.all_names = all_names

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = super().send(payload)
        if self.commands()[-1] == "CreatePropertyDefinitions":
            self.responses["GetAllProperties"] = _property_catalogue(self.all_names)
        return result


def test_init_properties_creates_only_what_is_missing() -> None:
    all_names = [spec.name for spec in APARTMENT_PROPERTIES]
    transport = CreatingTransport(all_names[:2], all_names)
    connection = ArchicadConnection(transport)

    found = init_properties(connection, {"zone-1": {"item-a"}, "zone-2": {"item-a", "item-b"}})

    definitions = transport.parameters_for("CreatePropertyDefinitions")["propertyDefinitions"]
    assert [d["propertyDefinition"]["name"] for d in definitions] == all_names[2:]
    assert definitions[0]["propertyDefinition"]["group"] == {"name": PROPERTY_GROUP_NAME}
    assert definitions[0]["propertyDefinition"]["availability"] == [
        {"classificationItemId": {"guid": "item-a"}},
        {"classificationItemId": {"guid": "item-b"}},
    ]
    assert definitions[0]["propertyDefinition"]["isEditable"] is True

    # Every property is resolvable afterwards, including the ones just made:
    # this is what a later write-back depends on.
    assert set(found) == set(all_names)
    assert transport.commands().count("GetAllProperties") == 2


def test_init_properties_is_a_no_op_when_everything_exists() -> None:
    all_names = [spec.name for spec in APARTMENT_PROPERTIES]
    connection, transport = connect({"GetAllProperties": _property_catalogue(all_names)})

    init_properties(connection, {"zone-1": {"item-a"}})
    assert transport.commands() == ["GetAllProperties"]


def test_init_properties_refuses_when_nothing_is_classified() -> None:
    connection, _ = connect({"GetAllProperties": _property_catalogue([])})
    with pytest.raises(ArchicadError, match="classification"):
        init_properties(connection, {"zone-1": set()})


def test_init_properties_surfaces_a_per_definition_error() -> None:
    """Tapir reports these inside a successful response, one slot per input."""
    connection, _ = connect(
        {
            "GetAllProperties": _property_catalogue([]),
            "CreatePropertyGroups": {"propertyGroupIds": [{"propertyGroupId": {"guid": "g"}}]},
            "CreatePropertyDefinitions": {
                "propertyIds": [{"propertyId": {"guid": "ok"}}] * (len(APARTMENT_PROPERTIES) - 1)
                + [{"error": {"code": 4, "message": "name already used"}}]
            },
        }
    )
    with pytest.raises(ArchicadError, match="name already used"):
        init_properties(connection, {"zone-1": {"item-a"}})


# -- write-back -----------------------------------------------------------


def _assessment(*apartments: ApartmentResult) -> BuildingAssessment:
    return BuildingAssessment(
        ruleset_name="nsw_adg",
        ruleset_version="1.0.0",
        area_key="sydney_metro",
        area_label="Sydney Metro",
        minimum_minutes=120.0,
        continuity=Continuity.CUMULATIVE,
        apartments=apartments,
        counted_total=len(apartments),
        meeting_minimum=sum(1 for a in apartments if a.meets_minimum),
        with_no_sunlight=sum(1 for a in apartments if a.receives_no_sunlight),
        compliant_share=1.0,
        no_sunlight_share=0.0,
        required_share=0.7,
        maximum_no_sunlight_share=0.15,
    )


def _apartment(
    apartment_id: str,
    *,
    living: float = 141.0,
    open_space: float | None = 200.0,
    meets: bool = True,
) -> ApartmentResult:
    return ApartmentResult(
        apartment_id=apartment_id,
        apartment_name=f"Apt {apartment_id}",
        living_room_minutes=living,
        open_space_minutes=open_space,
        governing_minutes=living,
        meets_minimum=meets,
        receives_no_sunlight=living == 0.0,
        counted=True,
        note="",
    )


def _write_responses(names: list[str], results: list[Any] | None = None) -> dict[str, Any]:
    return {
        "GetAllProperties": _property_catalogue(names),
        "GetElementsByIFCIds": {
            "elementsByIFCIds": [{"ifcId": "apt-1", "elements": [{"elementId": {"guid": "z1"}}]}]
        },
        "SetPropertyValuesOfElements": {"executionResults": results or []},
    }


def test_write_sends_display_strings_in_hours() -> None:
    names = [spec.name for spec in APARTMENT_PROPERTIES]
    responses = _write_responses(names, [{"success": True}] * len(names))
    connection, transport = connect(responses)

    report = write_assessment(
        connection, _assessment(_apartment("apt-1")), run_stamp="2026-08-13 00:00 UTC"
    )

    values = transport.parameters_for("SetPropertyValuesOfElements")["elementPropertyValues"]
    by_property = {entry["propertyId"]["guid"]: entry["propertyValue"]["value"] for entry in values}
    identifiers = {name: f"prop-{index}" for index, name in enumerate(names)}

    # 141 minutes is 2.35 hours. The decimal point is not a locale question:
    # Tapir's PropertyConversionUtils pins it to '.' regardless of the
    # project's unit preferences.
    assert by_property[identifiers["Living Room Sunlight (h)"]] == "2.35"
    assert by_property[identifiers["Private Open Space Sunlight (h)"]] == "3.33"
    assert by_property[identifiers["Meets Minimum"]] == "Yes"
    assert by_property[identifiers["No Direct Sunlight"]] == "No"
    assert by_property[identifiers["Sun Study Run"]] == "2026-08-13 00:00 UTC"
    assert "nsw_adg@1.0.0" in by_property[identifiers["Sun Study Ruleset"]]

    assert all(entry["elementId"] == {"guid": "z1"} for entry in values)
    assert report.complete
    assert report.values_written == len(names)


def test_an_apartment_without_open_space_leaves_that_property_undefined() -> None:
    names = [spec.name for spec in APARTMENT_PROPERTIES]
    responses = _write_responses(names, [{"success": True}] * (len(names) - 1))
    connection, transport = connect(responses)

    report = write_assessment(connection, _assessment(_apartment("apt-1", open_space=None)))

    values = transport.parameters_for("SetPropertyValuesOfElements")["elementPropertyValues"]
    identifiers = {name: f"prop-{index}" for index, name in enumerate(names)}
    written = {entry["propertyId"]["guid"] for entry in values}

    assert identifiers["Private Open Space Sunlight (h)"] not in written
    assert report.values_skipped == 1
    assert report.complete


def test_an_empty_execution_result_is_a_failure_naming_the_cause() -> None:
    """Tapir leaves the slot untouched when the property is not available.

    Reading ``{}`` as success would report a full write of a project where
    every value was silently rejected.
    """
    names = [spec.name for spec in APARTMENT_PROPERTIES]
    responses = _write_responses(names, [{}] + [{"success": True}] * (len(names) - 1))
    connection, _ = connect(responses)

    report = write_assessment(connection, _assessment(_apartment("apt-1")))

    assert not report.complete
    assert report.values_written == len(names) - 1
    assert "not available" in report.failures[0]
    assert "init-properties" in report.failures[0]


def test_a_reported_failure_carries_the_message_through() -> None:
    names = [spec.name for spec in APARTMENT_PROPERTIES]
    responses = _write_responses(
        names,
        [{"success": False, "error": {"code": 9, "message": "element is locked"}}]
        + [{"success": True}] * (len(names) - 1),
    )
    connection, _ = connect(responses)

    report = write_assessment(connection, _assessment(_apartment("apt-1")))
    assert "element is locked" in report.failures[0]


def test_an_unmatched_apartment_is_reported_not_guessed_at() -> None:
    names = [spec.name for spec in APARTMENT_PROPERTIES]
    responses = _write_responses(names, [{"success": True}] * len(names))
    connection, _ = connect(responses)

    report = write_assessment(connection, _assessment(_apartment("apt-1"), _apartment("apt-2")))

    assert report.zones_unmatched == ("Apt apt-2",)
    assert not report.complete


def test_an_ambiguous_apartment_is_skipped() -> None:
    names = [spec.name for spec in APARTMENT_PROPERTIES]
    responses = _write_responses(names, [{"success": True}] * len(names))
    responses["GetElementsByIFCIds"] = {
        "elementsByIFCIds": [
            {"ifcId": "apt-1", "elements": [{"elementId": {"guid": "z1"}}]},
            {
                "ifcId": "apt-2",
                "elements": [{"elementId": {"guid": "a"}}, {"elementId": {"guid": "b"}}],
            },
        ]
    }
    connection, _ = connect(responses)

    report = write_assessment(connection, _assessment(_apartment("apt-1"), _apartment("apt-2")))
    assert report.zones_ambiguous == ("Apt apt-2",)


def test_write_refuses_before_init_properties() -> None:
    connection, _ = connect({"GetAllProperties": _property_catalogue([])})
    with pytest.raises(ArchicadError, match="init-properties"):
        write_assessment(connection, _assessment(_apartment("apt-1")))


def test_write_refuses_a_result_list_of_the_wrong_length() -> None:
    """Without a parallel list there is no way to say which values landed."""
    names = [spec.name for spec in APARTMENT_PROPERTIES]
    connection, _ = connect(_write_responses(names, [{"success": True}]))
    with pytest.raises(ArchicadError, match="parallel"):
        write_assessment(connection, _assessment(_apartment("apt-1")))


# -- the property definitions themselves ----------------------------------


def test_no_property_is_declared_boolean() -> None:
    """Decision D21: the boolean display-string form is not documented anywhere.

    A string set from a string cannot be misparsed; a boolean set from a
    guessed literal can be, silently and in the wrong direction.
    """
    assert not any(spec.data_type == "boolean" for spec in APARTMENT_PROPERTIES)


def test_property_types_are_all_in_tapirs_enumeration() -> None:
    known = {
        "number", "integer", "string", "boolean", "length", "area", "volume", "angle",
        "numberList", "integerList", "stringList", "booleanList", "lengthList", "areaList",
        "volumeList", "angleList", "singleEnum", "multiEnum",
    }  # fmt: skip
    assert {spec.data_type for spec in APARTMENT_PROPERTIES} <= known


def test_every_property_has_a_description() -> None:
    """The description is what a colleague reads in the Property Manager."""
    assert all(len(spec.description) > 30 for spec in APARTMENT_PROPERTIES)


def test_property_names_are_unique() -> None:
    names = [spec.name for spec in APARTMENT_PROPERTIES]
    assert len(set(names)) == len(names)


def test_every_property_is_given_a_value() -> None:
    """A property created but never written is a permanently empty column."""
    from sun_study.archicad.write import _values_for

    values = _values_for(_assessment(_apartment("a")), _apartment("a"), "stamp")
    assert set(values) == {spec.name for spec in APARTMENT_PROPERTIES}


def test_requests_are_json_serialisable() -> None:
    """Everything sent must survive ``json.dumps`` -- no numpy scalars."""
    names = [spec.name for spec in APARTMENT_PROPERTIES]
    connection, transport = connect(_write_responses(names, [{"success": True}] * len(names)))
    write_assessment(connection, _assessment(_apartment("apt-1")))
    for payload in transport.sent:
        json.dumps(payload)
