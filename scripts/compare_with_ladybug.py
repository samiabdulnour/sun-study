"""Compare sun-study against a Ladybug result carried in a Rhino model.

Validation harness for docs/validation.md section 6. Not part of the product,
and it takes the model as an argument because the reference geometry is client
work and must never enter this repository.

    uv run python scripts/compare_with_ladybug.py path/to/model.3dm \
        --latitude -33.8373 --longitude 151.0436

How the comparison is set up
----------------------------
The reference model carries the Ladybug output as colour-mapped meshes: each
face of the analysis grid is painted with the band it fell in. Integrating area
per colour reproduces the study's published table exactly, so those faces are a
per-face ground truth rather than a summary.

The comparison therefore samples **the reference analysis mesh's own faces**
rather than generating an independent grid. That removes grid resolution as a
confound entirely and leaves only the sun positions and the occlusion under
test, which is the part actually being validated.

Both weightings are reported. Ladybug's sunlight-hours component counts sunlit
timesteps, so uniform weighting is the like-for-like comparison; trapezoidal is
this tool's default and is shown alongside so the size of that choice is
visible rather than argued about. See decision D11.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
from pathlib import Path
from typing import Any

import numpy as np

from sun_study.core.analysis import Weighting, cumulative_minutes, instant_weights, sunlit_matrix
from sun_study.core.geometry import TriangleMesh
from sun_study.core.occlusion import Occluder
from sun_study.core.orientation import SiteOrientation
from sun_study.core.sampling import SamplePoints
from sun_study.core.solar import assessment_times, solar_position
from sun_study.ingest.rhino import read_3dm

# Colour -> band, read off the reference model's legend. Verified by
# integrating area per colour and matching the study's published table.
BAND_BY_COLOUR: dict[tuple[int, int, int], str] = {
    (8, 48, 107): "0hr",
    (43, 122, 191): "0-1hr",
    (77, 182, 172): "1-2hrs",
    (230, 238, 156): "2-3hrs",
    (255, 213, 79): "3-4hrs",
    (255, 183, 77): "4-5hrs",
    (244, 81, 30): ">5hrs",
}
BAND_ORDER = ["0hr", "0-1hr", "1-2hrs", "2-3hrs", "3-4hrs", "4-5hrs", ">5hrs"]
# Upper edge in minutes for each band; the last is open.
BAND_UPPER = {"0hr": 0.0, "0-1hr": 60.0, "1-2hrs": 120.0, "2-3hrs": 180.0,
              "3-4hrs": 240.0, "4-5hrs": 300.0, ">5hrs": float("inf")}  # fmt: skip


def band_for(minutes: float) -> str:
    if minutes <= 1e-9:
        return "0hr"
    for name in BAND_ORDER[1:]:
        if minutes < BAND_UPPER[name]:
            return name
    return ">5hrs"


def read_reference_faces(path: Path, layer_fragment: str) -> dict[str, Any]:
    """Centroid, normal, area and reference band for every analysis face."""
    import rhino3dm as r3

    document = r3.File3dm.Read(str(path))
    layers = {i: layer.FullPath for i, layer in enumerate(document.Layers)}

    centroids, normals, areas, bands = [], [], [], []
    unknown = collections.Counter()

    for obj in document.Objects:
        if layer_fragment not in layers.get(obj.Attributes.LayerIndex, ""):
            continue
        mesh = obj.Geometry
        if not isinstance(mesh, r3.Mesh) or len(mesh.VertexColors) == 0:
            continue

        vertices = np.array([[p.X, p.Y, p.Z] for p in mesh.Vertices], dtype=np.float64)
        colours = [mesh.VertexColors[i] for i in range(len(mesh.VertexColors))]

        for index in range(len(mesh.Faces)):
            face = mesh.Faces[index]
            corners = [face[0], face[1], face[2]]
            if face[3] != face[2]:
                corners.append(face[3])
            points = vertices[corners]

            # Fan triangulation gives both the area and a face normal.
            total_area, accumulated = 0.0, np.zeros(3)
            for i in range(1, len(corners) - 1):
                cross = np.cross(points[i] - points[0], points[i + 1] - points[0])
                total_area += 0.5 * float(np.linalg.norm(cross))
                accumulated += cross
            if total_area <= 0.0:
                continue

            length = float(np.linalg.norm(accumulated))
            if length == 0.0:
                continue

            counter = collections.Counter(tuple(colours[c][:3]) for c in corners)
            colour = counter.most_common(1)[0][0]
            band = BAND_BY_COLOUR.get(colour)
            if band is None:
                unknown[colour] += 1
                continue

            centroids.append(points.mean(axis=0))
            normals.append(accumulated / length)
            areas.append(total_area)
            bands.append(band)

    return {
        "centroids": np.asarray(centroids),
        "normals": np.asarray(normals),
        "areas": np.asarray(areas),
        "bands": np.asarray(bands),
        "unknown_colours": unknown,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("--latitude", type=float, required=True)
    parser.add_argument("--longitude", type=float, required=True)
    parser.add_argument("--timezone", default="Australia/Sydney")
    parser.add_argument("--north", type=float, default=0.0, help="Bearing of model +Y.")
    parser.add_argument("--timestep", type=int, default=60, help="Minutes between sun positions.")
    parser.add_argument("--offset", type=float, default=0.10, help="Sample offset, metres.")
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--occluders", default="3D_ Input")
    parser.add_argument("--reference-layer", default="503_ Solar on Facade")
    args = parser.parse_args()

    print("sun-study vs Ladybug reference")
    print("=" * 78)
    print("  ASSUMED, not read from the model (it carries no location or north):")
    print(f"    latitude {args.latitude}  longitude {args.longitude}  tz {args.timezone}")
    print(f"    north bearing of model +Y {args.north} deg")
    print(f"    timestep {args.timestep} min, sample offset {args.offset} m")
    print()

    model = read_3dm(args.model, layer_fragments=(args.occluders,))
    occluder_mesh = TriangleMesh.concatenate([e.mesh for e in model.elements])
    print(
        f"  occluders: {occluder_mesh.triangle_count} triangles from {len(model.elements)} objects"
    )

    reference = read_reference_faces(args.model, args.reference_layer)
    count = len(reference["areas"])
    print(f"  reference: {count} analysis faces, {reference['areas'].sum():.2f} m2")
    if reference["unknown_colours"]:
        print(f"  WARNING unmapped colours: {dict(reference['unknown_colours'])}")
    print()

    site = SiteOrientation(args.latitude, args.longitude, args.timezone, args.north)
    times = assessment_times(
        dt.date(args.year, 6, 21), args.timezone, dt.time(9, 0), dt.time(15, 0), args.timestep
    )
    suns = site.sun_vectors(solar_position(times, args.latitude, args.longitude))
    print(
        f"  {len(times)} sun positions, {times[0].strftime('%H:%M')}-{times[-1].strftime('%H:%M')}"
    )

    points = SamplePoints(
        reference["centroids"] + args.offset * reference["normals"],
        reference["normals"],
        tuple(str(i) for i in range(count)),
        reference["areas"],
    )
    lit = sunlit_matrix(points, Occluder(occluder_mesh), suns)

    for weighting in (Weighting.UNIFORM, Weighting.TRAPEZOIDAL):
        weights = instant_weights(len(times), float(args.timestep), weighting)
        minutes = cumulative_minutes(lit, weights)
        mine = np.array([band_for(m) for m in minutes])

        print()
        print(f"  --- {weighting} weighting (window sums to {weights.sum():g} min) ---")
        columns = ("band", "sun-study m2", "Ladybug m2", "delta m2", "delta %pt")
        widths = (8, 13, 12, 10, 10)
        print("  " + " ".join(f"{c:>{w}}" for c, w in zip(columns, widths, strict=True)))
        total = float(reference["areas"].sum())
        for band in BAND_ORDER:
            a = float(reference["areas"][mine == band].sum())
            b = float(reference["areas"][reference["bands"] == band].sum())
            print(f"  {band:>8} {a:13.2f} {b:12.2f} {a - b:10.2f} {100.0 * (a - b) / total:10.2f}")

        above_mine = float(reference["areas"][minutes >= 120.0].sum())
        above_ref = float(
            reference["areas"][
                np.isin(reference["bands"], ["2-3hrs", "3-4hrs", "4-5hrs", ">5hrs"])
            ].sum()
        )
        agree = float(reference["areas"][mine == reference["bands"]].sum())
        within_one = float(
            reference["areas"][
                np.abs(
                    np.array([BAND_ORDER.index(b) for b in mine])
                    - np.array([BAND_ORDER.index(b) for b in reference["bands"]])
                )
                <= 1
            ].sum()
        )
        print(
            f"  >2hrs   sun-study {above_mine / total:7.2%}   Ladybug {above_ref / total:7.2%}"
            f"   delta {100 * (above_mine - above_ref) / total:+.2f} pt"
        )
        print(
            f"  per-face agreement: exact band {agree / total:.2%}, within one band "
            f"{within_one / total:.2%}"
        )


if __name__ == "__main__":
    main()
