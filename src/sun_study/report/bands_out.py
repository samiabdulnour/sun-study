"""Area-weighted sunlight bands as CSV and JSON.

The massing-stage counterpart to ``csv_out``/``json_out``. Where those report
one row per apartment, these report one row per band with its square metres and
share, plus the three roll-ups, for each analysed surface set.

The grid spacing is carried in the header on purpose. A massing run is usually
coarse so an optimisation loop can afford hundreds of variants, and a coarse
percentage quoted as a fine one is exactly the kind of plausible wrongness this
project is built to avoid.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sun_study import __version__
from sun_study.core.analysis import BandedResult
from sun_study.disclaimer import DISCLAIMER, STATUS
from sun_study.report.csv_out import _comment_lines
from sun_study.rules.ruleset import Ruleset

__all__ = [
    "BAND_COLUMNS",
    "build_massing_header",
    "render_bands_csv",
    "render_bands_json",
    "write_bands_csv",
    "write_bands_json",
]

BAND_COLUMNS = ("surface", "band", "lower_minutes", "upper_minutes", "area_m2", "share")


def build_massing_header(
    ruleset: Ruleset,
    area_key: str,
    threshold_minutes: float,
    site_description: str,
    scene_config_description: str,
    scene_provenance: Mapping[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    """Provenance for a massing run.

    Deliberately says what this metric *is not*: an area share is not the ADG's
    per-apartment criterion, and the two must never be quoted for each other.
    """
    area = ruleset.area(area_key)
    return {
        "status": STATUS,
        "disclaimer": DISCLAIMER,
        "tool": f"sun-study {__version__}",
        "mode": "massing",
        "generated_at": generated_at,
        "site": site_description,
        "scene": scene_config_description,
        "metric": {
            "name": "share of surface area receiving at least the threshold duration",
            "threshold_minutes": threshold_minutes,
            "threshold_source": f"{ruleset.identifier} area {area_key}",
            "threshold_citation": area.citation,
            "assessment_date": ruleset.assessment.date,
            "window": f"{ruleset.assessment.window_start}-{ruleset.assessment.window_end}",
            "timestep_minutes": ruleset.assessment.timestep_minutes,
            "weighting": str(ruleset.assessment.weighting),
            "note": (
                "This is an area share, not the ADG per-apartment criterion. It is "
                "the metric available before apartments exist and is not a "
                "compliance figure. The two must not be quoted for one another."
            ),
        },
        "provenance": dict(scene_provenance),
    }


def _rows(surfaces: Mapping[str, BandedResult]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for surface, result in surfaces.items():
        for band in result.bands:
            rows.append(
                {
                    "surface": surface,
                    "band": band.label,
                    "lower_minutes": f"{band.lower_minutes:.0f}",
                    "upper_minutes": (
                        "" if band.upper_minutes is None else f"{band.upper_minutes:.0f}"
                    ),
                    "area_m2": f"{band.area_m2:.2f}",
                    "share": f"{band.share:.6f}",
                }
            )
        hours = result.threshold_minutes / 60.0
        for label, area, share in (
            (
                f">{hours:g}hrs",
                result.at_or_above_threshold_m2,
                result.at_or_above_threshold_share,
            ),
            (f"0-{hours:g}hrs", result.below_threshold_m2, result.below_threshold_share),
            ("0hr", result.zero_m2, result.zero_share),
        ):
            rows.append(
                {
                    "surface": f"{surface} (rollup)",
                    "band": label,
                    "lower_minutes": "",
                    "upper_minutes": "",
                    "area_m2": f"{area:.2f}",
                    "share": f"{share:.6f}",
                }
            )
    return rows


def render_bands_csv(surfaces: Mapping[str, BandedResult], header: Mapping[str, Any]) -> str:
    buffer = io.StringIO(newline="")
    for line in _comment_lines(header):
        buffer.write(line + "\r\n")
    buffer.write("#\r\n")

    writer = csv.DictWriter(buffer, fieldnames=list(BAND_COLUMNS), lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(_rows(surfaces))
    return buffer.getvalue()


def render_bands_json(surfaces: Mapping[str, BandedResult], header: Mapping[str, Any]) -> str:
    document = {
        "header": dict(header),
        "surfaces": {
            name: {
                "total_area_m2": round(result.total_area_m2, 3),
                "threshold_minutes": result.threshold_minutes,
                "at_or_above_threshold_m2": round(result.at_or_above_threshold_m2, 3),
                "at_or_above_threshold_share": round(result.at_or_above_threshold_share, 6),
                "below_threshold_m2": round(result.below_threshold_m2, 3),
                "below_threshold_share": round(result.below_threshold_share, 6),
                "zero_m2": round(result.zero_m2, 3),
                "zero_share": round(result.zero_share, 6),
                "bands": [
                    {
                        "label": band.label,
                        "lower_minutes": band.lower_minutes,
                        "upper_minutes": band.upper_minutes,
                        "area_m2": round(band.area_m2, 3),
                        "share": round(band.share, 6),
                    }
                    for band in result.bands
                ],
            }
            for name, result in surfaces.items()
        },
    }
    return json.dumps(document, indent=2) + "\n"


def write_bands_csv(
    destination: str | Path, surfaces: Mapping[str, BandedResult], header: Mapping[str, Any]
) -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_bands_csv(surfaces, header), encoding="utf-8", newline="")
    return path


def write_bands_json(
    destination: str | Path, surfaces: Mapping[str, BandedResult], header: Mapping[str, Any]
) -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_bands_json(surfaces, header), encoding="utf-8")
    return path
