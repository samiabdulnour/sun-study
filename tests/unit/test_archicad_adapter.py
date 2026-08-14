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

import contextlib
import http.server
import json
import math
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from sun_study.archicad.connection import (
    DEFAULT_PORT,
    MINIMUM_TAPIR_VERSION,
    PORT_RANGE,
    TAPIR_NAMESPACE,
    ArchicadConnection,
    ArchicadError,
    CommandFailedError,
    HttpTransport,
    Instance,
    TapirUnavailableError,
    find_instances,
    where_archicad_actually_is,
)
from sun_study.archicad.draw import (
    DEFAULT_BANDS,
    DEFAULT_LAYER_NAME,
    BandStyle,
    Pen,
    band_for,
    draw_assessment,
    indistinguishable_bands,
    match_pens,
    pen_table,
)
from sun_study.archicad.layout import (
    NavigatorItem,
    layout_results,
    project_map,
    storey_items,
)
from sun_study.archicad.read import (
    GeoreferencingMismatchError,
    classification_items_of,
    cross_check_georeferencing,
    elements_by_ifc_ids,
    export_ifc,
    ifc_ids_of_elements,
    layer_names,
    north_bearing_deg,
    project_info,
    read_geo_location,
    zones,
)
from sun_study.archicad.write import (
    APARTMENT_PROPERTIES,
    PROPERTY_GROUP_NAME,
    all_properties,
    default_property_value,
    diagnose_write_access,
    enum_values,
    existing_properties,
    init_properties,
    write_assessment,
)
from sun_study.ingest.ifc import read_ifc
from sun_study.rules.assessment import ApartmentResult, BuildingAssessment
from sun_study.rules.ruleset import Continuity

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_building.ifc"


class Sequential:
    """Different answers to repeated calls of one command.

    Only needed where a command is legitimately called more than once with
    different parameters -- ``GetPenTables``, which is asked for the active
    flag and then for that table's pens. The last answer repeats, so a test
    does not have to count calls it does not care about.
    """

    def __init__(self, *responses: dict[str, Any]) -> None:
        assert responses, "Sequential needs at least one response"
        self.responses = list(responses)
        self.calls = 0

    def next(self) -> dict[str, Any]:
        answer = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return answer


class FakeTransport:
    """Answers from a script, and records exactly what it was asked.

    Keyed by command name -- for Tapir calls, the *inner* command name -- so a
    test reads as a list of the exchanges it expects rather than as a queue
    whose order has to be maintained by hand. A ``Sequential`` value covers
    the few commands that are called twice with different parameters.
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.sent: list[dict[str, Any]] = []

    def _answer(self, command: str, kind: str) -> dict[str, Any]:
        if command not in self.responses:
            raise AssertionError(f"unscripted {kind} command {command!r}")
        scripted = self.responses[command]
        return scripted.next() if isinstance(scripted, Sequential) else scripted

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(payload)
        command = payload["command"]
        if command == "API.ExecuteAddOnCommand":
            inner = payload["parameters"]["addOnCommandId"]["commandName"]
            return {
                "succeeded": True,
                "result": {"addOnCommandResponse": self._answer(inner, "Tapir")},
            }
        return {"succeeded": True, "result": self._answer(command, "official")}

    def all_parameters_for(self, command: str) -> list[dict[str, Any]]:
        """The add-on parameters of every call to ``command``, in order."""
        return [
            dict(payload["parameters"]["addOnCommandParameters"])
            for payload in self.sent
            if payload["command"] == "API.ExecuteAddOnCommand"
            and payload["parameters"]["addOnCommandId"]["commandName"] == command
        ]

    def parameters_for(self, command: str) -> dict[str, Any]:
        """The add-on parameters of the one call to ``command``."""
        matches = self.all_parameters_for(command)
        assert len(matches) == 1, f"expected one {command} call, got {len(matches)}"
        return matches[0]

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


def test_a_failed_send_does_not_go_scanning_for_other_instances() -> None:
    """Building an error message must not do network I/O.

    An earlier version scanned all twenty ports here, so one dead connection
    cost a scan per failed call and the suite went from 14s to 146s. The scan
    belongs on the path that has decided to talk to a human, not on every
    failure.
    """
    transport = HttpTransport(host="http://127.0.0.1", port=1, timeout_seconds=0.5)
    started = time.monotonic()
    with pytest.raises(ArchicadError):
        transport.send({"command": "API.IsAlive", "parameters": {}})
    assert time.monotonic() - started < 2.0, "a refused connection must fail immediately"


# -- finding which Archicad to talk to ------------------------------------


class _FakeArchicad(http.server.BaseHTTPRequestHandler):
    """Answers the two commands the port scan uses, and nothing else."""

    project_name = "2400_SAMPLE1"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if payload.get("command") == "API.IsAlive":
            body = {"succeeded": True, "result": {"isAlive": True}}
        else:
            body = {
                "succeeded": True,
                "result": {"addOnCommandResponse": {"projectName": self.project_name}},
            }
        encoded = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence, or the suite's output is buried in request logs.

        The parameter shadows a builtin because ``http.server`` names it that.
        """


@contextlib.contextmanager
def _fake_archicad() -> Iterator[int]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _FakeArchicad)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_live_instance_is_found_and_names_its_project() -> None:
    """The port is only half the answer -- with several Archicads open, the
    project name is what tells a person which one they want."""
    with _fake_archicad() as port:
        found = find_instances(ports=[port])
    assert found == (Instance(port=port, project="2400_SAMPLE1"),)
    assert found[0].describe() == f"port {port} -- 2400_SAMPLE1"


def test_a_dead_port_is_skipped_rather_than_raising() -> None:
    """Most of the twenty ports are dead in any normal scan."""
    with _fake_archicad() as port:
        found = find_instances(ports=[1, port], timeout_seconds=0.5)
    assert [instance.port for instance in found] == [port]


def test_an_instance_that_will_not_name_its_project_is_still_reported() -> None:
    """An Archicad with no project open answers IsAlive and nothing else.
    Knowing the port is live is most of the value."""

    class Nameless(_FakeArchicad):
        project_name = ""

    server = http.server.HTTPServer(("127.0.0.1", 0), Nameless)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        found = find_instances(ports=[port])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert found == (Instance(port=port, project=""),)
    assert found[0].describe() == f"port {port}"


def test_the_port_hint_names_the_other_instance_and_the_flag_to_use() -> None:
    """The whole point: 'actively refused' sends people to a Work Environment
    setting that was never off, when the real cause is a second Archicad."""
    with _fake_archicad() as port:
        hint = where_archicad_actually_is(tried=1, ports=[port])
    assert "2400_SAMPLE1" in hint
    assert f"--port {port}" in hint


def test_the_port_hint_says_nothing_when_it_has_nothing_to_add() -> None:
    """With no other instance the default message is already right, and a
    second paragraph reporting a fruitless search is noise."""
    assert where_archicad_actually_is(tried=1, ports=[]) == ""


def test_the_port_actually_tried_is_not_offered_as_the_alternative() -> None:
    """Suggesting the port that just failed would be absurd. It can appear in
    the scan when the failure was mid-run rather than at connect."""
    with _fake_archicad() as port:
        assert where_archicad_actually_is(tried=port, ports=[port]) == ""


def test_the_scanned_range_is_the_one_archicad_uses() -> None:
    """From Tapir's own client: range(19723, 19743). Scanning fewer ports
    silently fails to find the instance a person is looking at."""
    assert PORT_RANGE == range(19723, 19743)
    assert DEFAULT_PORT == 19723


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
                # No group yet, so it has to be created and its id used.
                "API.GetAllPropertyGroupIds": {"propertyGroupIds": []},
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
    assert definitions[0]["propertyDefinition"]["group"] == {"propertyGroupId": {"guid": "g"}}, (
        "the group is addressed by identifier, not by name: Tapir resolves a name "
        "by taking the first match, so duplicate group names would misdirect it"
    )
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
            "API.GetAllPropertyGroupIds": {"propertyGroupIds": []},
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
    name: str | None = None,
) -> ApartmentResult:
    return ApartmentResult(
        apartment_id=apartment_id,
        apartment_name=name if name is not None else f"Apt {apartment_id}",
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


def test_a_zone_that_took_nothing_is_counted_separately() -> None:
    """The bug this fixes: a run where six of eight zones refused every value
    still reported writing "across 8 zones", because every matched zone was
    counted whether or not anything landed on it."""
    names = [spec.name for spec in APARTMENT_PROPERTIES]
    refused = {"success": False, "error": {"code": -2130312909, "message": "no access"}}
    responses = _write_responses(names, [refused] * len(names))
    responses["GetElementsByIFCIds"] = {
        "elementsByIFCIds": [
            {"ifcId": "apt-1", "elements": [{"elementId": {"guid": "z1"}}]},
            {"ifcId": "apt-2", "elements": [{"elementId": {"guid": "z2"}}]},
        ]
    }
    responses["SetPropertyValuesOfElements"] = {
        "executionResults": [{"success": True}] * len(names) + [refused] * len(names)
    }
    connection, _ = connect(responses)

    report = write_assessment(connection, _assessment(_apartment("apt-1"), _apartment("apt-2")))

    assert report.zones_written == ("z1",)
    assert report.zones_refused == ("z2",), "z1 took everything, z2 took nothing"
    assert "1 of 2 zones" in report.describe()
    assert "property of those elements, not of the request" in report.describe()


def test_a_zone_that_took_something_is_not_called_refused() -> None:
    """A partial write is a different problem from a locked element, and
    sending a half-written zone to the access diagnosis would mislead."""
    names = [spec.name for spec in APARTMENT_PROPERTIES]
    responses = _write_responses(
        names,
        [{"success": False, "error": {"code": 9, "message": "nope"}}]
        + [{"success": True}] * (len(names) - 1),
    )
    connection, _ = connect(responses)

    report = write_assessment(connection, _assessment(_apartment("apt-1")))
    assert report.zones_written == ("z1",)
    assert report.zones_refused == ()


# -- why a write was refused ----------------------------------------------


def _access_responses(**overrides: Any) -> dict[str, Any]:
    responses: dict[str, Any] = {
        "GetDetailsOfElements": {"detailsOfElements": [{"layerIndex": 4}, {"layerIndex": 4}]},
        "GetAttributesByType": {
            "attributes": [
                {"attributeId": {"guid": "L4"}, "index": 4, "name": "06 | Zone.Apartment"},
                {"attributeId": {"guid": "L9"}, "index": 9, "name": "something else"},
            ]
        },
        "GetLayers": {"layers": [{"index": 4, "isHidden": False, "isLocked": False}]},
        "GetHotlinks": {"hotlinks": []},
    }
    responses.update(overrides)
    return responses


def test_a_locked_layer_is_named_as_the_whole_cause() -> None:
    connection, transport = connect(
        _access_responses(GetLayers={"layers": [{"index": 4, "isLocked": True}]})
    )
    diagnosis = diagnose_write_access(connection, ["z1", "z2"])

    assert diagnosis is not None
    assert diagnosis.locked_layers == ("06 | Zone.Apartment",)
    assert "Unlock them in Layer Settings" in diagnosis.describe()
    assert transport.parameters_for("GetLayers")["attributeIds"] == [
        {"attributeId": {"guid": "L4"}}
    ], "only the layers the refusing elements actually sit on are queried"


def test_an_unlocked_layer_plus_a_hotlink_points_at_the_module() -> None:
    """The case seen on a real project: layers fine, elements read-only
    because they arrive through a hotlinked module."""
    connection, _ = connect(
        _access_responses(
            GetHotlinks={
                "hotlinks": [
                    {
                        "location": "X:/proj/Tower.mod",
                        "children": [{"location": "X:/proj/Core.mod"}],
                    }
                ]
            }
        )
    )
    diagnosis = diagnose_write_access(connection, ["z1"])

    assert diagnosis is not None
    assert diagnosis.hotlink_sources == ("X:/proj/Tower.mod", "X:/proj/Core.mod")
    assert "read-only in the host" in diagnosis.describe()
    assert "Drawing is unaffected" in diagnosis.describe()


def test_a_locked_layer_outranks_a_hotlink_because_it_is_the_cheaper_fix() -> None:
    connection, _ = connect(
        _access_responses(
            GetLayers={"layers": [{"index": 4, "isLocked": True}]},
            GetHotlinks={"hotlinks": [{"location": "X:/proj/Tower.mod"}]},
        )
    )
    diagnosis = diagnose_write_access(connection, ["z1"])
    assert diagnosis is not None
    assert "Layer Settings" in diagnosis.describe()


def test_no_lock_and_no_hotlink_leaves_the_element_itself() -> None:
    connection, _ = connect(_access_responses())
    diagnosis = diagnose_write_access(connection, ["z1"])

    assert diagnosis is not None
    assert diagnosis.unlocked_layers == ("06 | Zone.Apartment",)
    assert "the elements themselves being locked" in diagnosis.describe()


def test_the_diagnosis_never_turns_a_partial_write_into_an_exception() -> None:
    """It runs only after a write already failed. A second failure here must
    not cost the caller the report it already has."""

    class Broken:
        def send(self, payload: dict[str, Any]) -> dict[str, Any]:
            name = payload["parameters"]["addOnCommandId"]["commandName"]
            if name == "GetAddOnVersion":
                return {"succeeded": True, "result": {"addOnCommandResponse": {"version": "1.5.7"}}}
            return {"succeeded": False, "error": {"code": 1, "message": "unsupported"}}

    assert diagnose_write_access(ArchicadConnection(Broken()), ["z1"]) is None


def test_no_refused_zones_asks_archicad_nothing() -> None:
    connection, transport = connect({})
    assert diagnose_write_access(connection, []) is None
    assert transport.commands() == []


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


# -- what a listing has to say about writability --------------------------


def _catalogue_entry(**overrides: Any) -> dict[str, Any]:
    entry = {
        "propertyId": {"guid": "p1"},
        "propertyType": "Custom",
        "propertyGroupName": "Apartments",
        "propertyName": "Daylight",
        "propertyCollectionType": "Single",
        "propertyValueType": "Boolean",
        "propertyMeasureType": "Default",
        "propertyIsEditable": True,
        "isExpressionBased": False,
    }
    entry.update(overrides)
    return entry


def test_a_listing_carries_the_type_not_just_the_name() -> None:
    """A name alone cannot be written to: the display string depends on type."""
    connection, _ = connect({"GetAllProperties": {"properties": [_catalogue_entry()]}})
    entry = all_properties(connection)[0]

    assert entry.group == "Apartments"
    assert entry.value_type == "Boolean"
    assert entry.collection_type == "Single"
    assert entry.writable


def test_an_expression_based_property_is_not_writable() -> None:
    """It derives its value and refuses to be set, but lists like any other.

    Finding that out by attempting a write across ninety apartments is the
    expensive way.
    """
    connection, _ = connect(
        {"GetAllProperties": {"properties": [_catalogue_entry(isExpressionBased=True)]}}
    )
    entry = all_properties(connection)[0]

    assert not entry.writable
    assert "expression" in entry.describe()


def test_a_read_only_property_is_not_writable() -> None:
    connection, _ = connect(
        {"GetAllProperties": {"properties": [_catalogue_entry(propertyIsEditable=False)]}}
    )
    entry = all_properties(connection)[0]

    assert not entry.writable
    assert "read-only" in entry.describe()


def test_a_listing_still_reads_a_response_without_the_type_fields() -> None:
    """Older add-on builds may omit them; a listing must degrade, not crash."""
    minimal = {
        "propertyId": {"guid": "p1"},
        "propertyGroupName": "Apartments",
        "propertyName": "Daylight",
    }
    connection, _ = connect({"GetAllProperties": {"properties": [minimal]}})
    entry = all_properties(connection)[0]

    assert entry.name == "Daylight"
    assert entry.value_type == ""
    assert "?" in entry.describe()


def test_enumeration_values_are_read_from_the_definition() -> None:
    """An enum accepts its defined display strings and nothing else.

    'Yes' written to a property whose values are 'Y' and 'N' does not land,
    and does not error in a way a careless caller notices. So the accepted
    strings are read from Archicad rather than assumed.
    """
    connection, transport = connect(
        {
            "API.GetDetailsOfProperties": {
                "propertyDefinitions": [
                    {
                        "propertyDefinition": {
                            "propertyId": {"guid": "p1"},
                            "name": "Daylight",
                            "type": "singleEnum",
                            "possibleEnumValues": [
                                {"enumValue": {"displayValue": "Y", "nonLocalizedValue": "Y"}},
                                {"enumValue": {"displayValue": "N", "nonLocalizedValue": "N"}},
                            ],
                        }
                    }
                ]
            }
        }
    )
    assert enum_values(connection, ["p1"]) == {"p1": ("Y", "N")}
    assert transport.sent[0]["parameters"] == {"properties": [{"propertyId": {"guid": "p1"}}]}


def test_enumeration_values_are_matched_on_identifier_not_position() -> None:
    """A partial or reordered response must not attach one property's values
    to another -- writing 'Y' into an hours band would be silently plausible."""
    connection, _ = connect(
        {
            "API.GetDetailsOfProperties": {
                "propertyDefinitions": [
                    {
                        "propertyDefinition": {
                            "propertyId": {"guid": "second"},
                            "possibleEnumValues": [{"enumValue": {"displayValue": "2-3 hrs"}}],
                        }
                    }
                ]
            }
        }
    )
    assert enum_values(connection, ["first", "second"]) == {"second": ("2-3 hrs",)}


def test_a_non_enumerated_property_reports_no_values() -> None:
    connection, _ = connect(
        {
            "API.GetDetailsOfProperties": {
                "propertyDefinitions": [
                    {"propertyDefinition": {"propertyId": {"guid": "p1"}, "type": "string"}}
                ]
            }
        }
    )
    assert enum_values(connection, ["p1"]) == {"p1": ()}


def test_asking_for_no_enumerations_makes_no_call() -> None:
    connection, transport = connect({})
    assert enum_values(connection, []) == {}
    assert transport.sent == []


# -- drawing the result on the plan ---------------------------------------


def _zone(
    guid: str,
    *,
    outline: bool = True,
    holes: int = 0,
    storey: int | None = 0,
    name: str = "Living",
    number: str | None = None,
) -> Any:
    from sun_study.archicad.read import ArchicadZone

    return ArchicadZone(
        guid=guid,
        name=name,
        number=guid.upper() if number is None else number,
        storey_index=storey,
        outline=((0.0, 0.0), (4.0, 0.0), (4.0, 3.0), (0.0, 3.0)) if outline else (),
        hole_count=holes,
    )


def _draw_responses(*, layer_index: int = 7, **overrides: Any) -> dict[str, Any]:
    responses: dict[str, Any] = {
        "GetAttributesByType": {
            "attributes": [
                {"attributeId": {"guid": "l"}, "index": layer_index, "name": DEFAULT_LAYER_NAME}
            ]
        },
        "GetLayers": {
            "layers": [
                {
                    "attributeId": {"guid": "l"},
                    "index": layer_index,
                    "name": DEFAULT_LAYER_NAME,
                    "isHidden": False,
                    "isLocked": False,
                }
            ]
        },
        "GetElementsByType": {"elements": []},
        "CreateHatches": {"elements": [{"elementId": {"guid": "h"}}] * 99},
        "CreateTexts": {"elements": [{"elementId": {"guid": "t"}}] * 99},
    }
    responses.update(overrides)
    return responses


def test_a_fill_uses_the_zones_own_outline_and_storey() -> None:
    connection, transport = connect(_draw_responses())
    report = draw_assessment(
        connection,
        _assessment(_apartment("apt-1")),
        [_zone("z1", storey=3)],
        zone_by_apartment={"apt-1": "z1"},
    )

    hatches = transport.parameters_for("CreateHatches")["hatchesData"]
    apartment_fill = hatches[0]
    assert apartment_fill["coordinates"] == [
        {"x": 0.0, "y": 0.0},
        {"x": 4.0, "y": 0.0},
        {"x": 4.0, "y": 3.0},
        {"x": 0.0, "y": 3.0},
    ]
    assert apartment_fill["layerIndex"] == 7
    assert apartment_fill["floorInd"] == 3, "each apartment is drawn on its own storey"
    assert report.fills_drawn == 1
    assert report.legend_items == len(DEFAULT_BANDS)


def test_the_band_boundary_belongs_to_the_band_above() -> None:
    """Exactly two hours is '2-3 hrs'. The ADG reads 'at least two hours', and
    disagreeing by one band at exactly the threshold is the worst place to."""
    assert band_for(120.0, DEFAULT_BANDS).label == "2-3 hrs"
    assert band_for(119.999, DEFAULT_BANDS).label == "1-2 hrs"
    assert band_for(0.0, DEFAULT_BANDS).label == "0 hrs"
    assert band_for(1e9, DEFAULT_BANDS).label == "5+ hrs"


def test_the_previous_run_is_deleted_before_drawing() -> None:
    """Without this a second run doubles up: the new fills land on the old
    ones, the plan looks unchanged, and stale colours print from underneath."""
    stale = [{"elementId": {"guid": "old-1"}}, {"elementId": {"guid": "old-2"}}]
    connection, transport = connect(
        _draw_responses(
            GetElementsByType={"elements": stale},
            GetDetailsOfElements={"detailsOfElements": [{"layerIndex": 7}, {"layerIndex": 99}]},
            DeleteElements={"success": True},
        )
    )
    report = draw_assessment(
        connection,
        _assessment(_apartment("apt-1")),
        [_zone("z1")],
        zone_by_apartment={"apt-1": "z1"},
    )

    deleted = transport.parameters_for("DeleteElements")["elements"]
    assert deleted == [{"elementId": {"guid": "old-1"}}] * 2, (
        "only elements on the results layer are deleted, from both Hatch and Text"
    )
    assert report.fills_removed == 2
    assert transport.commands().index("DeleteElements") < transport.commands().index(
        "CreateHatches"
    )


def test_something_else_on_the_layer_is_not_touched() -> None:
    connection, transport = connect(
        _draw_responses(
            GetElementsByType={"elements": [{"elementId": {"guid": "keep"}}]},
            GetDetailsOfElements={"detailsOfElements": [{"layerIndex": 99}]},
        )
    )
    draw_assessment(
        connection,
        _assessment(_apartment("apt-1")),
        [_zone("z1")],
        zone_by_apartment={"apt-1": "z1"},
    )
    assert "DeleteElements" not in transport.commands()


def test_a_zone_with_no_outline_is_skipped_and_named() -> None:
    connection, _ = connect(_draw_responses())
    report = draw_assessment(
        connection,
        _assessment(_apartment("apt-1")),
        [_zone("z1", outline=False)],
        zone_by_apartment={"apt-1": "z1"},
    )
    assert report.fills_drawn == 0
    assert report.zones_without_outline == ("Z1 Living",)
    assert not report.complete


def test_zones_sharing_a_name_are_listed_distinguishably() -> None:
    """From a real run: "6 zones have holes ... RESI, RESI, RESI, RESI, RESI",
    which names no zone at all and cannot be acted on."""
    connection, _ = connect(_draw_responses())
    report = draw_assessment(
        connection,
        _assessment(_apartment("apt-1"), _apartment("apt-2")),
        [
            _zone("02327B92-3FD0", holes=1, name="RESI", number=""),
            _zone("044ED3D0-DA35", holes=1, name="RESI", number=""),
        ],
        zone_by_apartment={"apt-1": "02327B92-3FD0", "apt-2": "044ED3D0-DA35"},
    )

    assert report.zones_with_holes == ("RESI [02327B92]", "RESI [044ED3D0]")


def test_a_zone_with_a_name_of_its_own_is_not_tagged() -> None:
    """The GUID fragment is noise when the name already identifies the zone."""
    connection, _ = connect(_draw_responses())
    report = draw_assessment(
        connection,
        _assessment(_apartment("apt-1")),
        [_zone("z1", holes=1, name="Living", number="G.01")],
        zone_by_apartment={"apt-1": "z1"},
    )
    assert report.zones_with_holes == ("G.01 Living",)


def test_whole_arcminute_coordinates_are_recognised_as_a_preset() -> None:
    """Archicad's Sydney preset is exactly -33 deg 52' 00", 151 deg 13' 00".

    Both coordinates landing on a whole arcminute is a 1-in-13-million
    coincidence for a surveyed site, so it is the tell that Project Location
    was never set. Observed on a real project.
    """
    from sun_study.archicad.read import GeoLocation

    preset = GeoLocation(
        latitude_deg=-33.866667, longitude_deg=151.216667, altitude_m=0.0, north_radians=0.856118
    )
    assert preset.looks_like_a_city_preset


def test_a_surveyed_location_is_not_flagged() -> None:
    """From a second real project, which had its location properly set."""
    from sun_study.archicad.read import GeoLocation

    surveyed = GeoLocation(
        latitude_deg=-33.913162, longitude_deg=151.243890, altitude_m=62.0, north_radians=0.161055
    )
    assert not surveyed.looks_like_a_city_preset


def test_one_round_coordinate_is_not_enough_to_flag() -> None:
    """A site can sit on a whole arcminute in one axis by chance. Needing both
    is what keeps this from crying wolf on real projects."""
    from sun_study.archicad.read import GeoLocation

    half_round = GeoLocation(
        latitude_deg=-33.866667, longitude_deg=151.243890, altitude_m=0.0, north_radians=0.0
    )
    assert not half_round.looks_like_a_city_preset


def test_the_arcminute_test_survives_a_degrees_minutes_seconds_round_trip() -> None:
    """Archicad reports -33.866667, not -33.8666666..., so an exact preset
    arrives already rounded. A tolerance tighter than that would never fire."""
    from sun_study.archicad.read import GeoLocation

    for latitude in (-33.866667, -33.8666667, -33.86666666666667):
        location = GeoLocation(
            latitude_deg=latitude, longitude_deg=151.216667, altitude_m=0.0, north_radians=0.0
        )
        assert location.looks_like_a_city_preset, latitude


def test_a_zone_carries_the_layer_it_sits_on() -> None:
    """A zone's layer is what says whether it is an apartment: one project's
    zones were all on '10 | Calc.GFA', which is area take-off, not housing."""
    connection, _ = connect(
        {
            "GetElementsByType": {"elements": [{"elementId": {"guid": "z1"}}]},
            "GetDetailsOfElements": {
                "detailsOfElements": [
                    {"floorIndex": 3, "layerIndex": 42, "details": {"name": "RESI"}}
                ]
            },
        }
    )
    assert zones(connection)[0].layer_index == 42


def test_layer_names_map_index_to_name() -> None:
    connection, transport = connect(
        {
            "GetAttributesByType": {
                "attributes": [
                    {"attributeId": {"guid": "a"}, "index": 42, "name": "10 | Calc.GFA"},
                    {"attributeId": {"guid": "b"}, "index": 7, "name": "06 | Zone.Apartment"},
                ]
            }
        }
    )
    assert layer_names(connection) == {42: "10 | Calc.GFA", 7: "06 | Zone.Apartment"}
    assert transport.parameters_for("GetAttributesByType") == {"attributeType": "Layer"}


def test_a_zone_with_holes_is_drawn_but_reported() -> None:
    """CreateHatches takes a single contour, so an apartment wrapping a lift
    core gets coloured over the void. Saying so beats drawing it silently."""
    connection, _ = connect(_draw_responses())
    report = draw_assessment(
        connection,
        _assessment(_apartment("apt-1")),
        [_zone("z1", holes=1)],
        zone_by_apartment={"apt-1": "z1"},
    )
    assert report.fills_drawn == 1
    assert report.zones_with_holes == ("Z1 Living",)
    assert "drawn solid" in report.describe()


def test_a_per_element_failure_is_raised_not_swallowed() -> None:
    """A half-drawn diagram reporting success is worse than none: the missing
    apartments look like apartments that were never assessed."""
    connection, _ = connect(
        _draw_responses(
            CreateHatches={
                "elements": [
                    {"elementId": {"guid": "h"}},
                    {"error": {"code": 5, "message": "invalid polygon"}},
                ]
            }
        )
    )
    with pytest.raises(ArchicadError, match="invalid polygon"):
        draw_assessment(
            connection,
            _assessment(_apartment("apt-1")),
            [_zone("z1")],
            zone_by_apartment={"apt-1": "z1"},
        )


def test_drawing_needs_a_newer_add_on_than_the_numbers_do() -> None:
    """CreateHatches arrived in 1.5.7. Someone who only wants numbers should
    not be blocked by a picture they did not ask for, so the gate is separate."""
    transport = FakeTransport({"GetAddOnVersion": {"version": "1.5.4"}})
    connection = ArchicadConnection(transport)

    assert connection.require_tapir() == "1.5.4", "1.5.4 is fine for reading and writing"
    with pytest.raises(TapirUnavailableError, match="CreateHatches"):
        draw_assessment(connection, _assessment(_apartment("a")), [], zone_by_apartment={})


def test_the_legend_sits_clear_of_the_plan() -> None:
    connection, transport = connect(_draw_responses())
    draw_assessment(
        connection,
        _assessment(_apartment("apt-1")),
        [_zone("z1")],
        zone_by_apartment={"apt-1": "z1"},
    )
    swatches = transport.parameters_for("CreateHatches")["hatchesData"][1:]
    assert all(point["x"] >= 9.0 for fill in swatches for point in fill["coordinates"]), (
        "the legend must not land on top of the zones, which end at x=4"
    )

    labels = [t["text"] for t in transport.parameters_for("CreateTexts")["textsData"]]
    assert labels[: len(DEFAULT_BANDS)] == [b.label for b in reversed(DEFAULT_BANDS)]


# -- matching bands to the office's own pens ------------------------------


def _pens(*pens: tuple[int, tuple[float, float, float], str]) -> dict[str, Any]:
    """A ``GetPenTables`` answer carrying pens, colours as Archicad's 0-1 floats.

    The shape is from ``GetPenTablesCommand::Execute`` in
    ``archicad-addon/Sources/AttributeCommands.cpp``: a flat object per table,
    not one wrapped in a ``penTableAttribute`` key.
    """
    return {
        "penTables": [
            {
                "attributeId": {"guid": "p1"},
                "index": 1,
                "name": "00 FA Pens",
                "pens": [
                    {
                        "index": index,
                        "color": {"red": red, "green": green, "blue": blue},
                        "width": 0.18,
                        "description": description,
                    }
                    for index, (red, green, blue), description in pens
                ],
            }
        ]
    }


def _pen_tables_listed(*guids: str) -> dict[str, Any]:
    return {
        "attributes": [
            {"attributeId": {"guid": guid}, "index": position, "name": f"table {guid}"}
            for position, guid in enumerate(guids, start=1)
        ]
    }


def test_pen_colours_are_scaled_from_archicads_0_to_1_into_0_to_255() -> None:
    """The trap: Archicad reports colour as floats, every other colour in this
    project is 0-255, and mixing the scales silently produces near-black."""
    connection, _ = connect(
        {
            "GetAttributesByType": _pen_tables_listed("p1"),
            "GetPenTables": _pens((91, (1.0, 0.0, 0.5), "red-ish")),
        }
    )
    assert pen_table(connection) == (Pen(index=91, rgb=(255, 0, 128), description="red-ish"),)


def test_the_pen_table_is_found_by_enumerating_first() -> None:
    """The trap that bit GetLayers: GetPenTables *requires* attributeIds, so
    calling it bare to see what exists is a schema violation and Archicad
    rejects the whole command rather than the parameter."""
    connection, transport = connect(
        {
            "GetAttributesByType": _pen_tables_listed("p1"),
            "GetPenTables": _pens((1, (0.0, 0.0, 0.0), "")),
        }
    )
    pen_table(connection)

    assert transport.parameters_for("GetAttributesByType") == {"attributeType": "PenTable"}
    assert transport.parameters_for("GetPenTables") == {
        "attributeIds": [{"attributeId": {"guid": "p1"}}],
        "fields": ["pens"],
    }


def test_one_pen_table_is_not_asked_which_is_active() -> None:
    """With a single table the answer cannot change, and the flag costs a call."""
    connection, transport = connect(
        {
            "GetAttributesByType": _pen_tables_listed("p1"),
            "GetPenTables": _pens((1, (0.0, 0.0, 0.0), "")),
        }
    )
    pen_table(connection)
    assert len(transport.all_parameters_for("GetPenTables")) == 1


def test_the_pen_table_active_for_the_model_wins() -> None:
    """A project can carry several pen tables. The one that governs what gets
    drawn is the only one whose indices mean anything.

    The active flag is asked for on its own first, because every table carries
    255 pens and pulling all of them to read one boolean is a lot of JSON.
    """
    active = {
        "penTables": [
            {"attributeId": {"guid": "p1"}, "index": 1, "isActiveForModel": False},
            {"attributeId": {"guid": "p2"}, "index": 2, "isActiveForModel": True},
        ]
    }
    connection, transport = connect(
        {
            "GetAttributesByType": _pen_tables_listed("p1", "p2"),
            "GetPenTables": Sequential(active, _pens((7, (1.0, 1.0, 1.0), "white"))),
        }
    )
    assert pen_table(connection) == (Pen(index=7, rgb=(255, 255, 255), description="white"),)

    probe, fetch = transport.all_parameters_for("GetPenTables")
    assert probe["fields"] == ["isActiveForModel"], "the probe must not pull 2 x 255 pens"
    assert fetch["attributeIds"] == [{"attributeId": {"guid": "p2"}}]


def test_an_unreported_active_flag_falls_back_to_the_first_table() -> None:
    """Not knowing which table is active is a worse reason to refuse to draw
    than drawing from the first one, which is usually the only one."""
    connection, _ = connect(
        {
            "GetAttributesByType": _pen_tables_listed("p1", "p2"),
            "GetPenTables": Sequential(
                {"penTables": [{"error": {"code": 1, "message": "no"}}]},
                _pens((3, (0.0, 1.0, 0.0), "green")),
            ),
        }
    )
    assert pen_table(connection) == (Pen(index=3, rgb=(0, 255, 0), description="green"),)


def test_a_project_with_no_pen_table_is_an_error_not_an_empty_palette() -> None:
    """An empty palette would silently leave every band on its guessed pen."""
    connection, _ = connect({"GetAttributesByType": {"attributes": []}})
    with pytest.raises(ArchicadError, match="no pen tables"):
        pen_table(connection)


def test_a_pen_table_that_answers_with_no_pens_is_an_error() -> None:
    connection, _ = connect(
        {
            "GetAttributesByType": _pen_tables_listed("p1"),
            "GetPenTables": {"penTables": [{"attributeId": {"guid": "p1"}, "index": 1}]},
        }
    )
    with pytest.raises(ArchicadError, match="no pens"):
        pen_table(connection)


def test_each_band_takes_the_closest_pen_by_colour() -> None:
    """A pen index means nothing outside the table it came from, so the colour
    is the input and the pen number is derived from it."""
    bands = (
        BandStyle("cold", 60.0, fill_pen=1, rgb=(8, 48, 107)),
        BandStyle("hot", float("inf"), fill_pen=1, rgb=(244, 81, 30)),
    )
    pens = (
        Pen(index=40, rgb=(10, 50, 110)),
        Pen(index=41, rgb=(0, 255, 0)),
        Pen(index=42, rgb=(240, 80, 30)),
    )
    matched, distances = match_pens(bands, pens)

    assert [band.fill_pen for band in matched] == [40, 42]
    assert distances["cold"] < 6.0
    assert distances["hot"] < 5.0
    assert [band.label for band in matched] == ["cold", "hot"], "only the pen changes"


def test_two_bands_never_share_a_pen() -> None:
    """The bug this fixes, seen on a real office pen table.

    The 3-4 and 4-5 hour reference colours are only 30 apart, and a palette
    with one amber near both gave them the same pen -- a plan that cannot show
    where the four-hour line falls while looking entirely finished.
    """
    bands = (
        BandStyle("3-4 hrs", 240.0, fill_pen=1, rgb=(255, 213, 79)),
        BandStyle("4-5 hrs", 300.0, fill_pen=1, rgb=(255, 183, 77)),
    )
    amber = Pen(index=124, rgb=(255, 200, 60))
    matched, _ = match_pens(bands, (amber, Pen(index=125, rgb=(250, 150, 60))))

    assert len({band.fill_pen for band in matched}) == 2, "one pen cannot serve two bands"
    assert matched[0].fill_pen == 124, "the closest pairing overall still wins"
    assert matched[1].fill_pen == 125


def test_the_closest_pairing_wins_globally_not_band_by_band() -> None:
    """Assigning in band order would let an early band take a pen a later one
    needs far more. Pens are claimed by best pairing across the whole table."""
    bands = (
        BandStyle("first", 60.0, fill_pen=1, rgb=(100, 100, 100)),
        BandStyle("second", 120.0, fill_pen=1, rgb=(110, 110, 110)),
    )
    contested = Pen(index=7, rgb=(111, 111, 111))
    matched, _ = match_pens(bands, (contested, Pen(index=8, rgb=(0, 0, 0))))

    assert matched[1].fill_pen == 7, "'second' is nearer the contested pen, so it gets it"
    assert matched[0].fill_pen == 8


def test_fewer_pens_than_bands_leaves_the_tail_on_its_default() -> None:
    """Reusing a pen would hide a boundary; keeping the default at least says
    the mapping is incomplete rather than quietly merging two bands."""
    bands = (
        BandStyle("a", 60.0, fill_pen=91, rgb=(0, 0, 0)),
        BandStyle("b", 120.0, fill_pen=92, rgb=(255, 255, 255)),
    )
    matched, distances = match_pens(bands, (Pen(index=5, rgb=(10, 10, 10)),))

    assert [band.fill_pen for band in matched] == [5, 92]
    assert "b" not in distances, "an unassigned band reports no distance"


def test_bands_on_near_identical_pens_are_reported() -> None:
    """Distinct pen indices are not distinct colours, and only this catches
    the difference between a legible boundary and a technically correct one."""
    pens = (Pen(index=10, rgb=(255, 200, 60)), Pen(index=11, rgb=(255, 202, 62)))
    bands = (
        BandStyle("3-4 hrs", 240.0, fill_pen=10, rgb=(255, 213, 79)),
        BandStyle("4-5 hrs", 300.0, fill_pen=11, rgb=(255, 183, 77)),
    )
    assert indistinguishable_bands(bands, pens) == (("3-4 hrs", "4-5 hrs"),)


def test_bands_on_clearly_different_pens_are_not_reported() -> None:
    pens = (Pen(index=10, rgb=(8, 48, 107)), Pen(index=11, rgb=(244, 81, 30)))
    bands = (
        BandStyle("0 hrs", 1e-9, fill_pen=10, rgb=(8, 48, 107)),
        BandStyle("5+ hrs", float("inf"), fill_pen=11, rgb=(244, 81, 30)),
    )
    assert indistinguishable_bands(bands, pens) == ()


def test_the_default_bands_are_all_distinguishable_from_each_other() -> None:
    """A guard on the reference palette itself: if two band colours were ever
    edited closer than the legibility threshold, no pen table could save them."""
    as_pens = tuple(Pen(index=band.fill_pen, rgb=band.rgb) for band in DEFAULT_BANDS)
    assert indistinguishable_bands(DEFAULT_BANDS, as_pens) == ()


def test_a_palette_missing_a_colour_still_answers_but_the_distance_says_so() -> None:
    """min() over a palette always returns something. The distance is the only
    sign that a band landed on a pen nobody would have chosen by hand."""
    bands = (BandStyle("yellow", 60.0, fill_pen=1, rgb=(255, 213, 79)),)
    matched, distances = match_pens(bands, (Pen(index=5, rgb=(0, 0, 0)),))

    assert matched[0].fill_pen == 5
    assert distances["yellow"] > 100.0


def test_an_empty_palette_leaves_the_default_pens_alone() -> None:
    matched, distances = match_pens(DEFAULT_BANDS, ())
    assert matched == DEFAULT_BANDS
    assert distances == {}


def test_an_existing_group_is_reused_rather_than_created_again() -> None:
    """The bug this fixes: a second group with the same name.

    ``GetAllProperties`` reports each property's group *name*, so a group with
    no properties in it is invisible -- which an earlier version took as
    "absent" and created again on every run. Tapir resolves a group by name by
    taking the first match, so duplicates send definitions somewhere
    unpredictable, and Archicad rejects the lot.
    """
    all_names = [spec.name for spec in APARTMENT_PROPERTIES]
    connection, transport = connect(
        {
            "GetAllProperties": _property_catalogue([]),
            "API.GetAllPropertyGroupIds": {
                "propertyGroupIds": [{"propertyGroupId": {"guid": "g"}}]
            },
            "API.GetPropertyGroups": {
                "propertyGroups": [
                    {
                        "propertyGroup": {
                            "propertyGroupId": {"guid": "g"},
                            "name": PROPERTY_GROUP_NAME,
                        }
                    }
                ]
            },
            "CreatePropertyDefinitions": {
                "propertyIds": [{"propertyId": {"guid": "p"}}] * len(all_names)
            },
        }
    )
    init_properties(connection, {"zone-1": {"item-a"}})

    assert "CreatePropertyGroups" not in transport.commands(), (
        "an existing group must never be created a second time"
    )
    definitions = transport.parameters_for("CreatePropertyDefinitions")["propertyDefinitions"]
    assert definitions[0]["propertyDefinition"]["group"] == {"propertyGroupId": {"guid": "g"}}


def test_a_rejected_definition_reports_archicads_error_code() -> None:
    """Tapir's message for a rejected definition is a fixed string.

    It passes Archicad's own GSErrCode through as the code, so dropping that
    leaves nine identical lines saying nothing about why -- which is exactly
    what happened on a real project.
    """
    connection, _ = connect(
        {
            "GetAllProperties": _property_catalogue([]),
            "API.GetAllPropertyGroupIds": {"propertyGroupIds": []},
            "CreatePropertyGroups": {"propertyGroupIds": [{"propertyGroupId": {"guid": "g"}}]},
            "CreatePropertyDefinitions": {
                "propertyIds": [
                    {"error": {"code": -2130313081, "message": "failed to create the property"}}
                ]
                * len(APARTMENT_PROPERTIES)
            },
        }
    )
    with pytest.raises(ArchicadError) as caught:
        init_properties(connection, {"zone-1": {"item-a"}})

    message = str(caught.value)
    assert "-2130313081" in message, "the code is the whole diagnostic"
    assert "number" in message, "and the type, since it narrows which ones failed"
    assert "availability 1 classification items" in message


def test_a_group_that_cannot_be_created_says_so_with_its_code() -> None:
    connection, _ = connect(
        {
            "GetAllProperties": _property_catalogue([]),
            "API.GetAllPropertyGroupIds": {"propertyGroupIds": []},
            "CreatePropertyGroups": {
                "propertyGroupIds": [{"error": {"code": 7, "message": "name already used"}}]
            },
        }
    )
    with pytest.raises(ArchicadError, match="name already used"):
        init_properties(connection, {"zone-1": {"item-a"}})


def test_every_definition_carries_a_default_value() -> None:
    """Omitting it is rejected with APIERR_BADVALUE, and looks harmless.

    The schema does not require ``defaultValue``, but Tapir then sets the
    variant's status to null and Archicad refuses the whole definition with
    -2130313104. Nine properties failed that way on a real project, and a
    probe varying the name, type, availability and isEditable failed every
    time -- the missing default was the one thing they all shared.
    """
    all_names = [spec.name for spec in APARTMENT_PROPERTIES]
    transport = CreatingTransport([], all_names)
    init_properties(ArchicadConnection(transport), {"zone-1": {"item-a"}})

    definitions = transport.parameters_for("CreatePropertyDefinitions")["propertyDefinitions"]
    for entry in definitions:
        default = entry["propertyDefinition"]["defaultValue"]["basicDefaultValue"]
        assert default["status"] == "normal", "a null status is what Archicad rejects"
        assert default["type"] == entry["propertyDefinition"]["type"]
        assert "value" in default


def test_the_default_value_matches_the_declared_type() -> None:
    """A string default on a number property would be rejected the same way."""
    assert default_property_value("string")["basicDefaultValue"]["value"] == ""
    assert default_property_value("number")["basicDefaultValue"]["value"] == 0.0
    assert default_property_value("integer")["basicDefaultValue"]["value"] == 0
    assert default_property_value("boolean")["basicDefaultValue"]["value"] is False
    assert default_property_value("area")["basicDefaultValue"]["value"] == 0.0


def test_layers_are_enumerated_not_asked_for_by_id() -> None:
    """``GetLayers`` requires ``attributeIds``, so it cannot answer this.

    Calling it with no parameters violates its schema and Archicad rejects the
    whole command -- code 4002, "validation failed on rule 'required'" -- so
    the layer lookup has to enumerate instead of asking by identifier it does
    not yet have.
    """
    connection, transport = connect(_draw_responses(layer_index=12))
    draw_assessment(
        connection,
        _assessment(_apartment("apt-1")),
        [_zone("z1")],
        zone_by_apartment={"apt-1": "z1"},
    )

    # GetLayers may be called, but only once an identifier is in hand.
    assert transport.parameters_for("GetAttributesByType") == {"attributeType": "Layer"}
    assert transport.parameters_for("GetLayers") == {
        "attributeIds": [{"attributeId": {"guid": "l"}}]
    }, "GetLayers requires attributeIds; calling it bare is rejected as code 4002"
    hatches = transport.parameters_for("CreateHatches")["hatchesData"]
    assert all(fill["layerIndex"] == 12 for fill in hatches)


def test_a_known_error_code_is_translated_not_relayed() -> None:
    """Tapir's messages are fixed strings, so the code is the whole diagnostic.

    A bare number sends every reader to the same lookup table.
    """
    from sun_study.archicad.write import explain_code

    assert "APIERR_NOACCESSRIGHT" in explain_code(-2130312909)
    assert "hotlinked" in explain_code(-2130312909), "the likely cause on a solo file"
    assert "APIERR_BADVALUE" in explain_code(-2130313104)
    assert explain_code(12345) == "code 12345", "an unknown code is still shown"


def test_only_the_first_failure_per_element_is_reported_as_a_cause() -> None:
    """Tapir reuses one `err` across an element's properties without resetting.

    So the first genuine failure makes every later property on that element
    report "Failed to get property values" too. Printing all of them buries
    eight causes under forty-eight symptoms.
    """
    names = [spec.name for spec in APARTMENT_PROPERTIES]
    responses = _write_responses(names)
    responses["GetElementsByIFCIds"] = {
        "elementsByIFCIds": [
            {"ifcId": "apt-1", "elements": [{"elementId": {"guid": "z1"}}]},
            {"ifcId": "apt-2", "elements": [{"elementId": {"guid": "z2"}}]},
        ]
    }
    responses["SetPropertyValuesOfElements"] = {
        "executionResults": [
            {"success": False, "error": {"code": -2130312909, "message": "no access"}}
        ]
        * (2 * len(names))
    }
    connection, _ = connect(responses)

    report = write_assessment(connection, _assessment(_apartment("apt-1"), _apartment("apt-2")))
    described = report.describe()

    assert described.count("FAILED") == 2, "one line per element, not one per value"
    assert len(report.failures) == 2 * len(names)
    assert f"{2 * len(names)} failures over 2 elements" in described


def test_zones_sharing_a_name_are_still_counted_as_separate_elements() -> None:
    """The bug this fixes, from a project with 1341 zones.

    Eight of them were called ``RESI`` with no number, so collapsing failures
    by display name turned seven refusing elements into "56 failures over 1
    elements" -- and the GUID fragment is what makes the list of them mean
    anything.
    """
    names = [spec.name for spec in APARTMENT_PROPERTIES]
    responses = _write_responses(names)
    responses["GetElementsByIFCIds"] = {
        "elementsByIFCIds": [
            {"ifcId": "apt-1", "elements": [{"elementId": {"guid": "02327B92-3FD0"}}]},
            {"ifcId": "apt-2", "elements": [{"elementId": {"guid": "044ED3D0-DA35"}}]},
        ]
    }
    responses["SetPropertyValuesOfElements"] = {
        "executionResults": [{"success": False, "error": {"code": 1, "message": "no"}}]
        * (2 * len(names))
    }
    connection, _ = connect(responses)

    same_name = _assessment(_apartment("apt-1", name="RESI"), _apartment("apt-2", name="RESI"))
    report = write_assessment(connection, same_name)
    described = report.describe()

    assert len(report.failure_causes) == 2, "two elements, not one"
    assert f"{2 * len(names)} failures over 2 elements" in described
    assert "RESI [02327B92]" in described
    assert "RESI [044ED3D0]" in described


def test_a_hidden_results_layer_is_called_out() -> None:
    """A successful run onto a hidden layer looks exactly like one that did nothing.

    The command reports fills drawn and the drawing does not change, which is
    the most demoralising possible outcome and the easiest to misread as a bug
    in the tool.
    """
    responses = _draw_responses()
    responses["GetLayers"] = {
        "layers": [
            {
                "attributeId": {"guid": "l"},
                "index": 7,
                "name": DEFAULT_LAYER_NAME,
                "isHidden": True,
                "isLocked": False,
            }
        ]
    }
    connection, _ = connect(responses)
    report = draw_assessment(
        connection,
        _assessment(_apartment("apt-1")),
        [_zone("z1", storey=4)],
        zone_by_apartment={"apt-1": "z1"},
    )

    assert report.layer.hidden
    assert "THE LAYER IS HIDDEN" in report.describe()
    assert "storey index 4" in report.describe(), (
        "the other reason nothing appears: a plan on a different storey"
    )


def test_an_unreadable_layer_state_does_not_stop_the_drawing() -> None:
    """Not knowing the visibility is a worse reason to fail than drawing anyway."""

    class NoLayerDetail(FakeTransport):
        def send(self, payload: dict[str, Any]) -> dict[str, Any]:
            if (
                payload.get("parameters", {}).get("addOnCommandId", {}).get("commandName")
                == "GetLayers"
            ):
                return {"succeeded": False, "error": {"code": 1, "message": "nope"}}
            return super().send(payload)

    transport = NoLayerDetail({"GetAddOnVersion": {"version": "1.5.7"}, **_draw_responses()})
    report = draw_assessment(
        ArchicadConnection(transport),
        _assessment(_apartment("apt-1")),
        [_zone("z1")],
        zone_by_apartment={"apt-1": "z1"},
    )
    assert report.fills_drawn == 1
    assert not report.layer.hidden


# -- putting the drawing on a sheet ---------------------------------------


def _navigator_tree() -> dict[str, Any]:
    """A Project Map in Archicad's own shape, storeys nested in a folder.

    From ``NavigatorItemToObjectState`` in ``NavigatorCommands.cpp``: children
    are wrapped one level deep in a ``navigatorItem`` key, and a Story item's
    ``prefix`` carries its floor number as a string.
    """

    def story(guid: str, name: str, floor: int) -> dict[str, Any]:
        return {
            "navigatorItem": {
                "type": "StoryItem",
                "name": name,
                "navigatorItemId": {"guid": guid},
                "prefix": str(floor),
            }
        }

    return {
        "navigatorItemTree": {
            "type": "ProjectItem",
            "name": "Project",
            "navigatorItemId": {"guid": "root"},
            "prefix": "",
            "children": [
                {
                    "navigatorItem": {
                        "type": "FolderItem",
                        "name": "Stories",
                        "navigatorItemId": {"guid": "folder"},
                        "prefix": "",
                        "children": [
                            story("s8", "Level 08", 8),
                            story("s9", "Level 09", 9),
                        ],
                    }
                },
                {
                    "navigatorItem": {
                        "type": "SectionItem",
                        "name": "Section A",
                        "navigatorItemId": {"guid": "sec"},
                        "prefix": "",
                    }
                },
            ],
        }
    }


def _layout_responses(**overrides: Any) -> dict[str, Any]:
    responses: dict[str, Any] = {
        "GetNavigatorItemTree": _navigator_tree(),
        "CloneProjectMapItemToViewMap": {
            "navigatorItems": [
                {"navigatorItemId": {"guid": "v8"}},
                {"navigatorItemId": {"guid": "v9"}},
            ]
        },
        "CreateLayout": {"databases": [{"databaseId": {"guid": "lay"}}]},
        "CreateDrawings": {"elements": [{"elementId": {"guid": "d"}}] * 9},
    }
    responses.update(overrides)
    return responses


def test_storeys_are_found_by_floor_number_not_by_name() -> None:
    """Storey names are a practice's own business -- 'Level 08', 'L08', '8' --
    so a tool that matches on them finds nothing on the next project."""
    connection, _ = connect(_layout_responses())
    found, missing = storey_items(connection, [8, 9])

    assert [found[8].name, found[9].name] == ["Level 08", "Level 09"]
    assert missing == ()


def test_a_storey_with_no_project_map_item_is_reported_not_skipped() -> None:
    """Its fills exist but reach no sheet, and a missing plan reads as a
    storey with no apartments."""
    connection, _ = connect(
        _layout_responses(
            CloneProjectMapItemToViewMap={"navigatorItems": [{"navigatorItemId": {"guid": "v8"}}]}
        )
    )
    report = layout_results(connection, [8, 42], layout_name="Sun Study")

    assert report.drawings_placed == 1, "the storey that exists is still placed"
    assert report.missing_storeys == (42,)
    assert not report.complete
    assert "no Project Map storey found for index 42" in report.describe()


def test_one_linked_drawing_per_storey_lands_on_the_layout() -> None:
    connection, transport = connect(_layout_responses())
    report = layout_results(connection, [9, 8], layout_name="Solar Access", scale=100.0)

    drawings = transport.parameters_for("CreateDrawings")["drawingsData"]
    assert [d["navigatorItemId"]["guid"] for d in drawings] == ["v8", "v9"], (
        "storeys are placed in order, not in the order they were asked for"
    )
    assert all(d["layoutDatabaseId"] == {"guid": "lay"} for d in drawings)
    assert all(d["scale"] == 100.0 for d in drawings)
    assert {d["position"]["x"] for d in drawings} == {420.0, 840.0}, "laid out in a row"
    assert report.storeys == (8, 9)
    assert report.drawings_placed == 2
    assert report.complete


def test_only_the_storeys_that_carry_fills_are_cloned() -> None:
    """The Project Map has two storeys and a section; a study of one storey
    must not put the other on the sheet."""
    connection, transport = connect(
        _layout_responses(
            CloneProjectMapItemToViewMap={"navigatorItems": [{"navigatorItemId": {"guid": "v8"}}]}
        )
    )
    layout_results(connection, [8], layout_name="Sun Study")

    cloned = transport.parameters_for("CloneProjectMapItemToViewMap")["viewsData"]
    assert cloned == [{"navigatorItemId": {"guid": "s8"}}]


def test_no_storeys_makes_no_layout_at_all() -> None:
    """A run that drew nothing must not leave an empty sheet behind."""
    connection, transport = connect(_layout_responses())
    report = layout_results(connection, [], layout_name="Sun Study")

    assert report.drawings_placed == 0
    assert "CreateLayout" not in transport.commands()


def test_a_per_drawing_failure_is_raised_not_swallowed() -> None:
    """A sheet missing one storey silently is worse than no sheet."""
    connection, _ = connect(
        _layout_responses(
            CreateDrawings={
                "elements": [
                    {"elementId": {"guid": "d"}},
                    {"error": {"code": 7, "message": "layout is locked"}},
                ]
            }
        )
    )
    with pytest.raises(ArchicadError, match="layout is locked"):
        layout_results(connection, [8, 9], layout_name="Sun Study")


def test_a_clone_that_returns_the_wrong_count_is_refused() -> None:
    """Without a parallel list there is no way to say which view is which
    storey, and a mislabelled sheet is worse than a missing one."""
    connection, _ = connect(
        _layout_responses(
            CloneProjectMapItemToViewMap={"navigatorItems": [{"navigatorItemId": {"guid": "v8"}}]}
        )
    )
    with pytest.raises(ArchicadError, match="parallel"):
        layout_results(connection, [8, 9], layout_name="Sun Study")


def test_the_sheet_needs_an_older_add_on_than_the_drawing_does() -> None:
    """CreateLayout is 1.4.0 where CreateHatches is 1.5.7, so the sheet is not
    the binding constraint and is gated on its own."""
    transport = FakeTransport({"GetAddOnVersion": {"version": "1.3.0"}})
    with pytest.raises(TapirUnavailableError, match="CreateLayout"):
        layout_results(ArchicadConnection(transport), [8], layout_name="Sun Study")


def test_the_project_map_is_flattened_through_folders() -> None:
    """Storeys can sit inside folders, and nothing about placing them cares."""
    connection, _ = connect(_layout_responses())
    kinds = {item.kind for item in project_map(connection)}
    assert kinds == {"ProjectItem", "FolderItem", "StoryItem", "SectionItem"}


def test_a_non_storey_item_has_no_storey_index() -> None:
    """A Section item's prefix is empty, and reading it as floor 0 would put
    a section drawing on the ground-floor sheet."""
    section = NavigatorItem(identifier="sec", name="Section A", kind="SectionItem", prefix="")
    assert section.storey_index is None
