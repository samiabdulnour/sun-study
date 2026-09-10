// Building an axonometric view direction, and nothing else.
//
// Kept apart from the commands because this is the one piece of the add-on
// whose correctness cannot be read off a header. `API_AxonoPars` carries a
// `tranmat`, the dev kit describes it in a single line last revised in 2007,
// and which way its rows and signs run had to be settled by experiment. So it
// lives in one small file with the convention written down, and `GetProjection`
// exists to check that convention against an angle set by hand in Archicad.
//
// That check has been done. The office's own `JUNE 21 - 9AM` view, read back
// through `GetProjection`, is an orthonormal right-handed matrix whose rows
// are right, up and eye in that order, with no translation -- exactly what
// `ViewMatrix` builds. Its third row decodes to a bearing of 83.372 and an
// altitude of 18.998 in the project's frame, against 83.512 and 18.977 for
// the sun this tool computes for that instant. The frame is the project's:
// bearings here are clockwise from the project's +Y axis, and a true bearing
// must be turned by the project's north angle before it arrives.
//
// The matrix itself is documented, at least. `API_Tranmat` is a 3x4 laid out
// row-major, from `APIdefs_Base.h`:
//
//     x' = tmx[0] * x + tmx[1] * y + tmx[2]  * z + tmx[3]
//     y' = tmx[4] * x + tmx[5] * y + tmx[6]  * z + tmx[7]
//     z' = tmx[8] * x + tmx[9] * y + tmx[10] * z + tmx[11]

#if !defined (LORIINI_PROJECTION_HPP)
#define LORIINI_PROJECTION_HPP

#pragma once

#include "APIEnvir.h"
#include "ACAPinc.h"

namespace Loriini {

// Where the camera stands, as two angles.
//
// `azimuth` is measured in degrees clockwise from the project's +Y axis.
// It is a bearing in the project's own frame, not a true bearing: on a
// project whose +Y sits at true bearing 319, a sun at true 42.6 is passed as
// 83.5. It is also not Archicad's own `API_AxonoPars::azimuth`, which runs
// anticlockwise from +X and is derived from this one where it is written.
//
// `altitude` is degrees above the horizon. At 90 the camera is overhead and
// the view direction is straight down, which leaves the horizontal bearing
// with nothing to fix the roll -- see `ViewMatrix` for what happens there.
struct Direction {
	double azimuthDegrees;
	double altitudeDegrees;
};

// The transformation matrix for a camera standing at `from`, looking at the
// model.
//
// The camera looks *inward*: `from` is a direction out of the model toward
// the eye, so a sun eye view passes the sun's own position and everything the
// matrix keeps in front is what the sun can see.
//
// Degenerate case, and why it is not an error. When the camera is directly
// overhead the world up vector and the view direction are parallel, so the
// usual cross product for "right" collapses. North is then used to fix the
// roll instead, which is what a plan view is. A caller asking for 90 degrees
// of altitude gets a plan rather than a refusal, because that is a reasonable
// thing to ask for at solar noon in the tropics.
API_Tranmat ViewMatrix (const Direction& from);

// The inverse of the above, for `API_AxonoPars::invtranmat`.
//
// Computed as the transpose of the rotation part rather than by a general
// inversion, which is exact here: `ViewMatrix` is built from orthonormal
// rows and carries no translation, so its inverse is its transpose and no
// determinant is involved.
API_Tranmat InverseViewMatrix (const Direction& from);

}		// namespace Loriini

#endif
