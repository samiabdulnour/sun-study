"""Applying a ruleset to per-apartment sunlight durations.

Generic. This module knows about thresholds, shares and durations; it does not
know what the ADG is. Everything jurisdiction-specific arrives as a
``Ruleset`` loaded from YAML.

What "an apartment complies" means
----------------------------------
The published criterion is "living rooms **and** private open spaces of at
least 70% of apartments ... receive a minimum of 2 hours". Turning that into a
per-apartment boolean needs three readings that the wording does not settle,
and all three live in ``Ruleset.interpretation`` rather than here:

* whether both the living room and the open space must meet the minimum, or
  either;
* whether an apartment "receives no direct sunlight" when both are zero, or
  when either is;
* what to do with an apartment that has no private open space at all.

They are choices. They are reported with every result, and they are the first
thing to check when a headline percentage looks surprising.
"""

from __future__ import annotations

from dataclasses import dataclass

from sun_study.rules.ruleset import (
    Continuity,
    MissingOpenSpacePolicy,
    Requires,
    Ruleset,
)

__all__ = [
    "ApartmentMeasurement",
    "ApartmentResult",
    "BuildingAssessment",
    "assess_building",
]


@dataclass(frozen=True)
class ApartmentMeasurement:
    """What the analysis engine measured for one apartment.

    ``None`` for an open space means the apartment has none, which is different
    from having one that receives no sun. Collapsing the two is how a studio
    without a balcony silently becomes a failure.
    """

    apartment_id: str
    apartment_name: str
    living_room_minutes: float
    living_room_continuous_minutes: float
    open_space_minutes: float | None = None
    open_space_continuous_minutes: float | None = None

    def governing(self, continuity: Continuity) -> float:
        return (
            self.living_room_minutes
            if continuity is Continuity.CUMULATIVE
            else self.living_room_continuous_minutes
        )

    def governing_open_space(self, continuity: Continuity) -> float | None:
        if self.open_space_minutes is None:
            return None
        if continuity is Continuity.CUMULATIVE:
            return self.open_space_minutes
        return self.open_space_continuous_minutes


@dataclass(frozen=True)
class ApartmentResult:
    """One apartment, assessed, with everything needed to audit the verdict."""

    apartment_id: str
    apartment_name: str
    living_room_minutes: float
    open_space_minutes: float | None
    governing_minutes: float
    """The figure the verdict was taken on, after the continuity and
    both/either readings were applied."""
    meets_minimum: bool
    receives_no_sunlight: bool
    counted: bool
    """False when the ruleset excludes this apartment from the denominator."""
    note: str = ""


@dataclass(frozen=True)
class BuildingAssessment:
    """The building-level verdict, and the assumptions that produced it."""

    ruleset_name: str
    ruleset_version: str
    area_key: str
    area_label: str
    minimum_minutes: float
    continuity: Continuity
    apartments: tuple[ApartmentResult, ...]

    counted_total: int
    meeting_minimum: int
    with_no_sunlight: int
    compliant_share: float
    no_sunlight_share: float
    required_share: float
    maximum_no_sunlight_share: float

    @property
    def meets_minimum_share(self) -> bool:
        return self.compliant_share >= self.required_share

    @property
    def within_no_sunlight_cap(self) -> bool:
        return self.no_sunlight_share <= self.maximum_no_sunlight_share

    @property
    def complies(self) -> bool:
        """Both design criteria, not just the headline one."""
        return self.meets_minimum_share and self.within_no_sunlight_cap

    @property
    def ruleset_identifier(self) -> str:
        return f"{self.ruleset_name}@{self.ruleset_version}"

    def summary(self) -> str:
        verdict = "COMPLIES" if self.complies else "DOES NOT COMPLY"
        return (
            f"{verdict}: {self.meeting_minimum}/{self.counted_total} apartments "
            f"({self.compliant_share:.1%}) receive at least "
            f"{self.minimum_minutes:g} minutes, target {self.required_share:.0%}"
            f" [{'pass' if self.meets_minimum_share else 'FAIL'}]\n"
            f"  {self.with_no_sunlight}/{self.counted_total} "
            f"({self.no_sunlight_share:.1%}) receive no direct sunlight, "
            f"cap {self.maximum_no_sunlight_share:.0%}"
            f" [{'pass' if self.within_no_sunlight_cap else 'FAIL'}]"
        )


def _meets(value: float | None, minimum: float) -> bool:
    return value is not None and value >= minimum


def assess_building(
    measurements: list[ApartmentMeasurement] | tuple[ApartmentMeasurement, ...],
    ruleset: Ruleset,
    area_key: str,
) -> BuildingAssessment:
    """Assess every apartment, then the building, against ``ruleset``."""
    area = ruleset.area(area_key)
    minimum = area.minimum_sunlight_minutes
    continuity = ruleset.assessment.continuity
    interpretation = ruleset.interpretation

    results = []
    for measurement in measurements:
        living = measurement.governing(continuity)
        open_space = measurement.governing_open_space(continuity)
        counted, note = True, ""

        if open_space is None:
            policy = interpretation.apartments_without_open_space
            note = f"no private open space; {policy}"
            if policy is MissingOpenSpacePolicy.EXCLUDED:
                counted = False
                meets, no_sunlight = False, False
                governing = living
            elif policy is MissingOpenSpacePolicy.NON_COMPLIANT:
                meets = False
                no_sunlight = living <= 0.0
                governing = living
            else:  # living_room_only
                meets = living >= minimum
                no_sunlight = living <= 0.0
                governing = living
        elif interpretation.compliance_requires is Requires.BOTH:
            # The apartment is governed by whichever of the two is worse.
            governing = min(living, open_space)
            meets = governing >= minimum
            no_sunlight = (
                max(living, open_space) <= 0.0
                if interpretation.no_sunlight_requires is Requires.BOTH
                else min(living, open_space) <= 0.0
            )
        else:  # either
            governing = max(living, open_space)
            meets = _meets(living, minimum) or _meets(open_space, minimum)
            no_sunlight = (
                max(living, open_space) <= 0.0
                if interpretation.no_sunlight_requires is Requires.BOTH
                else min(living, open_space) <= 0.0
            )

        results.append(
            ApartmentResult(
                apartment_id=measurement.apartment_id,
                apartment_name=measurement.apartment_name,
                living_room_minutes=measurement.living_room_minutes,
                open_space_minutes=measurement.open_space_minutes,
                governing_minutes=governing,
                meets_minimum=meets,
                receives_no_sunlight=no_sunlight,
                counted=counted,
                note=note,
            )
        )

    counted_results = [r for r in results if r.counted]
    total = len(counted_results)
    meeting = sum(1 for r in counted_results if r.meets_minimum)
    dark = sum(1 for r in counted_results if r.receives_no_sunlight)

    return BuildingAssessment(
        ruleset_name=ruleset.name,
        ruleset_version=ruleset.version,
        area_key=area_key,
        area_label=area.label,
        minimum_minutes=minimum,
        continuity=continuity,
        apartments=tuple(results),
        counted_total=total,
        meeting_minimum=meeting,
        with_no_sunlight=dark,
        # An empty building is reported as 0%, not as vacuously compliant.
        compliant_share=(meeting / total) if total else 0.0,
        no_sunlight_share=(dark / total) if total else 0.0,
        required_share=ruleset.criterion("minimum_compliant_share").value,
        maximum_no_sunlight_share=ruleset.criterion("maximum_no_sunlight_share").value,
    )
