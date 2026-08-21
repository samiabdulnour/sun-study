"""Command line interface. No GUI, per brief section 3.

Every run prints the disclaimer, the resolved site and the ruleset before any
number, so the human can catch a wrong location, a wrong north or a wrong
threshold before reading a result rather than after quoting one.
"""

from __future__ import annotations

import collections
import contextlib
import datetime as dt
import re
import sys
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any

import numpy as np
import typer

from sun_study import __version__
from sun_study.archicad import naming
from sun_study.archicad.connection import (
    DEFAULT_PORT,
    PORT_RANGE,
    ArchicadConnection,
    ArchicadError,
    ArchicadNotRunningError,
    HttpTransport,
    activate,
    find_instances,
    where_archicad_actually_is,
)
from sun_study.archicad.draw import (
    DEFAULT_BANDS,
    DEFAULT_LAYER_NAME,
    BandStyle,
    Pen,
    draw_assessment,
    hidden_layers,
    indistinguishable_bands,
    match_pens,
    pen_table,
)
from sun_study.archicad.layers import export_state
from sun_study.archicad.layout import (
    DEFAULT_LAYOUT_SCALE,
    file_under_subset,
    layout_from_views,
    layout_results,
    layout_sheet,
    project_map,
)
from sun_study.archicad.model_bands import draw_model_bands, fit_to_project
from sun_study.archicad.penetration import (
    PATCH_STYLE,
    CellGroup,
    PlanInstant,
    draw_cell_groups,
    draw_penetration,
)
from sun_study.archicad.read import (
    ArchicadZone,
    classification_item_names,
    classification_items_of,
    clear_selection,
    cross_check_georeferencing,
    describe_connection,
    export_ifc,
    gdl_parameters,
    layer_names,
    library_objects,
    read_geo_location,
)
from sun_study.archicad.read import zones as read_zones
from sun_study.archicad.rooms import (
    DEFAULT_TOLERANCE_M,
    LIVING_ROOM_CODES,
    RoomMatch,
    is_living_room,
    match_rooms,
    room_labels,
    unknown_codes,
)
from sun_study.archicad.series import (
    FLOOR_STYLE,
    SUNLIT_STYLE,
    PatchRow,
    database_of,
    draw_patch_series,
    ensure_model_database,
    find_worksheet,
    restore_after,
)
from sun_study.archicad.sheets import TableRow, draw_table, straighten_and_tile
from sun_study.archicad.views import (
    VIEW_PREFIX,
    ModelSource,
    ensure_layer_combination,
    ensure_view_folder,
    next_view_folder,
    remove_previous,
    three_d_sources,
    tool_layers,
    views_for_sources,
    views_for_storeys,
)
from sun_study.archicad.write import (
    APARTMENT_PROPERTIES,
    NOT_ASSESSED_HOURS,
    PROPERTY_GROUP_NAME,
    ApartmentMatch,
    WriteReport,
    all_properties,
    default_property_value,
    delete_properties,
    diagnose_write_access,
    ensure_property_group,
    enum_values,
    init_properties,
    match_apartments,
    write_assessment,
)
from sun_study.core.analysis import (
    ZERO_TOLERANCE_MINUTES,
    band_by_area,
    cumulative_minutes,
    instant_weights,
    sunlit_matrix,
)
from sun_study.core.facade import face_panels
from sun_study.core.occlusion import Occluder
from sun_study.core.sampling import SamplePoints
from sun_study.core.solar import assessment_times, solar_position
from sun_study.disclaimer import DISCLAIMER, STATUS
from sun_study.ingest.ifc import GeoreferencingError, read_ifc
from sun_study.ingest.scene import (
    DEFAULT_MASSING_SPACING_M,
    MassingConfig,
    SceneConfig,
    SceneConfigError,
    massing_subject,
    open_ground_grid,
)
from sun_study.pipeline import (
    WEIGHTING_BY_RULESET,
    MassingResult,
    PipelineResult,
    run_assessment,
    run_massing,
)
from sun_study.report.bands_out import (
    build_massing_header,
    write_bands_csv,
    write_bands_json,
)
from sun_study.report.csv_out import write_csv
from sun_study.report.header import build_header
from sun_study.report.json_out import write_json
from sun_study.rules.ruleset import BUILTIN_RULESETS, RulesetError, load_ruleset

#: A sheet label that is a time of day. Those are shadow diagrams; the
#: banded and two-hour plans are not, and the two go to different subsets.
_CLOCK = re.compile(r"\d{1,2}:\d{2}")

#: What a run leaves behind, named so it files itself inside the office's
#: own numbering. One prefix, set in sun_study.archicad.naming.
FACADE_LAYER = naming.layer("Facade")
SHEET_NAME = naming.named("Sun Study")
LAYER_GROUP = naming.LAYER_GROUP

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Direct sunlight hours from an Archicad IFC export. " + STATUS,
)


def banner() -> None:
    typer.secho(f"sun-study {__version__}  --  {STATUS}", fg=typer.colors.YELLOW, bold=True)
    typer.secho(DISCLAIMER, fg=typer.colors.YELLOW)
    typer.echo("")


@app.command()
def info(
    ifc: Annotated[Path, typer.Argument(help="IFC file exported from Archicad.")],
    timezone: Annotated[
        str | None,
        typer.Option("--timezone", "-z", help="IANA timezone, e.g. Australia/Sydney."),
    ] = None,
) -> None:
    """Echo what the tool reads out of a model, without assessing anything.

    The fastest way to find out that a project has no north direction set.
    """
    banner()
    try:
        model = read_ifc(ifc)
    except GeoreferencingError as error:
        typer.secho(f"Georeferencing error: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from error

    typer.echo(model.describe(timezone))


@app.command("rulesets")
def list_rulesets() -> None:
    """List the built-in rulesets and the thresholds they carry."""
    banner()
    for name in BUILTIN_RULESETS:
        rules = load_ruleset(name)
        typer.secho(f"{rules.identifier}  {rules.title}", bold=True)
        typer.echo(f"  source: {rules.source.document} ({rules.source.publisher})")
        if rules.source.url:
            typer.echo(f"  url:    {rules.source.url}")
        for key, area in rules.areas.items():
            typer.echo(f"  area {key}: {area.minimum_sunlight_minutes:g} min -- {area.label}")
        typer.echo("")


@app.command()
def run(
    ifc: Annotated[Path, typer.Argument(help="IFC file exported from Archicad.")],
    timezone: Annotated[
        str, typer.Option("--timezone", "-z", help="IANA timezone, e.g. Australia/Sydney.")
    ],
    area: Annotated[
        str,
        typer.Option(
            "--area",
            help=(
                "Which geographic criterion applies. 'sydney_metro' is 2 hours; 'other' is 3 hours."
            ),
        ),
    ] = "sydney_metro",
    ruleset: Annotated[
        str, typer.Option("--ruleset", help="Built-in ruleset name, or a path to a YAML file.")
    ] = "nsw_adg",
    year: Annotated[
        int, typer.Option("--year", help="Which year's 21 June to assess. Fixed for repeatability.")
    ] = 2024,
    living_room: Annotated[
        list[str] | None,
        typer.Option(
            "--living-room",
            help=(
                "Zone name or category identifying a living room. Repeatable. "
                "Wrong values change the headline percentage, so this is always "
                "echoed in the output. Default: 'Living Room'."
            ),
        ),
    ] = None,
    livable_suffix: Annotated[
        str | None,
        typer.Option(
            "--livable-suffix",
            help=(
                "Openings whose ID ends with this are the living-room glazing, "
                "e.g. '_L'. Matches windows and doors. Use instead of "
                "--living-room where zones are placed per unit, not per room."
            ),
        ),
    ] = None,
    balcony: Annotated[
        list[str] | None,
        typer.Option("--balcony", help="Name prefix identifying private open space. Repeatable."),
    ] = None,
    apartment_zone_layer: Annotated[
        list[str] | None,
        typer.Option(
            "--apartment-zone-layer",
            help="Archicad layer whose zones are the apartments. Repeatable.",
        ),
    ] = None,
    apartment_zone_name: Annotated[
        list[str] | None,
        typer.Option(
            "--apartment-zone-name",
            help=(
                "Only zones with this name are apartments. Repeatable. Needed "
                "where a layer mixes dwellings and balconies -- one project has "
                "15 units named G08 and 20 balconies named BY together on "
                "'06 | Zone.Units', and a balcony counted as an apartment just "
                "looks like a flat with no living room."
            ),
        ),
    ] = None,
    open_space_zone_layer: Annotated[
        list[str] | None,
        typer.Option(
            "--open-space-zone-layer",
            help=(
                "Archicad layer whose zones are private open space. Repeatable. "
                "Takes precedence over --balcony."
            ),
        ),
    ] = None,
    open_space_zone_name: Annotated[
        list[str] | None,
        typer.Option(
            "--open-space-zone-name",
            help=(
                "Only zones with this name are private open space. Repeatable. "
                "Needed where one layer carries both, as '06 | Zone.Units' does "
                "with 15 units named G08 and 20 balconies named BY."
            ),
        ),
    ] = None,
    grid: Annotated[float, typer.Option("--grid", help="Sample grid spacing in metres.")] = 0.2,
    offset: Annotated[
        float,
        typer.Option("--offset", help="Outward sample offset from glazing, in metres."),
    ] = 0.05,
    context_radius: Annotated[
        float | None,
        typer.Option("--context-radius", help="Drop occluders beyond this many metres."),
    ] = None,
    exclude_above: Annotated[
        float | None,
        typer.Option(
            "--exclude-above",
            help=(
                "Drop geometry lying entirely above this height, in project "
                "metres. Hotlinked unit-type masters are parked overhead and "
                "export on the same layers as the real building, so nothing "
                "else separates them. The overhead warning reports the height "
                "to use."
            ),
        ),
    ] = None,
    csv_out: Annotated[
        Path | None, typer.Option("--csv", help="Write per-apartment results as CSV.")
    ] = None,
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Write per-apartment results as JSON.")
    ] = None,
) -> None:
    """Assess an IFC model's solar access and write the results."""
    banner()

    config = scene_config(
        timezone=timezone,
        living_room=living_room,
        livable_suffix=livable_suffix,
        balcony=balcony,
        apartment_zone_layer=apartment_zone_layer,
        apartment_zone_name=apartment_zone_name,
        open_space_zone_layer=open_space_zone_layer,
        open_space_zone_name=open_space_zone_name,
        grid=grid,
        offset=offset,
        context_radius=context_radius,
        exclude_above=exclude_above,
    )

    try:
        result = run_assessment(
            ifc, timezone=timezone, ruleset=ruleset, area=area, year=year, scene_config=config
        )
    except GeoreferencingError as error:
        typer.secho(f"Georeferencing error: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from error
    except SceneConfigError as error:
        typer.secho(f"Scene setting error: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from error
    except RulesetError as error:
        typer.secho(f"Ruleset error: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from error

    report_assessment(result, timezone=timezone, area=area, csv_out=csv_out, json_out=json_out)
    typer.echo("")
    typer.secho(DISCLAIMER, fg=typer.colors.YELLOW)


def resolve_bands(
    connection: ArchicadConnection, overrides: list[str] | None
) -> tuple[BandStyle, ...]:
    """Band styles pointed at real pens from the project's own pen table.

    A pen index means nothing outside the table it came from, so a hard-coded
    default is guaranteed wrong in somebody's project. The band *colours* are
    the part everyone already agrees on -- they come from the reference
    study's legend -- so the colour is the input and the pen is looked up.

    Explicit ``--pen`` overrides win, and are applied after matching so a
    person can correct one band without losing the rest.
    """
    # Validate first, so a typo fails before the pen table is read and its
    # mapping printed -- an error under a wall of output reads as noise.
    overridden = band_styles(overrides)
    try:
        pens = pen_table(connection)
    except ArchicadError as error:
        typer.secho(
            f"  could not read the pen table ({error}); using default pen indices, "
            f"which are a guess",
            fg=typer.colors.YELLOW,
        )
        return overridden

    styles, distances = match_pens(DEFAULT_BANDS, pens)
    typer.echo(f"  matched band colours against {len(pens)} pens in the project's pen table:")
    for band in styles:
        gap = distances.get(band.label, 0.0)
        quality = "exact" if gap < 12 else ("close" if gap < 60 else "POOR MATCH")
        typer.echo(
            f"    {band.label:<8} rgb{band.rgb} -> pen {band.fill_pen:<4} {quality}"
            + (f" (off by {gap:.0f})" if gap >= 12 else "")
        )
    if any(value >= 60 for value in distances.values()):
        typer.secho(
            "  A poor match means the pen table has no pen near that colour. "
            "Override it with --pen 'label=index'.",
            fg=typer.colors.YELLOW,
        )

    if not overrides:
        return _warn_if_alike(styles, pens)
    wanted = {band.label: band.fill_pen for band in overridden}
    corrected = tuple(
        replace(band, fill_pen=wanted.get(band.label, band.fill_pen)) for band in styles
    )
    return _warn_if_alike(corrected, pens)


def layer_of(zone: Any, names: dict[int, str]) -> str:
    """A zone's layer name, or a placeholder.

    Written out rather than inlined as ``names.get(zone.layer_index or -1)``,
    which was the original and is wrong: layer index 0 is a real Archicad
    layer, and ``0 or -1`` is -1, so every zone on it silently became
    "unknown".
    """
    if zone.layer_index is None:
        return "(no layer reported)"
    return names.get(zone.layer_index, "(unknown layer)")


def layer_matches(name: str, wanted: Sequence[str]) -> bool:
    """Whether a layer name is one the user asked for.

    Compared stripped as well as case-folded. Archicad layer names carry
    trailing spaces more often than anyone expects, and a padded listing hides
    them completely -- so a name copied character-perfect off the screen
    matched nothing, and the run reported a project with no apartments.
    """
    return name.strip().casefold() in {entry.strip().casefold() for entry in wanted}


def report_layout(
    connection: ArchicadConnection,
    storeys: Sequence[int],
    *,
    name: str,
    master: str | None = None,
    zoom: tuple[float, float, float, float] | None = None,
) -> None:
    """Put the drawn storeys on a sheet, and say what happened.

    Never fatal. The fills are already in the project by this point, and a
    sheet that could not be made is a worse reason to fail the run than to
    finish it with a note -- the numbers and the diagram are the deliverable,
    the layout is a convenience on top.
    """
    try:
        placed = layout_results(
            connection, storeys, layout_name=name, master_layout=master, zoom=zoom
        )
    except ArchicadError as error:
        typer.secho(
            f"  the fills are drawn, but the layout could not be made ({error})",
            fg=typer.colors.YELLOW,
        )
        return
    typer.secho(
        placed.describe(),
        fg=typer.colors.GREEN if placed.complete else typer.colors.YELLOW,
        bold=True,
    )

    # The same second pass the other sheets get. A Drawing's angle comes from
    # the Drawing tool's default -- the project's own north -- and its size is
    # only knowable once it exists; saving is what makes the layout readable.
    try:
        connection.run_tapir("SaveProject", {})
        sheet, _ = layout_sheet(connection, placed.database_id)
        typer.echo("  " + straighten_and_tile(connection, placed.database_id, sheet).describe())
    except ArchicadError as error:
        typer.secho(
            f"  the sheet is made; its drawings are still crooked: {error}",
            fg=typer.colors.YELLOW,
        )
    finally:
        # Finishing a sheet means standing in it, and a Layout has no Zones.
        # Whatever runs next reads the database that is current, so leaving
        # one in front makes the project look empty of apartments: the first
        # run to do this paired 0 of 10 flats and refused every plan drawing
        # after the sheet, several steps away from the cause.
        ensure_model_database(connection)


#: Coarser than the 200 mm assessment grid on purpose: the patch is drawn, not
#: quoted, and four times the rectangles buys nothing anybody can see on a sheet.
DEFAULT_PATCH_GRID_M = 0.25


def report_series(
    connection: ArchicadConnection,
    result: PipelineResult,
    *,
    worksheet_name: str,
    every: int,
    layer_name: str,
) -> bool:
    """Draw the per-instant series into a worksheet. True if anything is wrong.

    The worksheet is left *not* current whatever happens. A worksheet current
    at export time produces an IFC with no building in it, and that failure
    surfaces three steps later as a scene with no apartments -- the same trap
    a stray selection sets.
    """
    series = result.instants
    if series is None or series.floor_sunlit is None or series.floor_positions is None:
        typer.secho(
            "  no floor patch was computed, so there is no series to draw. Pass --patch-grid.",
            fg=typer.colors.RED,
            err=True,
        )
        return True

    home = None
    storeys = {i.storey_index: i for i in project_map(connection) if i.storey_index is not None}
    if storeys:
        home = database_of(connection, storeys[min(storeys)].identifier)

    try:
        target = find_worksheet(connection, worksheet_name)
        chosen = list(range(0, len(series.times), max(1, every)))
        captions = [f"{series.times[i]:%d %b %H:%M}" for i in chosen]

        floor, sunlit = series_styles(connection)
        rows = storey_rows(result)
        activate(connection, target.database_id, "Worksheet")
        drawn = draw_patch_series(
            connection,
            worksheet=target,
            positions=series.floor_positions,
            sunlit=series.floor_sunlit[:, chosen],
            times=captions,
            spacing_m=series.floor_spacing_m,
            layer_name=layer_name,
            rows=rows,
            floor_style=floor,
            sunlit_style=sunlit,
        )
    except ArchicadError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        return True
    finally:
        back = restore_after(connection, home) if home is not None else False

    typer.echo(drawn.describe())
    if not back:
        typer.secho(
            "  Archicad is left showing the worksheet, and on this version it "
            "cannot be switched back from here: every form of the command "
            "reports success and does nothing.\n"
            "  OPEN A FLOOR PLAN BEFORE THE NEXT RUN. The IFC export follows "
            "the window, not the database, so an export taken with a worksheet "
            "in front is 5.8 kB of empty project, and the run then fails much "
            "later as a scene with no apartments in it.",
            fg=typer.colors.YELLOW,
        )
    return False


def report_model_bands(
    connection: ArchicadConnection,
    result: MassingResult,
    *,
    config: MassingConfig,
    spacing_m: float,
    layer_name: str,
    pens: list[str] | None,
    flat_faces: bool,
    favorite: str | None = None,
) -> bool:
    """Colour the model itself: the facade banded by hours of sun.

    Page 2 of the reference study, made of Archicad elements rather than an
    image. The real walls cannot be recoloured -- no command in the add-on
    attaches a Surface to an existing wall -- so what is built is a skin of
    thin elements standing 30 mm proud of the facade, each in its band's
    colour, on a layer of its own. From outside it reads as the building
    painted; switch the layer off and the model is exactly as it was.

    The geometry is gridded a second time here, per planar face rather than
    per triangle, because a picture needs rectangles and a triangle soup has
    no rows to merge along. The areas it reports are therefore its own and
    will differ a little from the measured facade figure above -- the drawing
    grid drops faces narrower than one cell, which the measurement counts.
    """
    # The same reduction the measurement used, height cut included: without
    # it the picture colours geometry that is not in the denominator.
    reduced = massing_subject(result.model, config)
    panels = [
        panel
        for element in reduced.subject
        if element.mesh.triangle_count
        for panel in face_panels(
            element.mesh,
            element.global_id,
            spacing_m=spacing_m,
            surface_offset_m=config.surface_offset_m,
            vertical_tolerance_deg=config.vertical_tolerance_deg,
            horizontal=flat_faces,
        )
    ]
    if not panels:
        typer.secho(
            "  no upright faces to colour. Check --subject-layer: the skin is "
            "drawn on the scheme, not on the context.",
            fg=typer.colors.RED,
            err=True,
        )
        return True

    samples = SamplePoints.concatenate([panel.samples for panel in panels])
    flat = sum(1 for panel in panels if abs(float(panel.normal[2])) >= 0.5)
    typer.secho(
        f"  {len(panels)} faces ({len(panels) - flat} upright, {flat} flat), "
        f"{len(samples)} cells at {spacing_m:g} m "
        f"({samples.total_area_m2:.1f} m2 of surface)",
        bold=True,
    )

    times = assessment_times(
        result.ruleset.assessment.date_in(result.assessment_date.year),
        config.timezone,
        result.ruleset.assessment.start_time,
        result.ruleset.assessment.end_time,
        result.ruleset.assessment.timestep_minutes,
    )
    vectors = result.scene.orientation.sun_vectors(
        solar_position(times, result.model.latitude_deg, result.model.longitude_deg)
    )
    lit = sunlit_matrix(samples, Occluder(result.scene.occluders), vectors)
    minutes = cumulative_minutes(
        lit,
        instant_weights(
            lit.shape[1],
            float(result.ruleset.assessment.timestep_minutes),
            WEIGHTING_BY_RULESET[result.ruleset.assessment.weighting],
        ),
    )

    styles = resolve_bands(connection, pens)
    # Slice the durations back out per panel, in the order they were joined.
    per_panel: list[Any] = []
    start = 0
    for panel in panels:
        per_panel.append(minutes[start : start + len(panel)])
        start += len(panel)

    grouped: list[list[Any]] = []
    for index, style in enumerate(styles):
        lower = styles[index - 1].upper_minutes if index else 0.0
        chosen = [
            rectangle
            for panel, durations in zip(panels, per_panel, strict=True)
            for rectangle in panel.rectangles(
                _band_mask(durations, lower, style.upper_minutes)
            )
        ]
        grouped.append(chosen)

    try:
        # The export's frame is not the project's, and geometry created from
        # one and placed in the other lands beside the building and turned.
        transform = fit_to_project(connection, result.model)
        typer.echo(
            f"  fitted the export onto the project: {transform.rmse_m:.3f} m residual"
        )
        drawn = draw_model_bands(
            connection,
            bands=styles,
            rectangles=grouped,
            layer_name=layer_name,
            transform=transform,
            favorite=favorite,
        )
    except ArchicadError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        return True

    typer.echo(drawn.describe())
    if drawn.layer.invisible:
        typer.secho(
            f"  Layer {layer_name!r} is switched off in the combination you are "
            f"working in, so the skin is there but invisible.",
            fg=typer.colors.YELLOW,
        )
    return False


def report_model_views(
    connection: ArchicadConnection,
    *,
    layer_prefix: str,
    skin_layer: str,
    document_name: str | None,
    scale: float,
    also_hide: tuple[str, ...] = (),
    master_layout: str | None = None,
) -> bool:
    """Views of the coloured model -- the 3D window and a 3D Document -- on a sheet.

    Two of them because they are different things and an office wants both.
    The 3D window is live: it shows the model as it is now, turns freely, and
    is what somebody checks the study in. A 3D Document is a *drawing* made
    from a 3D view, with its own overrides and dimensions, and is what goes in
    the report.

    A 3D Document cannot be created through this add-on -- there is no command
    for it -- so this uses one the project already has. If there is none, the
    3D view is still made and the run says what is missing rather than failing.
    """
    sources = three_d_sources(connection)
    if not sources:
        typer.secho(
            "  the Project Map has no 3D window at all, which should not be "
            "possible; no views were made.",
            fg=typer.colors.RED,
            err=True,
        )
        return True

    live = next(
        (item for item in sources if item.kind == "AxonometryItem"),
        next((item for item in sources if item.kind == "PerspectiveItem"), None),
    )
    documents = [item for item in sources if item.kind == "DocumentFrom3DItem"]
    document = None
    if document_name:
        document = next(
            (item for item in documents if item.name.casefold() == document_name.casefold()),
            None,
        )
        if document is None:
            typer.secho(
                f"  no 3D Document named {document_name!r}. The project has "
                f"{len(documents)}: {', '.join(sorted({d.name for d in documents}))}",
                fg=typer.colors.YELLOW,
            )

    wanted: list[tuple[ModelSource, str]] = []
    if live is not None:
        wanted.append((live, f"{VIEW_PREFIX} Solar Model 3D"))
    if document is not None:
        wanted.append((document, f"{VIEW_PREFIX} Solar Model Document"))
    if not wanted:
        return True

    # Everything the project normally shows, plus the skin, minus this tool's
    # own 2D layers -- those are floor plan fills and would only clutter a
    # model view if they were ever drawn in 3D.
    combination = ensure_layer_combination(
        connection,
        f"{VIEW_PREFIX} Solar Model",
        show=[skin_layer],
        hide=[
            *(name for name in tool_layers(connection, layer_prefix) if name != skin_layer),
            *also_hide,
        ],
    )

    folder = ensure_view_folder(connection, f"{VIEW_PREFIX} Solar Model")
    typer.echo(f"  views go in the {VIEW_PREFIX} Solar Model folder ({folder[:8]})")
    try:
        views = views_for_sources(
            connection,
            wanted,
            combination=combination,
            folder=f"{VIEW_PREFIX} Solar Model",
            drawing_scale=scale,
        )
    except ArchicadError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        return True

    for view, (source, _) in zip(views, wanted, strict=True):
        typer.echo(f"    {view.name}  <- {source.kind} {source.name!r}")

    try:
        placed = layout_from_views(
            connection,
            [(view.navigator_id, view.name) for view in views],
            layout_name=f"{VIEW_PREFIX} Solar Model",
            scale=scale,
            master_layout=master_layout,
        )
    except ArchicadError as error:
        typer.secho(
            f"  the views are made; the sheet is not: {error}",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return False

    typer.echo(placed.describe())

    # Same second pass the plan sheets need, and for the same reason: a
    # Drawing's angle comes from the Drawing tool's default -- here the
    # project's own north, 279.9 degrees -- and its size is only knowable
    # once it exists. Saving is what makes the new layout readable at all.
    try:
        connection.run_tapir("SaveProject", {})
        sheet, _ = layout_sheet(connection, placed.database_id)
        typer.echo("    " + straighten_and_tile(connection, placed.database_id, sheet).describe())
    except ArchicadError as error:
        typer.secho(
            f"  the sheet is made; its drawings are still crooked: {error}",
            fg=typer.colors.YELLOW,
            err=True,
        )
    finally:
        # A Layout is left current by finishing one, and a Layout has no
        # model in it. Put the floor plan back for whatever reads next.
        ensure_model_database(connection)
    return False


def report_ground(
    connection: ArchicadConnection,
    result: PipelineResult,
    *,
    level_m: float,
    match: ApartmentMatch,
    layer_prefix: str,
    spacing_m: float,
    margin_m: float = 15.0,
) -> bool:
    """Solar access to the open ground, banded, drawn and tabulated.

    The other half of the reference study's summary sheets: the apartments
    answer the ADG, and this answers what the public domain and the communal
    open space get. Gridded around the assessed building at a stated height,
    with anything standing on it removed by firing a ray upward -- ground
    under a slab is not open ground.

    The height has to be given. Taken from the geometry it would be the lowest
    thing in the model, which on a developed project is a basement slab: a
    ground plane under the building, in shadow all day by construction, which
    is exactly what a first run reported.
    """
    subject = [
        element
        for apartment in match.by_apartment
        if (element := result.model.by_id(apartment)) is not None
    ]
    if not subject:
        typer.secho("  no apartments to grid the ground around", fg=typer.colors.RED, err=True)
        return True

    ground = open_ground_grid(
        subject,
        result.scene.occluders,
        MassingConfig(
            timezone=result.scene.config.timezone,
            ground_level_m=level_m,
            ground_spacing_m=spacing_m,
            ground_margin_m=margin_m,
        ),
    )
    if not len(ground):
        typer.secho(
            f"  no open ground at {level_m:g} m: every sample had something "
            f"directly above it. Check the height against the site.",
            fg=typer.colors.RED,
            err=True,
        )
        return True

    times = assessment_times(
        result.ruleset.assessment.date_in(result.assessment_date.year),
        result.scene.config.timezone,
        result.ruleset.assessment.start_time,
        result.ruleset.assessment.end_time,
        result.ruleset.assessment.timestep_minutes,
    )
    vectors = result.scene.orientation.sun_vectors(
        solar_position(times, result.model.latitude_deg, result.model.longitude_deg)
    )
    lit = sunlit_matrix(ground, Occluder(result.scene.occluders), vectors)
    minutes = cumulative_minutes(
        lit,
        instant_weights(
            lit.shape[1],
            float(result.ruleset.assessment.timestep_minutes),
            WEIGHTING_BY_RULESET[result.ruleset.assessment.weighting],
        ),
    )

    table = band_by_area(ground, minutes)
    typer.secho(
        f"  open ground at {level_m:g} m: {table.total_area_m2:.1f} m2 in {len(ground)} samples",
        bold=True,
    )
    for band in table.bands:
        typer.echo(f"    {band.label:<9} {band.area_m2:9.2f} m2   {band.share:6.2%}")
    typer.echo(f"    {table.summary()}")

    styles = resolve_bands(connection, None)
    groups = [
        CellGroup(
            label=band.label,
            mask=_band_mask(minutes, band.lower_minutes, band.upper_minutes),
            style=style,
            area_m2=band.area_m2,
            share=band.share,
        )
        for band, style in zip(table.bands, styles, strict=False)
    ]

    storey = _storey_at(connection, level_m)
    try:
        drawn = draw_cell_groups(
            connection,
            groups=groups,
            positions=ground.positions,
            parent_ids=ground.parent_ids,
            spacing_m=spacing_m,
            zone_by_apartment=match.by_apartment,
            zones=read_zones(connection),
            export_extents={
                apartment: element.mesh.vertices
                for apartment in match.by_apartment
                if (element := result.model.by_id(apartment)) is not None
            },
            layer_name=f"{layer_prefix} Ground",
            title=f"Solar access to open ground at {level_m:g} m",
            on_storey=storey,
        )
    except ArchicadError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        return True

    typer.echo(drawn.describe())
    return not drawn.complete


def _storey_at(connection: ArchicadConnection, level_m: float) -> int | None:
    """The storey whose level is nearest a height, for a fill to live on.

    Ground is not on a storey in any real sense, but a 2D fill has to be: an
    Archicad Fill belongs to one, and a plan of any other shows nothing.
    """
    try:
        stories = connection.run_tapir("GetStories", {})
    except ArchicadError:
        return None
    rows = stories.get("stories") if isinstance(stories, dict) else None
    if not isinstance(rows, list) or not rows:
        return None
    best = min(
        (row for row in rows if isinstance(row, dict) and "level" in row),
        key=lambda row: abs(float(row["level"]) - level_m),
        default=None,
    )
    return int(best["index"]) if best is not None and "index" in best else None


def report_area_bands(
    connection: ArchicadConnection,
    result: PipelineResult,
    *,
    match: ApartmentMatch,
    layer_prefix: str,
    bands: bool,
    hours: float | None,
    csv_out: Path | None = None,
    sheets: bool = False,
    master_layout: str | None = None,
    also_hide: Sequence[str] = (),
    storeys: Sequence[int] = (),
    zoom: tuple[float, float, float, float] | None = None,
    folder: str = "",
    instant_subset: str = "",
    analysis_subset: str = "",
) -> bool:
    """The whole-day area figures: printed, drawn, and written out.

    Two tables, inside and outside, because the ADG asks separately about the
    living room and the private open space and a single figure over both
    answers neither. The bands are the ones the reference study uses.
    """
    series = result.instants
    if series is None or series.floor_minutes is None or series.floor_positions is None:
        typer.secho(
            "  no floor patch was computed, so there are no areas to band. Pass --patch-grid.",
            fg=typer.colors.RED,
            err=True,
        )
        return True

    samples = result.scene.floor_samples
    if samples is None:
        return True
    outside = (
        series.floor_is_open_space
        if series.floor_is_open_space is not None
        else np.zeros(len(series.floor_minutes), dtype=bool)
    )

    tables = {
        "inside (rooms)": band_by_area(samples, series.floor_minutes, mask=~outside),
        "outside (open space)": band_by_area(samples, series.floor_minutes, mask=outside),
    }
    for where, table in tables.items():
        typer.secho(f"  {where}: {table.total_area_m2:.1f} m2 of floor", bold=True)
        for band in table.bands:
            typer.echo(f"    {band.label:<9} {band.area_m2:9.2f} m2   {band.share:6.2%}")
        typer.echo(f"    {table.summary()}")

    if csv_out:
        destination = csv_out.with_name(f"{csv_out.stem}-areas.csv")
        _write_area_csv(destination, tables)
        typer.echo(f"  wrote {destination}")

    styles = resolve_bands(connection, None)
    extents = {
        apartment: element.mesh.vertices
        for apartment in match.by_apartment
        if (element := result.model.by_id(apartment)) is not None
    }
    shared: dict[str, Any] = {
        "positions": series.floor_positions,
        "parent_ids": series.floor_parent_ids,
        "spacing_m": series.floor_spacing_m,
        "zone_by_apartment": match.by_apartment,
        "zones": read_zones(connection),
        "export_extents": extents,
    }

    problem = False
    sheet_tables: dict[str, Sequence[TableRow]] = {}
    sheet_titles: dict[str, str] = {}
    if bands:
        whole = band_by_area(samples, series.floor_minutes)
        # The same figures the console prints, on the sheet where a reader
        # looks for them rather than in a terminal nobody keeps.
        sheet_tables["Bands"] = [
            row for where, table in tables.items() for row in _table_rows(where, table, styles)
        ]
        sheet_titles["Bands"] = (
            f"Direct sun on the floor, {result.assessment_date:%d %B}, hours by area"
        )
        groups = [
            CellGroup(
                label=band.label,
                mask=_band_mask(series.floor_minutes, band.lower_minutes, band.upper_minutes),
                style=style,
                area_m2=band.area_m2,
                share=band.share,
            )
            for band, style in zip(whole.bands, styles, strict=False)
        ]
        problem |= _draw_groups(
            connection,
            groups,
            shared,
            layer_name=f"{layer_prefix} Bands",
            title=f"Direct sun on the floor, {result.assessment_date:%d %b}",
        )

    if hours:
        minimum = hours * 60.0
        achieved = series.floor_minutes >= minimum
        area = float(samples.areas[achieved].sum())
        total = float(samples.areas.sum())
        typer.secho(
            f"  {area:.1f} m2 of {total:.1f} receives at least {hours:g} hours "
            f"({area / total if total else 0:.1%})",
            bold=True,
        )
        groups = [
            CellGroup(
                label=f"{hours:g} hrs or more",
                mask=achieved,
                style=styles[-1],
                area_m2=area,
                share=area / total if total else 0.0,
            ),
            CellGroup(
                label=f"under {hours:g} hrs",
                mask=~achieved,
                style=styles[0],
                area_m2=total - area,
                share=(total - area) / total if total else 0.0,
            ),
        ]
        sheet_tables[f"{hours:g}h"] = [
            TableRow(
                f"{hours:g} hrs or more",
                area,
                area / total if total else 0.0,
                fill_pen=styles[-1].fill_pen,
            ),
            TableRow(
                f"under {hours:g} hrs",
                total - area,
                (total - area) / total if total else 0.0,
                fill_pen=styles[0].fill_pen,
            ),
            TableRow("all floor", total, 1.0),
        ]
        sheet_titles[f"{hours:g}h"] = (
            f"Floor receiving {hours:g} hours or more, {result.assessment_date:%d %B}"
        )
        problem |= _draw_groups(
            connection,
            groups,
            shared,
            layer_name=f"{layer_prefix} {hours:g}h",
            title=f"Floor receiving {hours:g} hours or more, {result.assessment_date:%d %b}",
        )

    if sheets and (bands or hours):
        made = [("Bands", f"{layer_prefix} Bands")] if bands else []
        if hours:
            made.append((f"{hours:g}h", f"{layer_prefix} {hours:g}h"))
        _sheet_per_instant(
            connection,
            labels=[label for label, _ in made],
            layers=[layer for _, layer in made],
            storeys=storeys,
            layer_prefix=layer_prefix,
            master_layout=master_layout,
            zoom=zoom,
            tables=sheet_tables,
            titles=sheet_titles,
            # Coarser than the instant sheets: these carry a legend beside the
            # plan, so the tile is wider and six of them will not fit an A1 at
            # 1:200.
            scale=300.0,
            folder=folder,
            also_hide=also_hide,
            instant_subset=instant_subset,
            analysis_subset=analysis_subset,
        )
    return problem


def _table_rows(where: str, table: Any, styles: Sequence[BandStyle]) -> list[TableRow]:
    """One surface's figures as table lines: a heading, the bands, the roll-up."""
    rows = [TableRow(where.upper(), table.total_area_m2, 1.0)]
    for band, style in zip(table.bands, styles, strict=False):
        rows.append(TableRow(band.label, band.area_m2, band.share, fill_pen=style.fill_pen))
    hours = table.threshold_minutes / 60.0
    rows.append(
        TableRow(
            f">{hours:g}hrs",
            table.at_or_above_threshold_m2,
            table.at_or_above_threshold_share,
        )
    )
    return rows


def _band_mask(minutes: Any, lower: float, upper: float | None) -> Any:
    """Cells whose duration falls in one band, half-open above an exact zero.

    upper is None on the open-ended top band, which is the one that has to
    catch everything above its floor rather than nothing.

    The no-sun band is recognised by an upper bound at or below the zero
    tolerance, not by an exact zero. ``band_by_area`` describes that band with
    an upper bound of 0 and ``BandStyle`` describes it with 1e-9, and reading
    only the first spelling silently produces an empty band: on the reference
    facade that hid 5,885 m2 of wall that never sees the sun -- the majority
    of the elevation -- while every other band looked right.
    """
    if upper is not None and upper <= ZERO_TOLERANCE_MINUTES:
        return minutes <= ZERO_TOLERANCE_MINUTES
    above = (minutes > ZERO_TOLERANCE_MINUTES) & (minutes >= lower)
    return above if upper is None else above & (minutes < upper)


def _draw_groups(
    connection: ArchicadConnection,
    groups: Sequence[CellGroup],
    shared: dict[str, Any],
    *,
    layer_name: str,
    title: str,
) -> bool:
    try:
        drawn = draw_cell_groups(
            connection, groups=groups, layer_name=layer_name, title=title, **shared
        )
    except ArchicadError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        return True
    typer.echo(drawn.describe())
    return not drawn.complete


def _write_area_csv(destination: Path, tables: dict[str, Any]) -> None:
    """One row per band per surface, so the figures can be checked outside."""
    import csv as _csv

    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = _csv.writer(handle)
        writer.writerow(["surface", "band", "area_m2", "share"])
        for where, table in tables.items():
            for band in table.bands:
                writer.writerow([where, band.label, f"{band.area_m2:.3f}", f"{band.share:.5f}"])
            writer.writerow([where, "total", f"{table.total_area_m2:.3f}", "1.0"])


def instant_key(result: PipelineResult, instant: int, label: str) -> list[TableRow]:
    """The key for one moment's sheet: what the colour means, and how much.

    A band table would be meaningless here -- a single instant has no bands --
    but a sheet with a coloured patch and nothing saying what it is, or how
    much floor it covers, leaves the reader to guess at both.
    """
    series = result.instants
    samples = result.scene.floor_samples
    if series is None or samples is None or series.floor_sunlit is None:
        return []

    outside = (
        series.floor_is_open_space
        if series.floor_is_open_space is not None
        else np.zeros(len(samples.areas), dtype=bool)
    )
    lit = series.floor_sunlit[:, instant]
    rows: list[TableRow] = []
    for where, mask in (("rooms", ~outside), ("open space", outside)):
        total = float(samples.areas[mask].sum())
        area = float(samples.areas[mask & lit].sum())
        rows.append(
            TableRow(where, area, area / total if total else 0.0, fill_pen=PATCH_STYLE.fill_pen)
        )
    whole = float(samples.areas.sum())
    everything = float(samples.areas[lit].sum())
    rows.append(TableRow("all floor", everything, everything / whole if whole else 0.0))
    return rows


def annotation_for(result: PipelineResult, instant: int) -> dict[str, list[str]]:
    """The text block against each apartment, in the reference drawing's form.

    Three lines: how much of the dwelling floor is in sun at this instant, how
    much of its private open space is, and the day's verdict.

    The first line is deliberately *not* called "Living". The office's own
    drawing says Living because its model has a living-room zone to measure;
    this one has a Zone per dwelling and rooms that are only label objects, so
    the honest figure is the whole floor of the flat. Printing that under the
    other name would overstate the room the ADG actually asks about.
    """
    series = result.instants
    if series is None:
        return {}
    verdicts = {a.apartment_id: a.meets_minimum for a in result.assessment.apartments}
    areas = series.lit_areas_at(instant)

    blocks: dict[str, list[str]] = {}
    for apartment, verdict in verdicts.items():
        room, open_space = areas.get(apartment, (0.0, 0.0))
        blocks[apartment] = [
            f"Sunlit floor {room:.2f} m\u00b2",
            f"P.O.S. {open_space:.2f} m\u00b2",
            "Achieved" if verdict else "Not Achieved",
        ]
    return blocks


def report_penetration(
    connection: ArchicadConnection,
    result: PipelineResult,
    *,
    wanted: Sequence[str],
    match: ApartmentMatch,
    layer_prefix: str,
    sheets: bool,
    master_layout: str | None,
    folder: str = "",
    also_hide: Sequence[str] = (),
    instant_subset: str = "",
    analysis_subset: str = "",
) -> bool:
    """Draw the study diagram on the floor plan. True if anything is wrong."""
    series = result.instants
    if series is None or series.floor_sunlit is None or series.floor_positions is None:
        typer.secho(
            "  no floor patch was computed, so there is nothing to draw. Pass --patch-grid.",
            fg=typer.colors.RED,
            err=True,
        )
        return True

    clock = [f"{stamp:%H:%M}" for stamp in series.times]
    chosen: list[PlanInstant] = []
    missing: list[str] = []
    for asked in wanted:
        tidy = asked.strip()
        if tidy in clock:
            index = clock.index(tidy)
            chosen.append(PlanInstant(label=tidy, lit=series.floor_sunlit[:, index]))
        else:
            missing.append(tidy)

    if missing:
        typer.secho(
            f"  no instant at {', '.join(missing)}. The run holds "
            f"{clock[0]} to {clock[-1]} every "
            f"{result.ruleset.assessment.timestep_minutes} minutes.",
            fg=typer.colors.RED,
            err=True,
        )
        return True

    try:
        drawn = draw_penetration(
            connection,
            instants=chosen,
            positions=series.floor_positions,
            parent_ids=series.floor_parent_ids,
            spacing_m=series.floor_spacing_m,
            zone_by_apartment=match.by_apartment,
            zones=read_zones(connection),
            # The dwelling's own extent, not its floor cells: those include
            # the balcony and pull the centre off by a different amount for
            # every flat. See penetration.fit_to_plan.
            export_extents={
                apartment: element.mesh.vertices
                for apartment in match.by_apartment
                if (element := result.model.by_id(apartment)) is not None
            },
            annotations=annotation_for(result, clock.index(chosen[0].label)),
            layer_prefix=layer_prefix,
        )
    except ArchicadError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        return True

    typer.echo(drawn.describe())
    if sheets:
        clock = [f"{stamp:%H:%M}" for stamp in series.times]
        _sheet_per_instant(
            connection,
            labels=[instant.label for instant in chosen],
            storeys=drawn.storeys,
            layer_prefix=layer_prefix,
            master_layout=master_layout,
            zoom=assessed_extent(read_zones(connection), match, margin_m=10.0),
            tables={
                instant.label: instant_key(result, clock.index(instant.label), instant.label)
                for instant in chosen
            },
            also_hide=also_hide,
            titles={
                instant.label: (
                    f"Direct sun on the floor at {instant.label}, {result.assessment_date:%d %B}"
                )
                for instant in chosen
            },
            folder=folder,
            instant_subset=instant_subset,
            analysis_subset=analysis_subset,
        )
    return not drawn.complete


def assessed_extent(
    zones: Sequence[ArchicadZone], match: ApartmentMatch, margin_m: float = 8.0
) -> tuple[float, float, float, float] | None:
    """The plan extent of the apartments being assessed, with a margin.

    Pinned onto every view this makes, because a view inherits whatever the
    storey was last zoomed to -- so a drawing taken from it crops the building
    wherever somebody happened to leave the screen, and the sheet quietly
    shows half a plan.
    """
    wanted = set(match.by_apartment.values())
    outlines = [zone.outline for zone in zones if zone.guid in wanted and zone.outline]
    if not outlines:
        return None
    points = [point for outline in outlines for point in outline]
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return (
        min(xs) - margin_m,
        min(ys) - margin_m,
        max(xs) + margin_m,
        max(ys) + margin_m,
    )


def _sheet_per_instant(
    connection: ArchicadConnection,
    *,
    labels: Sequence[str],
    storeys: Sequence[int],
    layer_prefix: str,
    master_layout: str | None,
    zoom: tuple[float, float, float, float] | None = None,
    scale: float = DEFAULT_LAYOUT_SCALE,
    layers: Sequence[str] | None = None,
    tables: dict[str, Sequence[TableRow]] | None = None,
    titles: dict[str, str] | None = None,
    folder: str = "",
    also_hide: Sequence[str] = (),
    instant_subset: str = "",
    analysis_subset: str = "",
) -> None:
    """A layer combination, a set of views and a Layout for each instant.

    ``folder`` and the clearing out of the last run both belong to the *run*,
    not to this call: a study makes two sets of sheets and each of them
    calling ``remove_previous`` would delete what the other had just made,
    while each choosing its own folder scattered one run's views across two.


    Without the combination the sheet shows the plan and none of the study: a
    Drawing inherits its View's layer combination, and the layer this tool
    just made is hidden in every combination the project already had. That is
    the whole reason this exists rather than reusing the storey views.
    """
    # One layer per label by default -- the instants -- or an explicit list,
    # which is how the whole-day sheets get theirs.
    every = list(layers) if layers else [f"{layer_prefix} {label}" for label in labels]
    # And every *other* layer this tool owns, whether or not this call knows
    # about it: a combination that names only its own group leaves the rest
    # showing, which put the 2-hour and banded plans on top of the 09:00 sheet.
    mine_all = set(tool_layers(connection, layer_prefix)) | set(every)
    # Plus whatever the run asked to be switched off -- the project's own
    # annotation layers, which are not this tool's to own but do clutter a
    # study drawing. Named per run rather than guessed at: what counts as
    # noise on a sun study is a decision about the drawing, not about the
    # layer's name.
    extra_hidden = sorted(set(also_hide))
    # A fresh, numbered folder each run: neither a view nor a folder can be
    # deleted through the API, so reusing one would mix this run's views with
    # the last run's inside it.
    # Cleaning is once per *run*, not once per call: a second call clearing up
    # again would delete the sheets the first one had just made, which is
    # exactly what happened when the whole-day sheets were added beside the
    # instants.
    finished: list[tuple[str, str]] = []
    items = {item.storey_index: item for item in project_map(connection)}
    wanted = [items[storey] for storey in storeys if storey in items]
    if not wanted:
        typer.secho("  no Project Map storey to make a view of", fg=typer.colors.YELLOW)
        return

    for label, mine in zip(labels, every, strict=True):
        try:
            combination = ensure_layer_combination(
                connection,
                f"{VIEW_PREFIX} Sun Study {label}",
                show=[mine],
                hide=[*sorted(mine_all - {mine}), *extra_hidden],
            )
            views = views_for_storeys(
                connection,
                wanted,
                combination=combination,
                suffix=label,
                drawing_scale=scale,
                zoom=zoom,
                folder=folder,
            )
            placed = layout_from_views(
                connection,
                [(view.navigator_id, view.name) for view in views],
                layout_name=f"{VIEW_PREFIX} Sun Study {label}",
                scale=scale,
                master_layout=master_layout,
            )
        except ArchicadError as error:
            typer.secho(f"  {label}: {error}", fg=typer.colors.RED, err=True)
            continue
        typer.echo(f"  {label}: {placed.describe().splitlines()[0]}")
        typer.echo(f"    views pinned to layer combination {combination!r}")
        finished.append((label, placed.database_id))

    if not finished:
        return

    # Saving is what makes a layout readable, and everything below reads it:
    # a drawing's angle and size are only knowable once it exists. See D39.
    connection.run_tapir("SaveProject", {})
    for label, database_id in finished:
        try:
            sheet, _ = layout_sheet(connection, database_id)
            pass_over = straighten_and_tile(connection, database_id, sheet)
            rows = (tables or {}).get(label)
            drawn = (
                draw_table(
                    connection, database_id, title=(titles or {}).get(label, label), rows=rows
                )
                if rows
                else 0
            )
        except ArchicadError as error:
            typer.secho(f"  {label}: {error}", fg=typer.colors.YELLOW, err=True)
            continue
        typer.echo(
            "    "
            + pass_over.describe()
            + (f", table of {drawn} rows on the sheet" if drawn else "")
        )
    # File the sheets where the practice keeps this kind of drawing, rather
    # than at the root of a book of 299 layouts. Split by what the sheet is:
    # a clock time is a shadow diagram, everything else -- the banded plan,
    # the two-hour plan, the facade -- is an ADG diagram.
    for subset, chosen in (
        (instant_subset, [label for label, _ in finished if _CLOCK.fullmatch(label)]),
        (analysis_subset, [label for label, _ in finished if not _CLOCK.fullmatch(label)]),
    ):
        if not subset or not chosen:
            continue
        try:
            filed = file_under_subset(
                connection, [f"{VIEW_PREFIX} Sun Study {label}" for label in chosen], subset
            )
        except ArchicadError as error:
            typer.secho(f"  {error}", fg=typer.colors.YELLOW, err=True)
            continue
        typer.secho(
            filed.describe(),
            fg=typer.colors.YELLOW if filed.no_such_subset else None,
        )

    ensure_model_database(connection)
    connection.run_tapir("SaveProject", {})


def storey_rows(result: PipelineResult) -> list[PatchRow]:
    """Split the floor cells by storey, lowest first.

    A worksheet has no storeys of its own, so without this every level of the
    building is drawn at its own coordinates and lands on top of the others --
    one tile showing a composite of the whole tower, which is a plan of
    nothing. The storey comes off the ``IfcSpace`` each cell belongs to, which
    is the export's own answer rather than one inferred from height.
    """
    series = result.instants
    if series is None or series.floor_positions is None:
        return []

    parents = np.array(series.floor_parent_ids)
    # Grouped by the floor's own elevation, not by the storey the export
    # names: on the reference project every IfcSpace comes through with no
    # storey at all, and the whole tower then draws as one composite tile. The
    # geometry always knows what level it is on.
    level_of: dict[str, float] = {}
    named: dict[float, str] = {}
    for apartment in dict.fromkeys(series.floor_parent_ids):
        cells = series.floor_positions[parents == apartment]
        if not len(cells):
            continue
        level = round(float(cells[:, 2].min()), 1)
        level_of[apartment] = level
        element = result.model.by_id(apartment)
        storey = element.storey if element is not None else None
        if storey:
            named.setdefault(level, storey)

    rows: list[PatchRow] = []
    for level in sorted(set(level_of.values()), reverse=True):
        mask = np.array([level_of.get(parent) == level for parent in series.floor_parent_ids])
        if mask.any():
            rows.append(PatchRow(label=named.get(level, f"RL {level:.1f}"), mask=mask))
    return rows


def series_styles(connection: ArchicadConnection) -> tuple[BandStyle, ...]:
    """The two fills of a patch tile, pointed at the project's own pens.

    Same rule as the band diagram, D27: the colour is the input and the pen is
    looked up, because a pen index means nothing outside the table it came
    from. Falls back to the built-in indices, loudly, when the table cannot be
    read -- a series drawn in the wrong two colours is still a readable
    drawing, which is not true of a banded one.
    """
    wanted = (FLOOR_STYLE, SUNLIT_STYLE)
    try:
        pens = pen_table(connection)
    except ArchicadError as error:
        typer.secho(
            f"  could not read the pen table ({error}); using default pen indices",
            fg=typer.colors.YELLOW,
        )
        return wanted

    styles, distances = match_pens(wanted, pens)
    for style in styles:
        gap = distances.get(style.label, 0.0)
        quality = "exact" if gap < 12 else ("close" if gap < 60 else "POOR MATCH")
        typer.echo(f"  {style.label:<7} rgb{style.rgb} -> pen {style.fill_pen:<4} {quality}")
    return styles


def report_write(connection: ArchicadConnection, written: WriteReport) -> None:
    """Print a write report, and chase down the cause of any refusals.

    ``APIERR_NOACCESSRIGHT`` names three possible causes and leaves the reader
    to work out which. The project can answer that, so it is asked rather than
    left as an exercise -- but only after a failure, because it costs calls.
    """
    typer.secho(
        written.describe(),
        fg=typer.colors.GREEN if written.complete else typer.colors.RED,
        bold=True,
    )
    if not written.zones_refused:
        return
    diagnosis = diagnose_write_access(connection, written.zones_refused)
    detail = diagnosis.describe() if diagnosis is not None else ""
    if detail:
        typer.secho(detail, fg=typer.colors.YELLOW)


def _warn_if_alike(styles: tuple[BandStyle, ...], pens: Sequence[Pen]) -> tuple[BandStyle, ...]:
    """Say so when two bands will look the same on the plan.

    Distinct pen indices are not distinct colours. Matching guarantees the
    first; only this checks the second, and a boundary a reader cannot see is
    a diagram that answers the wrong question while looking finished.
    """
    for left, right in indistinguishable_bands(styles, pens):
        typer.secho(
            f"  '{left}' and '{right}' are on pens of near-identical colour and will "
            f"not be tellable apart on the plan. Override one with --pen 'label=index'.",
            fg=typer.colors.YELLOW,
        )
    return styles


def band_styles(overrides: list[str] | None) -> tuple[BandStyle, ...]:
    """The drawing bands, with any ``--pen label=index`` overrides applied.

    An override naming a band that does not exist is an error rather than a
    no-op. A silently ignored ``--pen '2-3 hours=42'`` -- note "hours", not
    "hrs" -- produces a diagram in the default colours that looks exactly like
    one in the requested colours, and the person who typed it has no reason to
    look twice.
    """
    if not overrides:
        return DEFAULT_BANDS

    known = {band.label: band for band in DEFAULT_BANDS}
    wanted: dict[str, int] = {}
    for entry in overrides:
        label, separator, value = entry.partition("=")
        label = label.strip()
        if not separator or label not in known:
            raise typer.BadParameter(
                f"--pen {entry!r} does not name a band. Expected 'label=index' with "
                f"one of: " + ", ".join(repr(name) for name in known)
            )
        try:
            wanted[label] = int(value)
        except ValueError as error:
            raise typer.BadParameter(f"--pen {entry!r} needs a whole-number pen index") from error

    return tuple(
        replace(band, fill_pen=wanted[band.label]) if band.label in wanted else band
        for band in DEFAULT_BANDS
    )


def scene_config(
    *,
    timezone: str,
    living_room: list[str] | None = None,
    livable_suffix: str | None = None,
    balcony: list[str] | None = None,
    apartment_zone_layer: list[str] | None = None,
    apartment_zone_name: list[str] | None = None,
    open_space_zone_layer: list[str] | None = None,
    open_space_zone_name: list[str] | None = None,
    grid: float = 0.2,
    offset: float = 0.05,
    context_radius: float | None = None,
    exclude_above: float | None = None,
    patch_grid: float | None = None,
) -> SceneConfig:
    """Turn the shared scene options into a config.

    One place, so ``run`` and ``archicad-run`` cannot drift apart on the
    assumptions that decide the headline percentage. A flag that exists on one
    and not the other is a silently different answer from the same model.
    """
    return SceneConfig(
        timezone=timezone,
        living_room_space_names=tuple(living_room) if living_room else ("Living Room",),
        livable_opening_suffix=livable_suffix,
        balcony_name_prefixes=tuple(balcony) if balcony else ("Balcony",),
        apartment_zone_layers=tuple(apartment_zone_layer or ()),
        apartment_zone_names=tuple(apartment_zone_name or ()),
        open_space_zone_layers=tuple(open_space_zone_layer or ()),
        open_space_zone_names=tuple(open_space_zone_name or ()),
        grid_spacing_m=grid,
        surface_offset_m=offset,
        context_radius_m=context_radius,
        exclude_above_m=exclude_above,
        floor_patch_spacing_m=patch_grid,
    )


def report_assessment(
    result: PipelineResult,
    *,
    timezone: str,
    area: str,
    csv_out: Path | None = None,
    json_out: Path | None = None,
) -> None:
    """Print one assessment, and write it out if asked.

    Shared by ``run`` and ``archicad-run`` so a result computed from a live
    project is reported in exactly the same words, in the same order, as one
    computed from a file on disk. Two renderings of the same numbers is two
    places for the assumptions to drift out of the output.
    """
    typer.echo(result.model.describe(timezone))
    typer.echo("")
    typer.echo(result.scene.describe())
    typer.echo("")
    typer.echo(result.ruleset.describe(area))
    typer.echo("")
    typer.echo(
        f"  {result.sun_position_count} sun positions on {result.assessment_date.isoformat()}"
    )
    typer.echo("")

    for apartment in result.assessment.apartments:
        open_space = (
            "     --"
            if apartment.open_space_minutes is None
            else f"{apartment.open_space_minutes:7.1f}"
        )
        mark = "PASS" if apartment.meets_minimum else "fail"
        typer.echo(
            f"  {apartment.apartment_name:24} living {apartment.living_room_minutes:7.1f} min"
            f" | open space {open_space} min | {mark}"
        )

    typer.echo("")
    complies = result.assessment.complies
    typer.secho(
        result.assessment.summary(),
        fg=typer.colors.GREEN if complies else typer.colors.RED,
        bold=True,
    )

    if csv_out or json_out:
        header = build_header(
            assessment=result.assessment,
            ruleset=result.ruleset,
            site_description=result.scene.orientation.describe(),
            scene_provenance=result.scene.provenance,
            scene_config_description=result.scene.config.describe(),
            generated_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        )
        typer.echo("")
        if csv_out:
            typer.echo(f"  wrote {write_csv(csv_out, result.assessment, header)}")
        if json_out:
            typer.echo(f"  wrote {write_json(json_out, result.assessment, header)}")


@app.command()
def massing(
    ifc: Annotated[Path, typer.Argument(help="IFC file of the massing.")],
    timezone: Annotated[
        str, typer.Option("--timezone", "-z", help="IANA timezone, e.g. Australia/Sydney.")
    ],
    area: Annotated[
        str,
        typer.Option("--area", help="Which threshold applies: sydney_metro (2h) or other (3h)."),
    ] = "sydney_metro",
    ruleset: Annotated[
        str, typer.Option("--ruleset", help="Built-in ruleset name, or a path to a YAML file.")
    ] = "nsw_adg",
    year: Annotated[
        int, typer.Option("--year", help="Which year's assessment date to use.")
    ] = 2024,
    facade_grid: Annotated[
        float, typer.Option("--facade-grid", help="Facade sample spacing in metres.")
    ] = DEFAULT_MASSING_SPACING_M,
    ground_grid: Annotated[
        float, typer.Option("--ground-grid", help="Ground sample spacing in metres.")
    ] = DEFAULT_MASSING_SPACING_M,
    context: Annotated[
        list[str] | None,
        typer.Option(
            "--context",
            help=(
                "Name prefix marking an element as context: it shades the subject but "
                "is excluded from the area denominator. Repeatable."
            ),
        ),
    ] = None,
    ground_margin: Annotated[
        float, typer.Option("--ground-margin", help="Metres of ground to grid beyond the subject.")
    ] = 10.0,
    exclude_above: Annotated[
        float | None,
        typer.Option(
            "--exclude-above",
            help=(
                "Drop geometry lying entirely above this height, in project "
                "metres. A massing study measures area, so parked hotlink "
                "masters join the denominator rather than merely shading it."
            ),
        ),
    ] = None,
    ground_level: Annotated[
        float | None,
        typer.Option(
            "--ground-level",
            help=(
                "Height of the ground plane to assess, in project metres. "
                "Defaults to the lowest point of the subject, which on a "
                "developed model is a basement slab -- a ground plane under "
                "the building, in shadow all day by construction."
            ),
        ),
    ] = None,
    subject_layer: Annotated[
        list[str] | None,
        typer.Option(
            "--subject-layer",
            help=(
                "Archicad layer whose elements are the scheme being measured. "
                "Repeatable. Everything else still shades but is not counted. "
                "Without it, facade area on a developed model is every "
                "internal partition and balustrade as well as the envelope."
            ),
        ),
    ] = None,
    model_bands: Annotated[
        bool,
        typer.Option(
            "--model-bands",
            help=(
                "Colour the model in Archicad: a thin skin of real 3D "
                "elements over the facade, one colour per band. Needs "
                "Archicad open on the same project the IFC came from."
            ),
        ),
    ] = False,
    model_grid: Annotated[
        float,
        typer.Option(
            "--model-grid",
            help=(
                "Cell size of the facade skin in metres. Finer looks better "
                "and makes more elements; a face narrower than one cell is "
                "not drawn."
            ),
        ),
    ] = 0.5,
    model_layer: Annotated[
        str, typer.Option("--model-layer", help="Layer the facade skin is drawn on.")
    ] = FACADE_LAYER,
    model_flat: Annotated[
        bool,
        typer.Option(
            "--model-flat/--no-model-flat",
            help=(
                "Colour horizontal faces too -- balcony decks, terraces and "
                "soffits. They take more sun than any wall, so leaving them "
                "out shows the least-lit half of the building. Needs the slab "
                "layers naming with --subject-layer."
            ),
        ),
    ] = True,
    model_views: Annotated[
        bool,
        typer.Option(
            "--model-views",
            help=(
                "Make views of the coloured model -- the 3D window and a 3D "
                "Document -- and put them on a layout. A 3D Document cannot be "
                "created through the API, so one the project already has is "
                "used; name it with --model-document."
            ),
        ),
    ] = False,
    model_document: Annotated[
        str | None,
        typer.Option(
            "--model-document",
            help="Which existing 3D Document to make a view of, by name.",
        ),
    ] = None,
    model_scale: Annotated[
        float, typer.Option("--model-scale", help="Scale of the model views on the sheet.")
    ] = DEFAULT_LAYOUT_SCALE,
    master_layout: Annotated[
        str | None,
        typer.Option(
            "--master-layout",
            help=(
                "Master layout to build the sheet on, by name. Partial names "
                "work as long as they fit only one master. Without this the "
                "first master in the Layout Book is used, which on a real "
                "project was an A4."
            ),
        ),
    ] = None,
    model_favorite: Annotated[
        str | None,
        typer.Option(
            "--model-favorite",
            help=(
                "Wall favourite to take the skin's settings from. Needed for "
                "one setting no command can reach: a wall shows its building "
                "material's colour only when its surface override is off, and "
                "the Wall tool's default may have it on -- in which case every "
                "band renders the same colour. Make one wall with the override "
                "off, save it as a favourite, and name it here."
            ),
        ),
    ] = None,
    hide_layer: Annotated[
        list[str] | None,
        typer.Option(
            "--hide-layer",
            help=(
                "Layer to switch off in this tool's layer combinations. "
                "Repeatable. For the annotation layers that clutter a study "
                "drawing without adding to it."
            ),
        ),
    ] = None,
    port: Annotated[int, typer.Option("--port", help="Archicad's JSON API port.")] = DEFAULT_PORT,
    pen: Annotated[
        list[str] | None,
        typer.Option("--pen", help="Override one band's pen, as 'label=index'. Repeatable."),
    ] = None,
    csv_out: Annotated[
        Path | None, typer.Option("--csv", help="Write the band tables as CSV.")
    ] = None,
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Write the band tables as JSON.")
    ] = None,
) -> None:
    """Area-weighted sunlight bands for a massing, with no Zones or windows.

    Reports the share of facade area and of open ground reaching the threshold
    duration -- the metric a massing optimisation loop maximises. This is not
    the ADG per-apartment criterion and must not be quoted as one; use `run`
    once the model has Zones and windows.
    """
    banner()

    config = MassingConfig(
        timezone=timezone,
        context_name_prefixes=tuple(context) if context else ("Context",),
        facade_spacing_m=facade_grid,
        ground_spacing_m=ground_grid,
        ground_margin_m=ground_margin,
        exclude_above_m=exclude_above,
        subject_layers=tuple(subject_layer or ()),
        ground_level_m=ground_level,
    )

    try:
        result = run_massing(
            ifc, timezone=timezone, ruleset=ruleset, area=area, year=year, massing_config=config
        )
    except GeoreferencingError as error:
        typer.secho(f"Georeferencing error: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from error
    except SceneConfigError as error:
        typer.secho(f"Scene setting error: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from error
    except RulesetError as error:
        typer.secho(f"Ruleset error: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from error

    typer.echo(result.scene.describe())
    typer.echo("")
    typer.echo(
        f"  {result.sun_position_count} sun positions on "
        f"{result.assessment_date.isoformat()}, threshold "
        f"{result.threshold_minutes:g} min from {result.ruleset.identifier}"
    )

    surfaces = {"facade": result.facade, "ground": result.ground}
    for name, banded in surfaces.items():
        typer.echo("")
        typer.secho(f"  {name.upper()}  ({banded.total_area_m2:.1f} m2)", bold=True)
        for band in banded.bands:
            typer.echo(f"    {band.label:>8} {band.area_m2:11.2f} m2 {band.share:8.2%}")
        typer.echo(f"    {banded.summary()}")

    typer.echo("")
    typer.secho(result.summary(), fg=typer.colors.GREEN, bold=True)

    if model_bands:
        typer.echo("")
        connection = _connect(port)
        if report_model_bands(
            connection,
            result,
            config=config,
            spacing_m=model_grid,
            layer_name=model_layer,
            pens=pen,
            flat_faces=model_flat,
            favorite=model_favorite,
        ):
            raise typer.Exit(code=1)

    if model_views:
        typer.echo("")
        connection = _connect(port)
        if report_model_views(
            connection,
            layer_prefix=LAYER_GROUP,
            skin_layer=model_layer,
            document_name=model_document,
            scale=model_scale,
            also_hide=tuple(hide_layer or ()),
            master_layout=master_layout,
        ):
            raise typer.Exit(code=1)

    if csv_out or json_out:
        header = build_massing_header(
            ruleset=result.ruleset,
            area_key=result.area_key,
            threshold_minutes=result.threshold_minutes,
            site_description=result.scene.orientation.describe(),
            scene_config_description=result.scene.config.describe(),
            scene_provenance=result.scene.provenance,
            generated_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        )
        typer.echo("")
        if csv_out:
            typer.echo(f"  wrote {write_bands_csv(csv_out, surfaces, header)}")
        if json_out:
            typer.echo(f"  wrote {write_bands_json(json_out, surfaces, header)}")

    typer.echo("")
    typer.secho(DISCLAIMER, fg=typer.colors.YELLOW)


# -- talking to a live Archicad -------------------------------------------
#
# None of the three commands below are covered by CI, because there is no
# Archicad to run against. ``docs/archicad.md`` has the checklist for a human
# at a workstation, and ``tests/unit/test_archicad_adapter.py`` machine-checks
# everything short of Archicad actually answering.


def _connect(port: int) -> ArchicadConnection:
    connection = ArchicadConnection(HttpTransport(port=port))
    try:
        connection.require_tapir()
    except ArchicadNotRunningError as error:
        # Only here, and only once. The port scan is real network I/O, so it
        # belongs on the path that has already decided to tell a human --
        # not inside the transport, where every failed call would pay for it.
        found = _the_only_archicad(port)
        if found is not None:
            return found
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        elsewhere = where_archicad_actually_is(port)
        if elsewhere:
            typer.secho(elsewhere, fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=2) from error
    except ArchicadError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from error

    # Every read below is scoped to the current database, so a worksheet left
    # in front by the last run makes the project look empty of Zones.
    try:
        was = ensure_model_database(connection)
    except ArchicadError:
        was = None
    if was:
        typer.secho(
            f"  Archicad was showing a {was}, where the project has no Zones "
            f"and no walls. Switched the current database back to a floor plan.",
            fg=typer.colors.YELLOW,
        )
    return connection


def _the_only_archicad(tried: int) -> ArchicadConnection | None:
    """Fall through to the one running Archicad, when there is exactly one.

    Archicad hands each instance its own port, so the default is right only
    for whichever started first. Making a person look the number up and pass
    ``--port`` every time is a papercut with no upside: when the scan finds a
    single instance there is nothing to choose between, and the alternative is
    an error telling them to type what the tool already knows.

    Two or more instances still stop the run. Picking one would be guessing
    which project the results belong in, and writing an assessment into the
    wrong file is worse than any amount of typing.
    """
    running = [instance for instance in find_instances() if instance.port != tried]
    if len(running) != 1:
        return None

    only = running[0]
    typer.secho(
        f"  nothing on port {tried}; using the one Archicad that is running -- {only.describe()}",
        fg=typer.colors.YELLOW,
    )
    connection = ArchicadConnection(HttpTransport(port=only.port))
    try:
        connection.require_tapir()
    except ArchicadError:
        return None
    return connection


def _warn_if_zone_layers_hidden(
    connection: ArchicadConnection, wanted: Sequence[str], *, role: str = "zone layers"
) -> None:
    """Stop before the export when a layer the study needs is switched off.

    The translator exports what the current layer combination shows. Zones on
    a hidden layer are therefore not in the file, and the run fails several
    minutes and one large export later saying that the apartment zone layers
    matched nothing -- true, but it names the export's layers rather than the
    project's, so the cause is not in the message. On the reference project
    all four ``06 | Zone.*`` layers were hidden by a site-plan combination and
    the export carried 386 walls, 92 windows and no ``IfcSpace`` at all.

    Nothing here can fix it: Tapir 1.5.7 has no command that changes layer
    visibility or activates a layer combination, so this asks for a hand in
    Archicad rather than reaching for one.
    """
    # One layer commonly carries both the dwellings and the balconies, so the
    # two options name it twice and it would be reported twice.
    unique = list(dict.fromkeys(wanted))
    if not unique:
        return
    try:
        hidden = hidden_layers(connection, unique)
    except ArchicadError:
        return  # a diagnosis is not worth failing a run over
    if not hidden:
        return

    listed = ", ".join(repr(name) for name in hidden)
    typer.secho(
        f"  {len(hidden)} of the {role} this run needs are hidden in "
        f"Archicad right now: {listed}",
        fg=typer.colors.RED,
        err=True,
    )
    typer.secho(
        "  The IFC translator exports what the current layer combination "
        "shows, so those zones would not be in the export and the run would "
        "report no apartments. Switch to a layer combination that shows them "
        "-- or unhide them in Layer Settings -- and run again. Nothing in the "
        "add-on can do it from here.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=2)


@app.command("archicad-ports")
def archicad_ports() -> None:
    """List every running Archicad and the project each has open.

    Archicad gives each running instance its own port in order, so a second
    project opened alongside the first lands on 19724. A tool pointed at the
    default then reaches the wrong project, or -- once the first instance is
    closed -- nothing at all, which reads as "Archicad is not running" and
    sends people to check a setting that was never off.
    """
    banner()
    typer.echo(f"scanning ports {PORT_RANGE.start}-{PORT_RANGE.stop - 1} on this machine...")
    instances = find_instances()
    if not instances:
        typer.secho(
            "No Archicad is answering on any port. Check that it is running with a "
            "project open, and that the JSON interface is enabled in Options > Work "
            "Environment > Model Compare and JSON Interface.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    for instance in instances:
        default = "  (the default)" if instance.port == DEFAULT_PORT else ""
        typer.secho(f"  {instance.describe()}{default}", bold=True)
    if len(instances) > 1 or instances[0].port != DEFAULT_PORT:
        typer.echo("")
        typer.echo("Point a command at one of these with --port, e.g.:")
        typer.echo(f"  sun-study archicad-info --port {instances[-1].port}")


@app.command("archicad-info")
def archicad_info(
    port: Annotated[int, typer.Option("--port", help="Archicad's JSON API port.")] = DEFAULT_PORT,
    zone_names: Annotated[
        int,
        typer.Option(
            "--zone-names",
            help="How many distinct zone names to list, most common first. 0 for all.",
        ),
    ] = 20,
    properties: Annotated[
        bool,
        typer.Option(
            "--properties",
            help="Also list the project's property groups and names.",
        ),
    ] = False,
) -> None:
    """Check the connection to a running Archicad and echo what it reports.

    Run this first. It fails on a missing add-on, an unset project location or
    an unclassified model -- the three things that otherwise surface much later
    as a confusing error in the middle of a real run. It also lists the zone
    names, which is how you find out what to pass to ``--living-room``.
    """
    banner()
    connection = _connect(port)
    typer.echo(describe_connection(connection))

    try:
        found = read_zones(connection)
    except ArchicadError as error:
        typer.secho(f"  zones: unavailable ({error})", fg=typer.colors.RED)
        raise typer.Exit(code=2) from error

    typer.echo(f"  zones: {len(found)}")
    if not found:
        typer.secho(
            "  No Zones in the project. Sunlight results are written onto Zones, "
            "so there is nothing to write to yet.",
            fg=typer.colors.YELLOW,
        )
        return

    classified = classification_items_of(connection, [zone.guid for zone in found])
    unclassified = [zone for zone in found if zone.guid not in classified]
    if unclassified:
        typer.secho(
            f"  {len(unclassified)} of {len(found)} zones carry no classification, so "
            f"no custom property can be attached to them: "
            + ", ".join(zone.label for zone in unclassified[:8])
            + (" ..." if len(unclassified) > 8 else ""),
            fg=typer.colors.YELLOW,
        )
    else:
        typer.echo(f"  all {len(found)} zones are classified")

    _report_zone_layers(connection, found)
    _report_zone_names(found, limit=zone_names)

    if properties:
        _report_properties(connection)


def _report_zone_layers(connection: ArchicadConnection, found: Sequence[ArchicadZone]) -> None:
    """How many zones sit on each layer, most populated first.

    This is what tells a person what to pass to ``--apartment-zone-layer``,
    and it is the only cheap way to catch the duplication trap. A FUSE-manual
    project carries the apartments on ``06 | Zone.SEPP 65`` and a *duplicate*
    set on ``06 | Zone.Units``; it also carries GFA, NLA and storage zones on
    ``10 | Calc.*``. Counting them side by side shows at a glance which layers
    hold the same apartments twice -- two layers with the same count almost
    certainly do -- and which hold area take-off that must not be assessed.
    """
    try:
        names = layer_names(connection)
    except ArchicadError as error:
        typer.secho(f"  could not read the layer list ({error})", fg=typer.colors.YELLOW)
        return

    counted = collections.Counter(layer_of(zone, names) for zone in found)

    # The names *within* a layer, not just across the project. A project-wide
    # name tally cannot say what the apartments are called, because the biggest
    # counts belong to annotation and area take-off; per layer, the answer is
    # one line.
    by_layer: dict[str, collections.Counter[str]] = {}
    for zone in found:
        key = layer_of(zone, names)
        by_layer.setdefault(key, collections.Counter())[zone.name or "(unnamed)"] += 1

    width = max(len(name) for name in counted)
    typer.echo(f"  zones per layer ({len(counted)} layers carry zones):")
    for name, how_many in counted.most_common():
        top = by_layer[name].most_common(3)
        summary = ", ".join(f"{label} x{count}" for label, count in top)
        if len(by_layer[name]) > len(top):
            summary += ", ..."
        # Quoted, because a trailing space in a layer name is invisible in a
        # padded column and is exactly what makes a copied --zone-layer miss.
        quoted = f"{name!r}"
        typer.echo(f"    {how_many:>5}  {quoted:<{width + 2}}  {summary}")

    # Equal counts across two zone layers is the signature of the duplication
    # the manual warns about, and assessing both would count every apartment
    # twice.
    zone_layers = {
        name: how_many
        for name, how_many in counted.items()
        if name.casefold().replace(" ", "").startswith("06|zone.")
    }
    twins = collections.Counter(zone_layers.values())
    repeated = sorted(name for name, how_many in zone_layers.items() if twins[how_many] > 1)
    if len(repeated) > 1:
        typer.secho(
            f"  {' and '.join(repeated)} hold the same number of zones, which is what "
            f"duplicated zone sets look like. Pass only one to "
            f"--apartment-zone-layer, or every apartment is counted twice.",
            fg=typer.colors.YELLOW,
        )


def _report_properties(connection: ArchicadConnection) -> None:
    """Every property group in the project, with its property names.

    A practice that already runs a solar-access workflow by hand has a
    property the diagram is coloured from -- a Daylight flag ticked Y or N per
    apartment. Writing results into *that* is what makes the tool fit an
    existing standard instead of adding a parallel one beside it, and this is
    how its exact group and name get found. Guessing them would produce a
    second column that looks right and drives nothing.
    """
    entries = all_properties(connection)
    groups: dict[str, list[Any]] = {}
    for entry in entries:
        groups.setdefault(entry.group, []).append(entry)

    # An enumeration only accepts one of its defined display strings, and
    # GetAllProperties says a property is one without saying what it holds.
    # Only the custom ones are asked about: the built-in enumerations are
    # Archicad's own and are never write targets.
    enumerated = [
        entry.identifier
        for entry in entries
        if "Enumeration" in entry.collection_type and entry.kind == "Custom"
    ]
    try:
        values = enum_values(connection, enumerated)
    except ArchicadError as error:  # pragma: no cover - needs a live Archicad
        typer.secho(f"  (could not read enumeration values: {error})", fg=typer.colors.YELLOW)
        values = {}

    writable = sum(1 for entry in entries if entry.writable)
    typer.echo(f"  {len(entries)} properties in {len(groups)} groups, {writable} writable")
    for group, found in sorted(groups.items()):
        typer.secho(f"    {group}", bold=True)
        for entry in sorted(found, key=lambda e: e.name):
            allowed = values.get(entry.identifier, ())
            suffix = f"  accepts: {' / '.join(allowed)}" if allowed else ""
            typer.echo(f"      {entry.describe()}{suffix}")


def _report_zone_names(found: Sequence[ArchicadZone], *, limit: int) -> None:
    """The distinct Zone names, most common first.

    This is what tells a person what to pass to ``--living-room``. The default
    is ``Living Room``, and a real office file is at least as likely to say
    ``LIVING`` or ``Living/Dining``. Getting that wrong produces zero assessed
    apartments, which reads as a building with no living rooms rather than as
    a mistake -- so the names are printed before anyone has to guess.
    """
    counts = collections.Counter(zone.name.strip() or "(unnamed)" for zone in found)
    typer.echo(f"  {len(counts)} distinct zone names")

    shown = counts.most_common() if limit <= 0 else counts.most_common(limit)
    width = max((len(name) for name, _ in shown), default=0)
    for name, count in shown:
        typer.echo(f"    {name:<{width}}  {count:>5}")
    if 0 < limit < len(counts):
        typer.echo(f"    ... and {len(counts) - limit} more (--zone-names 0 for all)")

    # The number carries the identity when zones are placed per unit rather
    # than per room, and the name may then be a type rather than a room. A few
    # whole labels show which convention this project uses.
    typer.echo("  example zones (number | name):")
    for zone in found[:5]:
        typer.echo(f"    {zone.number or '(no number)'} | {zone.name or '(unnamed)'}")


@app.command("init-properties")
def init_properties_command(
    port: Annotated[int, typer.Option("--port", help="Archicad's JSON API port.")] = DEFAULT_PORT,
    recreate: Annotated[
        bool,
        typer.Option(
            "--recreate",
            help=(
                "Delete this tool's properties and make them again. The only "
                "way to correct a default already in the project -- an hours "
                "column created with a default of 0 reads as 'no sunlight' on "
                "every Zone that never took a write. Discards the values "
                "written so far, so follow it with a fresh archicad-run --write."
            ),
        ),
    ] = False,
) -> None:
    """Create the 'Sun Study' property group and its properties in the project.

    A separate, explicit step rather than something a results run does behind
    your back: property definitions are part of the project file, so on a
    Teamwork job this changes what everybody sees. Safe to run again -- it
    creates only what is missing.
    """
    banner()
    connection = _connect(port)

    try:
        found = read_zones(connection)
        if not found:
            typer.secho(
                "No Zones in the project, so there is nothing to make the "
                "properties available for. Place the apartment Zones first.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)

        if recreate:
            removed = delete_properties(connection)
            typer.secho(
                f"  deleted {len(removed)} existing properties and every value "
                f"written to them: {', '.join(removed) or 'none were there'}",
                fg=typer.colors.YELLOW,
            )

        classified = classification_items_of(connection, [zone.guid for zone in found])
        properties = init_properties(connection, classified)
    except ArchicadError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from error

    typer.secho(f"{PROPERTY_GROUP_NAME}: {len(properties)} properties ready", bold=True)
    for spec in APARTMENT_PROPERTIES:
        typer.echo(f"  {spec.name:34} {spec.data_type}")

    skipped = len(found) - len(classified)
    if skipped:
        typer.secho(
            f"  {skipped} unclassified zones will not accept these properties. "
            f"Classify them and run this again.",
            fg=typer.colors.YELLOW,
        )
    typer.echo(
        f"  an hours column reads {NOT_ASSESSED_HOURS:g} where nothing was ever "
        f"written to it, which no measurement can be. Schedule on 'Sun Study "
        f"Run' being non-empty to show only assessed apartments."
    )


@app.command("archicad-rooms")
def archicad_rooms(
    port: Annotated[int, typer.Option("--port", help="Archicad's JSON API port.")] = DEFAULT_PORT,
    zone_layer: Annotated[
        list[str] | None,
        typer.Option("--zone-layer", help="Archicad layer whose zones are the apartments."),
    ] = None,
    zone_name: Annotated[
        list[str] | None,
        typer.Option(
            "--zone-name",
            help=(
                "Only zones with this name. Repeatable. A layer can mix "
                "dwellings and balconies -- one project has 15 units and 20 "
                "balconies together on '06 | Zone.Units', told apart only by "
                "name -- and assessing a balcony as an apartment is silent."
            ),
        ),
    ] = None,
    living: Annotated[
        list[str] | None,
        typer.Option(
            "--living",
            help=(
                "Extra room codes that are living rooms, on top of the built-in "
                "vocabulary. Repeatable. Only needed where a project uses a code "
                "the office library has not used before."
            ),
        ),
    ] = None,
    tolerance: Annotated[
        float,
        typer.Option(
            "--tolerance",
            help=(
                "How far outside an apartment a room label may sit and still "
                "belong to it, in metres. A label is annotation and gets dragged "
                "to wherever it reads well. 0 requires strict containment; every "
                "use of the tolerance is reported with its distance."
            ),
        ),
    ] = DEFAULT_TOLERANCE_M,
) -> None:
    """Match room labels to apartments, and say whether that join works.

    Where a Zone is a whole unit, the rooms inside it are label objects
    carrying a code -- ``L/D`` for living/dining, ``B1`` for a bedroom. ADG
    4A-1 is about living rooms, so that code is the only thing separating the
    room the standard cares about from the rooms it does not.

    Run this before trusting any per-room result. It says how many apartments
    actually contain a labelled living room, which is the ceiling on what the
    assessment can measure.
    """
    banner()
    connection = _connect(port)
    try:
        names = layer_names(connection)
        found = read_zones(connection)
        if zone_layer:
            found = tuple(
                zone for zone in found if layer_matches(layer_of(zone, names), zone_layer)
            )
        if zone_name:
            wanted = {entry.strip().casefold() for entry in zone_name}
            found = tuple(zone for zone in found if zone.name.strip().casefold() in wanted)
        if not found:
            typer.secho(
                "No apartment zones. Pass --zone-layer, and run 'archicad-info' "
                "to see which layers carry zones and what the zones on them are called.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)

        labels = room_labels(gdl_parameters(connection, library_objects(connection)))
        typer.echo(f"  {len(found)} apartments, {len(labels)} named room labels")

        match = match_rooms(found, labels, tolerance_m=tolerance)
        typer.secho(
            match.describe(),
            fg=typer.colors.GREEN if match.matched else typer.colors.RED,
            bold=True,
        )
        _report_living_rooms(match, living or [], labels)
    except ArchicadError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from error


def _report_living_rooms(match: RoomMatch, living: Sequence[str], labels: Sequence[Any]) -> None:
    """How many apartments have a living room the assessment could measure."""
    typer.echo("")
    if not match.matched:
        typer.secho(
            "  Nothing matched, so no per-room assessment is possible from these "
            "labels. Either the apartments are on a different layer, or every "
            "label belongs to a hotlink master rather than a placed unit.",
            fg=typer.colors.RED,
        )
        return

    # A code nobody has classified is silent: an unrecognised living room is
    # simply not assessed, and no other line of output would say so.
    unknown = unknown_codes(labels)
    if unknown:
        typer.secho(
            f"  room codes not in the built-in vocabulary: {', '.join(unknown[:12])}. "
            f"If any of those is a living room, pass it with --living.",
            fg=typer.colors.YELLOW,
        )

    with_living = [
        guid
        for guid, rooms in match.by_zone.items()
        if any(is_living_room(room.code, extra=living) for room in rooms)
    ]
    total = len(match.by_zone) + len(match.zones_without_rooms)
    known = ", ".join(sorted(LIVING_ROOM_CODES) + sorted(code.upper() for code in living))

    typer.secho(
        f"  {len(with_living)} of {total} apartments contain a living room ({known})",
        fg=typer.colors.GREEN if len(with_living) == total else typer.colors.RED,
        bold=True,
    )
    if len(with_living) < total:
        typer.secho(
            "  The rest cannot be assessed against ADG 4A-1, which is about living "
            "rooms. If a living room here is coded something the list above does not "
            "name, pass it with --living.",
            fg=typer.colors.YELLOW,
        )


@app.command("archicad-objects")
def archicad_objects(
    port: Annotated[int, typer.Option("--port", help="Archicad's JSON API port.")] = DEFAULT_PORT,
    match: Annotated[
        str | None,
        typer.Option(
            "--match",
            help=(
                "Only library parts whose name contains this, case-insensitively. "
                "Without it, every library part is counted but none are opened."
            ),
        ),
    ] = None,
    sample: Annotated[
        int,
        typer.Option("--sample", help="How many matching objects to dump parameters for."),
    ] = 3,
    parameter: Annotated[
        str | None,
        typer.Option(
            "--parameter",
            help=(
                "Read this one GDL parameter across every matching object and "
                "report its values and the storeys they sit on. This is what "
                "says whether the objects are placed rooms or hotlink masters."
            ),
        ),
    ] = None,
) -> None:
    """List the project's library objects, and open a few to see what they carry.

    For a project where Zones are placed per *unit*, the rooms inside a unit
    exist only as library objects -- a "Room Name and Size Label" holding the
    room's name and size. That object is the only thing in the model that says
    which part of an apartment is the living room, and ADG 4A-1 turns on
    exactly that distinction.

    This finds the parameter the room name lives in. Nothing can be guessed
    here: parameter names are the library part author's business, and every
    office library names them differently.
    """
    banner()
    connection = _connect(port)
    try:
        found = library_objects(connection)
        if not found:
            typer.secho("No library objects in the project.", fg=typer.colors.YELLOW)
            return

        names = layer_names(connection)
        typer.echo(f"  {len(found)} library objects, by library part:")
        counted = collections.Counter(item.library_part or "(unnamed part)" for item in found)
        for part, how_many in counted.most_common(15):
            typer.echo(f"    {how_many:>5}  {part}")
        if len(counted) > 15:
            typer.echo(f"    ... and {len(counted) - 15} more library parts")

        if not match:
            typer.echo("")
            typer.echo('Re-run with --match to open one, e.g. --match "Room Name"')
            return

        wanted = match.casefold()
        hits = [item for item in found if wanted in item.library_part.casefold()]
        if not hits:
            typer.secho(f"Nothing matches {match!r}.", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)

        _report_objects(connection, hits, names, sample=sample, parameter=parameter)
    except ArchicadError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from error


def _report_parameter(
    connection: ArchicadConnection, hits: Sequence[Any], name: str, *, cap: int = 2000
) -> None:
    """Every value of one named parameter, and where those objects sit.

    The decisive question about room labels: whether the placed instances are
    reachable at all, or whether only the hotlink *masters* carry them. If
    every label sits on a storey the apartments are not on, matching a room to
    an apartment by position cannot work and the join has to go through the
    module instead.
    """
    if len(hits) > cap:
        typer.secho(
            f"  reading {name!r} for the first {cap} of {len(hits)} objects",
            fg=typer.colors.YELLOW,
        )
    opened = gdl_parameters(connection, hits[:cap])

    values = collections.Counter(
        (item.parameter(name) or "").strip() or "(empty)" for item in opened
    )
    named = [item for item in opened if (item.parameter(name) or "").strip()]

    typer.echo("")
    typer.secho(f"{name!r}: {len(named)} of {len(opened)} carry a value", bold=True)
    for value, how_many in values.most_common(25):
        typer.echo(f"    {how_many:>5}  {value}")
    if len(values) > 25:
        typer.echo(f"    ... and {len(values) - 25} more distinct values")

    if not named:
        typer.secho(
            f"  Nothing carries {name!r}. Either the parameter is named "
            f"differently on this library part, or these are placeholders.",
            fg=typer.colors.YELLOW,
        )
        return

    storeys = collections.Counter(item.storey_index for item in named)
    typer.echo(f"  those {len(named)} sit on {len(storeys)} storeys:")
    for storey, how_many in sorted(storeys.items(), key=lambda pair: (pair[0] is None, pair[0])):
        typer.echo(f"    {how_many:>5}  storey {storey}")


def _report_objects(
    connection: ArchicadConnection,
    hits: Sequence[Any],
    names: dict[int, str],
    *,
    sample: int,
    parameter: str | None = None,
) -> None:
    """Where the matching objects sit, and what one of them holds."""
    typer.echo("")
    typer.secho(f"{len(hits)} matching objects", bold=True)

    on_layers = collections.Counter(
        names.get(item.layer_index, "(unknown)") if item.layer_index is not None else "(none)"
        for item in hits
    )
    for layer, how_many in on_layers.most_common(6):
        typer.echo(f"    {how_many:>5}  on layer {layer!r}")

    # Hotlink masters are parked far above the building -- one project has them
    # around 67 m -- so a height histogram separates the placed instances from
    # the masters before anything tries to match a room to an apartment.
    heights = sorted(item.origin[2] for item in hits)
    typer.echo(
        f"    placement height: min {heights[0]:.2f} m, median "
        f"{heights[len(heights) // 2]:.2f} m, max {heights[-1]:.2f} m"
    )
    if heights[-1] - heights[0] > 30.0:
        typer.secho(
            "    that spread is wide enough to include hotlink masters parked "
            "above the building. Those are not placed rooms.",
            fg=typer.colors.YELLOW,
        )

    # Storeys, not just heights. A label on a storey no apartment sits on
    # cannot be matched to an apartment by position, and that is the whole
    # question about whether these are placed rooms or hotlink masters.
    storeys = collections.Counter(item.storey_index for item in hits)
    shown = sorted(storeys.items(), key=lambda pair: -pair[1])[:8]
    listed = ", ".join(f"{storey} x{how_many}" for storey, how_many in shown)
    typer.echo(f"    on {len(storeys)} storeys: {listed}" + (" ..." if len(storeys) > 8 else ""))

    if parameter:
        _report_parameter(connection, hits, parameter)

    for item in gdl_parameters(connection, hits[:sample]):
        typer.echo("")
        typer.secho(f"  {item.library_part}  at {item.origin}", bold=True)
        typer.echo(f"    storey {item.storey_index}, layer {layer_of(item, names)!r}")
        if not item.parameters:
            typer.secho("    no GDL parameters reported", fg=typer.colors.YELLOW)
            continue
        # Text parameters first: a room name is a string, and a library part
        # carries far more numbers than strings.
        text = [(k, v) for k, v in item.parameters if v and not _looks_numeric(v)]
        for key, value in text[:25]:
            typer.echo(f"    {key:<24} {value}")
        typer.echo(f"    ({len(item.parameters)} parameters, {len(text)} of them text)")


def _looks_numeric(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return value.startswith("<array")
    return True


@app.command("archicad-report")
def archicad_report(
    port: Annotated[int, typer.Option("--port", help="Archicad's JSON API port.")] = DEFAULT_PORT,
    out: Annotated[
        Path,
        typer.Option("--out", help="Where to write the report."),
    ] = Path("sun-study-report.txt"),
) -> None:
    """Everything the tool can learn about a project, in one file.

    Written for the case where whoever is reading the output is not sitting at
    the workstation. Every question this tool has needed answering so far --
    which layer holds the apartments, what the zones are called, which
    parameter carries the room name, whether the room labels reach the
    apartments, whether the properties exist -- took its own command, its own
    run and its own round trip. This asks all of them once.

    Read-only throughout. It creates nothing, writes no property and draws
    nothing, so it is safe on a live project someone else has open.
    """
    banner()
    connection = _connect(port)
    with _also_writing_to(out):
        _report_everything(connection)
    typer.echo("")
    typer.secho(f"wrote {out}", bold=True)
    typer.echo("  Read-only: nothing in the project was created or changed.")


@contextlib.contextmanager
def _also_writing_to(path: Path) -> Iterator[None]:
    """Send everything printed inside the block to a file as well as the screen.

    A tee rather than a redirect, because a long silent command looks hung and
    the point of the file is that it can be sent on afterwards.
    """

    class Tee:
        def __init__(self, *streams: Any) -> None:
            self.streams = streams

        def write(self, text: str) -> int:
            for stream in self.streams:
                stream.write(text)
            return len(text)

        def flush(self) -> None:
            for stream in self.streams:
                stream.flush()

    handle = path.open("w", encoding="utf-8", errors="replace")
    original = sys.stdout
    sys.stdout = Tee(original, handle)
    try:
        yield
    finally:
        sys.stdout = original
        handle.close()


def _report_everything(connection: ArchicadConnection) -> None:
    """Each section guarded, because one unanswerable question must not cost
    the answers to the others -- the whole value here is getting them together."""
    sections: list[tuple[str, Any]] = [
        ("CONNECTION AND SITE", lambda: typer.echo(describe_connection(connection))),
        ("ZONES", lambda: _report_zones_section(connection)),
        ("LIBRARY OBJECTS", lambda: _report_objects_section(connection)),
        ("ROOMS PER CANDIDATE LAYER", lambda: _report_rooms_section(connection)),
        ("PROPERTIES", lambda: _report_properties(connection)),
    ]
    for title, run in sections:
        typer.echo("")
        typer.secho(f"--- {title} " + "-" * max(0, 60 - len(title)), bold=True)
        try:
            run()
        except ArchicadError as error:
            typer.secho(f"  unavailable: {error}", fg=typer.colors.YELLOW)


def _report_zones_section(connection: ArchicadConnection) -> None:
    found = read_zones(connection)
    typer.echo(f"  {len(found)} zones")
    if found:
        _report_zone_layers(connection, found)
        _report_zone_names(found, limit=20)


def _report_objects_section(connection: ArchicadConnection) -> None:
    found = library_objects(connection)
    counted = collections.Counter(item.library_part or "(unnamed part)" for item in found)
    typer.echo(f"  {len(found)} library objects across {len(counted)} library parts:")
    for part, how_many in counted.most_common(10):
        typer.echo(f"    {how_many:>5}  {part}")


def _report_rooms_section(connection: ArchicadConnection) -> None:
    """The room match against every zone layer that could hold apartments.

    Trying each candidate rather than asking which to try is the point: on one
    project the answer was a layer whose name nobody would have guessed, and
    finding it cost several exchanges.
    """
    names = layer_names(connection)
    zones = read_zones(connection)
    labels = room_labels(gdl_parameters(connection, library_objects(connection)))
    typer.echo(f"  {len(labels)} named room labels in the project")
    if not labels:
        return

    candidates = sorted(
        {
            layer_of(zone, names)
            for zone in zones
            if layer_of(zone, names).replace(" ", "").casefold().startswith("06|zone.")
        }
    )
    if not candidates:
        typer.echo("  no '06 | Zone.*' layers, so no obvious apartment layer to try")
        return

    for layer in candidates:
        on_layer = [zone for zone in zones if layer_of(zone, names) == layer]
        typer.echo("")
        typer.secho(f"  {layer!r} -- {len(on_layer)} zones", bold=True)
        for line in match_rooms(on_layer, labels).describe().splitlines():
            typer.echo(f"  {line}")


@app.command("archicad-probe")
def archicad_probe(
    port: Annotated[int, typer.Option("--port", help="Archicad's JSON API port.")] = DEFAULT_PORT,
) -> None:
    """Find out why Archicad is refusing to create a property, one variable at a time.

    ``CreatePropertyDefinitions`` answers a rejection with a fixed message and
    a raw error code, so a failure says which properties were refused but not
    which *part* of the request it objected to. Nine identical codes narrow
    nothing.

    This sends several deliberately minimal definitions that differ in exactly
    one thing each -- the name, the type, the availability -- and prints the
    code for every one. Whichever variations succeed bracket the cause. It
    also names the classification items the zones actually carry, because
    "available for Unclassified" looks identical to a real classification
    until you resolve the identifiers.

    Anything it manages to create is left behind; delete it from the Property
    Manager afterwards.
    """
    banner()
    connection = _connect(port)

    try:
        found = read_zones(connection)
        if not found:
            typer.secho("No Zones to probe against.", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)

        sample = found[:8]
        classified = classification_items_of(connection, [zone.guid for zone in sample])
        items = sorted({item for values in classified.values() for item in values})
        names = classification_item_names(connection)

        typer.secho(
            f"{len(sample)} zones carry {len(items)} distinct classification items:", bold=True
        )
        for item in items:
            typer.echo(f"    {names.get(item, '(not found in any system)')}   {item}")
        if not items:
            typer.secho(
                "  None. A custom property attaches to an element through its "
                "classification, so there is nothing to make one available for.",
                fg=typer.colors.RED,
            )
        typer.echo("")

        group_id = ensure_property_group(connection)
        typer.echo(f"  group {PROPERTY_GROUP_NAME!r} is {group_id}")
        typer.echo("")
        _run_probe(connection, group_id, items)
    except ArchicadError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from error


def _run_probe(connection: ArchicadConnection, group_id: str, items: list[str]) -> None:
    """Create several one-variable-apart definitions and report each outcome."""
    available = [{"classificationItemId": {"guid": item}} for item in items]
    attempts: list[tuple[str, dict[str, Any]]] = [
        ("plain name, string, no availability", {"name": "SunStudyProbeA", "type": "string"}),
        (
            "plain name, string, with availability",
            {"name": "SunStudyProbeB", "type": "string", "availability": available},
        ),
        (
            "plain name, number, with availability",
            {"name": "SunStudyProbeC", "type": "number", "availability": available},
        ),
        (
            "name with parentheses, number, with availability",
            {"name": "SunStudyProbeD (h)", "type": "number", "availability": available},
        ),
        (
            "no isEditable",
            {"name": "SunStudyProbeE", "type": "string", "availability": available, "skip": True},
        ),
        (
            "WITH a default value (the suspected fix)",
            {
                "name": "SunStudyProbeF",
                "type": "string",
                "availability": available,
                "default": True,
            },
        ),
    ]

    definitions = []
    for _, attempt in attempts:
        definition: dict[str, Any] = {
            "name": attempt["name"],
            "description": "sun-study probe, safe to delete",
            "type": attempt["type"],
            "availability": attempt.get("availability", []),
            "group": {"propertyGroupId": {"guid": group_id}},
        }
        if not attempt.get("skip"):
            definition["isEditable"] = True
        if attempt.get("default"):
            definition["defaultValue"] = default_property_value(str(attempt["type"]))
        definitions.append({"propertyDefinition": definition})

    response = connection.run_tapir(
        "CreatePropertyDefinitions", {"propertyDefinitions": definitions}
    )
    results = response.get("propertyIds") if isinstance(response, dict) else []
    if not isinstance(results, list) or len(results) != len(attempts):
        typer.secho(f"unexpected probe response: {response!r}", fg=typer.colors.RED)
        return

    for (label, _), result in zip(attempts, results, strict=True):
        if isinstance(result, dict) and "error" in result:
            error = result["error"] or {}
            typer.secho(f"  FAILED  {label}: code {error.get('code', 'none')}", fg=typer.colors.RED)
        else:
            typer.secho(f"  worked  {label}", fg=typer.colors.GREEN)

    typer.echo("")
    typer.secho(
        "Whichever lines worked bracket the cause. Delete any created "
        "'SunStudyProbe*' properties from the Property Manager.",
        fg=typer.colors.YELLOW,
    )


@app.command("archicad-selftest")
def archicad_selftest(
    port: Annotated[int, typer.Option("--port", help="Archicad's JSON API port.")] = DEFAULT_PORT,
    count: Annotated[
        int, typer.Option("--zones", help="How many zones to write to. 0 for all.")
    ] = 8,
    zone_layer: Annotated[
        list[str] | None,
        typer.Option(
            "--zone-layer",
            help=(
                "Only test against zones on this Archicad layer. Repeatable. "
                "Without it the first zones in the project are used, which in a "
                "project with GFA or fire-compartment zones is usually not the "
                "apartments."
            ),
        ),
    ] = None,
    layer: Annotated[
        str, typer.Option("--layer", help="Archicad layer to draw on.")
    ] = DEFAULT_LAYER_NAME,
    pen: Annotated[
        list[str] | None,
        typer.Option("--pen", help="Override a band's fill pen as 'label=index'. Repeatable."),
    ] = None,
    properties: Annotated[
        bool,
        typer.Option(
            "--properties/--no-properties",
            help=(
                "Also create and write the Sun Study properties. Turn off to "
                "test the drawing on its own -- fills need no properties."
            ),
        ),
    ] = True,
    draw: Annotated[
        bool,
        typer.Option("--draw/--no-draw", help="Also draw the fills and legend."),
    ] = True,
) -> None:
    """Exercise the whole Archicad round trip with invented numbers.

    Everything the tool does to a project -- create properties, write values,
    create a layer, draw fills, draw a legend -- against real Zones, in
    seconds, with no geometry analysis in the way.

    That separation is the point. A real run spends most of its time casting
    rays, and finding out only at the end that a property was not available or
    a pen index was wrong is a slow way to learn it. This isolates the part
    that cannot be tested without an Archicad from the part that is tested
    exhaustively without one.

    **It writes invented values to your project.** They are spread across
    every band so the legend and the pen mapping are all visible at once, and
    every one is stamped SELFTEST. Re-run a real assessment, or delete the
    layer and clear the properties, before anybody looks at the result.
    """
    banner()
    typer.secho(
        "SELF TEST: this writes invented numbers onto real Zones and draws them. "
        "Nothing it produces is a measurement.",
        fg=typer.colors.YELLOW,
        bold=True,
    )
    typer.echo("")

    connection = _connect(port)
    try:
        found = read_zones(connection)
        if not found:
            typer.secho("No Zones in the project to test against.", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)

        names = layer_names(connection)
        in_project = len(found)
        if zone_layer:
            found = tuple(
                zone for zone in found if layer_matches(layer_of(zone, names), zone_layer)
            )
            if not found:
                typer.secho(
                    f"No zones on {', '.join(zone_layer)}. Run 'archicad-info "
                    f"--zone-names 0' to see what the project actually has.",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(code=2)

        chosen = found if count <= 0 else found[:count]
        assessment, match = _synthetic_assessment(chosen, band_styles(pen))

        # Which layer the sample came from is the fastest way to notice that
        # the tool is looking at GFA-calculation zones rather than apartments.
        # The project total stays visible either way, so a filter that matched
        # 40 of 1341 zones cannot be mistaken for the whole project.
        if zone_layer:
            typer.echo(
                f"  testing against {len(chosen)} of {len(found)} zones on "
                f"{', '.join(zone_layer)} ({in_project} zones in the project)"
            )
        else:
            typer.echo(f"  testing against {len(chosen)} of {in_project} zones")
            sampled = collections.Counter(layer_of(zone, names) for zone in chosen)
            for name, how_many in sampled.most_common():
                typer.echo(f"    {how_many} on layer {name!r}")
            typer.echo("    narrow this with --zone-layer if those are not the apartments")

        if properties:
            classified = classification_items_of(connection, [zone.guid for zone in chosen])
            init_properties(connection, classified)
            typer.echo(f"  properties ready in group {PROPERTY_GROUP_NAME!r}")

            written = write_assessment(
                connection, assessment, match=match, run_stamp="SELFTEST -- not a measurement"
            )
            report_write(connection, written)

        # Independent of the properties on purpose. A fill is geometry on a
        # layer; it needs no property, no classification and no schedule, so a
        # problem with the property system must not stand between a person and
        # the drawing.
        if draw:
            drawn = draw_assessment(
                connection,
                assessment,
                chosen,
                zone_by_apartment=match.by_apartment,
                bands=resolve_bands(connection, pen),
                layer_name=layer,
                title="SELF TEST -- invented values",
            )
            typer.secho(
                drawn.describe(),
                fg=typer.colors.GREEN if drawn.complete else typer.colors.RED,
                bold=True,
            )
    except ArchicadError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from error

    typer.echo("")
    typer.secho(
        "Now look in Archicad: the zones should carry Sun Study values, and the "
        f"layer {layer!r} should hold one fill per zone plus a legend, in as many "
        "colours as there are bands. Wrong colours mean the pen mapping needs "
        "--pen; missing values mean the zone's classification does not carry the "
        "property.",
        fg=typer.colors.YELLOW,
    )


def _synthetic_assessment(
    zones: Sequence[ArchicadZone], bands: Sequence[BandStyle]
) -> tuple[Any, Any]:
    """Invented results for real zones, one per band, cycling.

    Every band gets used so a single glance at the plan checks the whole pen
    mapping. Values sit in the middle of each band rather than on its edge, so
    a rounding disagreement cannot make the test look like a banding bug.
    """
    from sun_study.archicad.write import ApartmentMatch
    from sun_study.rules.assessment import ApartmentResult, BuildingAssessment
    from sun_study.rules.ruleset import Continuity

    lower = 0.0
    midpoints: list[float] = []
    for band in bands:
        upper = band.upper_minutes if band.upper_minutes != float("inf") else lower + 60.0
        midpoints.append(lower if upper <= lower else (lower + upper) / 2.0)
        lower = upper

    apartments = []
    for index, zone in enumerate(zones):
        minutes = midpoints[index % len(midpoints)]
        apartments.append(
            ApartmentResult(
                apartment_id=zone.guid,
                apartment_name=zone.label,
                living_room_minutes=minutes,
                open_space_minutes=None,
                governing_minutes=minutes,
                meets_minimum=minutes >= 120.0,
                receives_no_sunlight=minutes <= 0.0,
                counted=True,
                note="SELFTEST",
            )
        )

    counted = len(apartments)
    meeting = sum(1 for a in apartments if a.meets_minimum)
    assessment = BuildingAssessment(
        ruleset_name="SELFTEST",
        ruleset_version="0",
        area_key="sydney_metro",
        area_label="Self test",
        minimum_minutes=120.0,
        continuity=Continuity.CUMULATIVE,
        apartments=tuple(apartments),
        counted_total=counted,
        meeting_minimum=meeting,
        with_no_sunlight=sum(1 for a in apartments if a.receives_no_sunlight),
        compliant_share=meeting / counted if counted else 0.0,
        no_sunlight_share=0.0,
        required_share=0.7,
        maximum_no_sunlight_share=0.15,
    )
    match = ApartmentMatch({zone.guid: zone.guid for zone in zones}, (), ())
    return assessment, match


@app.command("archicad-run")
def archicad_run(
    timezone: Annotated[
        str, typer.Option("--timezone", "-z", help="IANA timezone, e.g. Australia/Sydney.")
    ],
    port: Annotated[int, typer.Option("--port", help="Archicad's JSON API port.")] = DEFAULT_PORT,
    area: Annotated[
        str,
        typer.Option("--area", help="Which criterion applies: sydney_metro (2h) or other (3h)."),
    ] = "sydney_metro",
    ruleset: Annotated[
        str, typer.Option("--ruleset", help="Built-in ruleset name, or a path to a YAML file.")
    ] = "nsw_adg",
    year: Annotated[int, typer.Option("--year", help="Which year's 21 June to assess.")] = 2024,
    living_room: Annotated[
        list[str] | None,
        typer.Option("--living-room", help="Zone name identifying a living room. Repeatable."),
    ] = None,
    livable_suffix: Annotated[
        str | None,
        typer.Option(
            "--livable-suffix",
            help="Openings whose ID ends with this are the living-room glazing, e.g. '_L'.",
        ),
    ] = None,
    balcony: Annotated[
        list[str] | None,
        typer.Option("--balcony", help="Name prefix identifying private open space. Repeatable."),
    ] = None,
    apartment_zone_layer: Annotated[
        list[str] | None,
        typer.Option(
            "--apartment-zone-layer",
            help="Archicad layer whose zones are the apartments. Repeatable.",
        ),
    ] = None,
    apartment_zone_name: Annotated[
        list[str] | None,
        typer.Option(
            "--apartment-zone-name",
            help=(
                "Only zones with this name are apartments. Repeatable. Needed "
                "where a layer mixes dwellings and balconies -- one project has "
                "15 units named G08 and 20 balconies named BY together on "
                "'06 | Zone.Units', and a balcony counted as an apartment just "
                "looks like a flat with no living room."
            ),
        ),
    ] = None,
    open_space_zone_layer: Annotated[
        list[str] | None,
        typer.Option(
            "--open-space-zone-layer",
            help="Archicad layer whose zones are private open space. Repeatable.",
        ),
    ] = None,
    open_space_zone_name: Annotated[
        list[str] | None,
        typer.Option(
            "--open-space-zone-name",
            help=(
                "Only zones with this name are private open space. Repeatable. "
                "Needed where one layer carries both, as '06 | Zone.Units' does "
                "with 15 units named G08 and 20 balconies named BY."
            ),
        ),
    ] = None,
    grid: Annotated[float, typer.Option("--grid", help="Sample grid spacing in metres.")] = 0.2,
    offset: Annotated[
        float, typer.Option("--offset", help="Outward sample offset from glazing, in metres.")
    ] = 0.05,
    exclude_above: Annotated[
        float | None,
        typer.Option(
            "--exclude-above",
            help=(
                "Drop geometry lying entirely above this height, in project "
                "metres. Hotlinked unit-type masters are parked overhead and "
                "export on the same layers as the real building, so nothing "
                "else separates them. The overhead warning reports the height "
                "to use."
            ),
        ),
    ] = None,
    write: Annotated[
        bool,
        typer.Option("--write/--no-write", help="Write the results back onto the project's Zones."),
    ] = False,
    draw: Annotated[
        bool,
        typer.Option(
            "--draw/--no-draw",
            help=(
                "Draw the result on the floor plan: one coloured fill per apartment "
                "plus a legend, on a dedicated layer. Replaces the previous run's."
            ),
        ),
    ] = False,
    layer: Annotated[
        str,
        typer.Option("--layer", help="Archicad layer to draw the result on."),
    ] = DEFAULT_LAYER_NAME,
    sheet: Annotated[
        bool,
        typer.Option(
            "--sheet/--no-sheet",
            help=(
                "Also make a Layout carrying one linked Drawing per storey that "
                "has fills. The Drawings stay linked, so re-running the study "
                "updates the sheet. Needs --draw."
            ),
        ),
    ] = False,
    sheet_name: Annotated[
        str,
        typer.Option("--sheet-name", help="Name for the Layout that --sheet creates."),
    ] = SHEET_NAME,
    layout_subset: Annotated[
        str,
        typer.Option(
            "--layout-subset",
            help=(
                "Layout Book subset the clock-time sheets are filed under, so "
                "they sit with the practice's own drawings of that kind rather "
                "than at the root of the book. Named, never created: the book "
                "is the office's structure. Empty leaves them at the root."
            ),
        ),
    ] = "SHADOW DIAGRAMS",
    adg_subset: Annotated[
        str,
        typer.Option(
            "--adg-subset",
            help=(
                "Layout Book subset for the sheets that are not a time of day "
                "-- the banded plan, the two-hour plan. Empty leaves them at "
                "the root."
            ),
        ),
    ] = "ADG DIAGRAMS",
    series_worksheet: Annotated[
        str | None,
        typer.Option(
            "--series-worksheet",
            help=(
                "Draw the nine-to-three series into this Worksheet: one small "
                "plan per instant, the sun patch on the floor of each "
                "apartment. The worksheet must already exist -- one created "
                "through the API cannot be opened in the same session -- and "
                "it is regenerated in full on every run."
            ),
        ),
    ] = None,
    patch_grid: Annotated[
        float | None,
        typer.Option(
            "--patch-grid",
            help=(
                "Grid spacing in metres for the floor patch. Enables the "
                "patch, which is a second ray-cast pass and roughly doubles "
                "the run. Implied by --series-worksheet."
            ),
        ),
    ] = None,
    plan_instant: Annotated[
        list[str] | None,
        typer.Option(
            "--plan-instant",
            help=(
                "Draw the sun patch on the floor plan at this time, e.g. "
                "'12:00'. Repeatable, one layer per instant, with the assessed "
                "area outlined and the lit areas annotated. This is the study "
                "drawing; --draw is the whole-day summary."
            ),
        ),
    ] = None,
    plan_ground: Annotated[
        float | None,
        typer.Option(
            "--plan-ground",
            help=(
                "Assess and draw the open ground at this height, in project "
                "metres -- solar access to the public domain and communal "
                "open space. Building footprints are removed, so what is "
                "banded is ground somebody can stand on."
            ),
        ),
    ] = None,
    plan_bands: Annotated[
        bool,
        typer.Option(
            "--plan-bands/--no-plan-bands",
            help=(
                "Draw the whole day banded by hours of sun on the floor, with "
                "a legend carrying each band's area and share -- the reference "
                "study's own summary sheet. Implies --patch-grid."
            ),
        ),
    ] = False,
    plan_hours: Annotated[
        float | None,
        typer.Option(
            "--plan-hours",
            help=(
                "Draw the floor area receiving at least this many hours, as "
                "its own diagram. 2 is the ADG figure. Implies --patch-grid."
            ),
        ),
    ] = None,
    series_every: Annotated[
        int,
        typer.Option(
            "--series-every",
            help="Draw every Nth instant. 3 turns a 10 minute step into half-hourly.",
        ),
    ] = 3,
    master_layout: Annotated[
        str | None,
        typer.Option(
            "--master-layout",
            help=(
                "Master layout to build the sheet on. Archicad refuses to make "
                "a Layout without one. Defaults to a master whose name states "
                "the drawing scale, and the run says which it used."
            ),
        ),
    ] = None,
    layer_combination: Annotated[
        str | None,
        typer.Option(
            "--layer-combination",
            help=(
                "Export with this layer combination of the project's own, by "
                "name -- an office IFC export combination is usually the right "
                "one. It is copied onto the layers rather than activated, "
                "which nothing in the add-on can do, and the layers are put "
                "back afterwards. Without this the tool uses its own: every "
                "layer on except those named with --hide-layer."
            ),
        ),
    ] = None,
    require_layer: Annotated[
        list[str] | None,
        typer.Option(
            "--require-layer",
            help=(
                "Stop unless this layer is visible when the export runs. "
                "Repeatable. Now a backstop rather than the first line of "
                "defence -- the run sets the layer state itself -- so this "
                "catches a combination that hides something the study needs."
            ),
        ),
    ] = None,
    hide_layer: Annotated[
        list[str] | None,
        typer.Option(
            "--hide-layer",
            help=(
                "Layer to switch off in this tool's layer combinations, and so "
                "on the sheets it makes. Repeatable. For the project's own "
                "annotation layers, which clutter a study drawing without "
                "adding to it."
            ),
        ),
    ] = None,
    allow_georeference_mismatch: Annotated[
        bool,
        typer.Option(
            "--allow-georeference-mismatch",
            help=(
                "Continue when the live project and its export disagree about "
                "the site. For testing the Archicad round trip only -- the "
                "numbers it produces are not usable."
            ),
        ),
    ] = False,
    pen: Annotated[
        list[str] | None,
        typer.Option(
            "--pen",
            help=(
                "Override a band's fill pen as 'label=index', e.g. '2-3 hrs=42'. "
                "Repeatable. Pen indices mean whatever the project's pen table "
                "says, so the defaults are a guess and the run echoes what it used."
            ),
        ),
    ] = None,
    ifc_out: Annotated[
        Path | None,
        typer.Option("--ifc-out", help="Keep the exported IFC here instead of discarding it."),
    ] = None,
    csv_out: Annotated[
        Path | None, typer.Option("--csv", help="Write per-apartment results as CSV.")
    ] = None,
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Write per-apartment results as JSON.")
    ] = None,
) -> None:
    """Export the open project, assess it, and optionally write results back.

    The geometry travels by IFC rather than over the JSON API: that path is
    the one covered by tests and a golden file, and the export is the same
    file the office would produce by hand. Archicad's own answer about where
    the site is gets cross-checked against the export before any number is
    printed, so a lost or re-interpreted georeference stops the run.
    """
    banner()
    connection = _connect(port)
    typer.echo(describe_connection(connection))
    typer.echo("")

    config = scene_config(
        timezone=timezone,
        living_room=living_room,
        livable_suffix=livable_suffix,
        balcony=balcony,
        apartment_zone_layer=apartment_zone_layer,
        apartment_zone_name=apartment_zone_name,
        open_space_zone_layer=open_space_zone_layer,
        open_space_zone_name=open_space_zone_name,
        grid=grid,
        offset=offset,
        exclude_above=exclude_above,
        # A series needs the patch, so asking for one is asking for both.
        patch_grid=patch_grid
        or (
            DEFAULT_PATCH_GRID_M
            if (series_worksheet or plan_instant or plan_bands or plan_hours)
            else None
        ),
    )

    with tempfile.TemporaryDirectory(prefix="sun-study-") as scratch:
        destination = ifc_out or Path(scratch) / "archicad-export.ifc"
        try:
            location = read_geo_location(connection)
            typer.echo(f"  exporting IFC to {destination} ...")
            cleared = clear_selection(connection)
            if cleared:
                typer.secho(
                    f"  cleared a selection of {cleared} element(s) first: with one "
                    f"in place the translator exports the selection alone, and the "
                    f"run would analyse an empty model",
                    fg=typer.colors.YELLOW,
                )
            # The translator exports what is *shown*, so the layer state is an
            # input to every number below. It is set here and put back after,
            # rather than checked and complained about: what somebody left on
            # screen is not a decision about the study.
            with export_state(
                connection,
                combination=layer_combination,
                # What the study needs on regardless of what the base shows:
                # neither of the reference project's own export combinations
                # shows its zone layers, and an export with no IfcSpace in it
                # reports no apartments at all.
                require=(
                    *(apartment_zone_layer or []),
                    *(open_space_zone_layer or []),
                    *(require_layer or []),
                ),
                hide=tuple(hide_layer or ()),
            ) as plan:
                typer.echo(plan.describe())
                # Still worth asking, and now about the state actually in force:
                # a layer the combination itself hides is a real problem, and one
                # Archicad has never heard of is a typo.
                _warn_if_zone_layers_hidden(
                    connection, [*(apartment_zone_layer or []), *(open_space_zone_layer or [])]
                )
                # Separately, because a missing context layer is not a missing
                # apartment: the run would succeed and simply overstate the sun.
                _warn_if_zone_layers_hidden(
                    connection, require_layer or [], role="layers named by --require-layer"
                )
                exported = export_ifc(connection, destination)
        except ArchicadError as error:
            typer.secho(str(error), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2) from error

        try:
            result = run_assessment(
                exported,
                timezone=timezone,
                ruleset=ruleset,
                area=area,
                year=year,
                scene_config=config,
            )
        except GeoreferencingError as error:
            typer.secho(f"Georeferencing error: {error}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2) from error
        except RulesetError as error:
            typer.secho(f"Ruleset error: {error}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2) from error

        # Before any number reaches the screen. A mismatch here means the two
        # halves of this run describe different sites, and every figure below
        # would be plausible and wrong.
        try:
            cross_check_georeferencing(location, result.model)
            typer.secho(
                "  georeferencing cross-check passed: the live project and its "
                "export place the site alike",
                fg=typer.colors.GREEN,
            )
        except ArchicadError as error:
            if not allow_georeference_mismatch:
                typer.secho(str(error), fg=typer.colors.RED, err=True)
                raise typer.Exit(code=2) from error
            typer.secho(str(error), fg=typer.colors.YELLOW, err=True)
            typer.secho(
                "  CONTINUING ANYWAY because --allow-georeference-mismatch was "
                "given. Every figure below is computed from a site the two "
                "sources disagree about. Do not quote any of it.",
                fg=typer.colors.RED,
                bold=True,
            )
        typer.echo("")
        report_assessment(result, timezone=timezone, area=area, csv_out=csv_out, json_out=json_out)

        partial = False
        if write or draw or plan_instant or plan_bands or plan_hours or plan_ground is not None:
            # One join, shared. Two independent ones would agree almost always,
            # and the time they did not would be a diagram whose colours
            # belonged to the neighbouring apartments.
            try:
                match = match_apartments(connection, result.assessment)
            except ArchicadError as error:
                typer.secho(str(error), fg=typer.colors.RED, err=True)
                raise typer.Exit(code=2) from error

        if write:
            typer.echo("")
            try:
                written = write_assessment(connection, result.assessment, match=match)
            except ArchicadError as error:
                # Not fatal when a drawing was also asked for. A fill is
                # geometry on a layer: it needs no property, no classification
                # and no schedule. An earlier version exited here, so a run
                # asking for --write --draw against a project that had never
                # had init-properties produced neither the values nor the
                # picture, and the picture was the part that needed nothing.
                typer.secho(str(error), fg=typer.colors.RED, err=True)
                if not draw:
                    raise typer.Exit(code=2) from error
                typer.secho(
                    "  Continuing to the drawing, which needs no properties.",
                    fg=typer.colors.YELLOW,
                )
                partial = True
            else:
                report_write(connection, written)
                if not written.complete:
                    partial = True
                    typer.secho(
                        "  The project now holds a partial set of results. Schedules "
                        "built on them will be missing rows.",
                        fg=typer.colors.RED,
                    )

        if draw:
            typer.echo("")
            try:
                styles = resolve_bands(connection, pen)
                drawn = draw_assessment(
                    connection,
                    result.assessment,
                    read_zones(connection),
                    zone_by_apartment=match.by_apartment,
                    bands=styles,
                    layer_name=layer,
                    title=f"Solar access {result.assessment.ruleset_identifier}",
                )
            except ArchicadError as error:
                typer.secho(str(error), fg=typer.colors.RED, err=True)
                raise typer.Exit(code=2) from error

            partial = partial or not drawn.complete
            typer.secho(
                drawn.describe(),
                fg=typer.colors.GREEN if drawn.complete else typer.colors.RED,
                bold=True,
            )
            typer.echo("  band to pen: " + ", ".join(f"{b.label}={b.fill_pen}" for b in styles))
            typer.secho(
                "  Check the colours against the project's pen table. A wrong pen "
                "index draws a plausible diagram in the wrong colours.",
                fg=typer.colors.YELLOW,
            )
            if sheet:
                report_layout(
                    connection,
                    drawn.storeys,
                    name=sheet_name,
                    master=master_layout,
                    zoom=assessed_extent(read_zones(connection), match, margin_m=10.0),
                )

        # One clearing out and one folder for the whole run, before any sheet
        # is made: the two sheet-building steps would otherwise delete each
        # other's work and file their views separately.
        view_folder = ""
        if sheet:
            gone, left = remove_previous(connection)
            view_folder = next_view_folder(connection)
            typer.echo("")
            typer.echo(f"  views go in {view_folder!r}")
            if gone:
                typer.echo(f"  removed {gone} views and layouts from the previous run")
            if left:
                typer.secho(
                    f"  {left} could not be removed and are still there. A View "
                    f"a placed Drawing points at cannot be deleted, and Archicad "
                    f"reports success either way.",
                    fg=typer.colors.YELLOW,
                )

        if plan_instant:
            typer.echo("")
            partial = (
                report_penetration(
                    connection,
                    result,
                    wanted=plan_instant,
                    match=match,
                    layer_prefix=layer,
                    sheets=sheet,
                    master_layout=master_layout,
                    folder=view_folder,
                    also_hide=tuple(hide_layer or ()),
                    instant_subset=layout_subset,
                    analysis_subset=adg_subset,
                )
                or partial
            )

        if plan_ground is not None:
            typer.echo("")
            partial = (
                report_ground(
                    connection,
                    result,
                    level_m=plan_ground,
                    match=match,
                    layer_prefix=layer,
                    spacing_m=patch_grid or DEFAULT_PATCH_GRID_M,
                )
                or partial
            )

        if plan_bands or plan_hours:
            typer.echo("")
            partial = (
                report_area_bands(
                    connection,
                    result,
                    match=match,
                    layer_prefix=layer,
                    bands=plan_bands,
                    hours=plan_hours,
                    csv_out=csv_out,
                    sheets=sheet,
                    master_layout=master_layout,
                    folder=view_folder,
                    also_hide=tuple(hide_layer or ()),
                    storeys=sorted(
                        {
                            zone.storey_index
                            for zone in read_zones(connection)
                            if zone.guid in set(match.by_apartment.values())
                            and zone.storey_index is not None
                        }
                    ),
                    zoom=assessed_extent(read_zones(connection), match, margin_m=10.0),
                    instant_subset=layout_subset,
                    analysis_subset=adg_subset,
                )
                or partial
            )

        if series_worksheet:
            typer.echo("")
            partial = (
                report_series(
                    connection,
                    result,
                    worksheet_name=series_worksheet,
                    every=series_every,
                    layer_name=layer,
                )
                or partial
            )

    typer.echo("")
    typer.secho(DISCLAIMER, fg=typer.colors.YELLOW)
    if partial:
        # A non-zero exit so a partial write cannot pass unnoticed in a script.
        raise typer.Exit(code=3)


def main() -> None:
    """Entry point. Makes output degrade rather than abort.

    Windows consoles default to a legacy code page, and Python then raises
    ``UnicodeEncodeError`` on any character it cannot represent -- killing the
    command mid-listing and losing everything the human was after.

    That is not hypothetical. A real Archicad project carried a property name
    containing a subscript digit, and listing the project's properties died on
    it. Nobody chose that character and nobody can predict the next one: an
    Archicad file is full of names from libraries, add-ons and other people's
    templates. Windows is the primary deployment platform here, so an
    unrepresentable character has to become a question mark, not an exception.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:  # pragma: no branch - always present on 3.11+
            reconfigure(errors="replace")
    app()


if __name__ == "__main__":
    main()
