"""The ruleset, the assessment engine, and the exported results.

Two things get the most attention. First, that the engine is genuinely generic
-- a threshold change must be a YAML edit and never a code change, or the next
council's DCP means a rewrite. Second, that no exported number can be separated
from the ruleset version, the continuity setting and the interpretation choices
that produced it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from sun_study.disclaimer import DISCLAIMER, STATUS
from sun_study.report.csv_out import COLUMNS, render_csv
from sun_study.report.header import build_header
from sun_study.report.json_out import render_json, results_document
from sun_study.rules.assessment import ApartmentMeasurement, assess_building
from sun_study.rules.ruleset import (
    BUILTIN_RULESETS,
    Continuity,
    MissingOpenSpacePolicy,
    Requires,
    Ruleset,
    RulesetError,
    load_ruleset,
)

RULESET_DIR = Path(__file__).resolve().parents[2] / "src" / "sun_study" / "rules" / "rulesets"


@pytest.fixture(scope="module")
def adg() -> Ruleset:
    return load_ruleset("nsw_adg")


def measurement(
    name: str, living: float, open_space: float | None = None, continuous: float | None = None
) -> ApartmentMeasurement:
    return ApartmentMeasurement(
        apartment_id=f"id-{name}",
        apartment_name=name,
        living_room_minutes=living,
        living_room_continuous_minutes=living if continuous is None else continuous,
        open_space_minutes=open_space,
        open_space_continuous_minutes=open_space,
    )


# ---------------------------------------------------------------------------
# The ruleset is data, and its thresholds are the published ones.
# ---------------------------------------------------------------------------
def test_builtin_rulesets_all_load() -> None:
    for name in BUILTIN_RULESETS:
        assert load_ruleset(name).name == name


def test_adg_thresholds_match_the_published_criteria(adg: Ruleset) -> None:
    """Objective 4A-1 design criteria 1, 2 and 3.

    Quoted from the NSW Department of Planning technical note "Solar access
    requirements in SEPP 65", read from the published PDF.
    """
    assert adg.area("sydney_metro").minimum_sunlight_minutes == 120.0  # 2 hours
    assert adg.area("other").minimum_sunlight_minutes == 180.0  # 3 hours
    assert adg.criterion("minimum_compliant_share").value == 0.70
    assert adg.criterion("maximum_no_sunlight_share").value == 0.15

    assert adg.assessment.date == "06-21"  # mid winter
    assert adg.assessment.window_start == "09:00"
    assert adg.assessment.window_end == "15:00"
    assert adg.assessment.window_minutes == 360.0


def test_the_three_hour_criterion_outside_sydney_is_not_forgotten(adg: Ruleset) -> None:
    """Criterion 2 applies everywhere except Sydney Metro, Newcastle and
    Wollongong, and a tool that only knows the 2 hour figure passes buildings
    that should fail."""
    assert (
        adg.area("other").minimum_sunlight_minutes
        > adg.area("sydney_metro").minimum_sunlight_minutes
    )
    assert "Newcastle and Wollongong" in adg.area("sydney_metro").label


def test_every_threshold_carries_a_citation(adg: Ruleset) -> None:
    """A number in a compliance tool that cannot be traced to a published
    document is worse than no number."""
    for key, area in adg.areas.items():
        assert len(area.citation) > 40, f"area {key} has no real citation"
        assert "4A-1" in area.citation
    for key, criterion in adg.criteria.items():
        assert len(criterion.citation) > 40, f"criterion {key} has no real citation"
        assert "4A-1" in criterion.citation
    assert adg.assessment.citation


def test_the_ruleset_records_where_it_came_from(adg: Ruleset) -> None:
    assert adg.source.publisher.startswith("NSW Department of Planning")
    assert adg.source.url is not None
    assert adg.source.url.startswith("https://www.planning.nsw.gov.au/")
    assert adg.identifier == "nsw_adg@1.0.0"


def test_defaults_are_the_documented_ones(adg: Ruleset) -> None:
    """Continuity cumulative and trapezoidal weighting, per D11 and D12."""
    assert adg.assessment.continuity is Continuity.CUMULATIVE
    assert adg.interpretation.compliance_requires is Requires.BOTH
    assert (
        adg.interpretation.apartments_without_open_space is MissingOpenSpacePolicy.LIVING_ROOM_ONLY
    )


def test_vegetation_exclusion_has_a_published_basis(adg: Ruleset) -> None:
    """D10 is not merely convention: "not including trees" is in the
    definition of solar access itself."""
    assert "not including" in adg.notes["solar_access_definition"]
    assert "trees" in adg.notes["solar_access_definition"]


# ---------------------------------------------------------------------------
# Loading and validation.
# ---------------------------------------------------------------------------
def test_unknown_ruleset_raises() -> None:
    with pytest.raises(RulesetError, match="No ruleset"):
        load_ruleset("nsw_adg_2099")


def test_unknown_area_raises(adg: Ruleset) -> None:
    with pytest.raises(RulesetError, match="Unknown area"):
        adg.area("mars")


def test_malformed_yaml_raises(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("name: [unclosed\n", encoding="utf-8")
    with pytest.raises(RulesetError, match="not valid YAML"):
        load_ruleset(path)


def test_a_threshold_without_a_citation_is_rejected(tmp_path: Path) -> None:
    """Enforced by the schema, not by reviewer diligence."""
    raw = yaml.safe_load((RULESET_DIR / "nsw_adg.yaml").read_text(encoding="utf-8"))
    raw["criteria"]["minimum_compliant_share"]["citation"] = ""

    path = tmp_path / "uncited.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(RulesetError, match="not a valid ruleset"):
        load_ruleset(path)


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    """A typo in a ruleset must fail rather than be silently ignored, or a
    threshold can be 'set' in a file and have no effect."""
    raw = yaml.safe_load((RULESET_DIR / "nsw_adg.yaml").read_text(encoding="utf-8"))
    raw["assessment"]["continuty"] = "continuous"  # misspelled

    path = tmp_path / "typo.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(RulesetError, match="not a valid ruleset"):
        load_ruleset(path)


def test_a_ruleset_missing_required_criteria_is_rejected(tmp_path: Path) -> None:
    raw = yaml.safe_load((RULESET_DIR / "nsw_adg.yaml").read_text(encoding="utf-8"))
    del raw["criteria"]["maximum_no_sunlight_share"]

    path = tmp_path / "partial.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(RulesetError, match="missing required criteria"):
        load_ruleset(path)


def test_changing_a_threshold_is_a_data_edit_not_a_code_change(tmp_path: Path) -> None:
    """The whole point of the YAML. A council requiring 3 continuous hours must
    be a new file, not a new branch in the engine."""
    raw = yaml.safe_load((RULESET_DIR / "nsw_adg.yaml").read_text(encoding="utf-8"))
    raw["name"] = "invented_council_dcp"
    raw["areas"]["sydney_metro"]["minimum_sunlight_minutes"] = 180
    raw["assessment"]["continuity"] = "continuous"

    path = tmp_path / "invented.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    rules = load_ruleset(path)

    # 200 cumulative minutes but only 60 unbroken: passes the ADG, fails this.
    apartments = [measurement("A", 200.0, 200.0, continuous=60.0)]
    assert not assess_building(apartments, rules, "sydney_metro").apartments[0].meets_minimum


# ---------------------------------------------------------------------------
# Assessment.
# ---------------------------------------------------------------------------
def test_an_apartment_is_governed_by_its_worse_half(adg: Ruleset) -> None:
    """ "Living rooms *and* private open spaces ... receive a minimum of 2 hours"."""
    result = assess_building([measurement("A", 300.0, 60.0)], adg, "sydney_metro")
    apartment = result.apartments[0]
    assert apartment.governing_minutes == 60.0
    assert not apartment.meets_minimum


def test_both_halves_above_the_minimum_passes(adg: Ruleset) -> None:
    apartment = assess_building([measurement("A", 130.0, 121.0)], adg, "sydney_metro").apartments[0]
    assert apartment.governing_minutes == 121.0
    assert apartment.meets_minimum


def test_seventy_percent_is_the_threshold_and_it_is_inclusive(adg: Ruleset) -> None:
    """Exactly 70% complies: the criterion reads "at least 70%"."""
    apartments = [measurement(f"pass-{i}", 200.0, 200.0) for i in range(7)]
    apartments += [measurement(f"fail-{i}", 10.0, 10.0) for i in range(3)]

    result = assess_building(apartments, adg, "sydney_metro")
    assert result.compliant_share == pytest.approx(0.70)
    assert result.meets_minimum_share
    assert result.complies


def test_just_under_seventy_percent_fails(adg: Ruleset) -> None:
    apartments = [measurement(f"pass-{i}", 200.0, 200.0) for i in range(6)]
    apartments += [measurement(f"fail-{i}", 10.0, 10.0) for i in range(4)]

    result = assess_building(apartments, adg, "sydney_metro")
    assert result.compliant_share == pytest.approx(0.60)
    assert not result.complies


def test_the_fifteen_percent_dark_cap_can_fail_a_compliant_building(adg: Ruleset) -> None:
    """Criterion 3 is a separate test, and a building can pass criterion 1 and
    still fail on it. A tool reporting only the headline percentage would call
    this building compliant."""
    apartments = [measurement(f"pass-{i}", 200.0, 200.0) for i in range(8)]
    apartments += [measurement(f"dark-{i}", 0.0, 0.0) for i in range(2)]

    result = assess_building(apartments, adg, "sydney_metro")
    assert result.meets_minimum_share, "80% meet the minimum, so criterion 1 passes"
    assert result.no_sunlight_share == pytest.approx(0.20)
    assert not result.within_no_sunlight_cap
    assert not result.complies


def test_an_apartment_with_a_sunlit_balcony_is_not_counted_as_dark(adg: Ruleset) -> None:
    """ "Receives no direct sunlight" means the apartment gets none at all."""
    result = assess_building([measurement("A", 0.0, 300.0)], adg, "sydney_metro")
    assert not result.apartments[0].receives_no_sunlight
    assert not result.apartments[0].meets_minimum


def test_apartments_without_open_space_are_assessed_on_the_living_room(adg: Ruleset) -> None:
    """A studio with no balcony is a different case from one whose balcony
    never sees the sun, and collapsing them fails it for the wrong reason."""
    result = assess_building([measurement("studio", 200.0, None)], adg, "sydney_metro")
    apartment = result.apartments[0]
    assert apartment.meets_minimum
    assert apartment.open_space_minutes is None
    assert "no private open space" in apartment.note
    assert result.counted_total == 1


@pytest.mark.parametrize(
    ("policy", "expect_counted", "expect_meets"),
    [
        ("living_room_only", True, True),
        ("non_compliant", True, False),
        ("excluded", False, False),
    ],
)
def test_the_missing_open_space_policy_is_honoured(
    tmp_path: Path, policy: str, expect_counted: bool, expect_meets: bool
) -> None:
    raw = yaml.safe_load((RULESET_DIR / "nsw_adg.yaml").read_text(encoding="utf-8"))
    raw["interpretation"]["apartments_without_open_space"] = policy
    path = tmp_path / f"{policy}.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    result = assess_building(
        [measurement("studio", 300.0, None)], load_ruleset(path), "sydney_metro"
    )
    assert result.apartments[0].counted is expect_counted
    assert result.apartments[0].meets_minimum is expect_meets
    assert result.counted_total == (1 if expect_counted else 0)


def test_continuous_continuity_uses_the_unbroken_duration(tmp_path: Path) -> None:
    """Councils differ, and the same building passes one reading and fails the
    other. This is why the setting is printed in every header."""
    raw = yaml.safe_load((RULESET_DIR / "nsw_adg.yaml").read_text(encoding="utf-8"))
    raw["assessment"]["continuity"] = "continuous"
    path = tmp_path / "continuous.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    apartments = [measurement("A", 200.0, 200.0, continuous=90.0)]
    cumulative = assess_building(apartments, load_ruleset("nsw_adg"), "sydney_metro")
    continuous = assess_building(apartments, load_ruleset(path), "sydney_metro")

    assert cumulative.apartments[0].meets_minimum
    assert not continuous.apartments[0].meets_minimum


def test_the_three_hour_area_fails_what_the_two_hour_area_passes(adg: Ruleset) -> None:
    apartments = [measurement("A", 150.0, 150.0)]
    assert assess_building(apartments, adg, "sydney_metro").apartments[0].meets_minimum
    assert not assess_building(apartments, adg, "other").apartments[0].meets_minimum


def test_an_empty_building_is_not_vacuously_compliant(adg: Ruleset) -> None:
    result = assess_building([], adg, "sydney_metro")
    assert result.counted_total == 0
    assert result.compliant_share == 0.0
    assert not result.complies


def test_every_result_carries_the_ruleset_name_and_version(adg: Ruleset) -> None:
    """Brief section 5.7."""
    result = assess_building([measurement("A", 200.0, 200.0)], adg, "sydney_metro")
    assert result.ruleset_name == "nsw_adg"
    assert result.ruleset_version == "1.0.0"
    assert result.ruleset_identifier == "nsw_adg@1.0.0"


# ---------------------------------------------------------------------------
# Reporting.
# ---------------------------------------------------------------------------
@pytest.fixture()
def reported(adg: Ruleset) -> tuple[Any, dict[str, Any]]:
    assessment = assess_building(
        [
            measurement("Apartment 1", 300.0, 250.0),
            measurement("Apartment 2", 60.0, 0.0),
            measurement("Studio 3", 200.0, None),
        ],
        adg,
        "sydney_metro",
    )
    header = build_header(
        assessment=assessment,
        ruleset=adg,
        site_description="lat 33.750000S lon 151.250000E tz Australia/Sydney",
        scene_provenance={"source": "sample_building.ifc", "windows_assessed": 3},
        scene_config_description="grid 200 mm | vegetation excluded",
        generated_at="2024-01-01T00:00:00+00:00",
    )
    return assessment, header


def test_csv_carries_the_disclaimer(reported: tuple[Any, dict[str, Any]]) -> None:
    """Brief section 9 names the CSV header specifically."""
    text = render_csv(*reported)
    assert f"# status: {STATUS}" in text
    assert DISCLAIMER.split(".")[0] in text


def test_csv_carries_the_ruleset_version_and_citations(
    reported: tuple[Any, dict[str, Any]],
) -> None:
    text = render_csv(*reported)
    assert "# ruleset.identifier: nsw_adg@1.0.0" in text
    assert "4A-1" in text
    assert "# ruleset.continuity: cumulative" in text
    assert "# ruleset.weighting: trapezoidal" in text
    assert "# interpretation.compliance_requires: both" in text


def test_csv_table_has_a_row_per_apartment(reported: tuple[Any, dict[str, Any]]) -> None:
    rows = [line for line in render_csv(*reported).splitlines() if not line.startswith("#")]
    assert rows[0] == ",".join(COLUMNS)
    assert len(rows) == 4  # header plus three apartments
    assert "Apartment 1" in rows[1]


def test_csv_leaves_missing_open_space_blank_not_zero(
    reported: tuple[Any, dict[str, Any]],
) -> None:
    """A blank means "no balcony"; a zero would mean "a balcony in permanent
    shade", and the two are different findings."""
    rows = [line for line in render_csv(*reported).splitlines() if not line.startswith("#")]
    studio = next(row for row in rows if "Studio 3" in row)
    fields = studio.split(",")
    assert fields[COLUMNS.index("open_space_minutes")] == ""

    dark = next(row for row in rows if "Apartment 2" in row)
    assert dark.split(",")[COLUMNS.index("open_space_minutes")] == "0.0"


def test_csv_uses_crlf_for_windows(reported: tuple[Any, dict[str, Any]]) -> None:
    assert "\r\n" in render_csv(*reported)


def test_json_round_trips_and_carries_the_header(
    reported: tuple[Any, dict[str, Any]],
) -> None:
    document = json.loads(render_json(*reported))
    assert document["header"]["status"] == STATUS
    assert document["header"]["ruleset"]["identifier"] == "nsw_adg@1.0.0"
    assert document["header"]["provenance"]["source"] == "sample_building.ifc"
    assert len(document["apartments"]) == 3


def test_json_summary_reports_both_criteria(reported: tuple[Any, dict[str, Any]]) -> None:
    assessment, header = reported
    summary = results_document(assessment, header)["summary"]
    assert summary["counted_apartments"] == 3
    assert "meets_minimum_share" in summary
    assert "within_no_sunlight_cap" in summary
    assert summary["complies"] == assessment.complies


def test_json_preserves_a_missing_open_space_as_null(
    reported: tuple[Any, dict[str, Any]],
) -> None:
    document = json.loads(render_json(*reported))
    studio = next(a for a in document["apartments"] if a["apartment_name"] == "Studio 3")
    assert studio["open_space_minutes"] is None


def test_header_is_deterministic(reported: tuple[Any, dict[str, Any]]) -> None:
    """generated_at is injected rather than read from the clock, so golden
    comparisons do not drift."""
    assessment, header = reported
    assert header["generated_at"] == "2024-01-01T00:00:00+00:00"
    assert render_json(assessment, header) == render_json(assessment, header)
