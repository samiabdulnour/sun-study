"""Per-apartment results as CSV.

The disclaimer and the full provenance header are written as comment lines
above the table. That is deliberate: a CSV gets opened in Excel, copied into a
report and emailed on, and by then nobody remembers which ruleset version or
which continuity setting produced it. Brief section 9 requires the disclaimer
on the CSV specifically.

Comment lines are prefixed with ``#``. Excel shows them as text rows above the
table rather than hiding them, which is the point.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sun_study.rules.assessment import BuildingAssessment

__all__ = ["COLUMNS", "write_csv"]

COLUMNS = (
    "apartment_id",
    "apartment_name",
    "living_room_minutes",
    "living_room_hours",
    "open_space_minutes",
    "open_space_hours",
    "governing_minutes",
    "meets_minimum",
    "receives_no_sunlight",
    "counted",
    "note",
    "ruleset",
    "area",
)


def _comment_lines(header: Mapping[str, Any], prefix: str = "# ") -> list[str]:
    """Flatten the header into readable ``# key: value`` lines."""
    lines: list[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                walk(inner, f"{path}.{key}" if path else str(key))
        else:
            text = " ".join(str(value).split())
            lines.append(f"{prefix}{path}: {text}")

    walk(dict(header), "")
    return lines


def render_csv(assessment: BuildingAssessment, header: Mapping[str, Any]) -> str:
    """The CSV as a string, so tests do not need the filesystem."""
    buffer = io.StringIO(newline="")

    for line in _comment_lines(header):
        buffer.write(line + "\r\n")
    buffer.write("#\r\n")

    writer = csv.DictWriter(buffer, fieldnames=list(COLUMNS), lineterminator="\r\n")
    writer.writeheader()

    for apartment in assessment.apartments:
        open_space = apartment.open_space_minutes
        writer.writerow(
            {
                "apartment_id": apartment.apartment_id,
                "apartment_name": apartment.apartment_name,
                "living_room_minutes": f"{apartment.living_room_minutes:.1f}",
                "living_room_hours": f"{apartment.living_room_minutes / 60.0:.2f}",
                # Empty, not zero: no balcony is not a balcony in permanent shade.
                "open_space_minutes": "" if open_space is None else f"{open_space:.1f}",
                "open_space_hours": "" if open_space is None else f"{open_space / 60.0:.2f}",
                "governing_minutes": f"{apartment.governing_minutes:.1f}",
                "meets_minimum": "yes" if apartment.meets_minimum else "no",
                "receives_no_sunlight": "yes" if apartment.receives_no_sunlight else "no",
                "counted": "yes" if apartment.counted else "no",
                "note": apartment.note,
                "ruleset": assessment.ruleset_identifier,
                "area": assessment.area_key,
            }
        )

    return buffer.getvalue()


def write_csv(
    destination: str | Path, assessment: BuildingAssessment, header: Mapping[str, Any]
) -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" so csv controls line endings on every platform, including the
    # Windows workstations this runs on.
    path.write_text(render_csv(assessment, header), encoding="utf-8", newline="")
    return path
