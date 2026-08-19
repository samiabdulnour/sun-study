"""Fitting the IFC world frame onto Archicad's project frame.

A sun patch is computed in the export's coordinates and drawn in the
project's. Those frames differ by a rotation whenever the export is
north-aligned, so getting this wrong puts every patch in the wrong place at
the wrong angle -- and, being a plausible-looking plan, does not announce it.
"""

from __future__ import annotations

import numpy as np
import pytest

from sun_study.core.geometry import fit_plan_transform, rotation_about_z

SOURCE = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 6.0], [0.0, 6.0], [4.0, 3.0]])


def moved(points: np.ndarray, degrees: float, shift: tuple[float, float]) -> np.ndarray:
    turn = rotation_about_z(degrees)[:2, :2]
    return np.asarray(points @ turn.T + np.array(shift), dtype=np.float64)


def test_a_pure_shift_is_recovered_exactly() -> None:
    fitted = fit_plan_transform(SOURCE, moved(SOURCE, 0.0, (123.5, -87.25)))

    assert fitted.rmse_m == pytest.approx(0.0, abs=1e-9)
    assert fitted.offset == pytest.approx([123.5, -87.25])
    assert fitted.apply(SOURCE) == pytest.approx(moved(SOURCE, 0.0, (123.5, -87.25)))


def test_a_rotation_and_a_shift_are_recovered_together() -> None:
    """The reference project's own case: the export is north-aligned and the
    project frame is turned 9.2 degrees away from it."""
    target = moved(SOURCE, 9.228, (1000.0, -2000.0))
    fitted = fit_plan_transform(SOURCE, target)

    assert fitted.rmse_m == pytest.approx(0.0, abs=1e-9)
    assert fitted.apply(SOURCE) == pytest.approx(target)
    assert np.linalg.det(fitted.rotation) == pytest.approx(1.0), "a rotation, not a reflection"


def test_a_mirrored_pairing_is_not_fitted_as_a_reflection() -> None:
    """Without the determinant guard a bad pairing comes back mirrored, which
    draws a plan that looks almost right and is inside out."""
    mirrored = SOURCE * np.array([1.0, -1.0])
    fitted = fit_plan_transform(SOURCE, mirrored)

    assert np.linalg.det(fitted.rotation) == pytest.approx(1.0)
    assert fitted.rmse_m > 1.0, "and it must say the fit is bad rather than hide it"


def test_scale_is_not_absorbed_but_reported_as_error() -> None:
    """A fitted scale would mean the pairs are wrong, not that the model is
    bigger. Letting it soak up the error would hide the mismatch."""
    fitted = fit_plan_transform(SOURCE, SOURCE * 2.0)

    assert fitted.rmse_m > 1.0
    assert np.linalg.norm(fitted.rotation @ np.array([1.0, 0.0])) == pytest.approx(1.0)


def test_one_bad_pair_shows_up_in_the_residual() -> None:
    """The whole point of reporting rmse: a patch drawn from a transform
    fitted on a mis-joined apartment is wrong everywhere."""
    target = moved(SOURCE, 30.0, (5.0, 5.0))
    target[2] += np.array([4.0, -3.0])

    fitted = fit_plan_transform(SOURCE, target)

    assert fitted.rmse_m > 1.0


def test_two_pairs_fit_perfectly_and_prove_nothing() -> None:
    """Recorded so a caller does not read a zero residual as confirmation."""
    source = SOURCE[:2]
    fitted = fit_plan_transform(source, moved(source, 45.0, (2.0, 2.0)))

    assert fitted.rmse_m == pytest.approx(0.0, abs=1e-9)


def test_too_few_pairs_is_refused() -> None:
    with pytest.raises(ValueError, match="at least two pairs"):
        fit_plan_transform(SOURCE[:1], SOURCE[:1])


def test_mismatched_lengths_are_refused() -> None:
    with pytest.raises(ValueError, match="source points against"):
        fit_plan_transform(SOURCE, SOURCE[:3])
