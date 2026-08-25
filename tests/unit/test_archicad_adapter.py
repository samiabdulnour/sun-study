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
import itertools
import json
import math
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from sun_study.archicad import naming
from sun_study.archicad.connection import (
    DEFAULT_PORT,
    MINIMUM_TAPIR_VERSION,
    PORT_RANGE,
    TAPIR_NAMESPACE,
    ArchicadConnection,
    ArchicadError,
    CommandFailedError,
    Database,
    HttpTransport,
    Instance,
    TapirUnavailableError,
    find_instances,
    where_archicad_actually_is,
)
from sun_study.archicad.draw import (
    DEFAULT_BANDS,
    BandStyle,
    Pen,
    band_for,
    default_layer_name,
    draw_assessment,
    hidden_layers,
    indistinguishable_bands,
    match_pens,
    pen_table,
)
from sun_study.archicad.layout import (
    DEFAULT_SHEET,
    LayoutSheet,
    NavigatorItem,
    choose_master,
    layout_results,
    layout_sheet,
    master_layouts,
    project_map,
    storey_items,
)
from sun_study.archicad.read import (
    GeoreferencingMismatchError,
    classification_items_of,
    cross_check_georeferencing,
    elements_by_ifc_ids,
    expanded_ifc_guid,
    export_ifc,
    gdl_parameters,
    ifc_ids_of_elements,
    layer_names,
    library_objects,
    north_bearing_deg,
    project_info,
    read_geo_location,
    zones,
)
from sun_study.archicad.write import (
    APARTMENT_PROPERTIES,
    NOT_ASSESSED_HOURS,
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


#: The prefix the tool leads its own layers, views and layouts with. Tests
#: build names from it rather than spelling one out: the prefix files the
#: output inside an office's numbering and is expected to differ per office.
SS = naming.prefix()


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


def test_an_ifc_global_id_is_offered_as_a_plain_guid_too() -> None:
    """AC26 matches the expanded spelling, and an export writes the compressed one.

    Live, ``GetElementsByIFCIds`` answered nothing for all fifteen of a
    project's apartment Zones when asked with the 22-character GlobalId its
    own export had just written, and answered every one of them when asked
    with the hyphenated GUID. The two are the same 128 bits.
    """
    assert expanded_ifc_guid("0UHJKXnLzA2OlQcIwF4FFe") == "1E453521-C55F-4A09-8BDA-992E8F10F3E8"
    assert expanded_ifc_guid("not a global id") is None

    connection, transport = connect(
        {
            "GetElementsByIFCIds": {
                "elementsByIFCIds": [
                    {"ifcId": "0UHJKXnLzA2OlQcIwF4FFe", "elements": []},
                    {
                        "ifcId": "1E453521-C55F-4A09-8BDA-992E8F10F3E8",
                        "elements": [{"elementId": {"guid": "zone-1"}}],
                    },
                ]
            }
        }
    )
    mapping = elements_by_ifc_ids(connection, ["0UHJKXnLzA2OlQcIwF4FFe"])
    assert mapping == {"0UHJKXnLzA2OlQcIwF4FFe": ["zone-1"]}, (
        "the caller asked in one spelling and must be answered in it"
    )
    sent = transport.parameters_for("GetElementsByIFCIds")["ifcIds"]
    assert sent == [
        "0UHJKXnLzA2OlQcIwF4FFe",
        "1E453521-C55F-4A09-8BDA-992E8F10F3E8",
    ], "both spellings go in one request, not two"


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


def test_a_hidden_zone_layer_is_reported_before_the_export() -> None:
    """The failure this exists to catch, seen on the reference project.

    The translator exports what the layer combination shows, so zones on a
    hidden layer are simply not in the file. Downstream that reads as
    "apartment zone layers matched nothing" against a list of the layers that
    *did* export, which never contains the one at fault.
    """
    connection, _ = connect(
        _access_responses(GetLayers={"layers": [{"index": 4, "isHidden": True}]})
    )

    assert hidden_layers(connection, ["06 | Zone.Apartment"]) == ["06 | Zone.Apartment"]


def test_a_visible_layer_is_not_reported() -> None:
    connection, _ = connect(_access_responses())

    assert hidden_layers(connection, ["06 | Zone.Apartment"]) == []


def test_a_layer_name_archicad_does_not_know_is_left_to_the_export() -> None:
    """A typo is not a visibility problem, and guessing at one here would send
    the reader to Layer Settings for a name that is not in them. The scene
    already reports it against the export, where the real names are."""
    connection, _ = connect(_access_responses())

    assert hidden_layers(connection, ["06|Zone.Apartment"]) == []


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
                {"attributeId": {"guid": "l"}, "index": layer_index, "name": default_layer_name()}
            ]
        },
        "GetLayers": {
            "layers": [
                {
                    "attributeId": {"guid": "l"},
                    "index": layer_index,
                    "name": default_layer_name(),
                    "isHidden": False,
                    "isLocked": False,
                }
            ]
        },
        "GetElementsByType": {"elements": []},
        "CreateLayers": {"attributeIds": [{"attributeId": {"guid": "l"}}]},
        "CreateHatches": {"elements": [{"elementId": {"guid": "h"}}] * 99},
        "CreateTexts": {"elements": [{"elementId": {"guid": "t"}}] * 99},
        # A Text takes no layer, so it is created on the Text tool's default
        # and moved afterwards. The move is read back, never believed.
        "SetDetailsOfElements": {"executionResults": [{"success": True}]},
        "GetDetailsOfElements": {"detailsOfElements": [{"layerIndex": layer_index}] * 99},
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
            # The clear asks once per element type -- Hatch, Text, PolyLine --
            # and then the legend labels' move asks a fourth time.
            GetDetailsOfElements=Sequential(
                *[{"detailsOfElements": [{"layerIndex": 7}, {"layerIndex": 99}]}] * 3,
                {"detailsOfElements": [{"layerIndex": 7}] * 99},
            ),
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
    # Three because the fake answers the same list for each element type, and
    # the clear now asks about PolyLines too: the assessed-area outlines are
    # polylines, and a clear that missed them left every run's outlines under
    # the next one's.
    assert deleted == [{"elementId": {"guid": "old-1"}}] * 3, (
        "only elements on the results layer are deleted, across all three types"
    )
    assert report.fills_removed == 3
    assert transport.commands().index("DeleteElements") < transport.commands().index(
        "CreateHatches"
    )


def test_something_else_on_the_layer_is_not_touched() -> None:
    connection, transport = connect(
        _draw_responses(
            GetElementsByType={"elements": [{"elementId": {"guid": "keep"}}]},
            GetDetailsOfElements=Sequential(
                *[{"detailsOfElements": [{"layerIndex": 99}]}] * 3,
                {"detailsOfElements": [{"layerIndex": 7}] * 99},
            ),
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


def test_no_sun_never_lands_on_a_green_when_the_palette_has_blues() -> None:
    """The bug, from the reference project. The 0-hour band asks for navy and
    that pen table has no dark blue at all -- its deepest is val 0.84 -- so
    under Euclidean RGB every blue sat 136 away on lightness while a mid teal
    sat 110 away on nothing in particular, and the teal won. The plan showed
    "no sun" in green beside "under an hour" in blue.
    """
    bands = (BandStyle("0 hrs", 1e-9, fill_pen=1, rgb=(8, 48, 107)),)
    pens = (
        Pen(index=89, rgb=(64, 142, 114)),  # a teal, nearer in RGB
        Pen(index=31, rgb=(42, 42, 255)),  # a blue, further in RGB
    )
    (matched,), _ = match_pens(bands, pens)

    assert matched.fill_pen == 31, "hue decides; lightness is the forgivable error"


def test_no_sun_takes_the_deeper_blue_so_the_cold_end_runs_the_right_way() -> None:
    """Both are blue, so either satisfies the hue rule and the legend still
    has to read correctly: none must not be paler than under-an-hour."""
    bands = (
        BandStyle("0 hrs", 1e-9, fill_pen=1, rgb=(8, 48, 107)),
        BandStyle("0-1 hrs", 60.0, fill_pen=1, rgb=(43, 122, 191)),
    )
    pens = (
        Pen(index=112, rgb=(79, 120, 222)),  # paler, less saturated
        Pen(index=31, rgb=(42, 42, 255)),  # deeper, vivid
    )
    matched, _ = match_pens(bands, pens)

    assert [band.fill_pen for band in matched] == [31, 112]


def test_a_grey_is_matched_on_lightness_because_hue_means_nothing_there() -> None:
    """Hue is noise below a tenth of saturation, and a scale with a grey in it
    would otherwise match that grey to whatever hue the noise pointed at."""
    bands = (BandStyle("none", 1e-9, fill_pen=1, rgb=(128, 128, 128)),)
    pens = (Pen(index=7, rgb=(132, 130, 129)), Pen(index=8, rgb=(255, 0, 0)))
    (matched,), _ = match_pens(bands, pens)

    assert matched.fill_pen == 7


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
    assert default_property_value("number")["basicDefaultValue"]["value"] == NOT_ASSESSED_HOURS
    assert default_property_value("integer")["basicDefaultValue"]["value"] == -1
    assert default_property_value("boolean")["basicDefaultValue"]["value"] is False
    assert default_property_value("area")["basicDefaultValue"]["value"] == NOT_ASSESSED_HOURS


def test_an_unwritten_hours_column_cannot_be_mistaken_for_a_measurement() -> None:
    """A Zone in a hotlinked module refuses every write, so its default is
    what a schedule prints. Zero was indistinguishable from a flat measured
    and found to get no sun -- 11 of 15 apartments on the reference project
    read 0.000 for that reason."""
    assert NOT_ASSESSED_HOURS < 0, "no duration is negative, so this cannot be a reading"


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
                "name": default_layer_name(),
                "isHidden": True,
                "isLocked": False,
            }
        ]
    }
    connection, transport = connect(responses)
    report = draw_assessment(
        connection,
        _assessment(_apartment("apt-1")),
        [_zone("z1", storey=4)],
        zone_by_apartment={"apt-1": "z1"},
    )

    shown = transport.parameters_for("CreateLayers")["layerDataArray"][0]
    assert shown["isHidden"] is False, "the run must try to show it first"
    assert transport.parameters_for("CreateLayers")["overwriteExisting"] is True

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


def master(identifier: str, name: str) -> dict[str, Any]:
    return {
        "navigatorItem": {
            "type": "MasterLayoutItem",
            "name": name,
            "navigatorItemId": {"guid": identifier},
            "prefix": "",
        }
    }


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


def _layout_book_tree() -> dict[str, Any]:
    """The Layout Book, where masters live nested under a Masters folder."""
    return {
        "navigatorItemTree": {
            "type": "BookItem",
            "name": "SAMPLE",
            "navigatorItemId": {"guid": "book"},
            "prefix": "",
            "children": [
                {
                    "navigatorItem": {
                        "type": "MasterFolderItem",
                        "name": "Masters",
                        "navigatorItemId": {"guid": "masters"},
                        "prefix": "",
                        "children": [
                            master("m-a3", "A3 - HORIZONTAL"),
                            master("m-a1-200", "A1 - VERTICAL 1:200"),
                            master("m-a1-100", "A1 - VERTICAL 1:100"),
                        ],
                    }
                },
            ],
        }
    }


def _layout_responses(**overrides: Any) -> dict[str, Any]:
    responses: dict[str, Any] = {
        # The Project Map for the storeys, then the Layout Book twice: once to
        # choose the master, once to look for a layout of this name to reuse.
        "GetNavigatorItemTree": Sequential(_navigator_tree(), _layout_book_tree()),
        "SetViewSettings": {},
        "GetViewSettings": {
            "viewSettings": [
                # 53 x 37 m of building, which at 1:200 is 265 x 185 mm of paper.
                {"zoom": {"xMin": 0.0, "yMin": 0.0, "xMax": 53.0, "yMax": 37.0}},
                {"zoom": {"xMin": 0.0, "yMin": 0.0, "xMax": 53.0, "yMax": 37.0}},
            ]
        },
        "ChangeWindow": {"success": True},
        "GetElementsByType": {"elements": []},
        "CloneProjectMapItemToViewMap": {
            "navigatorItems": [
                {"navigatorItemId": {"guid": "v8"}},
                {"navigatorItemId": {"guid": "v9"}},
            ]
        },
        "CreateLayout": {"databases": [{"databaseId": {"guid": "lay"}}]},
        "GetLayoutSettings": {
            "layoutSettings": [
                {
                    "layoutName": "Sun Study",
                    "horizontalSize": 841.0,
                    "verticalSize": 594.0,
                    "leftMargin": 10.0,
                    "topMargin": 10.0,
                    "rightMargin": 10.0,
                    "bottomMargin": 10.0,
                }
            ]
        },
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
    assert report.storeys == (8, 9)
    assert report.drawings_placed == 2
    assert report.complete


def test_a_storey_plan_is_placed_at_full_magnification_in_metres() -> None:
    """The two faults that put a storey sheet 140 metres off the paper.

    ``CreateDrawings`` takes a *magnification* in its ``scale`` field, so the
    scale denominator sent there asked for 20000% and produced drawings 187 m
    across. Its ``position`` is in metres while the page is described in
    millimetres, so a centre meant for 140 mm was read as 140 m. Both were
    fixed for the times-of-day sheets and left in place for this one, which is
    why the same run produced one sheet that was right and one that was not.
    """
    connection, transport = connect(_layout_responses())
    layout_results(connection, [8, 9], layout_name="Sun Study", scale=100.0)

    drawings = transport.parameters_for("CreateDrawings")["drawingsData"]
    assert all(d["scale"] == 1.0 for d in drawings), (
        "the field is a magnification; the scale belongs to the view"
    )
    assert all(0.010 <= d["position"]["x"] <= 0.831 for d in drawings), "on the page, in metres"
    assert all(0.010 <= d["position"]["y"] <= 0.584 for d in drawings), "on the page, in metres"


def test_the_storeys_are_pinned_to_the_scale_the_run_reports() -> None:
    """Otherwise the drawing inherits whatever scale the storey was saved at,
    and "placed 2 drawings at 1:200" describes something that did not happen."""
    connection, transport = connect(_layout_responses())
    layout_results(connection, [8, 9], layout_name="Sun Study", scale=200.0)

    pinned = transport.parameters_for("SetViewSettings")["navigatorItemIdsWithViewSettings"]
    assert [entry["navigatorItemId"]["guid"] for entry in pinned] == ["v8", "v9"]
    assert all(entry["viewSettings"]["drawingScale"] == 200 for entry in pinned)


def test_a_layout_is_built_on_a_master_that_names_the_scale() -> None:
    """CreateLayout refuses a Layout with no master.

    Its schema says only ``layoutName`` is required; the implementation fails
    with -2130313112, "Either masterLayoutName or masterNavigatorItemId must be
    provided". An office keeps dozens of masters and they are not
    interchangeable, so the one whose name states the scale wins and the run
    reports it.
    """
    connection, transport = connect(_layout_responses())
    report = layout_results(connection, [8, 9], layout_name="Sun Study", scale=200.0)

    sent = transport.parameters_for("CreateLayout")["layoutsData"]
    assert sent == [{"layoutName": "Sun Study", "masterNavigatorItemId": {"guid": "m-a1-200"}}], (
        "the 1:200 master, not the first in the book"
    )
    assert report.master_name == "A1 - VERTICAL 1:200"
    assert "on master 'A1 - VERTICAL 1:200'" in report.describe()


def test_finishing_a_sheet_puts_the_floor_plan_back() -> None:
    """Measured on the reference project, one command after this was added.

    Straightening and tiling a sheet has to stand in the layout to do it, and
    a Layout holds no Zones. Every read after it is scoped to the database
    that is current, so the run went on to pair 0 of 10 apartments and refuse
    every plan drawing -- reporting it as a disagreement between the export
    and the project, which is several steps from the cause.
    """
    import sun_study.cli as cli

    calls: list[str] = []

    from sun_study.archicad.sheets import SheetReport

    def note(name: str) -> Any:
        def fake(*_: Any, **__: Any) -> Any:
            calls.append(name)
            return SheetReport(0, 0) if name == "straighten_and_tile" else None

        return fake

    connection, _ = connect(_layout_responses(SaveProject={}))
    monkey = pytest.MonkeyPatch()
    monkey.setattr(cli, "straighten_and_tile", note("straighten_and_tile"))
    monkey.setattr(cli, "ensure_model_database", note("ensure_model_database"))
    try:
        cli.report_layout(connection, [8, 9], name="Sun Study")
    finally:
        monkey.undo()

    assert calls == ["straighten_and_tile", "ensure_model_database"], (
        "the floor plan goes back after the sheet, and after it in that order"
    )


def test_a_reused_layout_says_it_kept_its_own_master() -> None:
    """Otherwise --master-layout looks like it worked and did nothing.

    A layout is reused by name, because making a second of the same name
    leaves the Layout Book carrying two of everything. Nothing in the add-on
    can change an existing layout's master, so a run that says "on master X"
    over a sheet that is on master Y is the tool lying about its own output.
    """
    from sun_study.archicad.layout import layout_from_views

    book = _layout_book_tree()
    book["navigatorItemTree"]["children"].append(
        {
            "navigatorItem": {
                "type": "LayoutItem",
                "name": "Sun Study",
                "navigatorItemId": {"guid": "old"},
                "prefix": "",
            }
        }
    )
    connection, transport = connect(
        _layout_responses(
            GetNavigatorItemTree=book,
            GetDatabaseIdFromNavigatorItemId={"databases": [{"databaseId": {"guid": "old"}}]},
        )
    )
    report = layout_from_views(connection, [("v8", "one")], layout_name="Sun Study")

    assert "CreateLayout" not in transport.commands(), "a second sheet of one name is worse"
    assert report.reused
    assert "is on whatever master it was made on" in report.describe()
    assert "delete the layout to rebuild it" in report.describe()


def test_drawings_are_tiled_inside_the_sheet_not_run_off_it() -> None:
    """Six storeys at a fixed 420 mm spacing is 2.5 m of paper.

    Five of the six plans then sit outside an A1 and the sheet reads as empty,
    which looks like a study that produced nothing.
    """
    sheet = LayoutSheet(width_mm=841.0, height_mm=594.0, left_mm=10.0, top_mm=10.0)
    positions = sheet.grid(6)

    assert len(positions) == 6
    assert all(10.0 <= x <= 841.0 and 10.0 <= y <= 594.0 for x, y in positions)
    assert len({y for _, y in positions}) > 1, "six drawings do not fit on one row"


def _drawing(
    width: float, height: float, *, ratio: float = 1.0, angle: float = 0.0
) -> dict[str, Any]:
    return {
        "details": {
            "angle": angle,
            "ratio": ratio,
            "bounds": {"xMin": 0.0, "yMin": 0.0, "xMax": width, "yMax": height},
        }
    }


def _tilted(width: float, height: float, radians: float) -> dict[str, Any]:
    """A drawing whose frame stands at ``radians`` while its angle field lies."""
    corners = [(0.0, 0.0), (width, 0.0), (width, height), (0.0, height)]
    cos, sin = math.cos(radians), math.sin(radians)
    turned = [(x * cos - y * sin, x * sin + y * cos) for x, y in corners]
    return {
        "details": {
            "angle": 0.0,
            "ratio": 1.0,
            "bounds": {
                "xMin": min(x for x, _ in turned),
                "yMin": min(y for _, y in turned),
                "xMax": max(x for x, _ in turned),
                "yMax": max(y for _, y in turned),
            },
            "clipPolygon": [{"x": x, "y": y} for x, y in turned]
            + [{"x": turned[0][0], "y": turned[0][1]}],
        }
    }


def test_a_drawing_is_measured_by_its_corners_not_by_its_angle_field() -> None:
    """The field says what was last written to it, not where the drawing is.

    A run set ``angle`` to zero through ``SetDetailsOfElements``, read zero
    back, and reported six drawings straightened. Every frame on the sheet was
    still at 279.9 degrees -- the Drawing tool's default, which is this
    project's north -- and the sheet came out visibly crooked with the
    settings dialog insisting on 0.00 degrees.
    """
    from sun_study.archicad.sheets import straighten_and_tile

    off_axis = math.radians(9.9)
    connection, transport = connect(
        {
            "ChangeWindow": {"success": True},
            "GetElementsByType": {"elements": [{"elementId": {"guid": "d1"}}]},
            "GetDetailsOfElements": Sequential(
                {"detailsOfElements": [_tilted(0.19, 0.29, off_axis)]},
                {"detailsOfElements": [_tilted(0.19, 0.29, 0.0)]},
            ),
            "RotateElements": {},
            "MoveElements": {},
        }
    )
    report = straighten_and_tile(connection, "lay", LayoutSheet(841.0, 594.0))

    assert report.straightened == 1, "the corners say it is crooked even though the field does not"
    assert "SetDetailsOfElements" not in transport.commands(), (
        "writing the angle field is what looked like it worked and did not"
    )
    turn = transport.parameters_for("RotateElements")["elementsWithRotations"][0]["rotation"]
    swept = math.atan2(
        turn["endPoint"]["y"] - turn["origin"]["y"], turn["endPoint"]["x"] - turn["origin"]["x"]
    )
    assert swept == pytest.approx(-off_axis, abs=1e-6), "turned back by what it was off by"


def test_a_frame_square_to_the_page_is_left_alone() -> None:
    """A quarter turn is upright too, so the angle is read modulo one."""
    from sun_study.archicad.sheets import straighten_and_tile

    connection, transport = connect(
        {
            "ChangeWindow": {"success": True},
            "GetElementsByType": {"elements": [{"elementId": {"guid": "d1"}}]},
            "GetDetailsOfElements": {"detailsOfElements": [_tilted(0.19, 0.29, math.pi / 2)]},
            "MoveElements": {},
        }
    )
    report = straighten_and_tile(connection, "lay", LayoutSheet(841.0, 594.0))

    assert report.straightened == 0
    assert "RotateElements" not in transport.commands()


def test_a_drawing_too_big_for_its_sheet_is_shrunk_before_it_is_tiled() -> None:
    """A view of the coloured model arrived 1,429 x 1,256 mm on an A1.

    Nothing can be done about that by moving it, and the Drawing's *scale* is
    not settable -- ``SetDetailsOfElements`` takes a ``drawingScale``, answers
    success and leaves it alone. Its magnification is settable, so that is
    what gives, and the run has to say so: a drawing at 47% is no longer at
    the scale the sheet claims for it.
    """
    from sun_study.archicad.sheets import straighten_and_tile

    connection, transport = connect(
        {
            "ChangeWindow": {"success": True},
            "GetElementsByType": {"elements": [{"elementId": {"guid": "d1"}}]},
            "GetDetailsOfElements": Sequential(
                {"detailsOfElements": [_drawing(1.4292, 1.2557)]},
                {"detailsOfElements": [_drawing(0.670, 0.589, ratio=0.469)]},
            ),
            "SetDetailsOfElements": {"executionResults": [{"success": True}]},
            "MoveElements": {},
        }
    )
    report = straighten_and_tile(connection, "lay", LayoutSheet(841.0, 594.0))

    sent = transport.parameters_for("SetDetailsOfElements")["elementsWithDetails"]
    assert sent[0]["details"]["typeSpecificDetails"]["ratio"] == pytest.approx(0.469, abs=0.005)
    assert report.shrunk_to == pytest.approx(0.469, abs=0.005)
    assert "no longer at the scale" in report.describe(), "a quiet shrink is a lie on the sheet"


def test_a_drawing_already_shrunk_is_not_shrunk_again() -> None:
    """A Drawing's bounds do not follow its magnification.

    Setting a 3D view's drawing to 46.9% stores 46.9% and it reads back, while
    the bounds go on reporting the size it had before -- until Archicad
    regenerates the drawing, which happens when somebody opens the layout. So
    a pass that worked the full size out as "bounds over magnification" read
    3,050 mm off a drawing 1,429 mm wide, shrank it again to 22.1%, and would
    have halved it on every run after that.
    """
    from sun_study.archicad.sheets import straighten_and_tile

    connection, transport = connect(
        {
            "ChangeWindow": {"success": True},
            "GetElementsByType": {"elements": [{"elementId": {"guid": "d1"}}]},
            # Stale bounds: still the full-size ones, at a magnification that
            # says this drawing has already been fitted.
            "GetDetailsOfElements": {"detailsOfElements": [_drawing(1.4292, 1.2557, ratio=0.469)]},
            "MoveElements": {},
        }
    )
    report = straighten_and_tile(connection, "lay", LayoutSheet(841.0, 594.0))

    assert "SetDetailsOfElements" not in transport.commands(), "already fitted; leave it"
    assert report.shrunk_to == 1.0, "nothing was changed, so nothing is reported"


def test_a_drawing_left_at_a_scale_denominator_is_repaired_in_one_write() -> None:
    """The old fault: the scale denominator went into the magnification field,
    so eighteen storey plans stood at 200x and 187 metres across.

    Its full size is its bounds over its magnification -- 937 mm here -- because
    Archicad drew the two together. Resetting it to 100% and measuring again
    would look more careful and be worse: the bounds do not follow the change,
    so the second read returns the same 187 metres and the drawing ends up at
    0.1% of a nonsense. Measured on the reference project, which is what this
    number is.
    """
    from sun_study.archicad.sheets import straighten_and_tile

    connection, transport = connect(
        {
            "ChangeWindow": {"success": True},
            "GetElementsByType": {"elements": [{"elementId": {"guid": "d1"}}]},
            "GetDetailsOfElements": {"detailsOfElements": [_drawing(187.37, 249.88, ratio=200.0)]},
            "SetDetailsOfElements": {"executionResults": [{"success": True}]},
            "MoveElements": {},
        }
    )
    report = straighten_and_tile(connection, "lay", LayoutSheet(841.0, 594.0))

    written = transport.parameters_for("SetDetailsOfElements")["elementsWithDetails"]
    assert written[0]["details"]["typeSpecificDetails"]["ratio"] == pytest.approx(0.471, abs=0.005)
    assert report.shrunk_to == pytest.approx(0.471, abs=0.005)


def test_drawings_that_fit_are_moved_and_not_resized() -> None:
    from sun_study.archicad.sheets import straighten_and_tile

    connection, transport = connect(
        {
            "ChangeWindow": {"success": True},
            "GetElementsByType": {"elements": [{"elementId": {"guid": "d1"}}]},
            "GetDetailsOfElements": {"detailsOfElements": [_drawing(0.189, 0.295)]},
            "MoveElements": {},
        }
    )
    report = straighten_and_tile(connection, "lay", LayoutSheet(841.0, 594.0))

    assert report.shrunk_to == 1.0
    assert "SetDetailsOfElements" not in transport.commands()
    assert report.moved == 1
    assert report.describe() == "straightened 0, tiled 1"


def test_a_sheet_that_will_not_describe_itself_falls_back_and_says_so() -> None:
    """The fills are already drawn by then; no sheet at all would be worse."""
    connection, _ = connect({"GetLayoutSettings": {"layoutSettings": []}})
    sheet, assumed = layout_sheet(connection, "lay")

    assert sheet == DEFAULT_SHEET
    assert assumed


def test_the_sheet_size_comes_from_the_layout_when_it_is_stated() -> None:
    connection, _ = connect(_layout_responses())
    sheet, assumed = layout_sheet(connection, "lay")

    assert (sheet.width_mm, sheet.height_mm) == (841.0, 594.0)
    assert not assumed


def test_a_named_master_that_does_not_exist_lists_the_ones_that_do() -> None:
    """Falling back to an arbitrary sheet would issue the study on somebody
    else's title block."""
    connection, _ = connect(_layout_responses())
    with pytest.raises(ArchicadError, match="A1 - VERTICAL 1:200"):
        layout_results(
            connection, [8, 9], layout_name="Sun Study", master_layout="A1 VERTICAL 1:250"
        )


def test_a_master_is_matched_past_spacing_and_case() -> None:
    connection, _ = connect({"GetNavigatorItemTree": _layout_book_tree()})
    masters = master_layouts(connection)
    assert choose_master(masters, "a1 -  vertical 1:100", 200.0).name == "A1 - VERTICAL 1:100"
    assert choose_master(masters, None, 50.0).name == "A3 - HORIZONTAL", (
        "no master names 1:50, so the first in the book is used"
    )


def test_a_master_is_found_by_the_words_in_its_name() -> None:
    """Nobody reproduces an office's punctuation from memory.

    The reference project keeps ``DA A1 - VERTICAL - No Scale`` next to ``DA
    A1 - VERTICAL COVER/NO SCALE`` in a book of seventy-one, and what a person
    types is "A1 no scale". Exact-match-or-nothing answered that by printing
    all seventy-one and stopping.
    """
    from sun_study.archicad.layout import NavigatorItem

    masters = (
        NavigatorItem("m1", "A4", "MasterLayoutItem", ""),
        NavigatorItem("m2", "DA A1 - VERTICAL 1:200", "MasterLayoutItem", ""),
        NavigatorItem("m3", "DA A1 - VERTICAL - No Scale", "MasterLayoutItem", ""),
        NavigatorItem("m4", "DA A1 - VERTICAL COVER/NO SCALE", "MasterLayoutItem", ""),
    )
    assert choose_master(masters, "DA A1 no scale", 200.0).name == "DA A1 - VERTICAL - No Scale", (
        "the candidate carrying the fewest words nobody asked for"
    )
    assert choose_master(masters, "cover no scale", 200.0).name == "DA A1 - VERTICAL COVER/NO SCALE"


def test_two_masters_equally_close_are_a_question_not_a_guess() -> None:
    """Either choice would put the study on a title block for something else."""
    from sun_study.archicad.layout import NavigatorItem

    masters = (
        NavigatorItem("m1", "A1 PLANS No Scale", "MasterLayoutItem", ""),
        NavigatorItem("m2", "A1 COVER No Scale", "MasterLayoutItem", ""),
    )
    with pytest.raises(ArchicadError, match="fits 2 master layouts equally well"):
        choose_master(masters, "A1 no scale", 200.0)


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


# -- rooms that exist only as library objects ------------------------------


def _object_responses(**overrides: Any) -> dict[str, Any]:
    """A project where rooms are label objects, not Zones.

    Modelled on a real file: the placed labels sit at storey height, and the
    hotlink masters they came from are parked ~67 m above the building.
    """
    responses: dict[str, Any] = {
        "GetElementsByType": {
            "elements": [{"elementId": {"guid": g}} for g in ("o1", "o2", "master")]
        },
        "GetDetailsOfElements": {
            "detailsOfElements": [
                {
                    "floorIndex": 10,
                    "layerIndex": 5,
                    "details": {
                        "libPart": {"name": "Room Name and Size Label 19"},
                        "origin": {"x": 3.0, "y": 4.0, "z": 30.9},
                    },
                },
                {
                    "floorIndex": 10,
                    "layerIndex": 5,
                    "details": {
                        "libPart": {"name": "Room Name and Size Label 19"},
                        "origin": {"x": 9.0, "y": 4.0, "z": 30.9},
                    },
                },
                {
                    "floorIndex": 10,
                    "layerIndex": 5,
                    "details": {
                        "libPart": {"name": "Room Name and Size Label 19"},
                        "origin": {"x": 3.0, "y": 4.0, "z": 67.6},
                    },
                },
            ]
        },
        "GetGDLParametersOfElements": {
            "gdlParametersOfElements": [
                {
                    "parameters": [
                        {"name": "roomName", "type": "String", "value": "LIVING"},
                        {"name": "A", "type": "Length", "value": 6.0},
                        {"name": "B", "type": "Length", "value": 4.5},
                        {"name": "gs_list", "type": "Array", "value": [1, 2, 3, 4]},
                    ]
                }
            ]
        },
    }
    responses.update(overrides)
    return responses


def test_a_library_object_carries_its_part_name_and_placement() -> None:
    """Where Zones are per unit, the room only exists as a label object, and
    its placement is the only thing that says which room a window belongs to."""
    connection, _ = connect(_object_responses())
    found = library_objects(connection)

    assert len(found) == 3
    assert found[0].library_part == "Room Name and Size Label 19"
    assert found[0].origin == (3.0, 4.0, 30.9)
    assert found[0].storey_index == 10
    assert found[0].layer_index == 5


def test_parameters_are_not_read_unless_asked_for() -> None:
    """A library part can carry hundreds of parameters and a project holds
    thousands of objects; reading both together answers one question very
    expensively."""
    connection, transport = connect(_object_responses())
    library_objects(connection)
    assert "GetGDLParametersOfElements" not in transport.commands()


def test_the_room_name_is_readable_from_the_gdl_parameters() -> None:
    connection, _ = connect(_object_responses())
    opened = gdl_parameters(connection, library_objects(connection)[:1])

    assert opened[0].parameter("roomName") == "LIVING"
    assert opened[0].parameter("ROOMNAME") == "LIVING", "matched case-insensitively"
    assert opened[0].parameter("nope") is None


def test_an_array_parameter_is_summarised_not_dumped() -> None:
    """A room name is a string. Array parameters are noise in a probe whose
    whole job is to find which parameter holds a name."""
    connection, _ = connect(_object_responses())
    opened = gdl_parameters(connection, library_objects(connection)[:1])
    assert opened[0].parameter("gs_list") == "<array of 4>"


def test_only_the_sampled_objects_are_opened() -> None:
    """The probe opens a few, not all of them."""
    connection, transport = connect(_object_responses())
    found = library_objects(connection)
    gdl_parameters(connection, found[:1])

    asked = transport.parameters_for("GetGDLParametersOfElements")["elements"]
    assert asked == [{"elementId": {"guid": "o1"}}]


def test_a_hotlink_master_is_visible_by_its_height() -> None:
    """Masters are parked far above the building -- 67 m in one real project --
    so nothing that matches a room to an apartment can take placement on
    trust. The height is carried so the caller can see them."""
    connection, _ = connect(_object_responses())
    heights = sorted(item.origin[2] for item in library_objects(connection))
    assert heights[-1] - heights[0] > 30.0


def test_a_project_with_no_objects_is_not_an_error() -> None:
    connection, _ = connect({"GetElementsByType": {"elements": []}})
    assert library_objects(connection) == ()


def test_an_open_modal_dialog_is_explained_rather_than_relayed() -> None:
    """The most common way a run fails on a workstation somebody is also using.

    Archicad blocks its entire API while a modal dialog is open and answers
    "Invalid program status", which reads like a fault in the tool.
    """

    class Busy:
        def send(self, payload: dict[str, Any]) -> dict[str, Any]:
            return {
                "succeeded": False,
                "error": {
                    "code": 4001,
                    "message": (
                        "Invalid program status (there is an open modal dialog: "
                        "Object Selection Settings)"
                    ),
                },
            }

    connection = ArchicadConnection(Busy())
    with pytest.raises(CommandFailedError) as raised:
        connection.run_official("API.GetAllClassificationSystems")

    message = str(raised.value)
    assert "Object Selection Settings" in message, "the dialog Archicad named is kept"
    assert "Close the dialog in Archicad" in message
    assert "not a problem with the project or the tool" in message


def test_other_failures_keep_their_code_for_looking_up() -> None:
    """Only 4001 has a known fix. Everything else needs its number."""

    class Failing:
        def send(self, payload: dict[str, Any]) -> dict[str, Any]:
            return {"succeeded": False, "error": {"code": -2130312909, "message": "no access"}}

    with pytest.raises(CommandFailedError, match=r"code -2130312909"):
        ArchicadConnection(Failing()).run_official("API.GetAllClassificationSystems")


# ---------------------------------------------------------------------------
# The layer state the export runs under. The translator exports what is
# *shown*, so this is an input to every number the study produces -- and until
# now it was whatever combination somebody had left active.
# ---------------------------------------------------------------------------
def _layer_attributes(*names: str) -> dict[str, Any]:
    return {
        "attributes": [
            {"attributeId": {"guid": f"L{index}"}, "index": index, "name": name}
            for index, name in enumerate(names)
        ]
    }


def written(
    name: str, *, hidden: bool, locked: bool, wireframe: bool = False, group: int = 1
) -> dict[str, Any]:
    """One layer as ``_write`` sends it.

    Every field is named because ``CreateLayers`` writes the whole layer: one
    left out is one reset to its default, so a restore that mentions only
    visibility and lock flattens how the layer renders in 3D.
    """
    return {
        "name": name,
        "isHidden": hidden,
        "isLocked": locked,
        "isWireframe": wireframe,
        "intersectionGroupNr": group,
    }


def _layer_states(*states: tuple[str, bool, bool]) -> dict[str, Any]:
    return {
        "layers": [
            {"layerAttribute": {"name": name, "isHidden": hidden, "isLocked": locked}}
            for name, hidden, locked in states
        ]
    }


def _layer_world(**overrides: Any) -> dict[str, Any]:
    """A project of three layers, one of them switched off and locked."""
    responses: dict[str, Any] = {
        "GetAttributesByType": Sequential(
            _layer_attributes("Walls", "Context", "Joinery"),
            {"attributes": []},  # no layer combinations yet
        ),
        "GetLayers": _layer_states(
            ("Walls", False, False), ("Context", True, True), ("Joinery", False, False)
        ),
        "CreateLayerCombinations": {"attributeIds": [{"attributeId": {"guid": "C1"}}]},
        "CreateLayers": {},
    }
    responses.update(overrides)
    return responses


def test_the_export_switches_on_what_the_study_needs_and_puts_it_back() -> None:
    """The whole point: the answer must not depend on what was on screen.

    A site-plan combination gave an export of 386 walls, 92 windows and no
    IfcSpace at all (D52), and the reference project's combination reverted
    three times in one session because the tool's own views kept applying
    theirs. Checking and complaining put the work on the person; this does it.
    """
    from sun_study.archicad.layers import export_state

    connection, transport = connect(_layer_world())
    with export_state(connection) as plan:
        pass

    applied, restored = transport.all_parameters_for("CreateLayers")
    assert applied["overwriteExisting"] is True
    assert applied["layerDataArray"] == [written("Context", hidden=False, locked=False)], (
        "only the layer that was off, and only switched on"
    )
    assert restored["layerDataArray"] == [written("Context", hidden=True, locked=True)], (
        "put back exactly as it was, lock included"
    )
    assert plan.shown == ("Context",)
    assert plan.changed == 1


def test_the_layers_are_put_back_even_when_the_export_fails() -> None:
    """An export that raises must not leave somebody's model rearranged."""
    from sun_study.archicad.layers import export_state

    connection, transport = connect(_layer_world())
    with contextlib.suppress(RuntimeError), export_state(connection):
        raise RuntimeError("the translator fell over")

    _, restored = transport.all_parameters_for("CreateLayers")
    assert restored["layerDataArray"] == [written("Context", hidden=True, locked=True)]


def test_a_named_layer_is_switched_off_for_the_export() -> None:
    """Furniture and joinery shade a room without being part of it, and they
    are a project's own business rather than something to guess from names."""
    from sun_study.archicad.layers import export_state

    connection, transport = connect(_layer_world())
    with export_state(connection, hide=["joinery"]) as plan:
        pass

    applied, _ = transport.all_parameters_for("CreateLayers")
    assert written("Joinery", hidden=True, locked=False) in applied["layerDataArray"]
    assert plan.hidden == ("Joinery",)


# -- layer state belongs to a database, not to the project ----------------
# Measured on the reference project, in this order: on a floor plan
# "05 | Dims/Notes.DA" reads hidden; switch to a layout and it reads visible;
# write it hidden there and it takes; switch away and back and it is visible
# again. The layout's combination is reapplied every time, so a write made in
# one is discarded and a snapshot taken in one is a layout's opinion. D63.


def _go_to(connection: ArchicadConnection, guid: str, window: str) -> None:
    connection.run_tapir("ChangeWindow", {"databaseId": {"guid": guid}, "windowType": window})


# -- filing the sheets where the practice keeps them ----------------------
# CreateLayout takes a parentNavigatorItemId, answers with a database id, and
# puts the sheet at the book root anyway -- measured on the reference project.
# So the sheet is made first and moved second, and the move is read back.


def _node(name: str, kind: str, *children: dict[str, Any]) -> dict[str, Any]:
    return {
        "navigatorItemId": {"guid": name},
        "name": name,
        "type": kind,
        "children": [{"navigatorItem": child} for child in children],
    }


def _book(*children: dict[str, Any]) -> dict[str, Any]:
    return {"navigatorItemTree": _node("BOOK", "BookItem", *children)}


def test_a_sheet_is_moved_into_the_subset_the_practice_already_uses() -> None:
    from sun_study.archicad.layout import file_under_subset

    loose = _book(
        _node("SHADOW DIAGRAMS", "SubSetItem"),
        _node(f"{SS} Sun Study 09:00", "LayoutItem"),
    )
    filed = _book(
        _node("SHADOW DIAGRAMS", "SubSetItem", _node(f"{SS} Sun Study 09:00", "LayoutItem")),
    )
    connection, transport = connect(
        {
            "GetNavigatorItemTree": Sequential(loose, filed),
            "MoveNavigatorItem": {"success": True},
        }
    )
    report = file_under_subset(connection, [f"{SS} Sun Study 09:00"], "SHADOW DIAGRAMS")

    assert transport.parameters_for("MoveNavigatorItem") == {
        "navigatorItemIdToMove": {"guid": f"{SS} Sun Study 09:00"},
        "parentNavigatorItemId": {"guid": "SHADOW DIAGRAMS"},
    }
    assert report.moved == (f"{SS} Sun Study 09:00",)
    assert "filed 1 sheet(s) under 'SHADOW DIAGRAMS'" in report.describe()


def test_a_sheet_already_in_the_subset_is_not_moved_again() -> None:
    """The ordinary case on a rerun, and moving it would be a write for
    nothing."""
    from sun_study.archicad.layout import file_under_subset

    filed = _book(
        _node("ADG DIAGRAMS", "SubSetItem", _node(f"{SS} Sun Study Bands", "LayoutItem")),
    )
    connection, transport = connect({"GetNavigatorItemTree": filed})
    report = file_under_subset(connection, [f"{SS} Sun Study Bands"], "ADG DIAGRAMS")

    assert "MoveNavigatorItem" not in transport.commands()
    assert report.already == (f"{SS} Sun Study Bands",) and report.moved == ()


def test_a_subset_that_is_not_there_is_reported_not_created() -> None:
    """The Layout Book is the practice's own structure. Inventing a subset in
    it is a bigger decision than a sun study gets to make."""
    from sun_study.archicad.layout import file_under_subset

    connection, transport = connect(
        {"GetNavigatorItemTree": _book(_node(f"{SS} Sun Study 09:00", "LayoutItem"))}
    )
    report = file_under_subset(connection, [f"{SS} Sun Study 09:00"], "SHADOW DIAGRAMS")

    assert report.no_such_subset is True
    assert "MoveNavigatorItem" not in transport.commands()
    assert "no subset called 'SHADOW DIAGRAMS'" in report.describe()


def test_a_move_that_reported_success_and_did_nothing_is_caught() -> None:
    """MoveNavigatorItem answers {"success": true}. So does CreateLayout when
    it ignores the parent it was given, which is how this whole function came
    to exist -- so the book is read back rather than believed."""
    from sun_study.archicad.layout import file_under_subset

    loose = _book(
        _node("SHADOW DIAGRAMS", "SubSetItem"),
        _node(f"{SS} Sun Study 09:00", "LayoutItem"),
    )
    connection, _ = connect(
        {
            "GetNavigatorItemTree": loose,
            "MoveNavigatorItem": {"success": True},
        }
    )
    with pytest.raises(ArchicadError, match="would not move into"):
        file_under_subset(connection, [f"{SS} Sun Study 09:00"], "SHADOW DIAGRAMS")


def test_a_sheet_the_book_does_not_hold_is_named_rather_than_counted() -> None:
    from sun_study.archicad.layout import file_under_subset

    book = _book(_node("SHADOW DIAGRAMS", "SubSetItem"))
    connection, _ = connect({"GetNavigatorItemTree": book})
    report = file_under_subset(connection, [f"{SS} Sun Study 09:00"], "SHADOW DIAGRAMS")

    assert report.missing == (f"{SS} Sun Study 09:00",)
    assert report.moved == ()


# -- the views every drawing is made from ---------------------------------


def _view_world(**overrides: Any) -> dict[str, Any]:
    responses: dict[str, Any] = {
        "GetNavigatorItemTree": _navigator(
            "PublicViewMap", ("FolderItem", f"{SS} Sun Study fix 01")
        ),
        "CreateViewMapFolder": {"navigatorItemId": {"guid": "F1"}},
        "CreateViewsInViewMap": {"navigatorItems": [{"navigatorItemId": {"guid": "V1"}}]},
        "SetViewSettings": {},
    }
    responses.update(overrides)
    return responses


def _storey() -> Any:
    from sun_study.archicad.layout import NavigatorItem

    return NavigatorItem(identifier="N0", name="LEVEL 01", kind="StoryItem", prefix="1")


def test_a_view_carries_the_scale_and_is_pinned_square_to_the_page() -> None:
    """The two denominators compound rather than cancel, so the view carries
    the scale and the Drawing is placed at 100%. And a view inherits the
    storey's rotation -- this project's is turned to true north, which is
    where every drawing's 279.9 degrees came from."""
    from sun_study.archicad.views import views_for_storeys

    connection, transport = connect(_view_world())
    made = views_for_storeys(
        connection,
        [_storey()],
        combination=f"{SS} Sun Study 09:00",
        suffix="09:00",
        drawing_scale=200,
        folder=f"{SS} Sun Study fix 01",
    )

    assert [view.name for view in made] == [f"{SS} LEVEL 01 09:00"]
    sent = transport.parameters_for("SetViewSettings")["navigatorItemIdsWithViewSettings"]
    settings = sent[0]["viewSettings"]
    assert settings["drawingScale"] == 200
    assert settings["rotation"] == 0
    assert settings["layerCombination"] == f"{SS} Sun Study 09:00"


def test_a_view_that_refuses_its_settings_is_raised_not_drawn_anyway() -> None:
    """This one call decides the layer combination, the scale and the rotation
    of every drawing on the sheet. Ignoring a per-item error here is the
    difference between a wrong sheet and a run that says which view refused."""
    from sun_study.archicad.views import views_for_storeys

    connection, _ = connect(
        _view_world(
            SetViewSettings={
                "executionResults": [{"error": {"message": "view is locked", "code": -1}}]
            }
        )
    )
    with pytest.raises(ArchicadError, match="view is locked"):
        views_for_storeys(
            connection,
            [_storey()],
            combination=f"{SS} X",
            suffix="09:00",
            drawing_scale=200,
            folder=f"{SS} Sun Study fix 01",
        )


# -- the selection that empties an export ---------------------------------


def test_nothing_selected_is_not_reported_as_something_cleared() -> None:
    from sun_study.archicad.read import clear_selection

    connection, transport = connect({"GetSelectedElements": {"elements": []}})
    assert clear_selection(connection) == 0
    assert "ChangeSelectionOfElements" not in transport.commands()


def test_a_selection_is_cleared_and_counted_so_the_run_can_say_so() -> None:
    """The tool sets this trap itself: CreateHatches leaves its last fill
    selected, so drawing one run's result empties the next run's export."""
    from sun_study.archicad.read import clear_selection

    picked = [{"elementId": {"guid": "E1"}}, {"elementId": {"guid": "E2"}}]
    connection, transport = connect(
        {
            "GetSelectedElements": Sequential({"elements": picked}, {"elements": []}),
            "ChangeSelectionOfElements": {},
        }
    )
    assert clear_selection(connection) == 2
    assert transport.parameters_for("ChangeSelectionOfElements") == {
        "removeElementsFromSelection": picked
    }


def test_a_selection_that_will_not_clear_stops_the_run_where_it_is() -> None:
    """Everything about this failure is silent: the command reports success,
    the export writes 5.8 kB against 86 MB, and the run fails three steps
    later as "apartment zone layers matched nothing" -- sending the reader to
    check a layer name that was right. Better to stop at the cause."""
    from sun_study.archicad.read import clear_selection

    picked = [{"elementId": {"guid": "E1"}}]
    connection, _ = connect(
        {
            "GetSelectedElements": {"elements": picked},
            "ChangeSelectionOfElements": {},
        }
    )
    with pytest.raises(ArchicadError, match="still selected"):
        clear_selection(connection)


# -- clearing out the last run --------------------------------------------
# Destructive, and it reports what it did. Both halves were wrong: it counted
# drawings that go with their layout, and it counted its own folders as
# things it had failed to delete.


def _navigator(map_id: str, *items: tuple[str, str]) -> dict[str, Any]:
    """A navigator tree of ``(kind, name)`` children, ids from the name."""
    return {
        "navigatorItemTree": {
            "navigatorItemId": {"guid": f"{map_id}-root"},
            "name": map_id,
            "children": [
                {
                    "navigatorItem": {
                        "navigatorItemId": {"guid": name},
                        "name": name,
                        "type": kind,
                    }
                }
                for kind, name in items
            ],
        }
    }


def test_the_drawings_that_go_with_a_layout_are_not_counted_as_removed() -> None:
    """A DrawingItem shows in the Layout Book under the layout holding it. It
    goes when the layout goes, so asking for it by id deletes nothing -- and
    counting it said thirty-five views and layouts where five layouts were."""
    from sun_study.archicad.views import remove_previous

    connection, transport = connect(
        {
            "GetNavigatorItemTree": Sequential(
                _navigator(
                    "LayoutBook",
                    ("LayoutItem", f"{SS} Sun Study 09:00"),
                    ("DrawingItem", f"{SS} LEVEL 01 09:00"),
                    ("DrawingItem", f"{SS} LEVEL 02 09:00"),
                ),
                _navigator("LayoutBook"),
                _navigator("PublicViewMap"),
                _navigator("LayoutBook"),
                _navigator("PublicViewMap"),
            ),
            "DeleteNavigatorItems": {},
        }
    )
    gone, left = remove_previous(connection)

    asked = transport.parameters_for("DeleteNavigatorItems")["navigatorItemIds"]
    assert [entry["navigatorItemId"]["guid"] for entry in asked] == [f"{SS} Sun Study 09:00"]
    assert (gone, left) == (1, 0)


def test_the_tools_own_run_folder_is_not_a_view_it_failed_to_delete() -> None:
    """A folder cannot be deleted through the API, which is why each run makes
    a new one. Counting it as left behind made every run after the first warn
    that a placed Drawing was holding a view -- about a folder."""
    from sun_study.archicad.views import remove_previous

    connection, _ = connect(
        {
            "GetNavigatorItemTree": Sequential(
                _navigator("LayoutBook"),
                _navigator(
                    "PublicViewMap",
                    ("FolderItem", f"{SS} Sun Study fix 01"),
                    ("StoryItem", f"{SS} LEVEL 01 09:00"),
                ),
                _navigator("PublicViewMap", ("FolderItem", f"{SS} Sun Study fix 01")),
                _navigator("LayoutBook"),
                _navigator("PublicViewMap", ("FolderItem", f"{SS} Sun Study fix 01")),
            ),
            "DeleteNavigatorItems": {},
        }
    )
    gone, left = remove_previous(connection)

    assert (gone, left) == (1, 0), "the storey view went; the folder was never a target"


def test_a_view_a_drawing_still_points_at_is_reported_as_left_behind() -> None:
    """The real condition the warning is for. DeleteNavigatorItems reports
    success and the view stays, so the count comes from reading the tree
    again rather than from the length of the request."""
    from sun_study.archicad.views import remove_previous

    stuck = _navigator("PublicViewMap", ("StoryItem", f"{SS} LEVEL 01 09:00"))
    connection, _ = connect(
        {
            "GetNavigatorItemTree": Sequential(
                _navigator("LayoutBook"), stuck, stuck, _navigator("LayoutBook"), stuck
            ),
            "DeleteNavigatorItems": {},
        }
    )
    assert remove_previous(connection) == (0, 1)


def test_something_the_tool_did_not_make_is_never_touched() -> None:
    """The prefix is the whole guarantee: it is somebody's project."""
    from sun_study.archicad.views import remove_previous

    connection, transport = connect(
        {
            "GetNavigatorItemTree": Sequential(
                _navigator("LayoutBook", ("LayoutItem", "A1 Site Plan")),
                _navigator("PublicViewMap", ("StoryItem", "LEVEL 01")),
                _navigator("LayoutBook", ("LayoutItem", "A1 Site Plan")),
                _navigator("PublicViewMap", ("StoryItem", "LEVEL 01")),
            ),
        }
    )
    assert remove_previous(connection) == (0, 0)
    assert "DeleteNavigatorItems" not in transport.commands()


# -- the results layer ----------------------------------------------------
# Also untested until now. It is what decides whether a run's output can be
# seen: a hidden results layer makes a successful run look exactly like one
# that did nothing.


def _one_layer(*, hidden: bool, locked: bool = False, **rest: Any) -> dict[str, Any]:
    attribute = {
        "name": "Sun Study.Results",
        "isHidden": hidden,
        "isLocked": locked,
        **rest,
    }
    return {
        "GetAttributesByType": {
            "attributes": [{"attributeId": {"guid": "g"}, "index": 12, "name": "Sun Study.Results"}]
        },
        "GetLayers": {"layers": [{"layerAttribute": attribute}]},
        "CreateLayers": {},
    }


def test_a_results_layer_that_is_already_visible_is_left_entirely_alone() -> None:
    from sun_study.archicad.draw import ensure_layer

    connection, transport = connect(_one_layer(hidden=False))
    layer = ensure_layer(connection, "Sun Study.Results")

    assert (layer.index, layer.hidden, layer.invisible) == (12, False, False)
    assert "CreateLayers" not in transport.commands(), "nothing to do, so nothing written"


def test_a_hidden_results_layer_is_switched_on_because_nobody_would_see_the_run() -> None:
    """Measured on the reference project: a fresh results layer came back
    hidden, and the run drew a thousand fills nobody could see."""
    from sun_study.archicad.draw import ensure_layer

    connection, transport = connect(
        {
            **_one_layer(hidden=True),
            "GetLayers": Sequential(
                {"layers": [{"layerAttribute": {"name": "Sun Study.Results", "isHidden": True}}]},
                {"layers": [{"layerAttribute": {"name": "Sun Study.Results", "isHidden": False}}]},
            ),
        }
    )
    layer = ensure_layer(connection, "Sun Study.Results")

    assert layer.hidden is False
    written_layer = transport.parameters_for("CreateLayers")
    assert written_layer["overwriteExisting"] is True
    assert written_layer["layerDataArray"][0]["isHidden"] is False


def test_switching_the_layer_on_does_not_flatten_the_rest_of_it() -> None:
    """overwriteExisting writes the whole layer, so a field left out is a
    field reset. Naming only visibility and lock turned wireframe off and
    moved the layer to intersection group 1 -- on a layer somebody may have
    pointed --layer-name at deliberately."""
    from sun_study.archicad.draw import ensure_layer

    connection, transport = connect(
        _one_layer(hidden=True, isWireframe=True, intersectionGroupNr=5)
    )
    ensure_layer(connection, "Sun Study.Results")

    sent = transport.parameters_for("CreateLayers")["layerDataArray"][0]
    assert sent["isWireframe"] is True
    assert sent["intersectionGroupNr"] == 5


def test_a_layer_that_is_not_there_is_created_rather_than_demanded() -> None:
    from sun_study.archicad.draw import ensure_layer

    connection, transport = connect(
        {
            "GetAttributesByType": Sequential(
                {"attributes": []},
                {
                    "attributes": [
                        {"attributeId": {"guid": "g"}, "index": 12, "name": "Sun Study.Results"}
                    ]
                },
            ),
            "GetLayers": {"layers": [{"layerAttribute": {"name": "Sun Study.Results"}}]},
            "CreateLayers": {},
        }
    )
    assert ensure_layer(connection, "Sun Study.Results").index == 12
    assert transport.parameters_for("CreateLayers")["overwriteExisting"] is False


def test_a_layer_created_but_not_listed_stops_the_run_with_the_reason() -> None:
    """Without an index there is nowhere to put a fill, and carrying on would
    draw the study onto whatever layer happened to be current."""
    from sun_study.archicad.draw import ensure_layer

    connection, _ = connect(
        {
            "GetAttributesByType": {"attributes": []},
            "GetLayers": {"layers": []},
            "CreateLayers": {},
        }
    )
    with pytest.raises(ArchicadError, match="does not list it"):
        ensure_layer(connection, "Sun Study.Results")


# -- the combination every sheet is drawn through -------------------------
# Untested until now, which is how the second sheet came to be built from the
# first sheet's layout. It decides what a drawing shows, and it is the thing
# that has to be right when somebody says "these layers should not be there".


def _combination_world(**overrides: Any) -> dict[str, Any]:
    responses: dict[str, Any] = {
        "GetAttributesByType": {
            "attributes": [
                {"attributeId": {"guid": "a"}, "index": 1, "name": "Walls"},
                {"attributeId": {"guid": "b"}, "index": 2, "name": "05 | Grids.Main Floor Plan"},
                {"attributeId": {"guid": "c"}, "index": 3, "name": "Sun Study 09:00"},
            ]
        },
        "GetLayers": {
            "layers": [
                {
                    "layerAttribute": {
                        "name": "Walls",
                        "isHidden": False,
                        "isLocked": True,
                        "isWireframe": True,
                        "intersectionGroupNr": 7,
                    }
                },
                {
                    "layerAttribute": {
                        "name": "05 | Grids.Main Floor Plan",
                        "isHidden": False,
                        "isLocked": False,
                    }
                },
                {
                    "layerAttribute": {
                        "name": "Sun Study 09:00",
                        "isHidden": True,
                        "isLocked": False,
                    }
                },
            ]
        },
        "CreateLayerCombinations": {"attributeIds": [{"attributeId": {"guid": "C1"}}]},
    }
    responses.update(overrides)
    return responses


def _combination_written(transport: FakeTransport) -> dict[str, dict[str, Any]]:
    sent = transport.parameters_for("CreateLayerCombinations")
    (combination,) = sent["layerCombinationDataArray"]
    by_guid = {row["attributeId"]["guid"]: row for row in combination["layers"]}
    return by_guid


def test_the_study_layer_is_shown_and_the_named_clutter_is_hidden() -> None:
    """What the run asks for: its own layer on, the office's grids and notes
    off, and everything else exactly as the plan already had it."""
    from sun_study.archicad.views import ensure_layer_combination

    connection, transport = connect(_combination_world())
    connection.note_model_database()

    name = ensure_layer_combination(
        connection,
        f"{SS} Sun Study 09:00",
        show=["Sun Study 09:00"],
        hide=["05 | Grids.Main Floor Plan"],
    )

    assert name == f"{SS} Sun Study 09:00"
    written_layers = _combination_written(transport)
    assert written_layers["c"]["isHidden"] is False, "the study's own layer is the point"
    assert written_layers["b"]["isHidden"] is True, "named clutter is forced off"
    assert written_layers["a"]["isHidden"] is False, "everything else is left as it stands"


def test_a_layer_that_is_shown_is_never_left_locked() -> None:
    """Somebody will want to move a label, and a locked layer refuses."""
    from sun_study.archicad.views import ensure_layer_combination

    connection, transport = connect(_combination_world())
    connection.note_model_database()
    ensure_layer_combination(connection, f"{SS} X", show=["Walls"], hide=[])

    assert _combination_written(transport)["a"]["isLocked"] is False


def test_the_parts_of_a_layer_the_study_has_no_opinion_on_are_carried_through() -> None:
    """A combination names every field of every layer. Dropping the two the
    tool does not care about would write them back as defaults -- turning
    wireframe off and moving the layer to intersection group 1 -- on every
    layer of somebody's project, every run."""
    from sun_study.archicad.views import ensure_layer_combination

    connection, transport = connect(_combination_world())
    connection.note_model_database()
    ensure_layer_combination(connection, f"{SS} X", show=[], hide=[])

    walls = _combination_written(transport)["a"]
    assert walls["isWireframe"] is True
    assert walls["intersectionGroupNr"] == 7


def test_a_combination_is_built_from_the_model_even_from_inside_a_layout() -> None:
    """The bug this pass found. layout_from_views leaves a layout current, so
    from the second sheet onward "the layers as they stand" was the previous
    sheet's layout answering with its own combination -- and each sheet
    inherited what the one before it happened to show."""
    from sun_study.archicad.views import ensure_layer_combination

    connection, transport = connect(
        _combination_world(
            GetCurrentWindowType={"currentWindowType": "Layout"},
            GetElementsByType={"elements": []},
            GetNavigatorItemTree={
                "navigatorItemTree": {
                    "navigatorItemId": {"guid": "R"},
                    "name": "Project Map",
                    "children": [
                        {
                            "navigatorItem": {
                                "navigatorItemId": {"guid": "N0"},
                                "name": "Ground",
                                "type": "StoryItem",
                                "prefix": "0",
                            }
                        }
                    ],
                }
            },
            GetDatabaseIdFromNavigatorItemId={"databases": [{"databaseId": {"guid": "S0"}}]},
            ChangeWindow={"success": True},
        )
    )
    _go_to(connection, "L1", "Layout")

    ensure_layer_combination(connection, f"{SS} X", show=[], hide=[])

    moves = transport.all_parameters_for("ChangeWindow")
    assert moves[-1] == {"databaseId": {"guid": "S0"}, "windowType": "FloorPlan"}, (
        "a floor plan is put back before the layers are read"
    )
    assert connection.database is not None and connection.database.is_model


def test_a_refused_switch_stops_the_read_instead_of_answering_about_another_sheet() -> None:
    """Three call sites issued ChangeWindow and ignored the answer. A refused
    move leaves the previous database current, so the read that follows
    succeeds -- against the wrong layout -- and the drawings measured belong
    to a sheet nobody asked about."""
    from sun_study.archicad.sheets import measure_drawings

    connection, transport = connect(
        {
            "ChangeWindow": {"success": False},
            "GetElementsByType": {"elements": [{"elementId": {"guid": "D1"}}]},
        }
    )
    with pytest.raises(ArchicadError) as raised:
        measure_drawings(connection, "lay-1")

    assert "Could not make Layout database" in str(raised.value)
    assert "GetElementsByType" not in transport.commands(), "no read against the wrong sheet"


def test_a_connection_that_has_not_moved_makes_no_claim_about_where_it_is() -> None:
    """None, not a guess. A fresh connection has not navigated anything, and
    refusing on that basis would break every caller on an untouched project."""
    connection, _ = connect({})
    assert connection.database is None


def test_the_database_is_remembered_from_the_command_that_moved_it() -> None:
    """There is no GetCurrentDatabase to ask -- unregistered on Tapir 1.5.7 --
    and GetCurrentWindowType answers for the window, which moves separately."""
    connection, _ = connect({"ChangeWindow": {"success": True}})

    _go_to(connection, "L1", "Layout")
    assert connection.database == Database(guid="L1", window_type="Layout")
    assert not connection.database.is_model

    _go_to(connection, "S0", "FloorPlan")
    assert connection.database.is_model, "a floor plan is where the building is"


def test_a_refused_move_leaves_the_note_where_it_was() -> None:
    """ChangeWindow reports success as data. A worksheet made this session
    cannot be activated, and the database stays where it was when it says so."""
    connection, _ = connect({"ChangeWindow": Sequential({"success": True}, {"success": False})})

    _go_to(connection, "S0", "FloorPlan")
    _go_to(connection, "W1", "Worksheet")
    assert connection.database == Database(guid="S0", window_type="FloorPlan")


def test_reading_layers_on_a_layout_is_refused_rather_than_answered_wrongly() -> None:
    """The answer exists -- it is just the layout's, not the project's, and
    reporting it as the project's is what cost an afternoon: a check run after
    a tour of six layouts said a layer had not been put back when it had."""
    from sun_study.archicad.layers import read_layers

    connection, transport = connect({"ChangeWindow": {"success": True}, **_layer_world()})
    _go_to(connection, "L1", "Layout")

    with pytest.raises(ArchicadError) as raised:
        read_layers(connection)

    assert "Layout is the current database" in str(raised.value)
    assert "GetAttributesByType" not in transport.commands(), "refused before asking"


def test_writing_layers_on_a_layout_is_refused_because_it_would_be_discarded() -> None:
    """The write does take effect in the layout. It is gone on the next
    switch, when the combination is reapplied, and it never reaches the
    model -- so a restore made there restores nothing."""
    from sun_study.archicad.layers import LayerState, _write

    connection, transport = connect({"ChangeWindow": {"success": True}, "CreateLayers": {}})
    _go_to(connection, "L1", "Layout")

    with pytest.raises(ArchicadError) as raised:
        _write(connection, [LayerState("G1", "Context", hidden=True)])

    assert "discarded" in str(raised.value)
    assert "CreateLayers" not in transport.commands()


def test_the_export_state_refuses_on_a_layout_rather_than_snapshotting_one() -> None:
    """The one that matters. export_state promises the export does not depend
    on what was on screen; entered on a layout it would snapshot the layout's
    combination as the project's state and write that back afterwards."""
    from sun_study.archicad.layers import export_state

    connection, _ = connect({"ChangeWindow": {"success": True}, **_layer_world()})
    _go_to(connection, "L1", "Layout")

    with pytest.raises(ArchicadError), export_state(connection):
        pass


def test_going_back_to_a_floor_plan_makes_layer_work_possible_again() -> None:
    """The guard is about where the tool is, not a mode it latches into."""
    from sun_study.archicad.layers import read_layers

    connection, _ = connect({"ChangeWindow": {"success": True}, **_layer_world()})
    _go_to(connection, "L1", "Layout")
    _go_to(connection, "S0", "FloorPlan")

    assert [state.name for state in read_layers(connection)] == ["Walls", "Context", "Joinery"]


def test_the_walls_that_prove_a_floor_plan_are_recorded_as_proof() -> None:
    """ensure_model_database usually finds nothing to move. Returning early
    without saying so left the connection claiming not to know where it was,
    which switches the guard off for the whole run."""
    from sun_study.archicad.series import ensure_model_database

    connection, _ = connect(
        {
            "GetCurrentWindowType": {"currentWindowType": "FloorPlan"},
            "GetElementsByType": {"elements": [{"elementId": {"guid": "W1"}}]},
        }
    )
    assert ensure_model_database(connection) is None
    assert connection.database is not None and connection.database.is_model


def test_a_layout_window_over_a_readable_model_is_realigned_but_not_reported() -> None:
    """The window and the database move separately. With a layout window in
    front of a floor-plan database the walls read fine, so the switch is worth
    making to bring the two back together -- but telling somebody the project
    had no Zones and no walls would be a lie, and it is the sentence that
    sends them off to check a layer name."""
    from sun_study.archicad.series import ensure_model_database

    connection, transport = connect(
        {
            "GetCurrentWindowType": {"currentWindowType": "Layout"},
            "GetElementsByType": {"elements": [{"elementId": {"guid": "W1"}}]},
            "GetNavigatorItemTree": {
                "navigatorItemTree": {
                    "navigatorItemId": {"guid": "R"},
                    "name": "Project Map",
                    "children": [
                        {
                            "navigatorItem": {
                                "navigatorItemId": {"guid": "N0"},
                                "name": "Ground",
                                "type": "StoryItem",
                                "prefix": "0",
                            }
                        }
                    ],
                }
            },
            "GetDatabaseIdFromNavigatorItemId": {"databases": [{"databaseId": {"guid": "S0"}}]},
            "ChangeWindow": {"success": True},
        }
    )
    assert ensure_model_database(connection) is None, "nothing to warn about"
    assert "ChangeWindow" in transport.commands(), "still realigned"


def test_the_note_spares_the_two_reads_when_the_tool_moved_there_itself() -> None:
    """Cheap enough to use as a precondition, which is what lets a layer
    combination insist on the model before reading it."""
    from sun_study.archicad.series import ensure_model_database

    connection, transport = connect({})
    connection.note_model_database()

    assert ensure_model_database(connection) is None
    assert transport.commands() == [], "no round trip when the answer is known"


def test_a_layout_left_current_is_moved_off_and_the_move_is_recorded() -> None:
    """The other half: when it does move, the note comes from ChangeWindow."""
    from sun_study.archicad.series import ensure_model_database

    connection, _ = connect(
        {
            "GetCurrentWindowType": {"currentWindowType": "Layout"},
            "GetElementsByType": {"elements": []},
            "GetNavigatorItemTree": {
                "navigatorItemTree": {
                    "navigatorItemId": {"guid": "R"},
                    "name": "Project Map",
                    "children": [
                        {
                            "navigatorItem": {
                                "navigatorItemId": {"guid": "N0"},
                                "name": "Ground",
                                "type": "StoryItem",
                                "prefix": "0",
                            }
                        }
                    ],
                }
            },
            "GetDatabaseIdFromNavigatorItemId": {"databases": [{"databaseId": {"guid": "S0"}}]},
            "ChangeWindow": {"success": True},
        }
    )
    assert ensure_model_database(connection) == "Layout"
    assert connection.database == Database(guid="S0", window_type="FloorPlan")


def test_a_layer_both_hidden_and_locked_is_counted_once() -> None:
    """Summing the three lists reported "set 153 of 142 layers"."""
    from sun_study.archicad.layers import export_state

    connection, _ = connect(_layer_world())
    with export_state(connection) as plan:
        assert plan.changed == 1
        assert plan.shown == ("Context",) and plan.unlocked == ("Context",)
        assert "set 1 of 3 layers" in plan.describe()


def test_the_projects_own_combination_is_copied_rather_than_activated() -> None:
    """Nothing in the add-on can activate a layer combination -- D52 measured
    five commands, all unregistered. But a combination is only a set of
    visibilities, and GetLayerCombinations reads them out, so an office's IFC
    export combination can be applied by writing it onto the layers.
    """
    from sun_study.archicad.layers import export_state

    connection, transport = connect(
        _layer_world(
            GetAttributesByType=Sequential(
                _layer_attributes("Walls", "Context", "Joinery"),
                {"attributes": [{"attributeId": {"guid": "C9"}, "index": 4, "name": "IFC EXPORT"}]},
            ),
            GetLayerCombinations={
                "layerCombinations": [
                    {
                        "layerCombination": {
                            "name": "IFC EXPORT",
                            "layers": [
                                {"attributeId": {"guid": "L0"}, "isHidden": False},
                                {"attributeId": {"guid": "L1"}, "isHidden": False},
                                {"attributeId": {"guid": "L2"}, "isHidden": True},
                            ],
                        }
                    }
                ]
            },
        )
    )
    with export_state(connection, combination="ifc export") as plan:
        pass

    assert plan.combination.startswith("ifc export")
    assert "CreateLayerCombinations" not in transport.commands(), (
        "the project's own combination is used, not a second copy of it"
    )
    applied, _ = transport.all_parameters_for("CreateLayers")
    assert written("Joinery", hidden=True, locked=False) in applied["layerDataArray"]
    assert written("Context", hidden=False, locked=False) in applied["layerDataArray"]


def test_a_combination_the_project_does_not_have_stops_the_run() -> None:
    """Falling back to whatever is on screen would be the exact failure this
    exists to remove, arriving silently."""
    from sun_study.archicad.layers import export_state

    connection, _ = connect(_layer_world())
    with pytest.raises(ArchicadError, match="no layer combination called"):
        with export_state(connection, combination="12 | IFC ARCH. EXPORT"):
            pass


def test_the_study_layers_are_forced_on_over_the_base_combination() -> None:
    """Neither of the reference project's own IFC export combinations shows
    its '06 | Zone.*' layers.

    Following the office's settings exactly therefore reproduces D52: an
    export with no IfcSpace in it, and a run that reports no apartments. The
    base says what belongs in an export; the run says what this study needs.
    """
    from sun_study.archicad.layers import export_state

    connection, transport = connect(
        _layer_world(
            GetAttributesByType=Sequential(
                _layer_attributes("Walls", "Context", "Joinery"),
                {"attributes": [{"attributeId": {"guid": "C9"}, "index": 4, "name": "IFC EXPORT"}]},
            ),
            GetLayerCombinations={
                "layerCombinations": [
                    {
                        "layerCombination": {
                            "name": "IFC EXPORT",
                            "layers": [
                                {"attributeId": {"guid": "L0"}, "isHidden": False},
                                {"attributeId": {"guid": "L1"}, "isHidden": True},
                                {"attributeId": {"guid": "L2"}, "isHidden": True},
                            ],
                        }
                    }
                ]
            },
        )
    )
    with export_state(connection, combination="IFC EXPORT", require=["context"]) as plan:
        pass

    applied, _ = transport.all_parameters_for("CreateLayers")
    assert written("Context", hidden=False, locked=False) in applied["layerDataArray"], (
        "the base hides it and the study needs it, so the study wins"
    )
    assert plan.shown == ("Context",)


def test_legend_labels_are_moved_onto_the_studys_own_layer() -> None:
    """CreateTexts takes no layer -- coordinate, string, height, pen,
    justification, angle, and nothing else.

    So a label lands on whatever the Text tool defaults to, which on the
    reference project is '05 | Dims/Notes.DA', one of the office's annotation
    layers for the drawing set. Two things go wrong: the study's key is mixed
    into somebody else's layer, and it is outside what the next run clears, so
    switching the study off leaves the legend on the plan.

    That layer is also hidden, which is why the move has to borrow it: the
    first attempt reported "8 of 8 legend labels would not move", because
    SetDetailsOfElements answers success and does nothing to an element on a
    hidden layer.
    """
    from sun_study.archicad.draw import move_to_layer

    made = [{"elementId": {"guid": "t1"}}, {"elementId": {"guid": "t2"}}]
    connection, transport = connect(
        {
            "GetDetailsOfElements": Sequential(
                # Where they landed: the Text tool's default, not ours.
                {"detailsOfElements": [{"layerIndex": 42}, {"layerIndex": 42}]},
                # And where they are after the move.
                {"detailsOfElements": [{"layerIndex": 7}, {"layerIndex": 7}]},
            ),
            "GetAttributesByType": {
                "attributes": [
                    {"attributeId": {"guid": "a"}, "index": 42, "name": "05 | Dims/Notes.DA"},
                    {"attributeId": {"guid": "b"}, "index": 7, "name": default_layer_name()},
                ]
            },
            "GetLayers": {
                "layers": [
                    {"name": "05 | Dims/Notes.DA", "isHidden": True, "isLocked": False},
                    {"name": default_layer_name(), "isHidden": False, "isLocked": False},
                ]
            },
            "CreateLayers": {"attributeIds": [{"attributeId": {"guid": "a"}}]},
            "SetDetailsOfElements": {"executionResults": [{"success": True}] * 2},
        }
    )
    assert move_to_layer(connection, made, 7) == 2

    moved = transport.parameters_for("SetDetailsOfElements")["elementsWithDetails"]
    assert all(entry["details"]["layerIndex"] == 7 for entry in moved)
    borrowed, restored = transport.all_parameters_for("CreateLayers")
    assert borrowed["layerDataArray"] == [
        written("05 | Dims/Notes.DA", hidden=False, locked=False)
    ], "the layer they landed on is switched on so the move can take"
    assert restored["layerDataArray"] == [
        written("05 | Dims/Notes.DA", hidden=True, locked=False)
    ], "and switched straight back: it is the office's layer, not ours"


def test_text_already_on_the_studys_layer_is_left_alone() -> None:
    """Nothing to move, and no reason to unhide anything to move it."""
    from sun_study.archicad.draw import move_to_layer

    connection, transport = connect(
        {"GetDetailsOfElements": {"detailsOfElements": [{"layerIndex": 7}]}}
    )
    assert move_to_layer(connection, [{"elementId": {"guid": "t"}}], 7) == 1
    assert "SetDetailsOfElements" not in transport.commands()
    assert "CreateLayers" not in transport.commands()


def test_a_legend_label_carries_its_own_height() -> None:
    """Inherited, the Text tool's default drew a label 1.27 m tall in model
    space -- taller than the 1.5 m the rows were spaced at, so every label sat
    on the one below it and the key read as a smear."""
    connection, transport = connect(_draw_responses())
    draw_assessment(
        connection,
        _assessment(_apartment("apt-1")),
        [_zone("z1")],
        zone_by_apartment={"apt-1": "z1"},
    )

    texts = transport.parameters_for("CreateTexts")["textsData"]
    assert texts and all("height" in text for text in texts)


def test_legend_rows_clear_each_other() -> None:
    from sun_study.archicad.draw import DEFAULT_BANDS, TEXT_MODEL_HEIGHT_M, _legend

    _, texts = _legend(DEFAULT_BANDS, (0.0, 0.0), 7)
    ys = sorted(text["coordinate"]["y"] for text in texts)
    gaps = [b - a for a, b in itertools.pairwise(ys)]
    assert gaps, "more than one band, so there are gaps to check"
    assert min(gaps) > TEXT_MODEL_HEIGHT_M, (
        "consecutive labels must not overlap at the height they are drawn at"
    )
