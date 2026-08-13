"""Command line interface. No GUI, per brief section 3.

Every run prints the disclaimer, the resolved site and the ruleset before any
number, so the human can catch a wrong location, a wrong north or a wrong
threshold before reading a result rather than after quoting one.
"""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path
from typing import Annotated

import typer

from sun_study import __version__
from sun_study.archicad.connection import (
    DEFAULT_PORT,
    ArchicadConnection,
    ArchicadError,
    HttpTransport,
)
from sun_study.archicad.read import (
    classification_items_of,
    cross_check_georeferencing,
    describe_connection,
    export_ifc,
    read_geo_location,
)
from sun_study.archicad.read import zones as read_zones
from sun_study.archicad.write import (
    APARTMENT_PROPERTIES,
    PROPERTY_GROUP_NAME,
    init_properties,
    write_assessment,
)
from sun_study.disclaimer import DISCLAIMER, STATUS
from sun_study.ingest.ifc import GeoreferencingError, read_ifc
from sun_study.ingest.scene import DEFAULT_MASSING_SPACING_M, MassingConfig, SceneConfig
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
    balcony: Annotated[
        list[str] | None,
        typer.Option("--balcony", help="Name prefix identifying private open space. Repeatable."),
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

    config = SceneConfig(
        timezone=timezone,
        living_room_space_names=tuple(living_room) if living_room else ("Living Room",),
        balcony_name_prefixes=tuple(balcony) if balcony else ("Balcony",),
        grid_spacing_m=grid,
        surface_offset_m=offset,
        context_radius_m=context_radius,
    )

    try:
        result = run_assessment(
            ifc, timezone=timezone, ruleset=ruleset, area=area, year=year, scene_config=config
        )
    except GeoreferencingError as error:
        typer.secho(f"Georeferencing error: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from error
    except RulesetError as error:
        typer.secho(f"Ruleset error: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from error

    report_assessment(result, timezone=timezone, area=area, csv_out=csv_out, json_out=json_out)
    typer.echo("")
    typer.secho(DISCLAIMER, fg=typer.colors.YELLOW)


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
    except ArchicadError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from error
    return connection


@app.command("archicad-info")
def archicad_info(
    port: Annotated[int, typer.Option("--port", help="Archicad's JSON API port.")] = DEFAULT_PORT,
) -> None:
    """Check the connection to a running Archicad and echo what it reports.

    Run this first. It fails on a missing add-on, an unset project location or
    an unclassified model -- the three things that otherwise surface much later
    as a confusing error in the middle of a real run.
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
    balcony: Annotated[
        list[str] | None,
        typer.Option("--balcony", help="Name prefix identifying private open space. Repeatable."),
    ] = None,
    grid: Annotated[float, typer.Option("--grid", help="Sample grid spacing in metres.")] = 0.2,
    offset: Annotated[
        float, typer.Option("--offset", help="Outward sample offset from glazing, in metres.")
    ] = 0.05,
    write: Annotated[
        bool,
        typer.Option("--write/--no-write", help="Write the results back onto the project's Zones."),
    ] = False,
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

    config = SceneConfig(
        timezone=timezone,
        living_room_space_names=tuple(living_room) if living_room else ("Living Room",),
        balcony_name_prefixes=tuple(balcony) if balcony else ("Balcony",),
        grid_spacing_m=grid,
        surface_offset_m=offset,
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
        except ArchicadError as error:
            typer.secho(str(error), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2) from error

        typer.secho(
            "  georeferencing cross-check passed: the live project and its "
            "export place the site alike",
            fg=typer.colors.GREEN,
        )
        typer.echo("")
        report_assessment(result, timezone=timezone, area=area, csv_out=csv_out, json_out=json_out)

        partial = False
        if write:
            typer.echo("")
            try:
                written = write_assessment(connection, result.assessment)
            except ArchicadError as error:
                typer.secho(str(error), fg=typer.colors.RED, err=True)
                raise typer.Exit(code=2) from error

            partial = not written.complete
            typer.secho(
                written.describe(),
                fg=typer.colors.RED if partial else typer.colors.GREEN,
                bold=True,
            )
            if partial:
                typer.secho(
                    "  The project now holds a partial set of results. Schedules "
                    "built on them will be missing rows.",
                    fg=typer.colors.RED,
                )

    typer.echo("")
    typer.secho(DISCLAIMER, fg=typer.colors.YELLOW)
    if partial:
        # A non-zero exit so a partial write cannot pass unnoticed in a script.
        raise typer.Exit(code=3)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
