#include "ModelCommands.hpp"

#include "Triangulate.hpp"

#include <cmath>
#include <cstdio>
#include <vector>

namespace Loriini {

namespace {

// The format's first eight bytes and its version, matching
// `ingest/native.py`. Both sides carry them so a mismatched add-on and tool
// say so instead of reading one layout as another.
const char* const MAGIC = "LORIINIM";
const UInt32 FORMAT_VERSION = 3;

// As `Projection.cpp` has it: the kit offers no constant and a literal at the
// point of use is a literal somebody has to count the digits of.
const double PI = 3.14159265358979323846;


// A file written a field at a time, little-endian, as the reader expects.
//
// Buffered in memory and written once at the end: a large project is tens of
// megabytes and tens of thousands of small `fwrite` calls are most of the
// cost. Tens of megabytes is also small enough that holding it is not a
// question -- the IFC this replaces was 692 MB on the same project.
class Writer {
public:
	void Bytes (const void* data, size_t length)
	{
		const char* at = static_cast<const char*> (data);
		buffer.insert (buffer.end (), at, at + length);
	}

	void U32 (UInt32 value)			{ Bytes (&value, sizeof (value)); }
	void I32 (Int32 value)			{ Bytes (&value, sizeof (value)); }
	void F64 (double value)			{ Bytes (&value, sizeof (value)); }
	void F32 (float value)			{ Bytes (&value, sizeof (value)); }

	// Length-prefixed UTF-8, because a name can carry anything a person typed
	// and a null terminator is not a length.
	void Text (const GS::UniString& value)
	{
		// `auto` because the type `ToCStr` returns is private to
		// `GS::UniString`, and CC_UTF8 because the default is the system code
		// page -- a layer named with anything outside it would arrive here as
		// mojibake, and layer names are what every selection is made on.
		const auto utf8 = value.ToCStr (CC_UTF8);
		const char* bytes = utf8.Get ();
		const UInt32 length = static_cast<UInt32> (strlen (bytes));
		U32 (length);
		Bytes (bytes, length);
	}

	bool Save (const GS::UniString& path) const
	{
		FILE* file = nullptr;
		if (fopen_s (&file, path.ToCStr ().Get (), "wb") != 0 || file == nullptr) {
			return false;
		}
		const size_t written = buffer.empty ()
			? 0
			: fwrite (buffer.data (), 1, buffer.size (), file);
		fclose (file);
		return written == buffer.size ();
	}

	size_t Size () const			{ return buffer.size (); }

private:
	std::vector<char> buffer;
};


// The name of the layer an element sits on, or "" where it cannot be read.
//
// Cached, because a project has a hundred and fifty layers and tens of
// thousands of elements, and asking the attribute manager once per element is
// the difference between seconds and minutes.
class LayerNames {
public:
	GS::UniString Of (API_AttributeIndex index)
	{
		const auto found = known.find (index);
		if (found != known.end ()) {
			return found->second;
		}

		API_Attribute attribute = {};
		attribute.header.typeID = API_LayerID;
		attribute.header.index = index;
		GS::UniString name;
		if (ACAPI_Attribute_Get (&attribute) == NoError) {
			name = GS::UniString (attribute.header.name);
		}
		known[index] = name;
		return name;
	}

private:
	std::map<API_AttributeIndex, GS::UniString> known;
};


// Storey names by index, read once. The analysis needs the name rather than
// the index: a storey is named the same on every project that has one and
// indexed differently on each.
std::map<short, GS::UniString> StoreyNames ()
{
	std::map<short, GS::UniString> names;
	API_StoryInfo info = {};
	if (ACAPI_Environment (APIEnv_GetStorySettingsID, &info) != NoError || info.data == nullptr) {
		return names;
	}
	const short first = info.firstStory;
	const short last = info.lastStory;
	for (short index = first; index <= last; ++index) {
		const API_StoryType& storey = (*info.data)[index - first];
		names[storey.index] = GS::UniString (storey.uName);
	}
	BMKillHandle (reinterpret_cast<GSHandle*> (&info.data));
	return names;
}


// The IFC class the analysis expects for this element type.
//
// Only the distinctions the analysis actually makes. Everything that is not a
// space occludes, and the scene builder treats every occluder alike, so a
// coarse mapping here costs nothing and a fine one would be a second table to
// keep in step with Archicad's element list.
GS::UniString IfcClassOf (const API_ElemType& elemType)
{
	switch (elemType.typeID) {
		case API_ZoneID:		return "IfcSpace";
		case API_WallID:		return "IfcWall";
		case API_SlabID:		return "IfcSlab";
		case API_RoofID:		return "IfcRoof";
		case API_ColumnID:		return "IfcColumn";
		case API_BeamID:		return "IfcBeam";
		case API_MeshID:		return "IfcGeographicElement";
		case API_WindowID:		return "IfcWindow";
		case API_DoorID:		return "IfcDoor";
		case API_ShellID:		return "IfcShell";
		case API_MorphID:		return "IfcBuildingElementProxy";
		case API_CurtainWallID:	return "IfcCurtainWall";
		case API_ObjectID:		return "IfcBuildingElementProxy";
		default:				return "IfcBuildingElementProxy";
	}
}


// Zones shown in the 3D model for the length of one export, then put back.
//
// A Zone is what a communal-open-space study *measures*, and on a real project
// it is almost always switched off in "Filter Elements in 3D" -- nobody wants
// zone solids in a rendering. So the first live export carried 17,640 elements
// and not one space, and the study had nothing to measure.
//
// The alternative was to ask each office to tick the box, which is the same
// "configure every machine by hand" that sent the IFC route to the wall
// (D103). So the filter is set here, the model is regenerated, and the
// previous setting is restored whichever way the command leaves.
//
// The struct is read, modified and written rather than built, for the reason
// D98 gives: it also carries the storey filter, the marquee and the cut
// planes, and a command that composed it from nothing would throw away
// somebody's settings without mentioning it. `convert` is the kit's "must
// convert" flag -- without it the setting changes and the model goes on
// showing what it already converted, which reads exactly like doing nothing.
class ZonesInTheModel {
public:
	explicit ZonesInTheModel (GSErrCode& err)
	{
		err = ACAPI_Environment (APIEnv_Get3DImageSetsID, &settings);
		if (err != NoError) {
			return;
		}
		const auto found = settings.elemTypeFilter.find (API_ZoneID);
		wasShown = found != settings.elemTypeFilter.end () ? found->second : false;

		// Written back **whether or not anything needed changing**, and that is
		// the point rather than a detail.
		//
		// The kit's "must convert" flag is the only way from here to make
		// Archicad rebuild the 3D model, and the model has to be rebuilt
		// because the caller has just changed which layers are visible. An
		// earlier version skipped the write when the filter already showed
		// Zones, and then walked whatever had been converted when somebody
		// opened the 3D window -- minutes earlier, under different layers.
		//
		// Measured on Silverwater, 17 September 2026: `export_state` switched
		// `07 | Fills.Areas` on and Archicad confirmed it, the filter already
		// showed every element type so nothing was written, and the export
		// carried 22 of the project's 159 layers and not one element from that
		// layer. The layer change was real and simply arrived after the
		// conversion it was meant to affect.
		API_3DFilterAndCutSettings wanted = settings;
		wanted.elemTypeFilter[API_ZoneID] = true;
		bool convert = true;
		err = ACAPI_Environment (APIEnv_Change3DImageSetsID, &wanted, &convert);
		// Only a filter this *changed* has to be put back. The rebuild itself
		// leaves nothing to restore.
		changed = err == NoError && !wasShown;
	}

	~ZonesInTheModel ()
	{
		if (!changed) {
			return;
		}
		bool convert = true;
		ACAPI_Environment (APIEnv_Change3DImageSetsID, &settings, &convert);
	}

	bool WasAlreadyShown () const		{ return wasShown; }

private:
	API_3DFilterAndCutSettings settings = {};
	bool wasShown = false;
	bool changed = false;
};


// A Zone's own name, which is not its element ID and is what a study selects on.
//
// Archicad keeps two strings on a Zone and they are easy to mistake for each
// other. On Silverwater's communal open space the element ID is
// "Communal Open Space" and the *name* is "NON RESIDENTIAL" -- and it is the
// name that `--zone-name` matches. Archicad's own IFC export writes the ID
// into `Name` and the name into `LongName`, and `scene._named_one_of` checks
// both for exactly that reason, so this file carries both and keeps the same
// mapping.
//
// Empty for anything that is not a Zone. Only Zones have a second string, and
// inventing one for a wall would be a field nothing sets and everything has to
// read past.
GS::UniString ZoneNameOf (const API_Guid& guid, const API_ElemType& elemType)
{
	if (elemType.typeID != API_ZoneID) {
		return GS::UniString ();
	}
	API_Element element = {};
	element.header.guid = guid;
	if (ACAPI_Element_Get (&element) != NoError) {
		return GS::UniString ();
	}
	return GS::UniString (element.zone.roomName);
}


// One vertex, moved out of its body's own frame into the project's.
//
// `API_BodyType` carries a `tranmat`, and ignoring it is only harmless for the
// bodies that happen to be built in world coordinates. Walls and slabs are;
// **library parts are not**. A window or a door is a GDL object whose body is
// modelled about its own origin, so an untransformed walk puts every one of
// them within a few centimetres of z = 0 whatever storey it belongs to.
//
// Measured on Kogarah, 17 September 2026: all 106 marked living-room openings
// came out at z 0.10..0.20 m while their apartments stood at 23..43 m, so the
// nearest room was 23 m away, every opening resolved to nothing, and the study
// assessed 0 of 91 apartments. The massing and communal studies were unharmed
// and that is the tell -- they occlude with walls and slabs and never look at
// an opening.
//
// `tmx` is 3x4, row-major, the fourth column the offset -- the same layout
// `Projection.cpp` builds by hand.
API_Coord3D Placed (const API_Tranmat& tm, double x, double y, double z)
{
	API_Coord3D out = {};
	out.x = tm.tmx[0] * x + tm.tmx[1] * y + tm.tmx[2] * z + tm.tmx[3];
	out.y = tm.tmx[4] * x + tm.tmx[5] * y + tm.tmx[6] * z + tm.tmx[7];
	out.z = tm.tmx[8] * x + tm.tmx[9] * y + tm.tmx[10] * z + tm.tmx[11];
	return out;
}


// One element's triangles, gathered from every body Archicad converted for it.
struct Triangles {
	std::vector<float> xyz;			// nine floats per triangle

	void Add (const API_Coord3D& a, const API_Coord3D& b, const API_Coord3D& c)
	{
		const API_Coord3D corners[3] = { a, b, c };
		for (const API_Coord3D& corner : corners) {
			xyz.push_back (static_cast<float> (corner.x));
			xyz.push_back (static_cast<float> (corner.y));
			xyz.push_back (static_cast<float> (corner.z));
		}
	}

	UInt32 Count () const			{ return static_cast<UInt32> (xyz.size () / 9); }
};


// What happened to the polygons, reported with the export so a run can say
// how its faces were made rather than leave it to be inferred from a result.
struct FaceCounts {
	Int32 polygons = 0;
	Int32 withHoles = 0;			// an inner contour after a zero separator
	Int32 concave = 0;				// Archicad's own APIPgon_Concav flag
	Int32 triangulated = 0;			// by earcut, holes and concavity respected
	Int32 failed = 0;				// earcut gave nothing; the polygon is left out
	Int32 areaMismatch = 0;			// triangles and outline disagree by more than 1%
};


// One vertex of the active body, placed in the model.
bool VertexOf (const API_Tranmat& tranmat, Int32 index, Point3& out)
{
	API_Component3D vertex = {};
	vertex.header.typeID = API_VertID;
	vertex.header.index = index;
	if (ACAPI_3D_GetComponent (&vertex) != NoError) {
		return false;
	}
	const API_Coord3D placed = Placed (tranmat, vertex.vert.x, vertex.vert.y, vertex.vert.z);
	out = Point3 { placed.x, placed.y, placed.z };
	return true;
}


// Walk one body and add its polygons to `into`.
//
// `ACAPI_3D_GetComponent` is index-based and one-based, and a polygon's edges
// are reached through its edge *references* rather than directly: `fpedg` to
// `lpedg` index into the body's pedg array, each entry a signed edge index
// whose sign says which way round the edge is walked. A zero entry starts a
// new contour, which is a hole.
//
// Every contour is kept and the polygon is triangulated whole
// (`Triangulate.hpp`), because a fan from the first corner -- what this did
// first -- is exact only for a convex polygon, and a wall round a window is
// usually a concave outline. The fan covered the opening.
bool AddBody (Int32 bodyIndex, Triangles& into, FaceCounts& counts)
{
	API_Component3D body = {};
	body.header.typeID = API_BodyID;
	body.header.index = bodyIndex;
	if (ACAPI_3D_GetComponent (&body) != NoError) {
		return false;
	}
	const API_Tranmat tranmat = body.body.tranmat;

	std::vector<Point3> triangles;
	for (Int32 p = 1; p <= body.body.nPgon; ++p) {
		API_Component3D pgon = {};
		pgon.header.typeID = API_PgonID;
		pgon.header.index = p;
		if (ACAPI_3D_GetComponent (&pgon) != NoError) {
			continue;
		}
		++counts.polygons;
		if ((pgon.pgon.status & APIPgon_Concav) != 0) {
			++counts.concave;
		}

		std::vector<std::vector<Point3>> contours (1);
		for (Int32 e = pgon.pgon.fpedg; e <= pgon.pgon.lpedg; ++e) {
			API_Component3D pedg = {};
			pedg.header.typeID = API_PedgID;
			pedg.header.index = e;
			if (ACAPI_3D_GetComponent (&pedg) != NoError) {
				continue;
			}
			const Int32 edgeIndex = pedg.pedg.pedg;
			if (edgeIndex == 0) {
				if (!contours.back ().empty ()) {
					contours.emplace_back ();
				}
				continue;
			}

			API_Component3D edge = {};
			edge.header.typeID = API_EdgeID;
			edge.header.index = std::abs (edgeIndex);
			if (ACAPI_3D_GetComponent (&edge) != NoError) {
				continue;
			}
			Point3 point = {};
			if (VertexOf (tranmat, edgeIndex > 0 ? edge.edge.vert1 : edge.edge.vert2, point)) {
				contours.back ().push_back (point);
			}
		}
		while (contours.size () > 1 && contours.back ().size () < 3) {
			contours.pop_back ();
		}
		if (contours.size () > 1) {
			++counts.withHoles;
		}

		// A lone triangle is already exact.
		if (contours.size () == 1 && contours[0].size () == 3) {
			triangles.insert (triangles.end (), contours[0].begin (), contours[0].end ());
			continue;
		}
		const Triangulated made = Triangulate (contours, triangles);
		if (!made.ok) {
			++counts.failed;
			continue;
		}
		++counts.triangulated;
		if (std::fabs (made.triangleArea - made.polygonArea) > 0.01 * made.polygonArea + 1e-9) {
			++counts.areaMismatch;
		}
	}

	for (size_t k = 0; k + 2 < triangles.size (); k += 3) {
		into.Add (API_Coord3D { triangles[k].x, triangles[k].y, triangles[k].z },
				  API_Coord3D { triangles[k + 1].x, triangles[k + 1].y, triangles[k + 1].z },
				  API_Coord3D { triangles[k + 2].x, triangles[k + 2].y, triangles[k + 2].z });
	}
	return true;
}

}		// namespace


GS::String ExportModelCommand::GetName () const			{ return "ExportModel"; }

GS::Optional<GS::UniString> ExportModelCommand::GetInputParametersSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"path": {
				"type": "string",
				"description": "Where to write the model. Overwritten if it is there."
			}
		},
		"required": [ "path" ],
		"additionalProperties": false
	})");
}

GS::Optional<GS::UniString> ExportModelCommand::GetResponseSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"success": { "type": "boolean" },
			"path": { "type": "string" },
			"elements": { "type": "integer" },
			"triangles": { "type": "integer" },
			"bytes": { "type": "integer" },
			"zonesWereShown": {
				"type": "boolean",
				"description": "Whether Zones were already on in the 3D filter, or switched on for this export."
			},
			"polygons": { "type": "integer" },
			"polygonsWithHoles": { "type": "integer" },
			"polygonsConcave": { "type": "integer" },
			"triangulated": { "type": "integer" },
			"triangulationFailed": { "type": "integer" },
			"areaMismatch": { "type": "integer" }
		},
		"additionalProperties": false
	})");
}

GS::ObjectState ExportModelCommand::Execute (const GS::ObjectState& parameters,
											 GS::ProcessControl& /*processControl*/) const
{
	GS::UniString path;
	if (!parameters.Get ("path", path) || path.IsEmpty ()) {
		return Failed ("ExportModel needs a 'path' to write to.", APIERR_BADPARS);
	}

	// Zones into the model before the sight is taken, because turning them on
	// regenerates it. Held for the length of the export and put back after.
	GSErrCode zonesErr = NoError;
	const ZonesInTheModel zones (zonesErr);
	if (zonesErr != NoError) {
		return Failed (
			"Could not switch Zones on in the 3D filter, so the study would have "
			"nothing to measure. Show Zones in 'Filter Elements in 3D' and run again.",
			zonesErr);
	}

	// Rebuild the 3D model, explicitly, before anything is read.
	//
	// The caller has just changed which layers are visible, and the model does
	// not follow a layer change on its own -- it holds whatever was converted
	// when somebody last generated it. Writing the 3D filter back with the
	// kit's must-convert flag rebuilds after a *filter* change and, measured on
	// Silverwater on 17 September 2026, not after a layer one: an export taken
	// straight after `export_state` switched `07 | Fills.Areas` on carried the
	// same 22 layers as before, and none of that layer's Zones.
	//
	// So the model is regenerated outright. This is the documented way to make
	// Archicad build it, and it is what a person does by opening the 3D window.
	// Failure is not fatal: a model already converted under the right layers is
	// still the right model, and the counts in the response say what was found.
	// `APIDo_ChangeWindowID` onto the 3D window, which is the documented way
	// and the one a person takes by clicking the 3D button. There is no
	// `APIDo_Show3DID` in this kit; switching *to* the window is what builds
	// the model.
	API_WindowInfo intoThe3D = {};
	intoThe3D.typeID = APIWind_3DModelID;
	ACAPI_Automate (APIDo_ChangeWindowID, &intoThe3D);

	// The sight second, and this is the whole of why the first live run
	// returned nothing.
	//
	// `ACAPI_3D_GetNum` does not read "the 3D model"; it reads whichever
	// *sight* is currently selected, and an add-on starts with none. Measured
	// on Silverwater, 17 September 2026: with the 3D window converted and in
	// front, the body count came back 0 and the command wrote a 48-byte file
	// and called it success. Nothing about that reads as "you did not select
	// a sight".
	void* sight = nullptr;
	if (ACAPI_3D_GetCurrentWindowSight (&sight) != NoError || sight == nullptr) {
		return Failed (
			"Could not reach the 3D window's model. Open the 3D window so Archicad "
			"generates the model, then run this again.",
			APIERR_GENERAL);
	}
	// Two arguments: the sight to switch to, and the one being left. The
	// previous sight is handed back so it can be restored, and it is --
	// leaving an add-on's choice of sight selected would change what every
	// later 3D call in the session reads.
	void* previous = nullptr;
	ACAPI_3D_SelectSight (sight, &previous);

	Int32 bodyCount = 0;
	GSErrCode err = ACAPI_3D_GetNum (API_BodyID, &bodyCount);
	if (err != NoError) {
		return Failed ("Could not read the 3D model. Generate the 3D view and try again.", err);
	}

	// An empty model is refused rather than written. It can be honest -- every
	// layer hidden is a real state -- but the likelier cause by far is that the
	// 3D model has not been generated, and a 48-byte file reported as a
	// success is the shape of failure this project keeps meeting: the run says
	// it worked, the study measures nothing, and nothing says which happened.
	if (bodyCount == 0) {
		return Failed (
			"The 3D model holds no geometry. Open the 3D window so Archicad generates "
			"it -- and check the layers the study needs are switched on -- then run "
			"this again.",
			APIERR_GENERAL);
	}

	const std::map<short, GS::UniString> storeys = StoreyNames ();
	LayerNames layers;

	// Bodies are grouped back onto the element they were converted from: one
	// element is commonly several bodies, and the analysis selects by element
	// -- a layer, an element id prefix -- not by body.
	std::map<API_Guid, Triangles> byElement;
	std::map<API_Guid, API_Elem_Head> heads;
	FaceCounts faces;

	for (Int32 index = 1; index <= bodyCount; ++index) {
		API_Component3D body = {};
		body.header.typeID = API_BodyID;
		body.header.index = index;
		if (ACAPI_3D_GetComponent (&body) != NoError) {
			continue;
		}
		const API_Guid owner = body.body.parent.guid;
		if (owner == APINULLGuid) {
			continue;
		}
		heads[owner] = body.body.parent;
		AddBody (index, byElement[owner], faces);
	}

	Writer out;
	out.Bytes (MAGIC, 8);
	out.U32 (FORMAT_VERSION);

	// Where the project says it is. Read here rather than left to the Python
	// side so the file is self-contained: a model and the sun positions cast
	// against it must come from one statement of where north is.
	API_PlaceInfo place = {};
	if (ACAPI_Environment (APIEnv_GetPlaceSetsID, &place) != NoError) {
		place = {};
	}
	out.F64 (place.latitude);
	out.F64 (place.longitude);
	// The bearing of the model's **+Y axis**, which is what
	// `core.orientation.SiteOrientation.true_north_bearing_deg` means -- its
	// own banner calls it "true north bearing of model +Y". Archicad's
	// `place.north` is not that: on Silverwater it is 23.548 degrees while the
	// project's +Y stands at 293.548, and `cli._frame_turn` already converts
	// between them with this same 270 offset.
	//
	// Writing the raw field would have rotated every sun vector by 270
	// degrees. The drawing would have failed loudly -- the plan fit would be
	// out by a quarter turn -- but the *numbers* would have been wrong
	// quietly, which is the half nobody checks.
	out.F64 (std::fmod (270.0 + place.north * 180.0 / PI, 360.0));
	out.F64 (place.altitude);
	out.U32 (static_cast<UInt32> (byElement.size ()));

	UInt32 triangleTotal = 0;
	for (const auto& entry : byElement) {
		const API_Elem_Head& head = heads[entry.first];
		const Triangles& triangles = entry.second;

		out.Text (APIGuidToString (entry.first));
		out.Text (layers.Of (head.layer));
		out.Text (IfcClassOf (head.type));

		// The element id, which is what an --shadow-source "LABEL=prefix"
		// rule matches on.
		GS::UniString identifier;
		ACAPI_Database (APIDb_GetElementInfoStringID,
						const_cast<API_Guid*> (&entry.first), &identifier);
		out.Text (identifier);

		// The Zone's own name, beside the element ID, exactly as Archicad's IFC
		// export separates Name from LongName.
		out.Text (ZoneNameOf (entry.first, head.type));

		const auto storey = storeys.find (head.floorInd);
		out.Text (storey == storeys.end () ? GS::UniString () : storey->second);

		out.U32 (triangles.Count ());
		triangleTotal += triangles.Count ();
		if (!triangles.xyz.empty ()) {
			out.Bytes (triangles.xyz.data (), triangles.xyz.size () * sizeof (float));
		}
	}

	if (previous != nullptr) {
		void* ignored = nullptr;
		ACAPI_3D_SelectSight (previous, &ignored);
	}

	if (!out.Save (path)) {
		return Failed ("Could not write the model file. Check the path is writable.", APIERR_NOACCESSRIGHT);
	}

	GS::ObjectState answer;
	answer.Add ("success", true);
	answer.Add ("path", path);
	answer.Add ("elements", static_cast<Int32> (byElement.size ()));
	answer.Add ("triangles", static_cast<Int32> (triangleTotal));
	answer.Add ("bytes", static_cast<Int32> (out.Size ()));
	// Said, because a run that found no Zones needs to know whether they were
	// switched on for it or were there already.
	answer.Add ("zonesWereShown", zones.WasAlreadyShown ());
	// How the faces were made. `triangulationFailed` and `areaMismatch` are
	// the two that say a face in the file is not the face in the model.
	answer.Add ("polygons", faces.polygons);
	answer.Add ("polygonsWithHoles", faces.withHoles);
	answer.Add ("polygonsConcave", faces.concave);
	answer.Add ("triangulated", faces.triangulated);
	answer.Add ("triangulationFailed", faces.failed);
	answer.Add ("areaMismatch", faces.areaMismatch);
	return answer;
}

}		// namespace Loriini
