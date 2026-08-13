# sun-study

Direct sunlight hours from an Archicad model, written back into Archicad as Zone
properties, so an ADG compliance table becomes a native Archicad schedule instead of a
hand-typed document.

---

> ## ⚠ PROTOTYPE — NOT VALIDATED FOR SUBMISSION
>
> This tool is for **design-stage iteration only**. It has not yet been validated
> against a reference implementation on a real project, and its output **must not be
> used as a compliance figure in a DA submission**. A DA submission rests on a
> consultant's report.
>
> A reference comparison against the office's existing Grasshopper/Ladybug chain now
> passes within a stated tolerance — 0.19 percentage points on the headline metric, with
> 98.4% of facade area landing in exactly the same band face by face. That result is
> **conditional on a true-north value that was recovered rather than supplied**, so this
> notice stays until north is confirmed against the Grasshopper definition. See
> [`docs/validation.md`](docs/validation.md) §6.

---

## What it does

Computes how many hours of direct sunlight each apartment's living room windows and
private open space receive on 21 June between 09:00 and 15:00, assesses that against
the NSW Apartment Design Guide, and writes the result back into the Archicad project
as Zone property values.

It is a **geometric** sunlight-hours tool. There is no daylight factor, illuminance,
radiation, glare or energy modelling, and no weather file is involved — sun position is
pure astronomy from latitude, longitude and time.

## Status

| Milestone | Deliverable | State |
|---|---|---|
| M0 | Skeleton, CI, tooling, `core.solar` | **Done** — validated to 0.1° against NREL/TP-560-34302 |
| M1 | `core` engine on synthetic geometry | **Done** — all six §7 analytic cases green |
| M2 | IFC ingest and scene assembly | **Done** — fixture loads with correct north, units, containment |
| M3 | ADG ruleset, CSV/JSON output | **Done** — per-apartment results for the fixture |
| — | Massing mode: facade/ground area bands | **Done** — the metric a massing optimisation maximises |
| M4 | Archicad read adapter | **Written, untested against Archicad** — every command verified against the Tapir sources; needs the [checklist](docs/archicad.md#manual-test-checklist) run at a workstation |
| M5 | Archicad write-back | **Written, untested against Archicad** — same |
| M6 | Validation against Ladybug | **Within tolerance** — 0.19 pt on the headline metric, 98.4% per-face; conditional on confirming north |

## Architecture

```
archicad  ->  report  ->  ingest  ->  rules  ->  core
```

The arrow points one way. `core` is pure geometry and astronomy — no I/O, no Archicad,
no IFC — so its correctness is established entirely by tests that run in CI without an
Archicad licence. Everything that talks to Archicad lives in the `archicad` package and
nowhere else.

This is enforced, not merely documented: `tests/unit/test_architecture.py` walks the
AST of every module and fails the build on a violation, including on any third-party
import creeping into `core`.

If a compliance rule or a geometry operation ends up inside the Archicad adapter, it is
in the wrong place.

## Usage

```bash
uv run sun-study info  model.ifc --timezone Australia/Sydney   # what the tool reads
uv run sun-study rulesets                                      # thresholds and citations
uv run sun-study run   model.ifc --timezone Australia/Sydney \
    --area sydney_metro --csv results.csv --json results.json
```

For massing studies, before there are any apartments:

```bash
uv run sun-study massing model.ifc --timezone Australia/Sydney \
    --facade-grid 1.0 --ground-grid 1.0 --csv bands.csv
```

That reports the share of **facade area** and of **open ground** reaching two hours,
banded hourly with square metres and percentages — the metric a massing optimisation
loop maximises. It is not the ADG per-apartment criterion and the output says so:
before apartments exist the compliance figure cannot be computed at all.

`--area sydney_metro` is the 2-hour criterion; `--area other` is the 3-hour one that
applies outside Sydney Metro, Newcastle and Wollongong. Every run echoes the resolved
site, north bearing, ruleset version, continuity setting and room-matching rule before
printing a single number, so a wrong assumption is visible before a figure gets quoted.

## Against a running Archicad

With Archicad 26 open and the [Tapir add-on](https://github.com/ENZYME-APD/tapir-archicad-automation/releases)
installed (1.5.1 or newer):

```bash
uv run sun-study archicad-info                                  # what is Archicad reporting?
uv run sun-study init-properties                                # create the 'Sun Study' properties
uv run sun-study archicad-run --timezone Australia/Sydney --write
```

`archicad-run` exports an IFC from the open project, assesses it, and writes the result
onto the Zones so an ADG table becomes a native Archicad schedule. Write-back is opt-in:
without `--write` it only reports.

Geometry travels by IFC rather than over the JSON API, because that path is the one
covered by a fixture, a golden file and the Ladybug comparison — and the export is the
same file a colleague would produce by hand. Archicad's own georeferencing is then
cross-checked against the export, and a disagreement stops the run before any number is
printed.

**This package is not covered by CI**, because there is no Archicad to run against.
That is the reason `core` is kept pure: what can be tested is tested exhaustively, and
the part that cannot is kept thin, boring and free of analysis logic. Its request and
response handling *is* machine-checked, through a fake transport, in
`tests/unit/test_archicad_adapter.py`. The rest needs a human —
[`docs/archicad.md`](docs/archicad.md) records every command with its source and version,
the two facts that could not be verified without an Archicad, and the checklist that
settles them.

Try it on the committed fixture:

```bash
uv run sun-study run tests/fixtures/sample_building.ifc --timezone Australia/Sydney
```

## Rules are data

Thresholds live in [`src/sun_study/rules/rulesets/nsw_adg.yaml`](src/sun_study/rules/rulesets/nsw_adg.yaml)
with a citation for every number, quoted from the NSW Department of Planning technical
note. The assessment engine reads a ruleset and does not know what the ADG is, so a
council DCP requiring three *continuous* hours is a new YAML file and no new code — and
a test proves it.

Citations are enforced by the schema: a ruleset with a blank citation fails to load.

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                  # create the environment
uv run pytest            # run the suite
uv run ruff check .      # lint
uv run ruff format --check .
uv run mypy              # strict type checking
```

CI runs the same four commands on Ubuntu and Windows. Windows is the primary
deployment platform — the office runs Archicad 26 on Windows — so it is a first-class
CI target rather than an afterthought.

## Validation

The failure mode of a tool like this is being *plausibly* wrong, which nobody catches
by looking at a colourful mesh. Sun positions are therefore checked against two
independent published sources, both transcribed from the primary documents:

- **NREL/TP-560-34302** (Reda & Andreas), Table A4.1 for Julian Day and Table A5.1 for
  a fully worked solar position example. This is a *different and more accurate*
  algorithm, so agreement demonstrates absolute correctness.
- **NOAA's own published spreadsheet**, `NOAA_Solar_Calculations_day.ods`. This is the
  *same* algorithm, so it must agree to floating-point precision — which catches a
  single mistyped coefficient that a 0.1° tolerance would hide.

A third check cross-references `pvlib`'s independent SPA implementation across a year
of positions at seven latitudes. `pvlib` is a validation reference only and is never a
runtime dependency.

The geometry engine is checked the same way. All six analytic cases from the brief are
green: shadow lengths and overhang shadow lines land within 1 cm of closed-form
trigonometry, rotating the scene and true north together leaves results invariant to
1e−9, and a south-facing Sydney facade returns exactly zero midwinter sun — the tripwire
for inverted north handling. The BVH is checked differentially against brute force on
randomised scenes at every leaf size.

The ray caster is pure numpy with no native extension, so it installs on a Windows
workstation without a compiler. That makes it the production backend rather than a CI
fallback, so its speed is measured too: about 38k rays/s, roughly 20 seconds for a
200 apartment job (`uv run python scripts/benchmark_occlusion.py`).

See [`docs/validation.md`](docs/validation.md) for measured residuals,
[`docs/decisions.md`](docs/decisions.md) for the domain assumptions that change the
headline compliance percentage, and [`docs/archicad.md`](docs/archicad.md) for what was
verified about the Archicad protocol and what was not.

## The fixture

`tests/fixtures/sample_building.ifc` is a fictional four-apartment building generated by
`make_sample_building.py`. It is deliberately awkward in the ways real Archicad exports
are: millimetre units, true north 30° off project north, and one window with no space
boundary so the geometric containment fallback is exercised by a real file.

It is byte-for-byte reproducible, and a test regenerates it and compares bytes so the
committed file cannot drift from its generator.

```bash
uv run python tests/fixtures/make_sample_building.py   # regenerate the fixture
SUN_STUDY_UPDATE_GOLDEN=1 uv run pytest                # regenerate golden results
```

## Repository hygiene

This repository is public. It contains no client project files, no IFC exports of live
projects, and no site addresses. Test fixtures are synthetic geometry generated by the
tool, or a deliberately fictional sample building committed on purpose.

## Licence

MIT. See [`LICENSE`](LICENSE).
