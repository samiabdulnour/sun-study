"""Fitting the IFC world frame onto Archicad's project frame.

A sun patch is computed in the export's coordinates and drawn in the
project's. Those frames differ by a rotation whenever the export is
north-aligned, so getting this wrong puts every patch in the wrong place at
the wrong angle -- and, being a plausible-looking plan, does not announce it.
"""

from __future__ import annotations

import numpy as np
import pytest

from sun_study.core.geometry import PlanTransform, fit_plan_transform, rotation_about_z

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


# -- telling one moved Zone from a stale export ---------------------------
# A colleague's run refused at 0.70 m of residual and the message named no
# zone, so there was nothing to check. These are the two causes it has to
# separate, because the remedies are opposite: edit one zone, or re-export.

LIMIT_M = 0.5


def _named(source: np.ndarray, target: np.ndarray) -> PlanTransform:
    from dataclasses import replace

    fitted = fit_plan_transform(source, target)
    return replace(fitted, keys=tuple(f"Zone {n + 1}" for n in range(len(source))))


def test_one_moved_zone_is_named_and_the_rest_are_cleared() -> None:
    """The 2 m outlier that produced exactly the residual we were sent."""
    target = SOURCE.copy()
    target[2] += [1.8, 0.9]
    fitted = _named(SOURCE, target)

    assert fitted.rmse_m > LIMIT_M, "the fit is refused, which is why there is a message"
    blamed = fitted.blame(LIMIT_M)
    assert blamed is not None and blamed[0] == "Zone 3"
    assert blamed[1] == pytest.approx(0.0, abs=1e-9), "the other four agree exactly"


def test_a_rigid_fit_smears_one_outlier_over_every_pair() -> None:
    """Why the per-pair distances alone cannot be the diagnosis.

    The fit rotates and shifts to split the difference, so the innocent pairs
    are left visibly out too and the list looks like a uniformly stale export.
    This is the measurement the leave-one-out check exists to defeat.
    """
    target = SOURCE.copy()
    target[2] += [1.8, 0.9]
    fitted = _named(SOURCE, target)

    others = [m for n, m in enumerate(fitted.per_pair_m) if n != 2]
    assert max(others) > 0.3, "the innocent pairs are not near zero, so they accuse nobody"


def test_a_drifted_export_blames_no_single_zone() -> None:
    """Every pair nudged, which is a stale export. Naming one would be wrong."""
    drift = np.random.default_rng(0).normal(0.0, 0.6, SOURCE.shape)
    fitted = _named(SOURCE, SOURCE + drift)

    assert fitted.rmse_m > LIMIT_M
    assert fitted.blame(LIMIT_M) is None, "several removals rescue it, so none is the cause"
    assert "No one Zone stands out" in fitted.describe_disagreement(LIMIT_M)


def test_a_badly_out_of_step_export_says_no_removal_helps() -> None:
    drift = np.random.default_rng(1).normal(0.0, 3.0, SOURCE.shape)
    fitted = _named(SOURCE, SOURCE + drift)

    assert fitted.blame(LIMIT_M) is None
    said = fitted.describe_disagreement(LIMIT_M)
    assert "No single Zone explains it" in said
    assert "still over the 0.5 m limit" in said


def test_three_pairs_are_too_few_to_accuse_any_of_them() -> None:
    """Drop one of three and the remaining two fit perfectly by construction,
    so a leave-one-out check would clear whichever zone it was asked about."""
    target = SOURCE[:3].copy()
    target[1] += [1.5, 0.0]
    fitted = _named(SOURCE[:3], target)

    assert fitted.without_each_m == (), "not computed, rather than computed and wrong"
    assert fitted.blame(LIMIT_M) is None
    assert "too few to tell" in fitted.describe_disagreement(LIMIT_M)


def test_the_diagnosis_names_zones_rather_than_indices() -> None:
    """A GlobalId or a pair number is not something a person can go and look
    at; the zone's number and name are what the Zone dialog shows."""
    from dataclasses import replace

    target = SOURCE.copy()
    target[2] += [1.8, 0.9]
    fitted = replace(
        fit_plan_transform(SOURCE, target),
        keys=("A101", "A102", "COS Courtyard", "A201", "A202"),
    )

    said = fitted.describe_disagreement(LIMIT_M)
    assert "COS Courtyard is 1.61 m out" in said, "the worst pair, named and measured"
    assert "Leaving 'COS Courtyard' out" in said
    assert "pair 3" not in said, "an index is not something a person can go and look at"
