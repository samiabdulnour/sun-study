"""Which cells belong to which band -- the arithmetic under every diagram.

This has one interesting case and it is the boring-looking one. The no-sun
band is described two different ways in this codebase: ``band_by_area`` gives
it an upper bound of exactly 0, and ``BandStyle`` gives it 1e-9, the tolerance
below which a duration counts as none. Code that recognises only the first
spelling produces an *empty* no-sun band and says nothing about it -- on the
reference facade that quietly dropped 5,885 m2 of wall, the majority of the
elevation, while every other band looked correct.
"""

from __future__ import annotations

import numpy as np

from sun_study.archicad.draw import DEFAULT_BANDS
from sun_study.cli import _band_mask


def test_the_no_sun_band_is_found_under_either_spelling() -> None:
    minutes = np.array([0.0, 1e-12, 1e-9, 30.0, 400.0])

    for upper in (0.0, 1e-9):
        chosen = _band_mask(minutes, 0.0, upper)
        assert chosen.tolist() == [True, True, True, False, False]


def test_the_bands_of_a_legend_partition_every_duration() -> None:
    """No cell in two bands, and no cell in none: the areas have to add up."""
    minutes = np.array([0.0, 5.0, 59.9, 60.0, 119.0, 121.0, 200.0, 299.0, 301.0, 1000.0])

    counted = np.zeros(len(minutes), dtype=int)
    lower = 0.0
    for band in DEFAULT_BANDS:
        upper = None if band.upper_minutes == float("inf") else band.upper_minutes
        counted += _band_mask(minutes, lower, upper).astype(int)
        lower = band.upper_minutes

    assert counted.tolist() == [1] * len(minutes)


def test_the_open_top_band_catches_everything_above_its_floor() -> None:
    minutes = np.array([299.0, 300.0, 6000.0])

    chosen = _band_mask(minutes, 300.0, None)
    assert chosen.tolist() == [False, True, True]
