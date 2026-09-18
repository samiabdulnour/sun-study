#pragma once

// One planar polygon -- an outer contour and any holes -- as triangles.
//
// Kept free of Archicad types so the same code the add-on compiles can be
// compiled and tested on its own (`archicad-addon/Tests/TriangulateTest.cpp`),
// on a machine with no Archicad and no Visual Studio.
//
// Why it exists. The body walk used to fan each polygon from its first corner,
// which is exact for a convex polygon and wrong for anything else -- and
// Archicad draws a wall round a window as a *concave* outline far more often
// than as a polygon with a hole. A fan over that outline overlaps itself and
// covers the opening. On Kogarah D01, LEVEL 04, 98 of 162 openings had a wall
// face across them, 77 of those crossed by one of the fan's back-wound
// triangles. `ACAPI_3D_DecomposePgon` was tried first and returned vertices
// that were not the polygon's (the site mesh gained 6 million square metres),
// so the triangulation is done here, by earcut, from the contours alone.

// <array> first: earcut reads a point through std::tuple_element and does not
// include the header that declares it for std::array.
#include <array>
#include <cmath>
#include <vector>

#include "earcut.hpp"

namespace Loriini {

struct Point3 {
	double x;
	double y;
	double z;
};

struct Triangulated {
	bool ok = false;				// false: nothing usable came out, and nothing was added
	double polygonArea = 0.0;		// outer minus holes, in the polygon's own plane
	double triangleArea = 0.0;		// what the triangles cover, measured the same way
};

namespace Detail {

// Twice the signed area of a ring in 2D.
inline double Shoelace (const std::vector<std::array<double, 2>>& ring)
{
	double sum = 0.0;
	for (size_t i = 0, j = ring.size () - 1; i < ring.size (); j = i++) {
		sum += (ring[j][0] - ring[i][0]) * (ring[j][1] + ring[i][1]);
	}
	return sum;
}

}	// namespace Detail


// `contours[0]` is the outer contour, the rest are holes. Triangles are
// appended to `out` three points at a time, and only if the whole polygon
// triangulated -- a failure leaves `out` as it was.
//
// Areas are compared in the plane the polygon is projected to, which scales
// both by the same factor, so their ratio is the check: a triangulation that
// covers a hole, or leaves part of the face out, does not match.
inline Triangulated Triangulate (const std::vector<std::vector<Point3>>& contours, std::vector<Point3>& out)
{
	Triangulated result;
	if (contours.empty () || contours[0].size () < 3) {
		return result;
	}

	// Newell's normal of the outer contour: exact for a planar polygon of any
	// shape, where a cross product of two edges is at the mercy of which two.
	double nx = 0.0, ny = 0.0, nz = 0.0;
	const std::vector<Point3>& outer = contours[0];
	for (size_t i = 0, j = outer.size () - 1; i < outer.size (); j = i++) {
		nx += (outer[j].y - outer[i].y) * (outer[j].z + outer[i].z);
		ny += (outer[j].z - outer[i].z) * (outer[j].x + outer[i].x);
		nz += (outer[j].x - outer[i].x) * (outer[j].y + outer[i].y);
	}
	const double ax = std::fabs (nx), ay = std::fabs (ny), az = std::fabs (nz);
	if (ax + ay + az < 1e-18) {
		return result;				// no area: a sliver or a line
	}

	// Drop the normal's largest axis. The projection keeps every shape
	// non-degenerate, and earcut does not care which way round a ring runs.
	const int drop = (ax >= ay && ax >= az) ? 0 : (ay >= az ? 1 : 2);
	auto flat = [drop] (const Point3& p) -> std::array<double, 2> {
		if (drop == 0) return { p.y, p.z };
		if (drop == 1) return { p.z, p.x };
		return { p.x, p.y };
	};

	std::vector<std::vector<std::array<double, 2>>> rings;
	std::vector<Point3> flatOrder;			// earcut's indices run over the rings in order
	for (const std::vector<Point3>& contour : contours) {
		if (contour.size () < 3) {
			continue;
		}
		std::vector<std::array<double, 2>> ring;
		for (const Point3& p : contour) {
			ring.push_back (flat (p));
			flatOrder.push_back (p);
		}
		const double twice = std::fabs (Detail::Shoelace (ring)) / 2.0;
		result.polygonArea += rings.empty () ? twice : -twice;
		rings.push_back (ring);
	}

	const std::vector<uint32_t> indices = mapbox::earcut<uint32_t> (rings);
	if (indices.empty ()) {
		return result;
	}

	std::vector<std::array<double, 2>> flatPoints;
	for (const auto& ring : rings) {
		flatPoints.insert (flatPoints.end (), ring.begin (), ring.end ());
	}
	for (size_t k = 0; k + 2 < indices.size (); k += 3) {
		const auto& a = flatPoints[indices[k]];
		const auto& b = flatPoints[indices[k + 1]];
		const auto& c = flatPoints[indices[k + 2]];
		result.triangleArea += std::fabs ((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])) / 2.0;
		out.push_back (flatOrder[indices[k]]);
		out.push_back (flatOrder[indices[k + 1]]);
		out.push_back (flatOrder[indices[k + 2]]);
	}
	result.ok = true;
	return result;
}

}	// namespace Loriini
