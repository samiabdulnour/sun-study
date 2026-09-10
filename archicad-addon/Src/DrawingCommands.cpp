#include "DrawingCommands.hpp"
#include "Support.hpp"

// BMAllocateHandle, for the polygon memo handles.
#include "BM.hpp"

namespace Loriini {

namespace {

// One contour's worth of points, before it is flattened into Archicad's
// single coordinate array.
typedef GS::Array<API_Coord> Contour;


// Reads `[[{x,y},...], [{x,y},...]]` -- the outer contour first, then any
// holes.
//
// A hole is the whole reason this command takes a list of lists where Tapir
// takes one list. Tapir's schema says "single contour, no holes" in as many
// words, and a shadow cast across a courtyard has a hole in it. Drawn without
// one, the courtyard is filled in and the diagram claims a shadow where the
// sky is.
bool ReadContours (const GS::ObjectState& fill, GS::Array<Contour>& into, GS::UniString& why)
{
	GS::Array<GS::ObjectState> contours;
	if (!fill.Get ("contours", contours) || contours.IsEmpty ()) {
		why = "Every fill needs a 'contours' list, the outer contour first.";
		return false;
	}

	for (const GS::ObjectState& one : contours) {
		GS::Array<GS::ObjectState> points;
		if (!one.Get ("points", points)) {
			why = "Each contour needs a 'points' list.";
			return false;
		}
		if (points.GetSize () < 3) {
			why = "A contour needs at least three points.";
			return false;
		}

		Contour contour;
		for (const GS::ObjectState& point : points) {
			API_Coord coordinate = {};
			if (!point.Get ("x", coordinate.x) || !point.Get ("y", coordinate.y)) {
				why = "Each point needs an x and a y.";
				return false;
			}
			contour.Push (coordinate);
		}
		into.Push (contour);
	}
	return true;
}


// Lays the contours out the way Archicad's polygon memo wants them.
//
// Two conventions, neither of them guessable and both easy to get subtly
// wrong: the coordinate array is **one-based**, with index 0 unused, and every
// sub-contour repeats its first point as its last. `pends` then holds the
// index of each contour's final point, with `pends[0]` a zero that is not a
// contour at all.
//
// Getting the closing point wrong does not fail. It draws a fill with one
// edge missing, which on a plan reads as a rendering artefact rather than as
// a bug in the caller.
GSErrCode BuildPolygon (const GS::Array<Contour>& contours, API_HatchType& fill, API_ElementMemo& memo)
{
	Int32 total = 0;
	for (const Contour& contour : contours) {
		total += static_cast<Int32> (contour.GetSize ()) + 1;		// the repeated closing point
	}

	fill.poly.nCoords = total;
	fill.poly.nSubPolys = static_cast<Int32> (contours.GetSize ());
	fill.poly.nArcs = 0;

	memo.coords = reinterpret_cast<API_Coord**> (BMAllocateHandle ((total + 1) * sizeof (API_Coord), ALLOCATE_CLEAR, 0));
	memo.pends = reinterpret_cast<Int32**> (BMAllocateHandle ((fill.poly.nSubPolys + 1) * sizeof (Int32), ALLOCATE_CLEAR, 0));
	if (memo.coords == nullptr || memo.pends == nullptr) {
		return APIERR_MEMFULL;
	}

	Int32 at = 0;
	Int32 which = 0;
	for (const Contour& contour : contours) {
		for (const API_Coord& point : contour) {
			(*memo.coords)[++at] = point;
		}
		(*memo.coords)[++at] = contour[0];							// close it
		(*memo.pends)[++which] = at;
	}
	return NoError;
}


// Which kind of fill this is, in Archicad's own words.
//
// A solar diagram wants a **drafting** fill. A cover fill belongs to a room
// and a cut fill to something the section plane passes through, and either
// will disappear or change under a model view option the tool never set --
// which is how a diagram that was right on screen arrives empty on a sheet.
// Tapir has no field for this at all, so every fill it makes takes whatever
// the Fill tool was last left on.
bool ReadDetermination (const GS::ObjectState& fill, short& into, GS::UniString& why)
{
	GS::UniString kind;
	if (!fill.Get ("determination", kind)) {
		return false;
	}
	if (kind == "drafting") {
		into = APIHatch_DraftingFills;
	} else if (kind == "cut") {
		into = APIHatch_CutFills;
	} else if (kind == "cover") {
		into = APIHatch_CoverFills;
	} else {
		why = "determination takes 'drafting', 'cut' or 'cover'.";
		return false;
	}
	return true;
}


// Everything about one fill that is not its outline.
GSErrCode ApplyAttributes (const GS::ObjectState& wanted, API_HatchType& fill, GS::UniString& why)
{
	ReadInt (wanted, "layerIndex", fill.head.layer);
	ReadShort (wanted, "floorIndex", fill.head.floorInd);

	ReadShort (wanted, "contourPen", fill.contPen.penIndex);
	ReadShort (wanted, "fillPen", fill.fillPen.penIndex);
	ReadShort (wanted, "backgroundPen", fill.fillBGPen);
	ReadInt (wanted, "fillIndex", fill.fillInd);
	ReadInt (wanted, "lineTypeIndex", fill.ltypeInd);
	ReadInt (wanted, "buildingMaterialIndex", fill.buildingMaterial);

	double weight = 0.0;
	if (wanted.Get ("penWeight", weight)) {
		fill.penWeight = weight;
	}

	bool showArea = false;
	if (wanted.Get ("showArea", showArea)) {
		fill.showArea = showArea;
	}

	if (!ReadDetermination (wanted, fill.determination, why) && !why.IsEmpty ()) {
		return APIERR_BADPARS;
	}

	// The colours, and the flags that switch them on. Set together and never
	// separately: the RGB field without its flag is ignored in silence, and
	// the flag without the field paints black.
	API_RGBColor colour = {};
	if (ReadColour (wanted, "foregroundColour", colour, why)) {
		fill.foregroundRGB = colour;
		fill.hatchFlags |= APIHatch_HasFgRGBColor;
	} else if (!why.IsEmpty ()) {
		return APIERR_BADPARS;
	}
	if (ReadColour (wanted, "backgroundColour", colour, why)) {
		fill.backgroundRGB = colour;
		fill.hatchFlags |= APIHatch_HasBkgRGBColor;
	} else if (!why.IsEmpty ()) {
		return APIERR_BADPARS;
	}

	return NoError;
}

}		// namespace


GS::String CreateFillsCommand::GetName () const			{ return "CreateFills"; }
GS::String CreateFillsCommand::GetNamespace () const	{ return CommandNamespace (); }

GS::Optional<GS::UniString> CreateFillsCommand::GetInputParametersSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"fills": {
				"type": "array",
				"items": {
					"type": "object",
					"properties": {
						"contours": {
							"type": "array",
							"description": "The outline, outer contour first, every contour after it a hole. Do not repeat the first point at the end; Archicad's closing point is added here.",
							"items": {
								"type": "object",
								"properties": {
									"points": {
										"type": "array",
										"items": {
											"type": "object",
											"properties": { "x": { "type": "number" }, "y": { "type": "number" } },
											"required": [ "x", "y" ]
										},
										"minItems": 3
									}
								},
								"required": [ "points" ]
							},
							"minItems": 1
						},
						"layerIndex": { "type": "integer" },
						"floorIndex": { "type": "integer" },
						"contourPen": { "type": "integer" },
						"fillPen": { "type": "integer" },
						"backgroundPen": { "type": "integer" },
						"fillIndex": { "type": "integer" },
						"lineTypeIndex": { "type": "integer" },
						"buildingMaterialIndex": { "type": "integer" },
						"penWeight": { "type": "number" },
						"showArea": { "type": "boolean" },
						"determination": { "type": "string", "enum": [ "drafting", "cut", "cover" ] },
						"foregroundColour": {
							"type": "object",
							"description": "The fill's own colour, each channel 0 to 1. Set, the fill carries this colour instead of its fill pen's.",
							"properties": { "red": { "type": "number" }, "green": { "type": "number" }, "blue": { "type": "number" } },
							"required": [ "red", "green", "blue" ]
						},
						"backgroundColour": {
							"type": "object",
							"properties": { "red": { "type": "number" }, "green": { "type": "number" }, "blue": { "type": "number" } },
							"required": [ "red", "green", "blue" ]
						},
						"elementId": {
							"type": "string",
							"description": "The element's ID, set as it is created rather than in a second pass."
						}
					},
					"required": [ "contours" ]
				},
				"minItems": 1
			}
		},
		"required": [ "fills" ],
		"additionalProperties": false
	})");
}

GS::Optional<GS::UniString> CreateFillsCommand::GetResponseSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"success": { "type": "boolean" },
			"elements": {
				"type": "array",
				"items": { "type": "object", "properties": { "guid": { "type": "string" } } }
			},
			"error": { "type": "object" }
		}
	})");
}

GS::ObjectState CreateFillsCommand::Execute (const GS::ObjectState& parameters,
											 GS::ProcessControl& /*processControl*/) const
{
	GS::Array<GS::ObjectState> wanted;
	if (!parameters.Get ("fills", wanted) || wanted.IsEmpty ()) {
		return Failed ("Nothing to draw: 'fills' is empty.", APIERR_BADPARS);
	}

	GS::Array<API_Guid> made;
	GS::UniString why;
	GSErrCode failure = NoError;
	GS::UniString failureText;

	// One undo step for the whole batch. A shadow diagram is six thousand
	// fills on the reference project, and an undo that takes them back one at
	// a time is not an undo anybody can use.
	const GSErrCode err = ACAPI_CallUndoableCommand ("Draw solar diagram fills", [&] () -> GSErrCode {
		for (const GS::ObjectState& one : wanted) {
			GS::Array<Contour> contours;
			why.Clear ();
			if (!ReadContours (one, contours, why)) {
				failure = APIERR_BADPARS;
				failureText = why;
				return APIERR_BADPARS;
			}

			API_Element element = {};
			API_ElementMemo memo = {};
			element.header.type = API_HatchID;

			// The defaults first, so anything the caller does not name is
			// whatever the Fill tool is set to rather than zero. A layer index
			// of zero is not "no opinion", it is a real layer.
			GSErrCode step = ACAPI_Element_GetDefaults (&element, &memo);
			if (step != NoError) {
				failure = step;
				failureText = "Could not read the Fill tool's defaults.";
				return step;
			}

			why.Clear ();
			step = ApplyAttributes (one, element.hatch, why);
			if (step != NoError) {
				ACAPI_DisposeElemMemoHdls (&memo);
				failure = step;
				failureText = why;
				return step;
			}

			// The outline replaces whatever the defaults carried, so the
			// default's own polygon handles go first.
			ACAPI_DisposeElemMemoHdls (&memo);
			memo = {};
			step = BuildPolygon (contours, element.hatch, memo);
			if (step != NoError) {
				ACAPI_DisposeElemMemoHdls (&memo);
				failure = step;
				failureText = "Ran out of memory laying out a fill's outline.";
				return step;
			}

			step = ACAPI_Element_Create (&element, &memo);
			ACAPI_DisposeElemMemoHdls (&memo);
			if (step != NoError) {
				failure = step;
				failureText = "Archicad refused to create a fill.";
				return step;
			}

			// The ID, while the element is in hand. Tapir needs a second
			// command for this, and a second pass over six thousand fills is
			// both slow and a place for the two lists to fall out of step
			// (D73).
			GS::UniString identifier;
			if (one.Get ("elementId", identifier) && !identifier.IsEmpty ()) {
				step = ACAPI_Database (APIDb_ChangeElementInfoStringID, &element.header.guid, &identifier);
				if (step != NoError) {
					failure = step;
					failureText = "A fill was created but would not take its element ID.";
					return step;
				}
			}

			made.Push (element.header.guid);
		}
		return NoError;
	});

	if (err != NoError) {
		// The whole batch is rolled back, so nothing was drawn. Said plainly,
		// because a partial refusal and a total one need different responses
		// from the caller and guessing wrong wastes a run.
		return Failed (failureText.IsEmpty () ? GS::UniString ("Failed to draw the fills.") : failureText,
					   failure != NoError ? failure : err);
	}

	GS::Array<GS::ObjectState> elements;
	for (const API_Guid& guid : made) {
		GS::ObjectState one;
		one.Add ("guid", APIGuidToString (guid));
		elements.Push (one);
	}

	GS::ObjectState result = Succeeded ();
	result.Add ("elements", elements);
	return result;
}

}		// namespace Loriini
