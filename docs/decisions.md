# Decisions

Domain assumptions matter more than the code here. A wrong one produces confident,
wrong numbers that no amount of testing catches, because the arithmetic is fine — it is
being applied to the wrong windows.

Every decision that changes a headline compliance percentage is recorded here with the
option taken and the reasoning. **Open** decisions are ones the tool cannot sensibly
default; they are listed with a proposed default and must be settled before the
milestone that depends on them.

---

## Settled

### D1 — Solar position: NOAA in-house, pvlib as a dev-only reference

*Milestone M0. Alternatives: pvlib as a runtime dependency; NOAA with no cross-check.*

`core.solar` implements the NOAA algorithm directly, roughly 250 lines and no new
runtime dependency. `pvlib` is installed only in the dev group, where CI uses it to
cross-check a year of positions at seven latitudes against an independent SPA
implementation.

Taking pvlib at runtime would have pulled pandas and scipy into every office install
for a geometry tool, and its pandas-indexed API is an awkward fit for the numpy
`(n_points × n_suns)` ray batching the occlusion engine needs. Keeping it as a
validation reference gets the cross-check without the weight, and mirrors how Ladybug
is treated: a thing we measure against, never a thing we depend on.

Measured accuracy is in [`validation.md`](validation.md).

### D2 — `tzdata` is a runtime dependency

*Milestone M0.*

A pure-data wheel, no code. Python's `zoneinfo` reads the system tz database first and
falls back to this package; Windows ships no system tz database at all, and Windows is
the primary deployment platform. Declared unconditionally rather than Windows-only so
that Linux CI exercises the same code path the office machines will.

### D3 — Timezone is explicit configuration, never inferred

*Milestone M0. Alternative: derive from latitude/longitude via `timezonefinder`.*

The run configuration must name an IANA zone. There is no default and no inference, and
an unknown or missing zone raises `UnknownTimezoneError` rather than falling back.

IFC carries latitude and longitude but no reliable IANA timezone, so something has to
decide that 21 June 09:00 in Sydney means UTC+10. Deriving it from coordinates would
add a dependency with tens of megabytes of boundary data and, worse, make the decision
invisible — exactly the silent-wrong-answer hazard the brief flags around north and
geolocation. An hour of error moves every sun position by 15° of hour angle.

The resolved zone is echoed in the console banner and in every output file header.

### D4 — Azimuth uses the `atan2` form, not NOAA's `arccos` form

*Milestone M0.*

Algebraically identical; NOAA's form divides by `cos(lat) · sin(zenith)`, which is zero
at the poles and at the sub-solar point. `test_azimuth_forms_agree` asserts the two
agree to 1e−9° across a dense grid of latitudes and dates, so the substitution is
verified rather than assumed.

### D5 — Sun vectors use apparent (refracted) elevation

*Milestone M0.*

`SolarPosition` exposes both `elevation_deg` (refraction-corrected) and
`true_elevation_deg` (geometric). `unit_vectors_enu()` defaults to the apparent
position, because refraction is why a low sun's shadow falls where it actually falls.
Near the horizon the two differ by about half a degree, which is roughly one sun
diameter — not negligible for a 9am midwinter assessment. `apparent=False` is available
and the choice will be stated in the output header.

---

## Open — needed before M2 (IFC ingest)

### D6 — What counts as a "living room window"

**This is the one that most changes the headline percentage**, and Archicad will not
tell us. Options: Zone category convention, a window property flag, or a naming
convention.

*Proposed default:* Zone category on the parent `IfcSpace`, with the mapping declared
per project in the run config rather than hardcoded, and the resolved mapping echoed
in the output header. A window inherits "living room" from the space it serves.

*Also needs deciding:* what happens to a studio apartment where the living space and
bedroom are one Zone, and to a space with no category set. Proposed: fail loudly and
list the offending Zone GUIDs rather than silently assessing or silently skipping.

### D7 — Balcony geometry source

Zones are cleanest, but offices do not always zone balconies. A slab-based fallback
needs a rule for which slabs count.

*Proposed default:* Zones where present. Where absent, fall back to slabs on a
configured layer, and **report the count of apartments resolved by each route in the
output header** so a silent fallback cannot be mistaken for a clean run.

### D8 — Glazing extent

Whole window opening versus glazed area net of frame.

*Proposed default:* whole opening. Frames are typically ignored in this kind of
assessment, it is the more conservative reading, and the IFC opening is what is
reliably available. To be confirmed and recorded in the output.

### D9 — Context building extent

Radius cutoff, and whether approved-but-unbuilt developments are included.

*Proposed default:* configurable radius, no default value — the run must state one.
Approved-but-unbuilt excluded unless explicitly listed. Both stated in the output.

### D10 — Vegetation

*Proposed default:* excluded, always, and said so explicitly in the output header
rather than left as an unstated assumption.

---

## Open — needed before M3 (aggregation and rules)

### D11 — Sample weighting across the assessment window

Not in the brief's list, but found while writing `assessment_times`, and it is the same
class of error.

09:00–15:00 at a 10-minute step yields **37** instants, not 36, because both endpoints
are included. Multiplying a count of sunlit instants by the timestep therefore reports
up to 370 minutes of sun in a 360-minute window — an apartment can come out at 6.17
hours. It is a small bias, always optimistic, and it lands right on the 2-hour
threshold that decides compliance.

*Proposed default:* trapezoidal weighting, so the two endpoints count half. That
preserves the 37 published sun positions, totals exactly 360 minutes, and is the
standard treatment. The alternative — half-open intervals with 36 samples — is also
defensible and simpler; it just discards the 15:00 position.

Whichever is chosen must be stated in the output header, not left implicit.

### D12 — `continuity`: cumulative versus continuous

Councils differ; some DCPs require an unbroken duration.

*Proposed default:* `cumulative`, set in the ruleset YAML, printed in every output
header, and carried on every result record alongside the ruleset name and version. It
must never be an invisible assumption, because the same building passes under one
reading and fails under the other.
