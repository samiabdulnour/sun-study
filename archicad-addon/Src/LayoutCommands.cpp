#include "LayoutCommands.hpp"

namespace Loriini {

namespace {

// Moves to a layout's database and remembers where we were.
//
// A Drawing lives in its layout's database and `ACAPI_Element_Get` reads
// from the current one, so a caller naming a layout has it made current for
// the duration and put back after -- Tapir's `ChangeWindow` reports success
// and does not always move (D40), and the Python side cannot tell.
class VisitDatabase {
public:
	explicit VisitDatabase (const API_Guid& layout)
	{
		if (ACAPI_Database (APIDb_GetCurrentDatabaseID, &was) != NoError) {
			return;
		}
		API_DatabaseInfo target = {};
		target.typeID = APIWind_LayoutID;
		target.databaseUnId.elemSetId = layout;
		moved = ACAPI_Database (APIDb_ChangeCurrentDatabaseID, &target) == NoError;
	}

	~VisitDatabase ()
	{
		if (moved) {
			ACAPI_Database (APIDb_ChangeCurrentDatabaseID, &was);
		}
	}

	bool	moved = false;

private:
	API_DatabaseInfo	was = {};
};


// Reads the optional layout the caller names, and visits it. Returns the
// failure to report, or an empty state when all is well.
bool VisitNamedLayout (const GS::ObjectState& parameters, GS::Optional<VisitDatabase>& visit, GS::ObjectState& failure)
{
	GS::ObjectState layout;
	if (!parameters.Get ("layoutDatabaseId", layout)) {
		return true;
	}
	GS::UniString guid;
	if (!layout.Get ("guid", guid)) {
		failure = Failed ("layoutDatabaseId needs a guid.", APIERR_BADPARS);
		return false;
	}
	visit.New (APIGuidFromString (guid.ToCStr ().Get ()));
	if (!visit->moved) {
		failure = Failed ("Archicad refused to make that layout current.", APIERR_BADDATABASE);
		return false;
	}
	return true;
}


GS::ObjectState BoxState (const API_Box& box)
{
	GS::ObjectState state;
	state.Add ("xMin", box.xMin);
	state.Add ("yMin", box.yMin);
	state.Add ("xMax", box.xMax);
	state.Add ("yMax", box.yMax);
	return state;
}


const char* AnchorName (API_AnchorID anchor)
{
	switch (anchor) {
		case APIAnc_LT: return "LT";
		case APIAnc_MT: return "MT";
		case APIAnc_RT: return "RT";
		case APIAnc_LM: return "LM";
		case APIAnc_MM: return "MM";
		case APIAnc_RM: return "RM";
		case APIAnc_LB: return "LB";
		case APIAnc_MB: return "MB";
		case APIAnc_RB: return "RB";
		default:        return "?";
	}
}


// The clip frame, as the four corners of its bounding box, from the polygon
// memo. Reported as a box rather than a polygon because that is what the
// caller writes, and a frame that is not a rectangle is not one this add-on
// made.
bool ReadFrame (const API_Element& element, API_Box& frame)
{
	API_ElementMemo memo = {};
	if (ACAPI_Element_GetMemo (element.header.guid, &memo, APIMemoMask_Polygon) != NoError) {
		return false;
	}
	bool found = false;
	if (memo.coords != nullptr && element.drawing.poly.nCoords >= 1) {
		frame = { (*memo.coords)[1].x, (*memo.coords)[1].y, (*memo.coords)[1].x, (*memo.coords)[1].y };
		for (Int32 index = 1; index <= element.drawing.poly.nCoords; ++index) {
			const API_Coord& c = (*memo.coords)[index];
			frame.xMin = GS::Min (frame.xMin, c.x);
			frame.yMin = GS::Min (frame.yMin, c.y);
			frame.xMax = GS::Max (frame.xMax, c.x);
			frame.yMax = GS::Max (frame.yMax, c.y);
		}
		found = true;
	}
	ACAPI_DisposeElemMemoHdls (&memo);
	return found;
}


// A rectangle, as the closed five-point polygon a Drawing's clip frame is.
void WriteFrame (API_Element& element, API_ElementMemo& memo, const API_Box& frame)
{
	element.drawing.poly.nCoords = 5;
	element.drawing.poly.nSubPolys = 1;
	element.drawing.poly.nArcs = 0;

	memo.coords = reinterpret_cast<API_Coord**> (BMAllocateHandle ((element.drawing.poly.nCoords + 1) * sizeof (API_Coord), ALLOCATE_CLEAR, 0));
	memo.pends = reinterpret_cast<Int32**> (BMAllocateHandle ((element.drawing.poly.nSubPolys + 1) * sizeof (Int32), ALLOCATE_CLEAR, 0));
	if (memo.coords == nullptr || memo.pends == nullptr) {
		return;
	}
	(*memo.coords)[1] = { frame.xMin, frame.yMin };
	(*memo.coords)[2] = { frame.xMax, frame.yMin };
	(*memo.coords)[3] = { frame.xMax, frame.yMax };
	(*memo.coords)[4] = { frame.xMin, frame.yMax };
	(*memo.coords)[5] = (*memo.coords)[1];
	(*memo.pends)[0] = 0;
	(*memo.pends)[1] = element.drawing.poly.nCoords;
}


bool ReadBox (const GS::ObjectState& from, API_Box& into)
{
	return from.Get ("xMin", into.xMin) && from.Get ("yMin", into.yMin) &&
		   from.Get ("xMax", into.xMax) && from.Get ("yMax", into.yMax);
}

}		// namespace


// -- GetDrawings -------------------------------------------------------------

GS::String GetDrawingsCommand::GetName () const			{ return "GetDrawings"; }

GS::Optional<GS::UniString> GetDrawingsCommand::GetInputParametersSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"layoutDatabaseId": {
				"type": "object",
				"description": "The layout to read. Made current for the call and restored after; left out, the current database is read.",
				"properties": { "guid": { "type": "string" } },
				"required": [ "guid" ]
			}
		},
		"additionalProperties": false
	})");
}

GS::Optional<GS::UniString> GetDrawingsCommand::GetResponseSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"success": { "type": "boolean" },
			"drawings": {
				"type": "array",
				"items": {
					"type": "object",
					"properties": {
						"guid": { "type": "string" },
						"name": { "type": "string" },
						"pos": { "type": "object" },
						"anchor": { "type": "string" },
						"useOwnOrigoAsAnchor": { "type": "boolean" },
						"isCutWithFrame": { "type": "boolean" },
						"manualUpdate": { "type": "boolean" },
						"ratio": { "type": "number" },
						"drawingScale": { "type": "number" },
						"angle": { "type": "number" },
						"bounds": { "type": "object" },
						"frame": { "type": "object" },
						"contentBox": { "type": "object" }
					}
				}
			},
			"error": { "type": "object" }
		}
	})");
}

GS::ObjectState GetDrawingsCommand::Execute (const GS::ObjectState& parameters,
											 GS::ProcessControl& /*processControl*/) const
{
	GS::Optional<VisitDatabase> visit;
	GS::ObjectState failure;
	if (!VisitNamedLayout (parameters, visit, failure)) {
		return failure;
	}

	GS::Array<API_Guid> found;
	const GSErrCode err = ACAPI_Element_GetElemList (API_DrawingID, &found);
	if (err != NoError) {
		return Failed ("Failed to list the Drawings in the current database.", err);
	}

	GS::Array<GS::ObjectState> drawings;
	for (const API_Guid& guid : found) {
		API_Element element = {};
		element.header.guid = guid;
		if (ACAPI_Element_Get (&element) != NoError) {
			continue;
		}
		GS::ObjectState one;
		one.Add ("guid", APIGuidToString (guid));
		one.Add ("name", GS::UniString (element.drawing.name));
		GS::ObjectState pos;
		pos.Add ("x", element.drawing.pos.x);
		pos.Add ("y", element.drawing.pos.y);
		one.Add ("pos", pos);
		one.Add ("anchor", GS::UniString (AnchorName (element.drawing.anchorPoint)));
		one.Add ("useOwnOrigoAsAnchor", element.drawing.useOwnOrigoAsAnchor);
		one.Add ("isCutWithFrame", element.drawing.isCutWithFrame);
		one.Add ("manualUpdate", element.drawing.manualUpdate);
		one.Add ("ratio", element.drawing.ratio);
		one.Add ("drawingScale", element.drawing.drawingScale);
		one.Add ("angle", element.drawing.angle);
		one.Add ("bounds", BoxState (element.drawing.bounds));

		API_Box frame = {};
		if (ReadFrame (element, frame)) {
			one.Add ("frame", BoxState (frame));
		}
		// The kit's signature is untyped, so the guid is passed as a copy it
		// is allowed to point at.
		API_Box content = {};
		API_Guid lookup = guid;
		if (ACAPI_Database (APIDb_GetFullDrawingContentBoxID, &content, &lookup) == NoError) {
			one.Add ("contentBox", BoxState (content));
		}
		drawings.Push (one);
	}

	GS::ObjectState result = Succeeded ();
	result.Add ("drawings", drawings);
	return result;
}


// -- ArrangeDrawings ---------------------------------------------------------

GS::String ArrangeDrawingsCommand::GetName () const			{ return "ArrangeDrawings"; }

GS::Optional<GS::UniString> ArrangeDrawingsCommand::GetInputParametersSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"layoutDatabaseId": {
				"type": "object",
				"description": "The layout the drawings are on. Made current for the call and restored after; left out, the drawings are looked for in the current database.",
				"properties": { "guid": { "type": "string" } },
				"required": [ "guid" ]
			},
			"drawings": {
				"type": "array",
				"description": "Each Drawing element, where it goes, and optionally the clip frame it gets. Positions are metres on the layout.",
				"items": {
					"type": "object",
					"properties": {
						"guid": { "type": "string" },
						"x": { "type": "number" },
						"y": { "type": "number" },
						"frame": {
							"type": "object",
							"description": "A rectangular clip frame. In layout coordinates, or relative to the drawing's origin when frameRelativeToOrigin is set.",
							"properties": {
								"xMin": { "type": "number" }, "yMin": { "type": "number" },
								"xMax": { "type": "number" }, "yMax": { "type": "number" }
							},
							"required": [ "xMin", "yMin", "xMax", "yMax" ]
						}
					},
					"required": [ "guid", "x", "y" ],
					"additionalProperties": false
				}
			},
			"anchor": {
				"type": "string",
				"enum": [ "centre", "origin" ],
				"description": "What x and y place: the centre of the drawing's box, or the drawing's own origin -- for a 3D Document, the projected model origin. Default centre."
			},
			"frameRelativeToOrigin": { "type": "boolean", "description": "Whether a frame's coordinates are measured from the drawing's own origin rather than the layout's. Default false." },
			"fitFrame": { "type": "boolean", "description": "Drop the clip polygon so the frame follows the drawing's own extent. Ignored for a drawing given a frame. Default false." },
			"autoUpdate": { "type": "boolean", "description": "Let Archicad regenerate the drawing when the layout opens. Default true." }
		},
		"required": [ "drawings" ],
		"additionalProperties": false
	})");
}

GS::Optional<GS::UniString> ArrangeDrawingsCommand::GetResponseSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"success": { "type": "boolean" },
			"arranged": { "type": "integer" },
			"failures": { "type": "array", "items": { "type": "object" } },
			"error": { "type": "object" }
		}
	})");
}

GS::ObjectState ArrangeDrawingsCommand::Execute (const GS::ObjectState& parameters,
												 GS::ProcessControl& /*processControl*/) const
{
	GS::Array<GS::ObjectState> drawings;
	if (!parameters.Get ("drawings", drawings) || drawings.IsEmpty ()) {
		return Failed ("drawings must name at least one Drawing.", APIERR_BADPARS);
	}

	GS::UniString anchor = "centre";
	bool frameRelative = false;
	bool fitFrame = false;
	bool autoUpdate = true;
	parameters.Get ("anchor", anchor);
	parameters.Get ("frameRelativeToOrigin", frameRelative);
	parameters.Get ("fitFrame", fitFrame);
	parameters.Get ("autoUpdate", autoUpdate);
	const bool byOrigin = anchor == "origin";

	GS::Optional<VisitDatabase> visit;
	GS::ObjectState failure;
	if (!VisitNamedLayout (parameters, visit, failure)) {
		return failure;
	}

	Int32 arranged = 0;
	GS::Array<GS::ObjectState> failures;

	// One undo step for the whole sheet, which is how a person would want to
	// take it back. An element change outside an undo scope is refused.
	const GSErrCode err = ACAPI_CallUndoableCommand ("Arrange drawings", [&] () -> GSErrCode {
		for (const GS::ObjectState& wanted : drawings) {
			GS::UniString guid;
			double x = 0.0;
			double y = 0.0;
			wanted.Get ("guid", guid);
			wanted.Get ("x", x);
			wanted.Get ("y", y);
			GS::ObjectState frameState;
			API_Box frame = {};
			const bool hasFrame = wanted.Get ("frame", frameState) && ReadBox (frameState, frame);

			API_Element element = {};
			element.header.guid = APIGuidFromString (guid.ToCStr ().Get ());
			GSErrCode one = ACAPI_Element_Get (&element);
			if (one == NoError && element.header.type != API_DrawingID) {
				one = APIERR_BADELEMENTTYPE;
			}
			if (one == NoError) {
				API_Element mask;
				ACAPI_ELEMENT_MASK_CLEAR (mask);

				// By its own origin or by the middle of its box. The origin
				// is what a 3D Document's drawing is about: the projected
				// model origin, which on a site modelled around it is the
				// building, so the building goes where the origin goes
				// whatever size the content turns out to be. The middle is
				// for a drawing whose content is what it should show.
				element.drawing.useOwnOrigoAsAnchor = byOrigin;
				element.drawing.anchorPoint = APIAnc_MM;
				element.drawing.pos.x = x;
				element.drawing.pos.y = y;
				ACAPI_ELEMENT_MASK_SET (mask, API_DrawingType, useOwnOrigoAsAnchor);
				ACAPI_ELEMENT_MASK_SET (mask, API_DrawingType, anchorPoint);
				ACAPI_ELEMENT_MASK_SET (mask, API_DrawingType, pos);

				if (autoUpdate) {
					element.drawing.manualUpdate = false;
					ACAPI_ELEMENT_MASK_SET (mask, API_DrawingType, manualUpdate);
				}

				API_ElementMemo memo = {};
				UInt64 memoMask = 0;
				if (hasFrame) {
					if (frameRelative) {
						frame.xMin += x;  frame.xMax += x;
						frame.yMin += y;  frame.yMax += y;
					}
					element.drawing.isCutWithFrame = true;
					ACAPI_ELEMENT_MASK_SET (mask, API_DrawingType, isCutWithFrame);
					ACAPI_ELEMENT_MASK_SET (mask, API_DrawingType, poly);
					WriteFrame (element, memo, frame);
					memoMask = APIMemoMask_Polygon;
				} else if (fitFrame) {
					element.drawing.isCutWithFrame = false;
					ACAPI_ELEMENT_MASK_SET (mask, API_DrawingType, isCutWithFrame);
				}

				one = ACAPI_Element_Change (&element, &mask, memoMask != 0 ? &memo : nullptr, memoMask, true);
				if (memoMask != 0) {
					ACAPI_DisposeElemMemoHdls (&memo);
				}
			}

			if (one == NoError) {
				++arranged;
			} else {
				GS::ObjectState failed;
				failed.Add ("guid", guid);
				failed.Add ("code", static_cast<Int32> (one));
				failures.Push (failed);
			}
		}
		return NoError;
	});
	if (err != NoError) {
		return Failed ("Failed to open an undo step for the drawings.", err);
	}

	GS::ObjectState result = Succeeded ();
	result.Add ("arranged", arranged);
	result.Add ("failures", failures);
	return result;
}

}		// namespace Loriini
