// Tests for Src/Triangulate.hpp, runnable without Archicad or Visual Studio:
//
//     python -m ziglang c++ -std=c++14 -I archicad-addon/Src \
//         archicad-addon/Tests/TriangulateTest.cpp -o triangulate-test && ./triangulate-test
//
// Each face is built the way Archicad hands one over -- corner points in
// order, holes as further contours -- and checked three ways: the triangles
// cover the face's own area, a point in the opening is covered by none of
// them, and a point in the solid part is covered by one. The old fan is run
// over the same faces so the failure it caused is shown, not asserted.

#include "Triangulate.hpp"

#include <cstdio>
#include <functional>
#include <string>

using Loriini::Point3;

namespace {

int failures = 0;

void Check (bool condition, const std::string& what)
{
	std::printf ("  %s  %s\n", condition ? "ok  " : "FAIL", what.c_str ());
	failures += condition ? 0 : 1;
}

// Is `p` inside any triangle? Tested in the plane the triangles lie in by
// barycentric coordinates, with the plane taken from each triangle itself.
int Covering (const std::vector<Point3>& tris, const Point3& p)
{
	int count = 0;
	for (size_t k = 0; k + 2 < tris.size (); k += 3) {
		const Point3 a = tris[k], b = tris[k + 1], c = tris[k + 2];
		const double ux = b.x - a.x, uy = b.y - a.y, uz = b.z - a.z;
		const double vx = c.x - a.x, vy = c.y - a.y, vz = c.z - a.z;
		const double wx = p.x - a.x, wy = p.y - a.y, wz = p.z - a.z;
		const double uu = ux * ux + uy * uy + uz * uz, vv = vx * vx + vy * vy + vz * vz;
		const double uv = ux * vx + uy * vy + uz * vz;
		const double wu = wx * ux + wy * uy + wz * uz, wv = wx * vx + wy * vy + wz * vz;
		const double d = uv * uv - uu * vv;
		if (std::fabs (d) < 1e-18) continue;
		const double s = (uv * wv - vv * wu) / d, t = (uv * wu - uu * wv) / d;
		// on the plane?
		const double nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
		const double off = (wx * nx + wy * ny + wz * nz) / std::sqrt (nx * nx + ny * ny + nz * nz);
		if (std::fabs (off) < 1e-9 && s >= 0 && t >= 0 && s + t <= 1) ++count;
	}
	return count;
}

double Area3 (const std::vector<Point3>& tris)
{
	double sum = 0.0;
	for (size_t k = 0; k + 2 < tris.size (); k += 3) {
		const Point3 a = tris[k], b = tris[k + 1], c = tris[k + 2];
		const double ux = b.x - a.x, uy = b.y - a.y, uz = b.z - a.z;
		const double vx = c.x - a.x, vy = c.y - a.y, vz = c.z - a.z;
		const double nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
		sum += std::sqrt (nx * nx + ny * ny + nz * nz) / 2.0;
	}
	return sum;
}

// Does every triangle's normal point the same way as the outline's (Newell)?
bool AllFaceAsOutline (const std::vector<Point3>& tris, const std::vector<Point3>& outer)
{
	double nx = 0.0, ny = 0.0, nz = 0.0;
	for (size_t i = 0, j = outer.size () - 1; i < outer.size (); j = i++) {
		nx += (outer[j].y - outer[i].y) * (outer[j].z + outer[i].z);
		ny += (outer[j].z - outer[i].z) * (outer[j].x + outer[i].x);
		nz += (outer[j].x - outer[i].x) * (outer[j].y + outer[i].y);
	}
	for (size_t k = 0; k + 2 < tris.size (); k += 3) {
		const Point3 a = tris[k], b = tris[k + 1], c = tris[k + 2];
		const double ux = b.x - a.x, uy = b.y - a.y, uz = b.z - a.z;
		const double vx = c.x - a.x, vy = c.y - a.y, vz = c.z - a.z;
		const double tx = uy * vz - uz * vy, ty = uz * vx - ux * vz, tz = ux * vy - uy * vx;
		if (tx * nx + ty * ny + tz * nz <= 0.0) return false;
	}
	return !tris.empty ();
}

std::vector<Point3> OldFan (const std::vector<Point3>& outer)
{
	std::vector<Point3> tris;
	for (size_t i = 1; i + 1 < outer.size (); ++i) {
		tris.push_back (outer[0]);
		tris.push_back (outer[i]);
		tris.push_back (outer[i + 1]);
	}
	return tris;
}

// A face drawn in a wall's vertical plane: u along the wall, v up, placed on
// a wall running at `bearing` radians from +X, `offset` metres out.
std::function<Point3 (double, double)> WallPlane (double bearing, double offset)
{
	return [=] (double u, double v) {
		return Point3 { u * std::cos (bearing) - offset * std::sin (bearing),
						u * std::sin (bearing) + offset * std::cos (bearing), v + 31.2 };
	};
}

void Case (const char* name,
		   const std::vector<std::vector<Point3>>& contours,
		   double expectedArea,
		   const Point3& inOpening,
		   const Point3& inSolid)
{
	std::printf ("%s\n", name);
	std::vector<Point3> tris;
	const Loriini::Triangulated r = Loriini::Triangulate (contours, tris);
	Check (r.ok, "triangulated");
	Check (std::fabs (Area3 (tris) - expectedArea) < 1e-6 * (1 + expectedArea),
		   "3D area " + std::to_string (Area3 (tris)) + " = expected " + std::to_string (expectedArea));
	Check (std::fabs (r.triangleArea - r.polygonArea) <= 1e-9 * (1 + r.polygonArea),
		   "self-check: triangles cover the polygon's area");
	Check (Covering (tris, inOpening) == 0, "the opening is open");
	Check (Covering (tris, inSolid) >= 1, "the solid part is covered");
	Check (AllFaceAsOutline (tris, contours[0]), "every triangle faces the way the outline does");
	std::vector<Point3> reversedOuter (contours[0].rbegin (), contours[0].rend ());
	std::vector<std::vector<Point3>> flipped = contours;
	flipped[0] = reversedOuter;
	std::vector<Point3> other;
	Loriini::Triangulate (flipped, other);
	Check (AllFaceAsOutline (other, reversedOuter), "and still does with the outline reversed");
	const std::vector<Point3> fan = OldFan (contours[0]);
	std::printf ("        old fan: area %.3f, opening covered %d time(s)\n", Area3 (fan), Covering (fan, inOpening));
}

}	// namespace

int main ()
{
	// 1. The shape Archicad mostly uses: a wall face whose outline detours
	// round a window. 10 x 3 m, window 4..6 x 0.9..2.4 m, reached by a
	// zero-width slit from the bottom edge, as a single concave contour.
	for (double bearing : { 0.0, 0.7431, 2.0 }) {
		auto at = WallPlane (bearing, 5.0);
		Case ("wall face, window reached by a slit (concave outline)",
			  { { at (0, 0), at (4, 0), at (4, 0.9), at (4, 2.4), at (6, 2.4), at (6, 0.9), at (4, 0.9),
				  at (4, 0), at (10, 0), at (10, 3), at (0, 3) } },
			  30.0 - 3.0, at (5, 1.6), at (8, 1.5));
	}

	// 2. A window as a real hole: outer contour, then the hole.
	{
		auto at = WallPlane (0.3, -2.0);
		Case ("wall face, window as a hole",
			  { { at (0, 0), at (10, 0), at (10, 3), at (0, 3) },
				{ at (4, 0.9), at (4, 2.4), at (6, 2.4), at (6, 0.9) } },
			  30.0 - 3.0, at (5, 1.6), at (1, 1));
	}

	// 3. A door notched out of the bottom edge: a U, concave, no hole.
	{
		auto at = WallPlane (1.1, 0.0);
		Case ("wall face notched by a door (U outline)",
			  { { at (0, 0), at (4, 0), at (4, 2.1), at (5, 2.1), at (5, 0), at (10, 0), at (10, 3), at (0, 3) } },
			  30.0 - 2.1, at (4.5, 1.0), at (7, 1.0));
	}

	// 4. A sloped face, to be sure the projection keeps true area.
	{
		auto at = [] (double u, double v) { return Point3 { u, v * std::cos (0.5), v * std::sin (0.5) }; };
		Case ("sloped roof face with a skylight hole",
			  { { at (0, 0), at (6, 0), at (6, 4), at (0, 4) },
				{ at (2, 1), at (3, 1), at (3, 2), at (2, 2) } },
			  24.0 - 1.0, at (2.5, 1.5), at (5, 3));
	}

	// 5. A plain convex quad still comes out as two triangles.
	{
		std::vector<Point3> tris;
		Loriini::Triangulate ({ { { 0, 0, 0 }, { 1, 0, 0 }, { 1, 1, 0 }, { 0, 1, 0 } } }, tris);
		std::printf ("convex quad\n");
		Check (tris.size () == 6, "two triangles");
	}

	// 6. Degenerate input is refused and adds nothing.
	{
		std::vector<Point3> tris;
		const auto r = Loriini::Triangulate ({ { { 0, 0, 0 }, { 1, 0, 0 }, { 2, 0, 0 } } }, tris);
		std::printf ("collinear points\n");
		Check (!r.ok && tris.empty (), "refused, nothing added");
	}

	std::printf ("\n%s\n", failures == 0 ? "ALL PASSED" : "FAILURES");
	return failures == 0 ? 0 : 1;
}
