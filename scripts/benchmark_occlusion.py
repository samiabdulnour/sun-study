"""Measure the pure-numpy ray caster at the scale of a real project.

The office runs Archicad on Windows, where a native ray tracer may not build.
The numpy backend is therefore the one that has to be fast enough in practice,
not a fallback that merely keeps CI green. This script is how that claim stays
honest.

    uv run python scripts/benchmark_occlusion.py

Timings are indicative -- they move with CPU and numpy build -- but the shape
of the leaf-size curve and the order of magnitude of the solve time should
hold. Numbers quoted in docs/validation.md were taken on CI-class hardware.
"""

from __future__ import annotations

import time

import numpy as np

from sun_study.core.analysis import sunlit_matrix
from sun_study.core.geometry import TriangleMesh, box
from sun_study.core.occlusion import Occluder
from sun_study.core.sampling import SamplePoints, grid_on_rectangle

LEAF_SIZES = (8, 16, 32, 64, 128)


def synthetic_city(block_count: int, extent: float, seed: int = 0) -> TriangleMesh:
    """A field of boxes standing in for a subject building plus its context."""
    rng = np.random.default_rng(seed)
    blocks = []
    for _ in range(block_count):
        x, y = rng.uniform(-extent, extent, 2)
        width, depth = rng.uniform(2.0, 8.0, 2)
        height = rng.uniform(3.0, 45.0)
        blocks.append(box((x, y, 0.0), (x + width, y + depth, height)))
    return TriangleMesh.concatenate(blocks)


def synthetic_windows(count: int, extent: float, seed: int = 1) -> SamplePoints:
    """Living-room-sized windows on a 200 mm grid, scattered over the site."""
    rng = np.random.default_rng(seed)
    return SamplePoints.concatenate(
        [
            grid_on_rectangle(
                (
                    rng.uniform(-extent, extent),
                    rng.uniform(-extent, extent),
                    rng.uniform(1.0, 35.0),
                ),
                (1.8, 0.0, 0.0),
                (0.0, 0.0, 1.4),
                f"window-{index}",
                spacing_m=0.2,
            )
            for index in range(count)
        ]
    )


def sun_field(instant_count: int = 37, seed: int = 2) -> np.ndarray:
    rng = np.random.default_rng(seed)
    vectors = rng.normal(size=(instant_count, 3))
    vectors[:, 2] = np.abs(vectors[:, 2])
    return np.asarray(vectors / np.linalg.norm(vectors, axis=1, keepdims=True))


def main() -> None:
    mesh = synthetic_city(block_count=8000, extent=120.0)
    points = synthetic_windows(count=200, extent=100.0)
    suns = sun_field()

    facing = int((points.normals @ suns.T > 0).sum())
    print("occlusion benchmark")
    print("=" * 62)
    print(f"  scene          {mesh.triangle_count:,} triangles")
    print(f"  samples        {len(points):,} points x {len(suns)} sun positions")
    print(f"  rays cast      {facing:,} (back-facing pairs are skipped, not traced)")
    print()
    print(f"  {'leaf':>5} {'nodes':>9} {'build s':>9} {'solve s':>9} {'krays/s':>9}")

    for leaf in LEAF_SIZES:
        started = time.perf_counter()
        occluder = Occluder(mesh, max_leaf_triangles=leaf)
        build = time.perf_counter() - started

        started = time.perf_counter()
        sunlit_matrix(points, occluder, suns)
        solve = time.perf_counter() - started

        print(
            f"  {leaf:>5} {occluder.node_count:>9,} {build:>9.2f} "
            f"{solve:>9.2f} {facing / solve / 1000:>9.0f}"
        )

    print()
    print("  A 200 apartment job is roughly 800k rays; divide by the rate above")
    print("  for the expected solve time.")


if __name__ == "__main__":
    main()
