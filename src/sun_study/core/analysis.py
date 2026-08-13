"""Aggregating ray hits into direct sunlight duration.

The engine knows nothing about the ADG. It answers "how many minutes of direct
sun did this point receive, under this weighting" and hands the number to
``rules``, which owns every threshold and every citation.

Weighting -- read this before trusting a duration
-------------------------------------------------
An assessment window of 09:00 to 15:00 at a 10 minute step contains **37**
instants, not 36, because both endpoints are included. Counting sunlit instants
and multiplying by the timestep therefore reports up to 370 minutes of sun in a
360 minute window.

The bias is small, always optimistic, and lands squarely on the 2 hour
threshold that decides whether an apartment complies. So the weighting is
explicit and travels with the result:

``TRAPEZOIDAL`` (default) gives the two endpoints half weight, so the weights
sum to exactly the window length. ``UNIFORM`` gives every instant full weight
and is offered only because some reference tools do it that way; it will report
370 minutes for a fully sunlit ADG window.

Continuity
----------
Some DCPs require an unbroken duration rather than a cumulative total, and the
same building can pass under one reading and fail under the other. Both are
computed here; which one governs is a ruleset decision, never a default buried
in the engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import numpy.typing as npt

from sun_study.core.occlusion import Occluder
from sun_study.core.sampling import SamplePoints

FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]

__all__ = [
    "SunlightResult",
    "Weighting",
    "cumulative_minutes",
    "instant_weights",
    "longest_continuous_minutes",
    "sunlit_matrix",
]


class Weighting(StrEnum):
    """How much of the window each sampled instant stands for."""

    TRAPEZOIDAL = "trapezoidal"
    UNIFORM = "uniform"


def instant_weights(
    instant_count: int,
    timestep_minutes: float,
    weighting: Weighting = Weighting.TRAPEZOIDAL,
) -> FloatArray:
    """Minutes represented by each instant.

    Trapezoidal weights sum to ``(n - 1) * timestep``, the true window length.
    Uniform weights sum to ``n * timestep``, which overstates it by one step.
    """
    if instant_count < 0:
        raise ValueError(f"instant_count must be non-negative, got {instant_count}")
    if timestep_minutes <= 0.0:
        raise ValueError(f"timestep_minutes must be positive, got {timestep_minutes}")
    if instant_count == 0:
        return np.zeros(0, dtype=np.float64)

    weights = np.full(instant_count, float(timestep_minutes), dtype=np.float64)
    if weighting is Weighting.TRAPEZOIDAL and instant_count > 1:
        weights[0] = weights[-1] = timestep_minutes / 2.0
    elif weighting is Weighting.TRAPEZOIDAL:
        # A single instant spans no time at all under trapezoidal weighting.
        weights[0] = 0.0
    return weights


def sunlit_matrix(
    points: SamplePoints,
    occluder: Occluder,
    sun_vectors: FloatArray,
) -> BoolArray:
    """Boolean (n_points, n_instants): did this point see the sun at that instant.

    ``sun_vectors`` are unit vectors towards the sun **in the same frame as the
    geometry**. Use ``core.orientation.sun_vectors_in_model_frame`` to get
    there from a ``SolarPosition``; passing raw ENU vectors alongside a rotated
    model is the single easiest way to produce a confident wrong answer.

    Two things are resolved before any ray is cast, which is both a large
    speed-up and a correctness requirement:

    * An instant where the sun is at or below the horizon lights nothing. The
      test is on the +Z component, so the model frame must be Z-up.
    * A point whose normal faces away from the sun is self-shaded by the
      surface it sits on, whatever else is in the scene. Casting that ray would
      ask the wrong question and could return a spurious hit-free result.
    """
    directions = np.ascontiguousarray(sun_vectors, dtype=np.float64)
    if directions.ndim != 2 or directions.shape[1] != 3:
        raise ValueError(f"sun_vectors must have shape (n, 3), got {directions.shape}")

    point_count, instant_count = len(points), len(directions)
    sunlit = np.zeros((point_count, instant_count), dtype=np.bool_)
    if point_count == 0 or instant_count == 0:
        return sunlit

    above_horizon = directions[:, 2] > 0.0
    facing = (points.normals @ directions.T) > 0.0
    candidate = facing & above_horizon[None, :]

    pairs = np.argwhere(candidate)
    if pairs.size == 0:
        return sunlit

    blocked = occluder.any_hit(points.positions[pairs[:, 0]], directions[pairs[:, 1]])
    sunlit[pairs[:, 0], pairs[:, 1]] = ~blocked
    return sunlit


def cumulative_minutes(sunlit: BoolArray, weights: FloatArray) -> FloatArray:
    """Total sunlit minutes per point, one entry per row of ``sunlit``."""
    if sunlit.shape[1] != len(weights):
        raise ValueError(
            f"{sunlit.shape[1]} instants in the sunlit matrix but {len(weights)} weights"
        )
    return np.asarray(sunlit.astype(np.float64) @ weights, dtype=np.float64)


def longest_continuous_minutes(sunlit: BoolArray, timestep_minutes: float) -> FloatArray:
    """Longest unbroken sunlit span per point, in minutes.

    A run of ``k`` consecutive sunlit instants spans ``(k - 1) * timestep`` of
    continuous time, so a single isolated instant counts as zero. That is the
    conservative reading, and continuity thresholds exist precisely to exclude
    brief flashes of sun.
    """
    if timestep_minutes <= 0.0:
        raise ValueError(f"timestep_minutes must be positive, got {timestep_minutes}")

    point_count, instant_count = sunlit.shape
    longest = np.zeros(point_count, dtype=np.int64)
    if instant_count == 0:
        return np.zeros(point_count, dtype=np.float64)

    current = np.zeros(point_count, dtype=np.int64)
    for instant in range(instant_count):
        column = sunlit[:, instant]
        current = np.where(column, current + 1, 0)
        longest = np.maximum(longest, current)

    return np.asarray(np.maximum(longest - 1, 0) * timestep_minutes, dtype=np.float64)


@dataclass(frozen=True)
class SunlightResult:
    """Per-parent sunlight durations, with the settings that produced them.

    The settings travel with the numbers on purpose. A duration is meaningless
    without the weighting and timestep that generated it, and a result record
    that has been separated from its assumptions is how an optimistic figure
    ends up in a table nobody can reproduce.
    """

    parent_ids: tuple[str, ...]
    cumulative_minutes: FloatArray
    continuous_minutes: FloatArray
    timestep_minutes: float
    weighting: Weighting
    instant_count: int
    surface_offset_m: float

    @property
    def window_minutes(self) -> float:
        """The total the weighting can award, for sanity-checking a result."""
        return float(
            np.sum(instant_weights(self.instant_count, self.timestep_minutes, self.weighting))
        )

    def as_dict(self) -> dict[str, float]:
        return dict(zip(self.parent_ids, self.cumulative_minutes.tolist(), strict=True))


def summarise_by_parent(
    points: SamplePoints,
    sunlit: BoolArray,
    timestep_minutes: float,
    *,
    weighting: Weighting = Weighting.TRAPEZOIDAL,
    reducer: str = "mean",
) -> SunlightResult:
    """Reduce per-sample durations to one figure per parent element.

    ``reducer`` is ``mean`` (the share of the element's area in sun, expressed
    as minutes) or ``min`` (the worst-lit sample). Which is appropriate is a
    ruleset question, so it is a parameter rather than a decision made here.
    """
    if reducer not in {"mean", "min"}:
        raise ValueError(f"reducer must be 'mean' or 'min', got {reducer!r}")

    weights = instant_weights(sunlit.shape[1], timestep_minutes, weighting)
    per_sample_cumulative = cumulative_minutes(sunlit, weights)
    per_sample_continuous = longest_continuous_minutes(sunlit, timestep_minutes)

    parents = points.unique_parents
    reduce = np.mean if reducer == "mean" else np.min

    cumulative = np.array(
        [float(reduce(per_sample_cumulative[points.mask_for(pid)])) for pid in parents],
        dtype=np.float64,
    )
    continuous = np.array(
        [float(reduce(per_sample_continuous[points.mask_for(pid)])) for pid in parents],
        dtype=np.float64,
    )

    return SunlightResult(
        parent_ids=parents,
        cumulative_minutes=cumulative,
        continuous_minutes=continuous,
        timestep_minutes=timestep_minutes,
        weighting=weighting,
        instant_count=int(sunlit.shape[1]),
        surface_offset_m=points.surface_offset_m,
    )
