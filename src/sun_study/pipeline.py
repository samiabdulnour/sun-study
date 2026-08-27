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
import numpy.typing as npt

from sun_study.core.analysis import (
    BandedResult,
    Weighting,
    band_by_area,
    cumulative_minutes,
    instant_weights,
    lit_share_per_instant,
    longest_continuous_minutes,
    sunlit_matrix,
)
from sun_study.core.occlusion import Occluder
from sun_study.core.patches import Rectangle, merge_lit_cells
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

FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]

__all__ = [
    "WEIGHTING_BY_RULESET",
    "MassingResult",
    "PipelineResult",
    "run_assessment",
    "run_massing",
]

#: How a ruleset's named weighting maps onto the engine's. Public because a
#: caller computing durations outside the pipeline -- the open-ground study --
#: has to weight its instants the same way, or its hours mean something
#: slightly different from the apartments' beside it.
WEIGHTING_BY_RULESET = {
    RulesetWeighting.TRAPEZOIDAL: Weighting.TRAPEZOIDAL,
    RulesetWeighting.UNIFORM: Weighting.UNIFORM,
}


@dataclass(frozen=True)
class InstantSeries:
    """When each apartment was in sun, rather than for how long in total.

    The assessment answers "does this apartment get two hours". A study
    drawing answers "where is the sun at 09:15", and no amount of the first
    reconstructs the second. Both come off the same boolean matrix, which
    used to be computed and dropped inside ``_durations``.
    """

    times: tuple[dt.datetime, ...]
    """The instants, in the project's own timezone, in order."""

    apartment_ids: tuple[str, ...]
    """Rows of ``living_share`` and ``open_space_share``, in order."""

    living_share: FloatArray
    """``(n_apartments, n_instants)``, area-weighted share of the living-room
    glazing in sun at each instant."""

    open_space_share: FloatArray
    """The same for private open space. Rows for apartments with none are
    absent from ``open_space_ids`` rather than zero -- no balcony and a balcony
    in shadow are different facts."""

    open_space_ids: tuple[str, ...]

    floor_positions: FloatArray | None = None
    """``(n, 3)`` centres of the floor grid cells, or None when no patch was
    asked for."""

    floor_parent_ids: tuple[str, ...] = ()
    """The apartment each floor cell belongs to, parallel to ``floor_positions``."""

    floor_sunlit: BoolArray | None = None
    """``(n_cells, n_instants)``: did the sun reach this piece of floor then."""

    floor_is_open_space: BoolArray | None = None
    """Which floor cells are balcony rather than room."""

    floor_minutes: FloatArray | None = None
    """Total sunlit minutes on each floor cell across the whole window.

    The same weighting the compliance figure uses, over the same instants, so
    a banded plan and the schedule beside it cannot disagree about how long
    the sun was on a given piece of floor."""

    floor_areas: FloatArray | None = None
    """Square metres each floor cell stands for."""

    floor_spacing_m: float = 0.0

    def patches_at(self, instant: int) -> dict[str, tuple[Rectangle, ...]]:
        """The sun patch on each apartment's floor at one instant.

        Apartment id -> the rectangles that tile the lit part of its floor,
        ready to be drawn. Empty when the run was not asked for a patch, and
        an apartment with no sun at that instant is simply absent rather than
        present with an empty tuple -- there is nothing to draw for it.
        """
        if self.floor_sunlit is None or self.floor_positions is None:
            return {}
        lit = self.floor_sunlit[:, instant]
        patches: dict[str, tuple[Rectangle, ...]] = {}
        for apartment in dict.fromkeys(self.floor_parent_ids):
            mine = np.array([pid == apartment for pid in self.floor_parent_ids])
            rectangles = merge_lit_cells(
                self.floor_positions[mine], lit[mine], self.floor_spacing_m
            )
            if rectangles:
                patches[apartment] = rectangles
        return patches

    def lit_areas_at(self, instant: int) -> dict[str, tuple[float, float]]:
        """Apartment id -> (room area, open-space area) in sun, square metres.

        The two figures the office's own study sheet prints against each flat.
        Taken from the patch cells rather than from the duration, so the number
        in the annotation is the area of the fill drawn beside it -- they
        cannot drift apart.
        """
        if self.floor_sunlit is None or self.floor_positions is None:
            return {}
        cell = self.floor_spacing_m**2
        lit = self.floor_sunlit[:, instant]
        open_space = (
            self.floor_is_open_space
            if self.floor_is_open_space is not None
            else np.zeros(len(lit), dtype=bool)
        )
        areas: dict[str, tuple[float, float]] = {}
        for index, apartment in enumerate(self.floor_parent_ids):
            if not lit[index]:
                continue
            room, outside = areas.get(apartment, (0.0, 0.0))
            if open_space[index]:
                areas[apartment] = (room, outside + cell)
            else:
                areas[apartment] = (room + cell, outside)
        return areas

    def living_at(self, instant: int) -> dict[str, float]:
        """Apartment id -> lit share at one instant, for drawing it."""
        return {
            apartment: float(self.living_share[row, instant])
            for row, apartment in enumerate(self.apartment_ids)
        }

    def open_space_at(self, instant: int) -> dict[str, float]:
        return {
            apartment: float(self.open_space_share[row, instant])
            for row, apartment in enumerate(self.open_space_ids)
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
    instants: InstantSeries | None = None
    """Per-instant sunlit shares. Optional only so a caller that predates it
    keeps working; ``run_assessment`` always fills it in."""


@dataclass(frozen=True)
class _Durations:
    """What one set of sample points yielded, aggregated and per instant."""

    cumulative: dict[str, float]
    continuous: dict[str, float]
    parents: tuple[str, ...]
    lit_share: FloatArray
    """``(n_parents, n_instants)``. Kept rather than dropped: it costs one
    float per parent per instant and it is the only record of *when*."""


def _durations(
    points: SamplePoints,
    occluder: Occluder,
    sun_vectors: np.ndarray,
    timestep_minutes: float,
    weighting: Weighting,
) -> _Durations:
    """Cumulative and longest-continuous minutes, per parent element.

    Samples are reduced by mean, so an element's figure is the share of its
    area in sun expressed as minutes. Reducing by worst-sample instead is a
    ruleset question and is available on ``summarise_by_parent``.
    """
    if len(points) == 0:
        return _Durations({}, {}, (), np.zeros((0, len(sun_vectors)), dtype=np.float64))

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
    return _Durations(
        cumulative=cumulative,
        continuous=continuous,
        parents=points.unique_parents,
        lit_share=lit_share_per_instant(points, sunlit),
    )


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
    weighting = WEIGHTING_BY_RULESET[rules.assessment.weighting]

    living = _durations(scene.window_samples, occluder, sun_vectors, timestep, weighting)
    open_space = _durations(scene.open_space_samples, occluder, sun_vectors, timestep, weighting)

    # A second pass, over a second surface, against a different occluder set:
    # the glazing is taken out so the sun can reach the floor through it. Only
    # run when a patch was asked for -- it is a drawing, not a number, and it
    # roughly doubles the ray casting.
    floor_sunlit = None
    floor_minutes = None
    if scene.floor_samples is not None and scene.glazed_occluders is not None:
        floor_sunlit = sunlit_matrix(
            scene.floor_samples, Occluder(scene.glazed_occluders), sun_vectors
        )
        floor_minutes = cumulative_minutes(
            floor_sunlit, instant_weights(floor_sunlit.shape[1], timestep, weighting)
        )
    living_cumulative, living_continuous = living.cumulative, living.continuous
    open_cumulative, open_continuous = open_space.cumulative, open_space.continuous

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
        instants=InstantSeries(
            times=tuple(times),
            apartment_ids=living.parents,
            living_share=living.lit_share,
            open_space_share=open_space.lit_share,
            open_space_ids=open_space.parents,
            floor_positions=(
                scene.floor_samples.positions if scene.floor_samples is not None else None
            ),
            floor_parent_ids=(
                scene.floor_samples.parent_ids if scene.floor_samples is not None else ()
            ),
            floor_sunlit=floor_sunlit,
            floor_is_open_space=scene.floor_is_open_space,
            floor_minutes=floor_minutes,
            floor_areas=(scene.floor_samples.areas if scene.floor_samples is not None else None),
            floor_spacing_m=config.floor_patch_spacing_m or 0.0,
        ),
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
    zone: BandedResult | None = None
    """The named Zones, measured as one surface. ``None`` when none were named."""

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
            + (
                f"\n  named zones with >{hours:g}hrs: "
                f"{self.zone.at_or_above_threshold_share:.2%} "
                f"({self.zone.at_or_above_threshold_m2:.1f} of "
                f"{self.zone.total_area_m2:.1f} m2)"
                if self.zone is not None
                else ""
            )
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
    weighting = WEIGHTING_BY_RULESET[rules.assessment.weighting]
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
        zone=banded(scene.zone_samples) if len(scene.zone_samples) else None,
        sun_position_count=len(times),
        assessment_date=rules.assessment.date_in(year),
    )
