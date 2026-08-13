"""The provenance header that travels with every result file.

One place, so the CSV, the JSON and the console banner cannot drift apart and
so that no exported number can be separated from the assumptions that produced
it. Brief sections 5.3, 5.7 and 9.

A results file that has lost its ruleset version, its continuity setting or its
north bearing is not a weaker record -- it is an unreproducible one, and it
looks exactly like a good one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sun_study import __version__
from sun_study.disclaimer import DISCLAIMER, STATUS
from sun_study.rules.assessment import BuildingAssessment
from sun_study.rules.ruleset import Ruleset

__all__ = ["build_header"]


def build_header(
    assessment: BuildingAssessment,
    ruleset: Ruleset,
    site_description: str,
    scene_provenance: Mapping[str, Any],
    scene_config_description: str,
    generated_at: str,
) -> dict[str, Any]:
    """Everything a reader needs to judge, reproduce or challenge the numbers.

    ``generated_at`` is passed in rather than read from the clock so that
    golden-file comparisons stay deterministic.
    """
    area = ruleset.area(assessment.area_key)
    return {
        "status": STATUS,
        "disclaimer": DISCLAIMER,
        "tool": f"sun-study {__version__}",
        "generated_at": generated_at,
        "site": site_description,
        "scene": scene_config_description,
        "ruleset": {
            "identifier": ruleset.identifier,
            "title": ruleset.title,
            "area": assessment.area_key,
            "area_label": area.label,
            "minimum_sunlight_minutes": area.minimum_sunlight_minutes,
            "minimum_sunlight_citation": area.citation,
            "required_share": assessment.required_share,
            "required_share_citation": ruleset.criterion("minimum_compliant_share").citation,
            "maximum_no_sunlight_share": assessment.maximum_no_sunlight_share,
            "maximum_no_sunlight_citation": ruleset.criterion("maximum_no_sunlight_share").citation,
            "assessment_date": ruleset.assessment.date,
            "window": f"{ruleset.assessment.window_start}-{ruleset.assessment.window_end}",
            "timestep_minutes": ruleset.assessment.timestep_minutes,
            "continuity": str(ruleset.assessment.continuity),
            "weighting": str(ruleset.assessment.weighting),
            "source": ruleset.source.model_dump(exclude_none=True),
        },
        "interpretation": {
            "compliance_requires": str(ruleset.interpretation.compliance_requires),
            "no_sunlight_requires": str(ruleset.interpretation.no_sunlight_requires),
            "apartments_without_open_space": str(
                ruleset.interpretation.apartments_without_open_space
            ),
            "note": (
                "These are readings of the published wording, not the wording itself. "
                "They change which apartments pass and are recorded for that reason."
            ),
        },
        "provenance": dict(scene_provenance),
    }
