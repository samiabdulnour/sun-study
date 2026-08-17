"""Command line interface. No GUI, per brief section 3.

Every run prints the disclaimer, the resolved site and the ruleset before any
number, so the human can catch a wrong location, a wrong north or a wrong
threshold before reading a result rather than after quoting one.
"""

from __future__ import annotations

import collections
import datetime as dt
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any

import typer

from sun_study import __version__
from sun_study.archicad.connection import (
    DEFAULT_PORT,
    PORT_RANGE,
    ArchicadConnection,
    ArchicadError,
    ArchicadNotRunningError,
    HttpTransport,
    find_instances,
    where_archicad_actually_is,
)
from sun_study.archicad.draw import (
    DEFAULT_BANDS,
    DEFAULT_LAYER_NAME,
    BandStyle,
    Pen,
    draw_assessment,
    indistinguishable_bands,
    match_pens,
    pen_table,
)
from sun_study.archicad.layout import layout_results
from sun_study.archicad.read import (
    ArchicadZone,
    classification_item_names,
    classification_items_of,
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
from sun_study.archicad.write import (
    APARTMENT_PROPERTIES,
    PROPERTY_GROUP_NAME,
    WriteReport,
    all_properties,
    default_property_value,
    diagnose_write_access,
    ensure_property_group,
    enum_values,
    init_properties,
    match_apartments,
    write_assessment,
)
from sun_study.disclaimer import DISCLAIMER, STATUS
from sun_study.ingest.ifc import GeoreferencingError, read_ifc
from sun_study.ingest.scene import (
    DEFAULT_MASSING_SPACING_M,
    MassingConfig,
    SceneConfig,
    SceneConfigError,
)
from sun_study.pipeline import PipelineResult, run_assessment, run_massing
from sun_study.report.bands_out import (
    build_massing_header,
    write_bands_csv,
    write_bands_json,
)
from sun_study.report.csv_out import write_csv
from sun_study.report.header import build_header
from sun_study.report.json_out import write_json
from sun_study.rules.ruleset import BUILTIN_RULESETS, RulesetError, load_ruleset

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
    grid: Annotated[float, typer.Option("--grid", help="Sample grid spacing in metres.")] = 0.2,
    offset: Annotated[
        float,
        typer.Option("--offset", help="Outward sample offset from glazing, in metres."),
    ] = 0.05,
    context_radius: Annotated[
        float | None,
        typer.Option("--context-radius", help="Drop occluders beyond this many metres."),
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
        open_space_zone_layer=open_space_zone_layer,
        grid=grid,
        offset=offset,
        context_radius=context_radius,
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


def report_layout(connection: ArchicadConnection, storeys: Sequence[int], *, name: str) -> None:
    """Put the drawn storeys on a sheet, and say what happened.

    Never fatal. The fills are already in the project by this point, and a
    sheet that could not be made is a worse reason to fail the run than to
    finish it with a note -- the numbers and the diagram are the deliverable,
    the layout is a convenience on top.
    """
    try:
        placed = layout_results(connection, storeys, layout_name=name)
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
    open_space_zone_layer: list[str] | None = None,
    grid: float = 0.2,
    offset: float = 0.05,
    context_radius: float | None = None,
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
        open_space_zone_layers=tuple(open_space_zone_layer or ()),
        grid_spacing_m=grid,
        surface_offset_m=offset,
        context_radius_m=context_radius,
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


@app.command("archicad-rooms")
def archicad_rooms(
    port: Annotated[int, typer.Option("--port", help="Archicad's JSON API port.")] = DEFAULT_PORT,
    zone_layer: Annotated[
        list[str] | None,
        typer.Option("--zone-layer", help="Archicad layer whose zones are the apartments."),
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
        if not found:
            typer.secho(
                "No apartment zones. Pass --zone-layer, and run 'archicad-info' "
                "to see which layers carry zones.",
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
    open_space_zone_layer: Annotated[
        list[str] | None,
        typer.Option(
            "--open-space-zone-layer",
            help="Archicad layer whose zones are private open space. Repeatable.",
        ),
    ] = None,
    grid: Annotated[float, typer.Option("--grid", help="Sample grid spacing in metres.")] = 0.2,
    offset: Annotated[
        float, typer.Option("--offset", help="Outward sample offset from glazing, in metres.")
    ] = 0.05,
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
    ] = "Sun Study",
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
        open_space_zone_layer=open_space_zone_layer,
        grid=grid,
        offset=offset,
    )

    with tempfile.TemporaryDirectory(prefix="sun-study-") as scratch:
        destination = ifc_out or Path(scratch) / "archicad-export.ifc"
        try:
            location = read_geo_location(connection)
            typer.echo(f"  exporting IFC to {destination} ...")
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
        if write or draw:
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
                typer.secho(str(error), fg=typer.colors.RED, err=True)
                raise typer.Exit(code=2) from error

            report_write(connection, written)
            if not written.complete:
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
                report_layout(connection, drawn.storeys, name=sheet_name)

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
