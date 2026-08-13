"""The composition root: IFC in, assessed apartments out.

This is the only module that reaches across every layer, which is why it sits
at the package root rather than inside one of them. It contains no geometry, no
astronomy and no thresholds -- purely the wiring.

Keeping it separate from ``cli`` matters: the whole chain is then callable and
testable without going through a command line, which is what the fixture's
golden-file test does.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from sun_study.core.analysis import (
    BandedResult,
    Weighting,
    band_by_area,
    cumulative_minutes,
    instant_weights,
    longest_continuous_minutes,
    sunlit_matrix,
)
from sun_study.core.occlusion import Occluder
from sun_study.core.sampling import SamplePoints
from sun_study.core.solar import assessment_times, solar_position
from sun_study.ingest.ifc import IfcModel, read_ifc
from sun_study.ingest.scene import (
    MassingConfig,
    MassingScene,
    Scene,
    SceneConfig,
    build_massing_scene,
    build_scene,
)
from sun_study.rules.assessment import (
    ApartmentMeasurement,
    BuildingAssessment,
    assess_building,
)
from sun_study.rules.ruleset import Ruleset, load_ruleset
from sun_study.rules.ruleset import Weighting as RulesetWeighting

__all__ = ["MassingResult", "PipelineResult", "run_assessment", "run_massing"]

_WEIGHTING = {
    RulesetWeighting.TRAPEZOIDAL: Weighting.TRAPEZOIDAL,
    RulesetWeighting.UNIFORM: Weighting.UNIFORM,
}


@dataclass(frozen=True)
class PipelineResult:
    """Everything a report needs, and everything needed to audit the run."""

    model: IfcModel
    scene: Scene
    ruleset: Ruleset
    assessment: BuildingAssessment
    sun_position_count: int
    assessment_date: dt.date


def _durations(
    points: SamplePoints,
    occluder: Occluder,
    sun_vectors: np.ndarray,
    timestep_minutes: float,
    weighting: Weighting,
) -> tuple[dict[str, float], dict[str, float]]:
    """Cumulative and longest-continuous minutes, per parent element.

    Samples are reduced by mean, so an element's figure is the share of its
    area in sun expressed as minutes. Reducing by worst-sample instead is a
    ruleset question and is available on ``summarise_by_parent``.
    """
    if len(points) == 0:
        return {}, {}

    sunlit = sunlit_matrix(points, occluder, sun_vectors)
    weights = instant_weights(sunlit.shape[1], timestep_minutes, weighting)
    per_sample = cumulative_minutes(sunlit, weights)
    per_sample_continuous = longest_continuous_minutes(sunlit, timestep_minutes)

    cumulative: dict[str, float] = {}
    continuous: dict[str, float] = {}
    for parent in points.unique_parents:
        mask = points.mask_for(parent)
        cumulative[parent] = float(np.mean(per_sample[mask]))
        continuous[parent] = float(np.mean(per_sample_continuous[mask]))
    return cumulative, continuous


def run_assessment(
    ifc_path: str | Path,
    *,
    timezone: str,
    ruleset: str | Path | Ruleset = "nsw_adg",
    area: str = "sydney_metro",
    year: int = 2024,
    scene_config: SceneConfig | None = None,
) -> PipelineResult:
    """Read an IFC, compute direct sunlight, and assess it against a ruleset.

    ``year`` only fixes which 21 June is used. The sun's declination on the
    solstice varies by a few hundredths of a degree between years, far below
    the tolerances here, but it is an explicit parameter so a run is
    reproducible rather than dependent on the system clock.
    """
    rules = ruleset if isinstance(ruleset, Ruleset) else load_ruleset(ruleset)
    config = scene_config or SceneConfig(timezone=timezone)
    if config.timezone != timezone:
        raise ValueError(
            f"timezone {timezone!r} does not match scene_config.timezone "
            f"{config.timezone!r}; the run would use two different zones."
        )

    model = read_ifc(ifc_path)
    scene = build_scene(model, config)

    times = assessment_times(
        rules.assessment.date_in(year),
        timezone,
        rules.assessment.start_time,
        rules.assessment.end_time,
        rules.assessment.timestep_minutes,
    )
    sun_vectors = scene.orientation.sun_vectors(
        solar_position(times, model.latitude_deg, model.longitude_deg)
    )

    occluder = Occluder(scene.occluders)
    timestep = float(rules.assessment.timestep_minutes)
    weighting = _WEIGHTING[rules.assessment.weighting]

    living_cumulative, living_continuous = _durations(
        scene.window_samples, occluder, sun_vectors, timestep, weighting
    )
    open_cumulative, open_continuous = _durations(
        scene.open_space_samples, occluder, sun_vectors, timestep, weighting
    )

    names = {element.global_id: element.name for element in model.elements}
    measurements = [
        ApartmentMeasurement(
            apartment_id=apartment_id,
            apartment_name=names.get(apartment_id, apartment_id),
            living_room_minutes=living_cumulative[apartment_id],
            living_room_continuous_minutes=living_continuous[apartment_id],
            # None, not zero. An apartment with no balcony is a different case
            # from one whose balcony never sees the sun, and the ruleset
            # decides what to do with each.
            open_space_minutes=open_cumulative.get(apartment_id),
            open_space_continuous_minutes=open_continuous.get(apartment_id),
        )
        for apartment_id in sorted(living_cumulative, key=lambda gid: names.get(gid, gid))
    ]

    return PipelineResult(
        model=model,
        scene=scene,
        ruleset=rules,
        assessment=assess_building(measurements, rules, area),
        sun_position_count=len(times),
        assessment_date=rules.assessment.date_in(year),
    )


@dataclass(frozen=True)
class MassingResult:
    """A massing-stage study: area-weighted bands, no apartments involved."""

    model: IfcModel
    scene: MassingScene
    ruleset: Ruleset
    area_key: str
    threshold_minutes: float
    facade: BandedResult
    ground: BandedResult
    sun_position_count: int
    assessment_date: dt.date

    def summary(self) -> str:
        hours = self.threshold_minutes / 60.0
        return (
            f"facade area with >{hours:g}hrs on "
            f"{self.assessment_date.isoformat()}: "
            f"{self.facade.at_or_above_threshold_share:.2%} "
            f"({self.facade.at_or_above_threshold_m2:.1f} of "
            f"{self.facade.total_area_m2:.1f} m2)\n"
            f"  open ground with >{hours:g}hrs: "
            f"{self.ground.at_or_above_threshold_share:.2%} "
            f"({self.ground.at_or_above_threshold_m2:.1f} of "
            f"{self.ground.total_area_m2:.1f} m2)"
        )


def run_massing(
    ifc_path: str | Path,
    *,
    timezone: str,
    ruleset: str | Path | Ruleset = "nsw_adg",
    area: str = "sydney_metro",
    year: int = 2024,
    massing_config: MassingConfig | None = None,
) -> MassingResult:
    """Area-weighted sunlight bands for a massing, with no Zones or windows.

    This is the metric that drives a massing optimisation loop: the share of
    facade area receiving at least the threshold duration. It is deliberately
    *not* the ADG's per-apartment criterion, which cannot be computed before
    apartments exist. The threshold itself still comes from the ruleset, so the
    two stay anchored to the same cited number.
    """
    rules = ruleset if isinstance(ruleset, Ruleset) else load_ruleset(ruleset)
    config = massing_config or MassingConfig(timezone=timezone)
    if config.timezone != timezone:
        raise ValueError(
            f"timezone {timezone!r} does not match massing_config.timezone "
            f"{config.timezone!r}; the run would use two different zones."
        )

    model = read_ifc(ifc_path)
    scene = build_massing_scene(model, config)

    times = assessment_times(
        rules.assessment.date_in(year),
        timezone,
        rules.assessment.start_time,
        rules.assessment.end_time,
        rules.assessment.timestep_minutes,
    )
    sun_vectors = scene.orientation.sun_vectors(
        solar_position(times, model.latitude_deg, model.longitude_deg)
    )

    occluder = Occluder(scene.occluders)
    timestep = float(rules.assessment.timestep_minutes)
    weighting = _WEIGHTING[rules.assessment.weighting]
    weights = instant_weights(len(times), timestep, weighting)
    threshold = rules.area(area).minimum_sunlight_minutes

    def banded(points: SamplePoints) -> BandedResult:
        if len(points) == 0:
            return band_by_area(points, np.zeros(0), threshold_minutes=threshold)
        minutes = cumulative_minutes(sunlit_matrix(points, occluder, sun_vectors), weights)
        return band_by_area(points, minutes, threshold_minutes=threshold)

    return MassingResult(
        model=model,
        scene=scene,
        ruleset=rules,
        area_key=area,
        threshold_minutes=threshold,
        facade=banded(scene.facade_samples),
        ground=banded(scene.ground_samples),
        sun_position_count=len(times),
        assessment_date=rules.assessment.date_in(year),
    )
