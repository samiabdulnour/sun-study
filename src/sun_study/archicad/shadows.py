"""Shadow diagrams drawn into the plan, one layer to an hour.

The office's shadow diagrams were made from a 3D Document: model the scheme,
set the sun, place the document on a sheet, repeat twenty-one times. That
works and has two costs. A 3D Document cannot be created through the add-on at
all (D49), so every one of them is somebody's hand work before the tool can
touch it; and it renders *a* shadow, never the split between the shadow that
was already there and the shadow the proposal adds -- which is the only thing
a consent authority is reading the sheet for.

Drawn as fills in the plan instead, both go away. ``core.shadow`` does the
attribution arithmetic, and what arrives here is already rings: outlines where
the shadow is solid, tiled rectangles where it has a sunlit courtyard in it.
This module's whole job is putting them into Archicad and building the sheets.

One layer to an hour, and a combination to match
------------------------------------------------
Twenty-one hours of shadow on one layer would be twenty-one overlapping fills
and one unreadable plan. They could go on twenty-one worksheets instead, which
is what the communal hourly plans do -- but a worksheet carries only what this
tool draws into it, and a shadow diagram is meaningless without the
neighbourhood under it. The reference sheets show roads, boundaries and every
neighbouring building; that context is the *model*, and the only way to keep
it under each drawing is to stay on the plan.

So each hour gets a layer of its own and a Layer Combination that shows that
layer and hides its twenty siblings. A View pinned to that combination is a
drawing of the site plan with exactly one hour's shadow on it. The legend and
the site boundary go on a shared layer that every combination shows, because
they are the same at every hour and twenty-one copies of a legend is twenty-one
things to fix when the pen table changes.

Nothing here deletes a layer or a combination. Neither can be deleted through
the add-on -- ``DeleteAttributes`` refuses a ``LayerCombination`` outright
(D63) -- so both are reused by name, and a second run over the same project
redraws into the layers the first one made.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from sun_study.archicad import naming
from sun_study.archicad.connection import ArchicadConnection, ArchicadError
from sun_study.archicad.draw import (
    DRAWING_MINIMUM_TAPIR_VERSION,
    BandStyle,
    clear_layer,
    create_elements,
    ensure_layer,
)
from sun_study.archicad.ids import (
    NOT_GROUPED,
    SHADOW,
    GroupReport,
    StampReport,
    fill_id,
    group_by_value,
    stamp_in_order,
)
from sun_study.archicad.layers import ensure_combination, read_layers
from sun_study.archicad.layout import (
    Filing,
    file_under_subset,
    layout_from_views,
    storey_items,
)
from sun_study.archicad.read import elements_by_ifc_ids
from sun_study.archicad.views import views_for_storeys
from sun_study.core.geometry import PlanTransform, fit_plan_transform
from sun_study.core.patches import Ring
from sun_study.core.shadow import (
    BASELINE,
    SCENARIO,
    ShadowSeries,
    SourceSpec,
)

__all__ = [
    "BASELINE_RAMP",
    "DEFAULT_PER_SHEET",
    "SCENARIO_RAMP",
    "ShadowDrawReport",
    "ShadowSheetReport",
    "build_shadow_sheets",
    "combination_name",
    "draw_shadow_series",
    "drawing_order",
    "fit_project_frame",
    "layer_name",
    "sheet_name",
    "styles_for",
]

#: The greys a baseline is drawn in, lightest first, and the blues a scenario
#: is drawn in, palest first.
#:
#: Not a guess and not a taste. These six are sampled straight out of the
#: legend of SSDA 401 -- the Campsie SSDA sheet -- so a run reproduces the
#: sheet the office already draws by hand rather than approximating it. The
#: split is the one the sheet itself makes: what will be there is grey and
#: recedes, what is being argued about is blue and comes forward, and within
#: each ramp the later entry is the stronger one because it is the newer claim.
#:
#: Both ramps repeat if a project names more sources than there are steps.
#: Six is what the busiest reference sheet needed; a seventh would cycle back
#: to the lightest, which is visibly wrong on the sheet and is meant to be --
#: the answer then is to pass a colour, not to have the tool invent one.
BASELINE_RAMP: tuple[tuple[int, int, int], ...] = (
    (235, 235, 235),
    (216, 216, 216),
    (158, 158, 158),
)
SCENARIO_RAMP: tuple[tuple[int, int, int], ...] = (
    (204, 216, 225),
    (168, 193, 205),
    (147, 174, 192),
)

#: Where the shadow pens start. A pen number means whatever one project's pen
#: table says it means, so these are meant to be overridden; the colours above
#: are what lets a pen be *matched* rather than picked, and
#: ``draw.match_pens`` is what does the matching.
FIRST_SHADOW_PEN = 93


def styles_for(sources: Sequence[SourceSpec]) -> dict[str, BandStyle]:
    """A style per source, coloured by role and by position within its role.

    Derived from the source list rather than written out, because the legend
    is now the project's to declare: a sheet with six rows and a sheet with
    two are the same code path, and a table of hard-coded categories could
    only ever draw the one it was written for.
    """
    styles: dict[str, BandStyle] = {}
    seen = {BASELINE: 0, SCENARIO: 0}
    for index, source in enumerate(sources):
        ramp = BASELINE_RAMP if source.role == BASELINE else SCENARIO_RAMP
        position = seen[source.role]
        seen[source.role] += 1
        styles[source.key] = BandStyle(
            source.label.upper(),
            float("inf"),
            fill_pen=FIRST_SHADOW_PEN + index,
            rgb=ramp[position % len(ramp)],
        )
    return styles


def drawing_order(sources: Sequence[SourceSpec]) -> tuple[str, ...]:
    """Back to front: every baseline, then every scenario.

    The baselines are the ground the rest is read against, so they go down
    first whatever order they were named in; the scenarios go on top in the
    order the project named them, which is the order its argument runs --
    what the controls allow, then what is being asked for.

    Scenarios genuinely do overlap (see ``core.shadow.SCENARIO``), so unlike
    the old three-fill drawing this order decides what a reader actually sees
    where two of them cover the same ground, not merely which hairline wins on
    a shared edge. Last named is on top, and that is the one being applied for.
    """
    return tuple(s.key for s in sources if s.role == BASELINE) + tuple(
        s.key for s in sources if s.role == SCENARIO
    )


#: How far the fitted rotation may sit from the one the project's own north
#: angle implies before the run says so. A north-aligned export is rotated by
#: exactly (90 degrees - the project's north bearing); the fit is done on
#: geometry instead, so the two agreeing is an independent check that the
#: right thing is being corrected for. A degree is far wider than the
#: bounding-box noise the fit carries and far narrower than any real mistake.
FRAME_ROTATION_TOLERANCE_DEG = 1.0


def fit_project_frame(
    connection: ArchicadConnection,
    samples: Sequence[tuple[str, float, float]],
    *,
    north_radians: float | None = None,
) -> tuple[PlanTransform | None, str]:
    """Fit the rotation and shift taking export coordinates into the project's.

    A shadow is computed where the exporter put the geometry and drawn where
    Archicad keeps it. With a Survey Point export those differ by the site's
    north angle -- 31.5 degrees on Crows Nest -- and a fill drawn without the
    correction is the right shape in the wrong place, which no one reading the
    sheet could catch.

    Matched on IFC GlobalId, one pair per element: its plan centre as the
    export sees it against the centre of its bounding box as Archicad reports
    it. A bounding box is axis-aligned in whichever frame it is measured, so a
    rotated object's box centre drifts slightly from its true centre and the
    fit carries a few hundred millimetres of noise. That is immaterial against
    a rotation -- errors that are not systematic average out over hundreds of
    pairs -- and the residual is reported so a caller can say so.

    Returns ``None`` rather than raising when the join cannot be made. A
    project that will not give up its bounding boxes should lose the
    correction and be told, not lose the drawing.
    """
    if not samples:
        return None, "no elements offered for fitting, so the drawing frame is unchecked"
    found = elements_by_ifc_ids(connection, [gid for gid, _, _ in samples])
    pairs = [(x, y, found[gid][0]) for gid, x, y in samples if found.get(gid)]
    if len(pairs) < 3:
        return None, (
            f"only {len(pairs)} of {len(samples)} elements could be matched back to "
            f"Archicad, which is too few to fit a rotation. The fills are drawn in the "
            f"export's own coordinates; if that export was north-aligned they will be "
            f"rotated off the project."
        )

    boxes: list[Any] = []
    for start in range(0, len(pairs), 500):
        boxes.extend(
            connection.run_tapir(
                "Get3DBoundingBoxes",
                {
                    "elements": [
                        {"elementId": {"guid": g}} for _, _, g in pairs[start : start + 500]
                    ]
                },
            )["boundingBoxes3D"]
        )

    source: list[list[float]] = []
    target: list[list[float]] = []
    for (x, y, _), entry in zip(pairs, boxes, strict=False):
        box = entry.get("boundingBox3D") if isinstance(entry, dict) else None
        if not box:
            continue
        source.append([x, y])
        target.append([(box["xMin"] + box["xMax"]) / 2.0, (box["yMin"] + box["yMax"]) / 2.0])
    if len(source) < 3:
        return None, "too few bounding boxes came back to fit a rotation"

    fitted = fit_plan_transform(np.array(source), np.array(target))
    turned = math.degrees(math.atan2(fitted.rotation[1, 0], fitted.rotation[0, 0]))
    note = (
        f"  drawing frame: rotated {turned:+.2f} deg, shifted "
        f"{fitted.offset[0]:+.2f}, {fitted.offset[1]:+.2f} m, from {len(source)} matched "
        f"elements (residual {fitted.rmse_m:.2f} m)"
    )
    if north_radians is not None:
        implied = 90.0 - math.degrees(north_radians)
        # Compared on the shorter way round, so +179 and -179 are not read as
        # two degrees apart when they are one.
        gap = abs((-implied - turned + 180.0) % 360.0 - 180.0)
        if gap > FRAME_ROTATION_TOLERANCE_DEG:
            note += (
                "\n"
                f"  WARNING: the project's north angle implies {-implied:+.2f} deg, "
                f"{gap:.2f} deg from what the geometry fits. One of the two is not "
                f"describing this export; check the fills against the model before "
                f"the sheet goes anywhere."
            )
    return fitted, note


def layer_name(label: str) -> str:
    """The layer one hour's shadow is drawn on.

    The hour is in the name because it is the only thing that separates the
    twenty-one, and because a colleague switching layers by hand in the Layer
    Settings dialog is reading this string and nothing else.
    """
    return naming.layer(f"Shadow {label}")


def combination_name(label: str) -> str:
    """The Layer Combination that shows exactly that hour."""
    return naming.named(f"Shadow {label}")


def shared_layer_name() -> str:
    """Where the legend and the site boundary live.

    One layer, shown by every hour's combination. They are identical at every
    instant, and twenty-one copies of a legend is twenty-one things to correct
    when somebody changes a pen.
    """
    return naming.layer("Shadow Legend")


@dataclass(frozen=True)
class ShadowDrawReport:
    """What was drawn, and everything a reader should be suspicious of."""

    fills: int
    sources: tuple[SourceSpec, ...]
    """The legend this run drew, in order. Needed to read ``areas_m2``."""

    layers: tuple[str, ...]
    combinations: tuple[str, ...]
    cleared: int
    below_horizon: tuple[str, ...]
    permanently_dark_share: float
    stamped: StampReport
    grouped: GroupReport
    areas_m2: dict[str, dict[str, float]]
    """Per instant label, per category. The numbers behind the colours.

    Written out because a shadow diagram is read by area -- "the proposal adds
    340 m2 at 9am" -- and a figure somebody has to measure off a drawing with
    a scale rule is a figure that will be measured wrongly.
    """

    def describe(self) -> str:
        lines = [
            f"drew {self.fills} shadow fills across {len(self.layers)} layers, "
            f"{len(self.combinations)} layer combinations"
        ]
        if self.cleared:
            lines.append(f"  cleared {self.cleared} fills from an earlier run")
        if self.below_horizon:
            lines.append(
                "  the sun is below the horizon at "
                + ", ".join(self.below_horizon)
                + " -- those sheets are blank, which is the honest drawing of an "
                "hour before sunrise or after sunset"
            )
        if self.permanently_dark_share > 0.5:
            lines.append(
                f"  WARNING: {self.permanently_dark_share * 100:.0f}% of the plane is "
                "shaded at every hour. That is what handing the site mesh in as "
                "context looks like -- the shadow is being cast onto a datum "
                "below the terrain. Take the terrain layer out of the context "
                "layers, or move the datum with --shadow-datum."
            )
        lines.append(self.stamped.describe())
        if self.grouped.describe():
            lines.append(self.grouped.describe())
        for label, areas in self.areas_m2.items():
            written = ", ".join(
                f"{source.key} {areas.get(source.key, 0.0):,.0f}" for source in self.sources
            )
            lines.append(f"  {label}: {written} m2")
        return "\n".join(lines)


@dataclass(frozen=True)
class ShadowSheetReport:
    """The views and layouts built from a drawn series."""

    views: tuple[str, ...]
    sheets: tuple[str, ...]
    drawings: int
    filing: Filing | None

    def describe(self) -> str:
        lines = [
            f"made {len(self.views)} views and {len(self.sheets)} sheets "
            f"carrying {self.drawings} drawings"
        ]
        lines.extend(f"  {name}" for name in self.sheets)
        if self.filing is not None:
            lines.append("  " + self.filing.describe())
        return "\n".join(lines)


def _fill(ring: Ring, style: BandStyle, layer_index: int, storey: int | None) -> dict[str, Any]:
    """One ring as a hatch on one layer.

    ``floorInd`` is sent only when there is a storey to send. A worksheet has
    none, and Tapir's own tracker records a floor index silently destroying
    elements in a database that has none -- so it is omitted rather than
    defaulted.
    """
    hatch: dict[str, Any] = {
        "coordinates": [{"x": x, "y": y} for x, y in ring],
        "layerIndex": layer_index,
        "fillPenIndex": style.fill_pen,
        "fillBackgroundPenIndex": style.background_pen,
        "contourPenIndex": style.outline_pen,
        # Explicitly off. A Fill inherits the Fill tool's current default, and
        # on a real project that default has "Show Area Text" on -- which
        # prints a square-metre figure across every rectangle of a tiled
        # shadow, and a shadow with a courtyard in it is tiled.
        "showArea": False,
    }
    if storey is not None:
        hatch["floorInd"] = storey
    return hatch


def _visibility(
    layers: Sequence[Any], showing: str, shared: str, mine: Sequence[str]
) -> dict[str, bool]:
    """Layer states for one hour's combination: guid -> hidden.

    Every layer of the project keeps whatever it is doing, so the site plan
    underneath still shows the neighbours, the roads and the boundary. Only
    the tool's own shadow layers are decided here: the hour being drawn on,
    every other hour off, and the shared legend on.

    Written as "leave everything else alone" rather than "show what the study
    needs" on purpose. What belongs on a site plan is the practice's decision
    and it is already recorded in whatever combination they draw site plans
    with; a combination built from scratch here would be this tool's opinion
    of somebody else's drawing.
    """
    ours = {name.casefold() for name in mine}
    states: dict[str, bool] = {}
    for layer in layers:
        name = str(layer.name).casefold()
        if name == showing.casefold() or name == shared.casefold():
            states[layer.identifier] = False
        elif name in ours:
            states[layer.identifier] = True
        else:
            states[layer.identifier] = bool(layer.hidden)
    return states


def draw_shadow_series(
    connection: ArchicadConnection,
    series: ShadowSeries,
    *,
    styles: Mapping[str, BandStyle] | None = None,
    storey_index: int | None = 0,
    boundary: Ring | None = None,
    boundary_pen: int = 1,
    transform: PlanTransform | None = None,
) -> ShadowDrawReport:
    """Draw every instant onto its own layer, and record a combination for each.

    ``storey_index`` is which storey the fills belong to; ``None`` omits the
    floor index entirely, which is what a worksheet needs and what a plan must
    not have omitted.

    ``boundary`` is the site outline, drawn once on the shared layer. It is a
    ring rather than a layer name because by the time the shadows have been
    computed the boundary is already known -- it is what the planning envelope
    was extruded from -- and reading it back out of Archicad a second time
    would be a second chance to read a different polygon.

    Every layer is cleared before it is drawn, so a rerun replaces its own work
    rather than stacking a second set of fills on the first. Nothing outside
    the tool's own layers is touched.
    """
    connection.require_tapir_at_least(
        DRAWING_MINIMUM_TAPIR_VERSION, "CreateHatches, which draws the shadow,"
    )
    if not series.instants:
        raise ArchicadError("No instants to draw.")

    def placed(ring: Ring) -> Ring:
        """One ring, moved from the export's frame into the project's.

        Every ring goes through here, so a run either corrects all of them or
        none. Half-corrected is the one outcome worth engineering against: the
        fills would still tile against each other and still look like a
        shadow, while sitting somewhere the model is not.
        """
        if transform is None:
            return ring
        moved = transform.apply(np.asarray(ring, dtype=np.float64))
        return tuple((float(x), float(y)) for x, y in moved)

    palette = styles_for(series.sources)
    palette.update(styles or {})
    order = drawing_order(series.sources)

    shared = ensure_layer(connection, shared_layer_name())
    wanted = [layer_name(instant.label) for instant in series.instants]

    fills: list[dict[str, Any]] = []
    #: One ID per fill, in the same order. The create command answers in the
    #: order it was given, and that order is the only thing linking a hatch
    #: back to the hour and the category it was drawn for.
    #: ``None`` for a fill that must never be totalled -- see the site
    #: boundary below.
    element_ids: list[str | None] = []
    cleared = 0
    made_layers: list[str] = []
    for instant in series.instants:
        name = layer_name(instant.label)
        layer = ensure_layer(connection, name)
        made_layers.append(name)
        cleared += clear_layer(connection, layer.index)
        for category in order:
            style = palette.get(category)
            if style is None:
                continue
            for ring in instant.regions.get(category, ()):
                fills.append(_fill(placed(ring), style, layer.index, storey_index))
                # The hour *and* the category, so one schedule breaks down by
                # both: how much shadow at 9am, and how much of it is ours.
                element_ids.append(fill_id(SHADOW, instant.caption, category.upper()))

    if boundary is not None:
        cleared += clear_layer(connection, shared.index)
        fills.append(
            _fill(
                placed(boundary),
                BandStyle("SITE BOUNDARY", float("inf"), fill_pen=boundary_pen, background_pen=0),
                shared.index,
                storey_index,
            )
        )
        # Deliberately *not* given an Element ID. A schedule that totals
        # every fill whose ID contains SHADOW would otherwise add the whole
        # site to the shadow area and report a number nobody could explain.
        # Only measured fills are tagged; the boundary is furniture.
        element_ids.append(None)

    # Batched: twenty-one hours of a tiled shadow is thousands of hatches, and
    # one request carrying all of them is a payload Archicad parses in one go.
    made: list[dict[str, Any]] = []
    for start in range(0, len(fills), 500):
        made.extend(
            create_elements(connection, "CreateHatches", "hatchesData", fills[start : start + 500])
        )

    # Positional pairing, and the boundary's ``None`` drops out there: see
    # ``ids.stamp_in_order`` for why a mismatch is refused rather than trimmed.
    stamped = stamp_in_order(connection, made, element_ids)
    # One group per hour *and* category, so a colleague can pick up all of
    # 9am's additional shadow in one click without disturbing the existing
    # shadow underneath it.
    grouped = (
        group_by_value(connection, list(zip(made, element_ids, strict=True)))
        if len(made) == len(element_ids)
        else NOT_GROUPED
    )

    # After the fills, so the combinations are recorded against layers that
    # certainly exist and carry the right visibility.
    project = read_layers(connection)
    combinations: list[str] = []
    for instant in series.instants:
        name = combination_name(instant.label)
        ensure_combination(
            connection,
            name,
            _visibility(project, layer_name(instant.label), shared.name, wanted),
        )
        combinations.append(name)

    return ShadowDrawReport(
        fills=len(fills),
        sources=series.sources,
        layers=tuple(made_layers),
        combinations=tuple(combinations),
        cleared=cleared,
        below_horizon=tuple(i.label for i in series.instants if i.below_horizon),
        permanently_dark_share=series.permanently_dark_share,
        stamped=stamped,
        grouped=grouped,
        areas_m2={instant.label: dict(instant.areas_m2) for instant in series.instants},
    )


def instant_captions(series: ShadowSeries) -> dict[str, str]:
    """What each hour's drawing is called on the sheet, keyed by its label.

    ``JUNE 21 -9AM``, in the reference sheets' own words including the space
    before the dash. Kept next to the drawing code rather than derived at the
    layout end so the caption on a sheet and the layer a colleague switches on
    can never disagree about which hour is which.
    """
    return {instant.label: instant.caption for instant in series.instants}


#: Drawings to a sheet. Four is what the reference sheets carry -- 9, 10, 11
#: and 12 on one, 1, 2 and 3 on the next -- and it is a real constraint rather
#: than a preference: a site plan at 1:1000 on an A1 sheet is about half the
#: page in each direction, so four fit and five do not.
DEFAULT_PER_SHEET = 4


def _by_date(series: ShadowSeries) -> dict[str, list[Any]]:
    """Instants grouped by the day they belong to, in the order given.

    A sheet is a day: *SHADOW DIAGRAMS - JUNE 21*. Mixing 21 June and 21
    December onto one page would be four drawings that cannot be compared with
    each other, which is the only thing a reader does with them.
    """
    days: dict[str, list[Any]] = {}
    for instant in series.instants:
        days.setdefault(instant.moment.strftime("%B %d").upper(), []).append(instant)
    return days


def sheet_name(date_label: str, part: int, parts: int) -> str:
    """What one sheet of shadow diagrams is called.

    Numbered only when there is more than one, because *SHADOW DIAGRAMS - JUNE
    21 (1 of 1)* is a sheet number nobody wants and a title block nobody wants
    to read.
    """
    stem = f"Shadow Diagrams - {date_label}"
    return naming.named(stem if parts == 1 else f"{stem} ({part} of {parts})")


def build_shadow_sheets(
    connection: ArchicadConnection,
    series: ShadowSeries,
    *,
    storey_index: int = 0,
    drawing_scale: float = 1000.0,
    master_layout: str | None = None,
    subset: str | None = None,
    per_sheet: int = DEFAULT_PER_SHEET,
    zoom: tuple[float, float, float, float] | None = None,
) -> ShadowSheetReport:
    """One View per hour, then one Layout per four of them, filed.

    The View is the piece that makes this work: it is a view of the *site
    plan*, pinned to that hour's Layer Combination, so the drawing carries the
    neighbours and the roads with exactly one hour's shadow over them. That is
    the whole reason the fills went onto the plan rather than into worksheets.

    ``zoom`` pins what the drawing covers. Worth passing: a view otherwise
    inherits whatever the storey happened to be zoomed to, so a sheet made on
    a Monday crops the site wherever somebody left the screen on Friday. The
    shadow grid's own extent is the right answer and the caller has it.

    Sheets are made per day and filed under ``subset``. A subset that does not
    exist is reported rather than created -- the Layout Book is the office's
    structure to organise, not this tool's (D66).
    """
    found, missing = storey_items(connection, [storey_index])
    if missing or storey_index not in found:
        raise ArchicadError(
            f"No storey with index {storey_index} in the Project Map, so there is no "
            f"site plan to draw the shadows on. Choose the storey the site plan is "
            f"drawn at with --shadow-storey."
        )
    storey = found[storey_index]

    made: list[tuple[str, str]] = []
    for instant in series.instants:
        views = views_for_storeys(
            connection,
            [storey],
            combination=combination_name(instant.label),
            suffix=f"Shadow {instant.label}",
            drawing_scale=drawing_scale,
            zoom=zoom,
        )
        made.extend((view.navigator_id, instant.caption) for view in views)

    sheets: list[str] = []
    placed = 0
    position = 0
    for date_label, instants in _by_date(series).items():
        chunks = [
            instants[start : start + per_sheet] for start in range(0, len(instants), per_sheet)
        ]
        for part, chunk in enumerate(chunks, start=1):
            on_this_sheet = made[position : position + len(chunk)]
            position += len(chunk)
            name = sheet_name(date_label, part, len(chunks))
            report = layout_from_views(
                connection,
                on_this_sheet,
                layout_name=name,
                scale=drawing_scale,
                master_layout=master_layout,
            )
            sheets.append(name)
            placed += report.drawings_placed

    filing = file_under_subset(connection, sheets, subset) if subset else None
    return ShadowSheetReport(
        views=tuple(name for _, name in made),
        sheets=tuple(sheets),
        drawings=placed,
        filing=filing,
    )
