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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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
from sun_study.archicad.views import views_for_storeys
from sun_study.core.patches import Ring
from sun_study.core.shadow import ADDITIONAL, ENVELOPE, EXISTING, ShadowSeries

__all__ = [
    "DEFAULT_PER_SHEET",
    "DEFAULT_STYLES",
    "ShadowDrawReport",
    "ShadowSheetReport",
    "build_shadow_sheets",
    "combination_name",
    "draw_shadow_series",
    "layer_name",
    "sheet_name",
]

#: The three fills, styled as the reference sheets style them. Pen indices are
#: a guess and are meant to be overridden -- a pen number means whatever one
#: project's pen table says it means -- but the colours are not a guess: they
#: are read off the office's own legend, so a pen can be *matched* to them
#: rather than picked. ``draw.match_pens`` is what does the matching.
DEFAULT_STYLES: dict[str, BandStyle] = {
    EXISTING: BandStyle(
        "EXISTING BUILDING SHADOWS",
        float("inf"),
        fill_pen=93,
        rgb=(166, 166, 166),
    ),
    ENVELOPE: BandStyle(
        "LEP ENVELOPE",
        float("inf"),
        fill_pen=94,
        rgb=(244, 204, 204),
    ),
    ADDITIONAL: BandStyle(
        "ADDITIONAL SHADOW CAST BY PROPOSED BUILDING",
        float("inf"),
        fill_pen=95,
        rgb=(164, 194, 217),
    ),
}

#: Drawing order, back to front. The existing shadow is the ground everything
#: else is read against, so it goes down first; the envelope next, because the
#: argument is "the proposal against what was allowed anyway"; the proposal's
#: own addition last, on top, because it is the thing being looked at.
#:
#: They do not in fact overlap -- ``core.shadow`` differences all three against
#: the same "before" so the fills abut -- but the order still decides what a
#: reader sees where two of them share an edge, and an arbitrary one would
#: change between runs.
ORDER = (EXISTING, ENVELOPE, ADDITIONAL)


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
            existing = areas.get(EXISTING, 0.0)
            added = areas.get(ADDITIONAL, 0.0)
            lines.append(f"  {label}: existing {existing:,.0f} m2, added {added:,.0f} m2")
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

    palette = dict(DEFAULT_STYLES)
    palette.update(styles or {})

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
        for category in ORDER:
            style = palette.get(category)
            if style is None:
                continue
            for ring in instant.regions.get(category, ()):
                fills.append(_fill(ring, style, layer.index, storey_index))
                # The hour *and* the category, so one schedule breaks down by
                # both: how much shadow at 9am, and how much of it is ours.
                element_ids.append(fill_id(SHADOW, instant.caption, category.upper()))

    if boundary is not None:
        cleared += clear_layer(connection, shared.index)
        fills.append(
            _fill(
                boundary,
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
