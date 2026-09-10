#include "Projection.hpp"

#include <cmath>

namespace Loriini {

namespace {

const double PI = 3.14159265358979323846;

// How close to straight overhead counts as overhead. A hundredth of a degree
// in radians. The cross product does not fail suddenly at the pole -- it
// degrades, so a camera at 89.999 degrees would produce a "right" vector
// whose direction is decided by floating point noise and whose roll swings
// wildly between two runs that asked for the same thing. Snapping is the
// honest behaviour.
const double OVERHEAD_TOLERANCE = 1.7e-4;

struct Vector {
	double x;
	double y;
	double z;
};

Vector Cross (const Vector& a, const Vector& b)
{
	return { a.y * b.z - a.z * b.y,
			 a.z * b.x - a.x * b.z,
			 a.x * b.y - a.y * b.x };
}

Vector Normalised (const Vector& v)
{
	const double length = std::sqrt (v.x * v.x + v.y * v.y + v.z * v.z);
	if (length == 0.0) {
		return { 0.0, 0.0, 0.0 };
	}
	return { v.x / length, v.y / length, v.z / length };
}

// The unit vector pointing from the model out toward the camera.
//
// Bearing runs clockwise from north, so east is +x and north is +y, which is
// Archicad's project frame with north up. A bearing of 0 therefore puts the
// camera due north of the model and a bearing of 90 puts it due east.
Vector Eye (const Direction& from)
{
	const double azimuth = from.azimuthDegrees * PI / 180.0;
	const double altitude = from.altitudeDegrees * PI / 180.0;
	const double horizontal = std::cos (altitude);
	return { horizontal * std::sin (azimuth),
			 horizontal * std::cos (azimuth),
			 std::sin (altitude) };
}

// The three orthonormal rows of the view transform.
//
// `depth` is the eye direction itself, so a point further toward the camera
// gets a larger z' and the near/far ordering reads the way a renderer
// expects. `right` and `up` complete a right-handed frame.
void Frame (const Direction& from, Vector& right, Vector& up, Vector& depth)
{
	depth = Eye (from);

	// Overhead, where the bearing no longer fixes the roll. North is put at
	// the top of the page, which is the convention every plan in this project
	// is already drawn to.
	if (std::fabs (depth.z) > 1.0 - OVERHEAD_TOLERANCE) {
		const double sign = depth.z > 0.0 ? 1.0 : -1.0;
		right = { 1.0, 0.0, 0.0 };
		up = { 0.0, sign, 0.0 };
		depth = { 0.0, 0.0, sign };
		return;
	}

	const Vector worldUp = { 0.0, 0.0, 1.0 };
	right = Normalised (Cross (worldUp, depth));
	up = Cross (depth, right);
}

API_Tranmat FromRows (const Vector& first, const Vector& second, const Vector& third)
{
	API_Tranmat matrix = {};
	matrix.tmx[0] = first.x;   matrix.tmx[1] = first.y;   matrix.tmx[2] = first.z;   matrix.tmx[3] = 0.0;
	matrix.tmx[4] = second.x;  matrix.tmx[5] = second.y;  matrix.tmx[6] = second.z;  matrix.tmx[7] = 0.0;
	matrix.tmx[8] = third.x;   matrix.tmx[9] = third.y;   matrix.tmx[10] = third.z;  matrix.tmx[11] = 0.0;
	return matrix;
}

}		// namespace


API_Tranmat ViewMatrix (const Direction& from)
{
	Vector right, up, depth;
	Frame (from, right, up, depth);
	return FromRows (right, up, depth);
}


API_Tranmat InverseViewMatrix (const Direction& from)
{
	Vector right, up, depth;
	Frame (from, right, up, depth);

	// The transpose. Exact, because the rows are orthonormal and the
	// translation column is zero.
	const Vector firstColumn = { right.x, up.x, depth.x };
	const Vector secondColumn = { right.y, up.y, depth.y };
	const Vector thirdColumn = { right.z, up.z, depth.z };
	return FromRows (firstColumn, secondColumn, thirdColumn);
}

}		// namespace Loriini
