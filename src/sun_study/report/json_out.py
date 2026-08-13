"""Per-apartment results as JSON.

The same header as the CSV, in structured form, so a downstream script gets the
ruleset version, citations and interpretation settings without having to parse
comment lines.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sun_study.rules.assessment import BuildingAssessment

__all__ = ["render_json", "results_document", "write_json"]


def results_document(assessment: BuildingAssessment, header: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "header": dict(header),
        "summary": {
            "complies": assessment.complies,
            "counted_apartments": assessment.counted_total,
            "meeting_minimum": assessment.meeting_minimum,
            "compliant_share": round(assessment.compliant_share, 6),
            "required_share": assessment.required_share,
            "meets_minimum_share": assessment.meets_minimum_share,
            "with_no_sunlight": assessment.with_no_sunlight,
            "no_sunlight_share": round(assessment.no_sunlight_share, 6),
            "maximum_no_sunlight_share": assessment.maximum_no_sunlight_share,
            "within_no_sunlight_cap": assessment.within_no_sunlight_cap,
            "minimum_sunlight_minutes": assessment.minimum_minutes,
            "continuity": str(assessment.continuity),
        },
        "apartments": [
            {
                "apartment_id": apartment.apartment_id,
                "apartment_name": apartment.apartment_name,
                "living_room_minutes": round(apartment.living_room_minutes, 3),
                "open_space_minutes": (
                    None
                    if apartment.open_space_minutes is None
                    else round(apartment.open_space_minutes, 3)
                ),
                "governing_minutes": round(apartment.governing_minutes, 3),
                "meets_minimum": apartment.meets_minimum,
                "receives_no_sunlight": apartment.receives_no_sunlight,
                "counted": apartment.counted,
                "note": apartment.note,
            }
            for apartment in assessment.apartments
        ],
    }


def render_json(assessment: BuildingAssessment, header: Mapping[str, Any]) -> str:
    return json.dumps(results_document(assessment, header), indent=2, sort_keys=False) + "\n"


def write_json(
    destination: str | Path, assessment: BuildingAssessment, header: Mapping[str, Any]
) -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_json(assessment, header), encoding="utf-8")
    return path
