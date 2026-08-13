"""The whole chain through the CLI, on the fixture.

Milestone M3's acceptance criterion is per-apartment results for the fixture
building, so this exercises the actual entry point a person types rather than
the library underneath it.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sun_study.cli import app
from sun_study.disclaimer import STATUS
from sun_study.pipeline import PipelineResult, run_assessment

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SAMPLE = FIXTURES / "sample_building.ifc"

runner = CliRunner()


@pytest.fixture(scope="module")
def result() -> PipelineResult:
    return run_assessment(SAMPLE, timezone="Australia/Sydney")


# ---------------------------------------------------------------------------
# The pipeline.
# ---------------------------------------------------------------------------
def test_the_fixture_is_assessed_end_to_end(result: PipelineResult) -> None:
    assessment = result.assessment
    assert assessment.counted_total == 4
    assert assessment.meeting_minimum == 3
    assert assessment.compliant_share == pytest.approx(0.75)
    assert assessment.meets_minimum_share  # 75% >= 70%
    assert assessment.within_no_sunlight_cap  # 0% <= 15%
    assert assessment.complies


def test_the_failing_apartment_is_the_most_shaded_one(result: PipelineResult) -> None:
    """L00-A is on the shaded side of the lower storey, under a balcony."""
    by_name = {a.apartment_name: a for a in result.assessment.apartments}
    assert not by_name["Apartment L00-A"].meets_minimum
    assert all(
        by_name[n].meets_minimum for n in ("Apartment L00-B", "Apartment L01-A", "Apartment L01-B")
    )


def test_the_governing_figure_is_the_worse_of_the_two(result: PipelineResult) -> None:
    """L00-A's balcony clears 2 hours but its living room does not, and the
    apartment is governed by the living room."""
    apartment = next(
        a for a in result.assessment.apartments if a.apartment_name == "Apartment L00-A"
    )
    assert apartment.open_space_minutes is not None
    assert apartment.open_space_minutes > 120.0
    assert apartment.living_room_minutes < 120.0
    assert apartment.governing_minutes == apartment.living_room_minutes


def test_thirty_seven_sun_positions_are_used(result: PipelineResult) -> None:
    assert result.sun_position_count == 37
    assert result.assessment_date.isoformat() == "2024-06-21"


def test_the_area_selects_the_published_threshold(result: PipelineResult) -> None:
    """Sydney Metro is the 2 hour criterion, everywhere else is 3.

    This fixture does *not* discriminate between them: its governing figures
    are 106, 207, 256 and 360 minutes, so none falls in the 120 to 180 band
    where the two criteria disagree, and the verdict is 75% either way. That is
    a property of the fixture, not evidence that the area setting works, so it
    is stated here rather than left for someone to misread as coverage. The
    behavioural test is
    ``test_the_three_hour_area_fails_what_the_two_hour_area_passes``.
    """
    outside = run_assessment(SAMPLE, timezone="Australia/Sydney", area="other")

    assert result.assessment.minimum_minutes == 120.0
    assert outside.assessment.minimum_minutes == 180.0
    assert outside.assessment.area_key == "other"
    assert "All other areas" in outside.assessment.area_label

    governing = sorted(a.governing_minutes for a in outside.assessment.apartments)
    assert not any(120.0 <= value < 180.0 for value in governing), (
        f"the fixture now has an apartment between the two thresholds ({governing}); "
        f"this test's premise no longer holds and it should assert the difference"
    )
    # The threshold changed but the geometry did not, so the durations are the same.
    assert governing == pytest.approx(
        sorted(a.governing_minutes for a in result.assessment.apartments)
    )


def test_a_mismatched_timezone_is_rejected() -> None:
    """Two different zones in one run would be silently wrong."""
    from sun_study.ingest.scene import SceneConfig

    with pytest.raises(ValueError, match="two different zones"):
        run_assessment(
            SAMPLE,
            timezone="Australia/Sydney",
            scene_config=SceneConfig(timezone="Europe/London"),
        )


# ---------------------------------------------------------------------------
# The command line.
# ---------------------------------------------------------------------------
def test_run_prints_the_disclaimer_and_the_numbers(tmp_path: Path) -> None:
    invocation = runner.invoke(app, ["run", str(SAMPLE), "--timezone", "Australia/Sydney"])
    assert invocation.exit_code == 0, invocation.output

    assert STATUS in invocation.output
    assert "must not be used as a compliance figure" in invocation.output
    # Site, ruleset and interpretation are all echoed before any result.
    assert "Australia/Sydney" in invocation.output
    assert "true north bearing of model +Y 30.000 deg" in invocation.output
    assert "nsw_adg@1.0.0" in invocation.output
    assert "cumulative duration, trapezoidal weighting" in invocation.output
    assert "Apartment L00-A" in invocation.output
    assert "COMPLIES" in invocation.output


def test_run_writes_csv_and_json(tmp_path: Path) -> None:
    csv_path, json_path = tmp_path / "out.csv", tmp_path / "out.json"
    invocation = runner.invoke(
        app,
        [
            "run",
            str(SAMPLE),
            "--timezone",
            "Australia/Sydney",
            "--csv",
            str(csv_path),
            "--json",
            str(json_path),
        ],
    )
    assert invocation.exit_code == 0, invocation.output
    assert csv_path.is_file() and json_path.is_file()

    rows = [
        line
        for line in csv_path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    table = list(csv.DictReader(rows))
    assert len(table) == 4
    assert {row["apartment_name"] for row in table} == {
        "Apartment L00-A",
        "Apartment L00-B",
        "Apartment L01-A",
        "Apartment L01-B",
    }
    assert all(row["ruleset"] == "nsw_adg@1.0.0" for row in table)

    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["summary"]["complies"] is True
    assert document["summary"]["compliant_share"] == pytest.approx(0.75)
    assert document["header"]["status"] == STATUS
    assert "4A-1" in document["header"]["ruleset"]["minimum_sunlight_citation"]


def test_run_reports_a_missing_north_direction_clearly(tmp_path: Path) -> None:
    """The most likely real-world failure, and the message names the fix."""
    import ifcopenshell

    model = ifcopenshell.open(str(SAMPLE))
    for context in model.by_type("IfcGeometricRepresentationContext", include_subtypes=False):
        context.TrueNorth = None
    broken = tmp_path / "no_north.ifc"
    model.write(str(broken))

    invocation = runner.invoke(app, ["run", str(broken), "--timezone", "Australia/Sydney"])
    assert invocation.exit_code == 2
    assert "North Direction" in invocation.output


def test_run_rejects_an_unknown_area() -> None:
    invocation = runner.invoke(
        app, ["run", str(SAMPLE), "--timezone", "Australia/Sydney", "--area", "atlantis"]
    )
    assert invocation.exit_code == 2
    assert "Unknown area" in invocation.output


def test_info_echoes_the_model_without_assessing() -> None:
    invocation = runner.invoke(app, ["info", str(SAMPLE), "--timezone", "Australia/Sydney"])
    assert invocation.exit_code == 0, invocation.output
    assert "unit scale 0.001" in invocation.output
    assert "30.000 deg" in invocation.output
    assert "COMPLIES" not in invocation.output


def test_rulesets_command_lists_thresholds_and_source() -> None:
    invocation = runner.invoke(app, ["rulesets"])
    assert invocation.exit_code == 0, invocation.output
    assert "nsw_adg@1.0.0" in invocation.output
    assert "120 min" in invocation.output
    assert "180 min" in invocation.output
    assert "planning.nsw.gov.au" in invocation.output


def test_a_wrong_living_room_name_is_visible_not_silent() -> None:
    """Zero assessed apartments must be obvious in the output, because a
    building with no living rooms reads as trivially compliant."""
    invocation = runner.invoke(
        app,
        ["run", str(SAMPLE), "--timezone", "Australia/Sydney", "--living-room", "Kitchen"],
    )
    assert invocation.exit_code == 0, invocation.output
    assert "living rooms matched by [Kitchen]" in invocation.output
    assert "0 window samples" in invocation.output
    assert "DOES NOT COMPLY" in invocation.output, (
        "an empty assessment must not report as compliant"
    )


# ---------------------------------------------------------------------------
# The Archicad commands, with nothing listening.
#
# These cannot reach an Archicad in CI, so what they pin is the behaviour a
# person actually hits first: running the command with Archicad shut, or on
# the wrong port. That has to be a named, actionable failure rather than a
# traceback, because it is the single most common way the tool is used wrong.
# ---------------------------------------------------------------------------
CLOSED_PORT = ["--port", "1"]


@pytest.mark.parametrize("command", ["archicad-info", "init-properties"])
def test_archicad_commands_fail_helpfully_with_nothing_listening(command: str) -> None:
    invocation = runner.invoke(app, [command, *CLOSED_PORT])
    assert invocation.exit_code == 2, invocation.output
    assert "Could not reach Archicad" in invocation.output
    assert "JSON" in invocation.output, "the message must name the setting to check"


def test_archicad_run_fails_helpfully_with_nothing_listening() -> None:
    invocation = runner.invoke(
        app, ["archicad-run", "--timezone", "Australia/Sydney", *CLOSED_PORT]
    )
    assert invocation.exit_code == 2, invocation.output
    assert "Could not reach Archicad" in invocation.output


def test_archicad_run_does_not_write_back_unless_asked() -> None:
    """Write-back edits a file colleagues share, so it must stay opt-in.

    Pinned on the signature rather than on the help text: a default that flips
    to True is the kind of change that reads as harmless in a diff and is not.
    """
    import inspect

    from sun_study.cli import archicad_run

    assert inspect.signature(archicad_run).parameters["write"].default is False


def test_the_archicad_commands_are_registered() -> None:
    names = {command.name for command in app.registered_commands}
    assert {"archicad-info", "init-properties", "archicad-run"} <= names


def test_zone_names_are_listed_most_common_first() -> None:
    """What a person reads to find out what to pass to --living-room.

    The default is "Living Room" and a real office file rarely says that, so
    a wrong guess yields zero assessed apartments -- which reads as a building
    with no living rooms rather than as a mistake.
    """
    from sun_study.archicad.read import ArchicadZone
    from sun_study.cli import _report_zone_names

    zones = (
        [ArchicadZone(f"g{i}", "LIVING", f"{i}") for i in range(5)]
        + [ArchicadZone(f"b{i}", "BED 1", f"{i}") for i in range(3)]
        + [ArchicadZone("s0", "", "9")]
    )

    runner_result = CliRunner()
    with runner_result.isolation() as (out, _err, _):
        _report_zone_names(zones, limit=2)
        printed = out.getvalue().decode()

    assert "3 distinct zone names" in printed
    lines = [line.strip() for line in printed.splitlines() if line.strip()]
    assert lines[1].startswith("LIVING"), "most common name must come first"
    assert lines[1].endswith("5")
    assert lines[2].startswith("BED 1")
    assert "and 1 more" in lines[3], "a truncated list must say so"


def test_all_zone_names_are_listed_when_the_limit_is_zero() -> None:
    from sun_study.archicad.read import ArchicadZone
    from sun_study.cli import _report_zone_names

    zones = [ArchicadZone("a", "", "1"), ArchicadZone("b", "LIVING", "2")]

    with CliRunner().isolation() as (out, _err, _):
        _report_zone_names(zones, limit=0)
        printed = out.getvalue().decode()

    assert "(unnamed)" in printed, "an unnamed zone must be visible, not dropped"
    assert "more" not in printed


def test_output_survives_a_character_the_console_cannot_encode() -> None:
    """A real Archicad project killed the CLI with UnicodeEncodeError.

    Windows consoles default to a legacy code page and Python raises on any
    character it cannot represent, aborting the command and losing everything
    already printed. The offending name was a property containing a subscript
    digit -- nobody chose it, and nobody can predict the next one, because an
    Archicad file carries names from libraries, add-ons and other templates.

    Windows is the primary deployment platform, so this has to degrade.
    """
    import io
    import sys

    from sun_study.cli import main

    legacy = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    original, sys.stdout = sys.stdout, legacy
    try:
        with pytest.raises(SystemExit):
            main()  # no arguments: prints help, and reconfigures the stream
        legacy.write("subscript ₁ and a degree sign °")
    finally:
        sys.stdout = original

    assert legacy.errors == "replace", "main() must relax the stream's error handling"


def test_a_pen_override_replaces_only_the_named_band() -> None:
    from sun_study.cli import band_styles

    styles = {band.label: band.fill_pen for band in band_styles(["2-3 hrs=42"])}
    assert styles["2-3 hrs"] == 42
    assert styles["0 hrs"] != 42, "the other bands keep their defaults"


def test_a_pen_override_naming_no_band_is_an_error() -> None:
    """A silently ignored override draws the default colours and looks right.

    '2-3 hours' instead of '2-3 hrs' is the obvious typo, and nobody would
    look twice at a diagram that came out coloured.
    """
    import typer

    from sun_study.cli import band_styles

    with pytest.raises(typer.BadParameter, match="2-3 hours"):
        band_styles(["2-3 hours=42"])


def test_a_pen_override_needs_a_number() -> None:
    import typer

    from sun_study.cli import band_styles

    with pytest.raises(typer.BadParameter, match="whole-number"):
        band_styles(["2-3 hrs=orange"])


def test_drawing_is_opt_in_like_writing() -> None:
    """Both change a colleague's project file."""
    import inspect

    from sun_study.cli import archicad_run

    assert inspect.signature(archicad_run).parameters["draw"].default is False


def test_the_self_test_uses_every_band_so_one_glance_checks_the_palette() -> None:
    """A self test that only exercised one colour would prove one third of the
    pen mapping and look like it had proved all of it."""
    from sun_study.archicad.draw import DEFAULT_BANDS, band_for
    from sun_study.archicad.read import ArchicadZone
    from sun_study.cli import _synthetic_assessment

    zones = [ArchicadZone(f"z{i}", "Living", f"{i}") for i in range(len(DEFAULT_BANDS))]
    assessment, match = _synthetic_assessment(zones, DEFAULT_BANDS)

    used = {band_for(a.governing_minutes, DEFAULT_BANDS).label for a in assessment.apartments}
    assert used == {band.label for band in DEFAULT_BANDS}
    assert match.by_apartment == {f"z{i}": f"z{i}" for i in range(len(DEFAULT_BANDS))}
    assert not match.unmatched and not match.ambiguous


def test_the_self_test_values_sit_inside_their_bands_not_on_the_edge() -> None:
    """On an edge, a rounding disagreement would look like a banding bug."""
    from sun_study.archicad.draw import DEFAULT_BANDS

    zones_count = len(DEFAULT_BANDS)
    from sun_study.archicad.read import ArchicadZone
    from sun_study.cli import _synthetic_assessment

    zones = [ArchicadZone(f"z{i}", "Living", f"{i}") for i in range(zones_count)]
    assessment, _ = _synthetic_assessment(zones, DEFAULT_BANDS)

    edges = {band.upper_minutes for band in DEFAULT_BANDS}
    assert not (edges & {a.governing_minutes for a in assessment.apartments if a.governing_minutes})


def test_the_self_test_marks_everything_as_not_a_measurement() -> None:
    from sun_study.archicad.draw import DEFAULT_BANDS
    from sun_study.archicad.read import ArchicadZone
    from sun_study.cli import _synthetic_assessment

    assessment, _ = _synthetic_assessment([ArchicadZone("z", "L", "1")], DEFAULT_BANDS)
    assert assessment.ruleset_name == "SELFTEST"
    assert all(a.note == "SELFTEST" for a in assessment.apartments)


def test_a_georeference_mismatch_can_be_overridden_but_only_deliberately() -> None:
    import inspect

    from sun_study.cli import archicad_run

    parameter = inspect.signature(archicad_run).parameters["allow_georeference_mismatch"]
    assert parameter.default is False
