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
    ArchicadConnection,
    ArchicadError,
    HttpTransport,
)
from sun_study.archicad.draw import (
    DEFAULT_BANDS,
    DEFAULT_LAYER_NAME,
    BandStyle,
    draw_assessment,
)
from sun_study.archicad.read import (
    ArchicadZone,
    classification_item_names,
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
    all_properties,
    default_property_value,
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
    except ArchicadError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from error
    return connection


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

    _report_zone_names(found, limit=zone_names)

    if properties:
        _report_properties(connection)


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

        chosen = found if count <= 0 else found[:count]
        assessment, match = _synthetic_assessment(chosen, band_styles(pen))
        typer.echo(f"  testing against {len(chosen)} of {len(found)} zones")

        if properties:
            classified = classification_items_of(connection, [zone.guid for zone in chosen])
            init_properties(connection, classified)
            typer.echo(f"  properties ready in group {PROPERTY_GROUP_NAME!r}")

            written = write_assessment(
                connection, assessment, match=match, run_stamp="SELFTEST -- not a measurement"
            )
            typer.secho(
                written.describe(),
                fg=typer.colors.GREEN if written.complete else typer.colors.RED,
                bold=True,
            )

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
                bands=band_styles(pen),
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

        if draw:
            typer.echo("")
            try:
                styles = band_styles(pen)
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
