"""Loading and validating a ruleset.

A ruleset is data. Thresholds, the assessment window and the citations for both
live in YAML under ``rulesets/``; this module only gives them a validated shape.

There is deliberately no ``nsw_adg.py``. The brief's sketch listed one, but
section 5.7 is the sharper statement of the same idea -- "the engine reads a
ruleset; it does not know what 'ADG' means" -- and a module named after one
jurisdiction is exactly where a threshold ends up hardcoded. Adding a council
with a 3 hour continuous rule should be a new YAML file and no new code.

Every threshold requires a citation, enforced by the schema rather than by
convention. A number in a compliance tool that nobody can trace to a published
document is worse than no number at all.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "BUILTIN_RULESETS",
    "AreaVariant",
    "Assessment",
    "Continuity",
    "Criterion",
    "Interpretation",
    "MissingOpenSpacePolicy",
    "Requires",
    "Ruleset",
    "RulesetError",
    "load_ruleset",
]

RULESET_DIR = Path(__file__).parent / "rulesets"
BUILTIN_RULESETS = ("nsw_adg",)


class RulesetError(Exception):
    """A ruleset is missing, malformed, or missing a citation."""


class Continuity(StrEnum):
    """Whether the minimum duration must be unbroken.

    Councils differ, and the same building passes under one reading and fails
    under the other, so this is never implicit.
    """

    CUMULATIVE = "cumulative"
    CONTINUOUS = "continuous"


class Weighting(StrEnum):
    TRAPEZOIDAL = "trapezoidal"
    UNIFORM = "uniform"


class Requires(StrEnum):
    BOTH = "both"
    EITHER = "either"


class MissingOpenSpacePolicy(StrEnum):
    LIVING_ROOM_ONLY = "living_room_only"
    EXCLUDED = "excluded"
    NON_COMPLIANT = "non_compliant"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Criterion(_Strict):
    """A single threshold and the published words it comes from."""

    value: float
    citation: str = Field(min_length=20)

    @field_validator("citation")
    @classmethod
    def _must_be_a_real_citation(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("citation must not be blank")
        return value.strip()


class AreaVariant(_Strict):
    """A geographic variant of the minimum-duration threshold."""

    label: str
    minimum_sunlight_minutes: float = Field(gt=0)
    citation: str = Field(min_length=20)


class Assessment(_Strict):
    """When the assessment is made and how the samples are weighted."""

    date: str
    window_start: str
    window_end: str
    timestep_minutes: int = Field(gt=0)
    citation: str = Field(min_length=20)
    continuity: Continuity = Continuity.CUMULATIVE
    weighting: Weighting = Weighting.TRAPEZOIDAL

    @property
    def month_day(self) -> tuple[int, int]:
        month, day = self.date.split("-")
        return int(month), int(day)

    def date_in(self, year: int) -> dt.date:
        month, day = self.month_day
        return dt.date(year, month, day)

    @property
    def start_time(self) -> dt.time:
        return dt.time.fromisoformat(self.window_start)

    @property
    def end_time(self) -> dt.time:
        return dt.time.fromisoformat(self.window_end)

    @property
    def window_minutes(self) -> float:
        start, end = self.start_time, self.end_time
        return (end.hour - start.hour) * 60.0 + (end.minute - start.minute)


class Interpretation(_Strict):
    """Readings of the published wording, as distinct from the wording itself.

    Kept separate from ``criteria`` on purpose: these are choices, they are
    reported alongside every result, and someone assessing a run needs to be
    able to see at a glance which are the regulator's words and which are ours.
    """

    compliance_requires: Requires = Requires.BOTH
    no_sunlight_requires: Requires = Requires.BOTH
    apartments_without_open_space: MissingOpenSpacePolicy = MissingOpenSpacePolicy.LIVING_ROOM_ONLY


class Source(_Strict):
    document: str
    publisher: str
    objective: str | None = None
    technical_note: str | None = None
    url: str | None = None
    retrieved: str | None = None


class Ruleset(_Strict):
    """A complete, validated set of rules."""

    name: str
    version: str
    title: str
    source: Source
    assessment: Assessment
    areas: dict[str, AreaVariant] = Field(min_length=1)
    criteria: dict[str, Criterion]
    interpretation: Interpretation = Interpretation()
    notes: dict[str, str] = Field(default_factory=dict)

    @property
    def identifier(self) -> str:
        """What every result record carries, per brief section 5.7."""
        return f"{self.name}@{self.version}"

    def area(self, key: str) -> AreaVariant:
        try:
            return self.areas[key]
        except KeyError:
            raise RulesetError(
                f"Unknown area {key!r} for ruleset {self.identifier}. "
                f"Available: {', '.join(sorted(self.areas))}."
            ) from None

    def criterion(self, key: str) -> Criterion:
        try:
            return self.criteria[key]
        except KeyError:
            raise RulesetError(
                f"Ruleset {self.identifier} has no criterion {key!r}. "
                f"Available: {', '.join(sorted(self.criteria))}."
            ) from None

    def describe(self, area_key: str) -> str:
        """The header line that travels with every result file."""
        area = self.area(area_key)
        return (
            f"{self.identifier} | {self.title}\n"
            f"  area: {area.label} "
            f"(minimum {area.minimum_sunlight_minutes:g} min per apartment)\n"
            f"  assessed {self.assessment.date} "
            f"{self.assessment.window_start}-{self.assessment.window_end} "
            f"at {self.assessment.timestep_minutes} min steps, "
            f"{self.assessment.continuity} duration, "
            f"{self.assessment.weighting} weighting\n"
            f"  target {self.criterion('minimum_compliant_share').value:.0%} of apartments, "
            f"cap {self.criterion('maximum_no_sunlight_share').value:.0%} with no sunlight\n"
            f"  interpretation: compliance requires "
            f"{self.interpretation.compliance_requires} living room and open space; "
            f"apartments without open space are "
            f"{self.interpretation.apartments_without_open_space}"
        )


REQUIRED_CRITERIA = ("minimum_compliant_share", "maximum_no_sunlight_share")


def load_ruleset(name_or_path: str | Path) -> Ruleset:
    """Load a built-in ruleset by name, or any ruleset by path."""
    path = Path(name_or_path)
    if not path.suffix:
        path = RULESET_DIR / f"{name_or_path}.yaml"

    if not path.is_file():
        raise RulesetError(
            f"No ruleset at {path}. Built-in rulesets: {', '.join(BUILTIN_RULESETS)}."
        )

    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RulesetError(f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise RulesetError(f"{path} must contain a mapping at the top level")

    try:
        ruleset = Ruleset.model_validate(raw)
    except Exception as exc:
        raise RulesetError(f"{path} is not a valid ruleset: {exc}") from exc

    missing = [key for key in REQUIRED_CRITERIA if key not in ruleset.criteria]
    if missing:
        raise RulesetError(f"{path} is missing required criteria: {', '.join(missing)}")

    return ruleset
