"""Tying a model's coordinate frame to the compass.

This is where silent, plausible, wrong answers come from, so it is one small
module with one job and an unusually large number of tests.

The two frames
--------------
**ENU** is the world frame ``core.solar`` works in: +X east, +Y north, +Z up,
where north means *true* north.

**Model** is the frame the geometry is in -- the Archicad project coordinate
system. +Z is up, but +Y is *project* north, which is whatever direction the
drawing happens to be set up along. It is almost never true north.

``true_north_bearing_deg`` is the single number that relates them: the compass
bearing of the model's +Y axis, measured clockwise from true north. A model
drawn with its +Y axis pointing 30 degrees east of true north has a bearing of
30.

There is no default. ``ingest`` reads this from the IFC
``IfcGeometricRepresentationContext`` ``TrueNorth`` direction and fails loudly
when it is absent, because assuming "project Y is north" is exactly the
mistake that produces a confident wrong answer.

Sign convention
---------------
Compass bearings increase **clockwise** seen from above. Rotations in a
right-handed frame with +Z up increase **counter-clockwise**. The two run in
opposite senses, and reconciling them is the entire content of this module.

Converting an ENU direction to the model frame is a counter-clockwise rotation
about +Z by ``true_north_bearing_deg``. That is derived rather than guessed:
a direction at compass azimuth ``A`` is ``(sin A, cos A)`` in ENU, and the same
direction sits at azimuth ``A - bearing`` in the model frame, so

    sin(A - b) = sin A cos b - cos A sin b
    cos(A - b) = cos A cos b + sin A sin b

which is exactly the counter-clockwise rotation matrix applied to
``(sin A, cos A)``. ``test_orientation.py`` pins this with worked cases at the
cardinal points rather than trusting the algebra.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from sun_study.core.geometry import rotation_about_z
from sun_study.core.solar import SolarPosition

FloatArray = npt.NDArray[np.float64]

__all__ = ["SiteOrientation", "enu_to_model_matrix", "sun_vectors_in_model_frame"]


def enu_to_model_matrix(true_north_bearing_deg: float) -> FloatArray:
    """Rotation taking an ENU direction into the model frame.

    See the module docstring for the derivation of the sign.
    """
    return rotation_about_z(true_north_bearing_deg)


def sun_vectors_in_model_frame(
    position: SolarPosition,
    true_north_bearing_deg: float,
    *,
    apparent: bool = True,
) -> FloatArray:
    """Unit vectors pointing towards the sun, expressed in model coordinates.

    Shape (n, 3), parallel to ``position.times_utc``.
    """
    enu = position.unit_vectors_enu(apparent=apparent)
    return np.asarray(enu @ enu_to_model_matrix(true_north_bearing_deg).T, dtype=np.float64)


@dataclass(frozen=True)
class SiteOrientation:
    """Everything needed to place a model on the earth, and nothing else.

    Every field is mandatory. There is deliberately no default for latitude,
    longitude, timezone or north: the brief requires the tool to fail loudly
    rather than guess any of them, and a dataclass default is the easiest way
    for a guess to become invisible.
    """

    latitude_deg: float
    longitude_deg: float
    timezone: str
    true_north_bearing_deg: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.latitude_deg <= 90.0:
            raise ValueError(f"latitude_deg {self.latitude_deg} outside [-90, 90]")
        if not -180.0 <= self.longitude_deg <= 180.0:
            raise ValueError(f"longitude_deg {self.longitude_deg} outside [-180, 180]")
        if not self.timezone or not self.timezone.strip():
            raise ValueError(
                "timezone must be a non-empty IANA name such as 'Australia/Sydney'. "
                "There is no default: an unstated zone shifts every sun position by "
                "whole hours."
            )
        if not np.isfinite(self.true_north_bearing_deg):
            raise ValueError(f"true_north_bearing_deg {self.true_north_bearing_deg} is not finite")

    @property
    def normalised_bearing_deg(self) -> float:
        """The north bearing reduced to [0, 360)."""
        return float(np.mod(self.true_north_bearing_deg, 360.0))

    @property
    def hemisphere(self) -> str:
        return "southern" if self.latitude_deg < 0.0 else "northern"

    def describe(self) -> str:
        """One line for the console banner and every output file header.

        The human has to be able to eyeball this and catch a wrong site before
        reading any results, so it is a single dense line rather than a table.
        """
        lat_hemi = "N" if self.latitude_deg >= 0.0 else "S"
        lon_hemi = "E" if self.longitude_deg >= 0.0 else "W"
        return (
            f"lat {abs(self.latitude_deg):.6f}{lat_hemi} "
            f"lon {abs(self.longitude_deg):.6f}{lon_hemi} "
            f"tz {self.timezone} "
            f"true north bearing of model +Y {self.normalised_bearing_deg:.3f} deg "
            f"({self.hemisphere} hemisphere)"
        )

    def sun_vectors(self, position: SolarPosition, *, apparent: bool = True) -> FloatArray:
        return sun_vectors_in_model_frame(position, self.true_north_bearing_deg, apparent=apparent)
