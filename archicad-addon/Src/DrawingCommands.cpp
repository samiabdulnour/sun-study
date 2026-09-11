#include "DrawingCommands.hpp"
#include "Support.hpp"

// BMAllocateHandle, for the polygon memo handles.
#include "BM.hpp"

#include <cstring>

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


// -- CreateTexts --------------------------------------------------------------

namespace Loriini {

namespace {

// Archicad's own names for where the text hangs off its point.
bool ReadAnchor (const GS::UniString& name, API_AnchorID& into)
{
	static const struct { const char* name; API_AnchorID anchor; } anchors[] = {
		{ "LeftTop", APIAnc_LT },      { "LeftMiddle", APIAnc_LM },      { "LeftBottom", APIAnc_LB },
		{ "MiddleTop", APIAnc_MT },    { "MiddleMiddle", APIAnc_MM },    { "MiddleBottom", APIAnc_MB },
		{ "RightTop", APIAnc_RT },     { "RightMiddle", APIAnc_RM },     { "RightBottom", APIAnc_RB },
	};
	for (const auto& entry : anchors) {
		if (name == entry.name) {
			into = entry.anchor;
			return true;
		}
	}
	return false;
}


bool ReadJustification (const GS::UniString& name, API_JustID& into)
{
	if (name == "Left")   { into = APIJust_Left;   return true; }
	if (name == "Center") { into = APIJust_Center; return true; }
	if (name == "Right")  { into = APIJust_Right;  return true; }
	if (name == "Full")   { into = APIJust_Full;   return true; }
	return false;
}


// The content: one paragraph, one run, a line break wherever the string has
// one. The shape Tapir builds, because it is the shape Archicad reads back
// without complaint.
GSErrCode BuildContent (const GS::UniString& text, API_TextType& data, API_ElementMemo& memo)
{
	// UTF-8, not UTF-16. Archicad 26 reads `textContent` as a byte string, so
	// a UTF-16 copy -- which is what Tapir 1.5.8 writes for this version --
	// ends at the NUL that is the second byte of the first character. Measured
	// on the Kogarah solar study, 11 September 2026: a ten-character text and
	// a one-character text came back the same width from both commands.
	const auto utf8 = text.ToCStr (CC_UTF8);
	const char* bytes = utf8.Get ();
	const GSSize length = static_cast<GSSize> (strlen (bytes));
	memo.textContent = BMhAllClear (length + 1);
	if (memo.textContent == nullptr) {
		return APIERR_MEMFULL;
	}
	memcpy (*memo.textContent, bytes, length);

	const GS::UniChar newline = GS::UniChar (char (10));
	data.nLine = text.Count (newline) + 1;

	memo.paragraphs = reinterpret_cast<API_ParagraphType**> (BMhAllClear (sizeof (API_ParagraphType)));
	if (memo.paragraphs == nullptr) {
		return APIERR_MEMFULL;
	}
	API_ParagraphType& paragraph = (*memo.paragraphs)[0];
	paragraph.from = 0;
	paragraph.range = text.GetLength ();
	paragraph.tab = reinterpret_cast<API_TabType*> (BMpAllClear (sizeof (API_TabType)));
	paragraph.run = reinterpret_cast<API_RunType*> (BMpAllClear (sizeof (API_RunType)));
	paragraph.eolPos = reinterpret_cast<Int32*> (BMpAllClear (data.nLine * sizeof (Int32)));
	if (paragraph.tab == nullptr || paragraph.run == nullptr || paragraph.eolPos == nullptr) {
		return APIERR_MEMFULL;
	}
	paragraph.run[0].from = 0;
	paragraph.run[0].range = text.GetLength ();
	paragraph.run[0].pen = data.pen;
	paragraph.run[0].faceBits = data.faceBits;
	paragraph.run[0].font = data.font;
	paragraph.run[0].effectBits = data.effectsBits;
	paragraph.run[0].size = data.size;

	Int32 last = 0;
	for (Int32 line = 0; line < data.nLine; ++line) {
		const UIndex found = text.FindFirst (newline, line == 0 ? 0 : last + 1);
		const Int32 end = (found != MaxUIndex) ? static_cast<Int32> (found) : static_cast<Int32> (text.GetLength ());
		const Int32 offset = end - last - 1;
		paragraph.eolPos[line] = offset < 0 ? 0 : offset;
		last = (found != MaxUIndex) ? static_cast<Int32> (found) : end;
	}
	data.useEolPos = true;
	return NoError;
}

}		// namespace


GS::String CreateTextsCommand::GetName () const			{ return "CreateTexts"; }

GS::Optional<GS::UniString> CreateTextsCommand::GetInputParametersSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"texts": {
				"type": "array",
				"items": {
					"type": "object",
					"properties": {
						"coordinate": {
							"type": "object",
							"properties": { "x": { "type": "number" }, "y": { "type": "number" } },
							"required": [ "x", "y" ]
						},
						"text": { "type": "string" },
						"height": { "type": "number", "description": "Character height, in the unit the Text tool uses here: millimetres on paper." },
						"justification": { "type": "string", "enum": [ "Left", "Center", "Right", "Full" ] },
						"anchor": {
							"type": "string",
							"description": "Which point of the box sits on the coordinate. The Text tool's default when left out.",
							"enum": [ "LeftTop", "LeftMiddle", "LeftBottom", "MiddleTop", "MiddleMiddle", "MiddleBottom", "RightTop", "RightMiddle", "RightBottom" ]
						},
						"angle": { "type": "number", "description": "Radians, anticlockwise from +X." },
						"layerIndex": { "type": "integer" },
						"floorIndex": { "type": "integer" },
						"penIndex": { "type": "integer" },
						"width": {
							"type": "number",
							"description": "Box width in millimetres on paper, at which the text wraps. Left out, the box fits the text and never wraps."
						},
						"elementId": { "type": "string" }
					},
					"required": [ "coordinate", "text" ]
				},
				"minItems": 1
			}
		},
		"required": [ "texts" ],
		"additionalProperties": false
	})");
}

GS::Optional<GS::UniString> CreateTextsCommand::GetResponseSchema () const
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

GS::ObjectState CreateTextsCommand::Execute (const GS::ObjectState& parameters,
											 GS::ProcessControl& /*processControl*/) const
{
	GS::Array<GS::ObjectState> wanted;
	if (!parameters.Get ("texts", wanted) || wanted.IsEmpty ()) {
		return Failed ("Nothing to write: 'texts' is empty.", APIERR_BADPARS);
	}

	GS::Array<API_Guid> made;
	GSErrCode failure = NoError;
	GS::UniString failureText;

	const GSErrCode err = ACAPI_CallUndoableCommand ("Write site analysis texts", [&] () -> GSErrCode {
		for (const GS::ObjectState& one : wanted) {
			API_Element element = {};
			API_ElementMemo memo = {};
			element.header.type = API_TextID;

			GSErrCode step = ACAPI_Element_GetDefaults (&element, &memo);
			if (step != NoError) {
				failure = step;
				failureText = "Could not read the Text tool's defaults.";
				return step;
			}
			ACAPI_DisposeElemMemoHdls (&memo);
			memo = {};

			API_TextType& text = element.text;
			GS::ObjectState coordinate;
			if (!one.Get ("coordinate", coordinate) || !coordinate.Get ("x", text.loc.x) || !coordinate.Get ("y", text.loc.y)) {
				failure = APIERR_BADPARS;
				failureText = "Every text needs a coordinate with an x and a y.";
				return failure;
			}
			GS::UniString content;
			if (!one.Get ("text", content)) {
				failure = APIERR_BADPARS;
				failureText = "Every text needs its text.";
				return failure;
			}

			ReadInt (one, "layerIndex", element.header.layer);
			ReadShort (one, "floorIndex", element.header.floorInd);
			ReadShort (one, "penIndex", text.pen);

			double height = 0.0;
			if (one.Get ("height", height) && height > 0.0) {
				text.size = height;
			}
			double angle = 0.0;
			if (one.Get ("angle", angle)) {
				text.angle = angle;
			}
			GS::UniString word;
			if (one.Get ("justification", word) && !ReadJustification (word, text.just)) {
				failure = APIERR_BADPARS;
				failureText = "justification takes Left, Center, Right or Full.";
				return failure;
			}
			if (one.Get ("anchor", word) && !ReadAnchor (word, text.anchor)) {
				failure = APIERR_BADPARS;
				failureText = "anchor takes LeftTop ... RightBottom.";
				return failure;
			}

			// The box. Non-breaking is what a label is; a width is what a
			// note wraps at. The Text tool's own default is neither reliably.
			double width = 0.0;
			if (one.Get ("width", width) && width > 0.0) {
				text.nonBreaking = false;
				text.width = width;
			} else {
				text.nonBreaking = true;
				text.width = 0.0;
			}
			text.height = 0.0;

			step = BuildContent (content, text, memo);
			if (step != NoError) {
				ACAPI_DisposeElemMemoHdls (&memo);
				failure = step;
				failureText = "Ran out of memory laying out a text.";
				return step;
			}

			step = ACAPI_Element_Create (&element, &memo);
			ACAPI_DisposeElemMemoHdls (&memo);
			if (step != NoError) {
				failure = step;
				failureText = "Archicad refused to create a text.";
				return step;
			}

			GS::UniString identifier;
			if (one.Get ("elementId", identifier) && !identifier.IsEmpty ()) {
				step = ACAPI_Database (APIDb_ChangeElementInfoStringID, &element.header.guid, &identifier);
				if (step != NoError) {
					failure = step;
					failureText = "A text was created but would not take its element ID.";
					return step;
				}
			}
			made.Push (element.header.guid);
		}
		return NoError;
	});

	if (err != NoError) {
		return Failed (failureText.IsEmpty () ? GS::UniString ("Failed to write the texts.") : failureText,
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


// -- PlaceFigures ---------------------------------------------------------------

namespace Loriini {

namespace {

// Base64, decoded by hand: the picture comes over the wire inside the JSON,
// because no command can point Archicad at a path on the machine it runs on,
// and the kit ships no decoder this add-on can reach.
bool DecodeBase64 (const GS::UniString& text, GS::Array<char>& into)
{
	static const char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
	unsigned char lookup[256];
	for (int i = 0; i < 256; ++i) {
		lookup[i] = 0xFF;
	}
	for (unsigned char i = 0; i < 64; ++i) {
		lookup[static_cast<unsigned char> (alphabet[i])] = i;
	}

	const auto ascii = text.ToCStr ();
	const char* in = ascii.Get ();
	const USize length = static_cast<USize> (strlen (in));
	into.SetCapacity (length / 4 * 3 + 3);

	UInt32 buffer = 0;
	int bits = 0;
	for (USize i = 0; i < length; ++i) {
		const unsigned char c = static_cast<unsigned char> (in[i]);
		if (c == '=' || c == '\n' || c == '\r' || c == ' ') {
			continue;
		}
		const unsigned char value = lookup[c];
		if (value == 0xFF) {
			return false;
		}
		buffer = (buffer << 6) | value;
		bits += 6;
		if (bits >= 8) {
			bits -= 8;
			into.Push (static_cast<char> ((buffer >> bits) & 0xFF));
		}
	}
	return !into.IsEmpty ();
}


bool ReadFormat (const GS::UniString& name, API_PictureFormat& into)
{
	if (name == "jpeg" || name == "jpg")	{ into = APIPictForm_JPEG;	 return true; }
	if (name == "png")						{ into = APIPictForm_PNG;	 return true; }
	if (name == "tiff" || name == "tif")	{ into = APIPictForm_TIFF;	 return true; }
	if (name == "gif")						{ into = APIPictForm_GIF;	 return true; }
	if (name == "bmp")						{ into = APIPictForm_Bitmap; return true; }
	return false;
}

}		// namespace


GS::String PlaceFiguresCommand::GetName () const			{ return "PlaceFigures"; }

GS::Optional<GS::UniString> PlaceFiguresCommand::GetInputParametersSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"figures": {
				"type": "array",
				"items": {
					"type": "object",
					"properties": {
						"data": { "type": "string", "description": "The image file, base64." },
						"format": { "type": "string", "enum": [ "jpeg", "jpg", "png", "tiff", "tif", "gif", "bmp" ] },
						"box": {
							"type": "object",
							"description": "Where the picture goes before its turn, in model metres.",
							"properties": {
								"xMin": { "type": "number" }, "yMin": { "type": "number" },
								"xMax": { "type": "number" }, "yMax": { "type": "number" }
							},
							"required": [ "xMin", "yMin", "xMax", "yMax" ]
						},
						"angle": { "type": "number", "description": "Radians, anticlockwise, about the anchor." },
						"anchor": {
							"type": "string",
							"enum": [ "LeftTop", "LeftMiddle", "LeftBottom", "MiddleTop", "MiddleMiddle", "MiddleBottom", "RightTop", "RightMiddle", "RightBottom" ]
						},
						"layerIndex": { "type": "integer" },
						"floorIndex": { "type": "integer" },
						"name": { "type": "string" },
						"transparent": { "type": "boolean", "description": "Draw white pixels as clear." }
					},
					"required": [ "data", "format", "box" ]
				},
				"minItems": 1
			}
		},
		"required": [ "figures" ],
		"additionalProperties": false
	})");
}

GS::Optional<GS::UniString> PlaceFiguresCommand::GetResponseSchema () const
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

GS::ObjectState PlaceFiguresCommand::Execute (const GS::ObjectState& parameters,
											  GS::ProcessControl& /*processControl*/) const
{
	GS::Array<GS::ObjectState> wanted;
	if (!parameters.Get ("figures", wanted) || wanted.IsEmpty ()) {
		return Failed ("Nothing to place: 'figures' is empty.", APIERR_BADPARS);
	}

	GS::Array<API_Guid> made;
	GSErrCode failure = NoError;
	GS::UniString failureText;

	const GSErrCode err = ACAPI_CallUndoableCommand ("Place site analysis pictures", [&] () -> GSErrCode {
		for (const GS::ObjectState& one : wanted) {
			GS::UniString encoded;
			GS::Array<char> bytes;
			if (!one.Get ("data", encoded) || !DecodeBase64 (encoded, bytes)) {
				failure = APIERR_BADPARS;
				failureText = "A figure's 'data' is not base64.";
				return failure;
			}
			GS::UniString formatName;
			API_PictureFormat format = APIPictForm_JPEG;
			if (!one.Get ("format", formatName) || !ReadFormat (formatName, format)) {
				failure = APIERR_BADPARS;
				failureText = "format takes jpeg, png, tiff, gif or bmp.";
				return failure;
			}

			API_Element element = {};
			API_ElementMemo memo = {};
			element.header.type = API_PictureID;
			GSErrCode step = ACAPI_Element_GetDefaults (&element, nullptr);
			if (step != NoError) {
				failure = step;
				failureText = "Could not read the Figure tool's defaults.";
				return step;
			}

			API_PictureType& picture = element.picture;
			GS::ObjectState box;
			if (!one.Get ("box", box)
				|| !box.Get ("xMin", picture.destBox.xMin) || !box.Get ("yMin", picture.destBox.yMin)
				|| !box.Get ("xMax", picture.destBox.xMax) || !box.Get ("yMax", picture.destBox.yMax)) {
				failure = APIERR_BADPARS;
				failureText = "Every figure needs a box with xMin, yMin, xMax and yMax.";
				return failure;
			}
			// The box is the size on the drawing, not the pixel count: a
			// tile of the ground has a size in metres before it has one in
			// pixels.
			picture.usePixelSize = false;
			picture.mirrored = false;
			picture.rotAngle = 0.0;
			one.Get ("angle", picture.rotAngle);
			picture.anchorPoint = APIAnc_LB;
			GS::UniString word;
			if (one.Get ("anchor", word) && !ReadAnchor (word, picture.anchorPoint)) {
				failure = APIERR_BADPARS;
				failureText = "anchor takes LeftTop ... RightBottom.";
				return failure;
			}
			picture.storageFormat = format;
			bool transparent = false;
			one.Get ("transparent", transparent);
			picture.transparent = transparent;
			ReadInt (one, "layerIndex", element.header.layer);
			ReadShort (one, "floorIndex", element.header.floorInd);
			GS::UniString name;
			if (one.Get ("name", name) && !name.IsEmpty ()) {
				CopyName (name, picture.pictName);
			}

			memo.pictHdl = BMAllocateHandle (bytes.GetSize (), ALLOCATE_CLEAR, 0);
			if (memo.pictHdl == nullptr) {
				failure = APIERR_MEMFULL;
				failureText = "Ran out of memory holding a picture.";
				return failure;
			}
			memcpy (*memo.pictHdl, bytes.GetContent (), bytes.GetSize ());

			step = ACAPI_Element_Create (&element, &memo);
			ACAPI_DisposeElemMemoHdls (&memo);
			if (step != NoError) {
				failure = step;
				failureText = "Archicad refused to place a picture.";
				return step;
			}
			made.Push (element.header.guid);
		}
		return NoError;
	});

	if (err != NoError) {
		return Failed (failureText.IsEmpty () ? GS::UniString ("Failed to place the pictures.") : failureText,
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


// -- CreateMesh -------------------------------------------------------------------

namespace Loriini {

namespace {

// `[{x,y,z}, ...]`, at least three of them.
bool ReadOutline (const GS::ObjectState& mesh, GS::Array<API_Coord3D>& into, GS::UniString& why)
{
	GS::Array<GS::ObjectState> points;
	if (!mesh.Get ("outline", points) || points.GetSize () < 3) {
		why = "A mesh needs an 'outline' of at least three points.";
		return false;
	}
	for (const GS::ObjectState& point : points) {
		API_Coord3D c = {};
		if (!point.Get ("x", c.x) || !point.Get ("y", c.y) || !point.Get ("z", c.z)) {
			why = "Each outline point needs an x, a y and a z.";
			return false;
		}
		into.Push (c);
	}
	return true;
}

}		// namespace


GS::String CreateMeshCommand::GetName () const			{ return "CreateMesh"; }

GS::Optional<GS::UniString> CreateMeshCommand::GetInputParametersSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"outline": {
				"type": "array",
				"description": "The mesh's edge, each point with its own level. Do not repeat the first point.",
				"items": {
					"type": "object",
					"properties": { "x": { "type": "number" }, "y": { "type": "number" }, "z": { "type": "number" } },
					"required": [ "x", "y", "z" ]
				},
				"minItems": 3
			},
			"levelLines": {
				"type": "array",
				"description": "Contours inside the outline: each a run of points at their level.",
				"items": {
					"type": "array",
					"items": {
						"type": "object",
						"properties": { "x": { "type": "number" }, "y": { "type": "number" }, "z": { "type": "number" } },
						"required": [ "x", "y", "z" ]
					},
					"minItems": 2
				}
			},
			"holes": {
				"type": "array",
				"description": "Openings in the mesh, each a ring of points with levels, inside the outline and clear of each other.",
				"items": {
					"type": "array",
					"items": {
						"type": "object",
						"properties": { "x": { "type": "number" }, "y": { "type": "number" }, "z": { "type": "number" } },
						"required": [ "x", "y", "z" ]
					},
					"minItems": 3
				}
			},
			"skirt": { "type": "string", "enum": [ "solid", "skirt", "surface" ], "description": "A solid body down to skirtLevel, a surface with a skirt, or the surface alone." },
			"skirtLevel": { "type": "number" },
			"layerIndex": { "type": "integer" },
			"floorIndex": { "type": "integer" },
			"elementId": { "type": "string" }
		},
		"required": [ "outline" ],
		"additionalProperties": false
	})");
}

GS::Optional<GS::UniString> CreateMeshCommand::GetResponseSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"success": { "type": "boolean" },
			"guid": { "type": "string" },
			"error": { "type": "object" }
		}
	})");
}

GS::ObjectState CreateMeshCommand::Execute (const GS::ObjectState& parameters,
											GS::ProcessControl& /*processControl*/) const
{
	GS::UniString why;
	GS::Array<API_Coord3D> outline;
	if (!ReadOutline (parameters, outline, why)) {
		return Failed (why, APIERR_BADPARS);
	}
	GS::Array<GS::Array<API_Coord3D>> holes;
	{
		GS::Array<GS::Array<GS::ObjectState>> rings;
		parameters.Get ("holes", rings);
		for (const GS::Array<GS::ObjectState>& ring : rings) {
			GS::Array<API_Coord3D> hole;
			for (const GS::ObjectState& point : ring) {
				API_Coord3D c = {};
				if (!point.Get ("x", c.x) || !point.Get ("y", c.y) || !point.Get ("z", c.z)) {
					return Failed ("Each hole point needs an x, a y and a z.", APIERR_BADPARS);
				}
				hole.Push (c);
			}
			if (hole.GetSize () < 3) {
				return Failed ("A hole needs at least three points.", APIERR_BADPARS);
			}
			holes.Push (hole);
		}
	}
	API_Element element = {};
	element.header.type = API_MeshID;
	GSErrCode err = ACAPI_Element_GetDefaults (&element, nullptr);
	if (err != NoError) {
		return Failed ("Could not read the Mesh tool's defaults.", err);
	}
	ReadInt (parameters, "layerIndex", element.header.layer);
	ReadShort (parameters, "floorIndex", element.header.floorInd);

	GS::UniString skirt;
	if (parameters.Get ("skirt", skirt)) {
		element.mesh.skirt = (skirt == "solid") ? 1 : (skirt == "skirt") ? 2 : 3;
	}
	parameters.Get ("skirtLevel", element.mesh.skirtLevel);
	element.mesh.level = 0.0;

	// The polygon: one-based, every contour's first point repeated last, with
	// a level per vertex beside it -- the kit's own Element_Test lays it out
	// this way. The outline first, then each hole as a sub-contour of its own.
	GS::Array<GS::Array<API_Coord3D>> rings;
	rings.Push (outline);
	for (const GS::Array<API_Coord3D>& hole : holes) {
		rings.Push (hole);
	}
	Int32 total = 0;
	for (const GS::Array<API_Coord3D>& ring : rings) {
		total += static_cast<Int32> (ring.GetSize ()) + 1;
	}
	element.mesh.poly.nCoords = total;
	element.mesh.poly.nSubPolys = static_cast<Int32> (rings.GetSize ());
	element.mesh.poly.nArcs = 0;

	API_ElementMemo memo = {};
	memo.coords = reinterpret_cast<API_Coord**> (BMAllocateHandle ((total + 1) * sizeof (API_Coord), ALLOCATE_CLEAR, 0));
	memo.pends = reinterpret_cast<Int32**> (BMAllocateHandle ((rings.GetSize () + 1) * sizeof (Int32), ALLOCATE_CLEAR, 0));
	memo.meshPolyZ = reinterpret_cast<double**> (BMAllocateHandle ((total + 1) * sizeof (double), ALLOCATE_CLEAR, 0));
	if (memo.coords == nullptr || memo.pends == nullptr || memo.meshPolyZ == nullptr) {
		ACAPI_DisposeElemMemoHdls (&memo);
		return Failed ("Ran out of memory laying out the mesh.", APIERR_MEMFULL);
	}
	{
		Int32 at = 0;
		Int32 which = 0;
		for (const GS::Array<API_Coord3D>& ring : rings) {
			const Int32 first = at + 1;
			for (const API_Coord3D& c : ring) {
				++at;
				(*memo.coords)[at].x = c.x;
				(*memo.coords)[at].y = c.y;
				(*memo.meshPolyZ)[at] = c.z;
			}
			++at;
			(*memo.coords)[at] = (*memo.coords)[first];
			(*memo.meshPolyZ)[at] = (*memo.meshPolyZ)[first];
			(*memo.pends)[++which] = at;
		}
	}

	// The level lines: zero-based, each run ending where `meshLevelEnds` says,
	// every vertex with an ID of its own.
	Int32 total = 0;
	GS::Array<GS::Array<API_Coord3D>> runs;
	GS::Array<GS::Array<GS::ObjectState>> lines;
	parameters.Get ("levelLines", lines);
	for (const GS::Array<GS::ObjectState>& line : lines) {
		GS::Array<API_Coord3D> run;
		for (const GS::ObjectState& point : line) {
			API_Coord3D c = {};
			if (point.Get ("x", c.x) && point.Get ("y", c.y) && point.Get ("z", c.z)) {
				run.Push (c);
			}
		}
		if (run.GetSize () >= 2) {
			total += static_cast<Int32> (run.GetSize ());
			runs.Push (run);
		}
	}
	if (total > 0) {
		element.mesh.levelLines.nCoords = total;
		element.mesh.levelLines.nSubLines = static_cast<Int32> (runs.GetSize ());
		memo.meshLevelCoords = reinterpret_cast<API_MeshLevelCoord**> (BMAllocateHandle (total * sizeof (API_MeshLevelCoord), ALLOCATE_CLEAR, 0));
		memo.meshLevelEnds = reinterpret_cast<Int32**> (BMAllocateHandle (runs.GetSize () * sizeof (Int32), ALLOCATE_CLEAR, 0));
		if (memo.meshLevelCoords == nullptr || memo.meshLevelEnds == nullptr) {
			ACAPI_DisposeElemMemoHdls (&memo);
			return Failed ("Ran out of memory laying out the level lines.", APIERR_MEMFULL);
		}
		Int32 at = 0;
		Int32 which = 0;
		for (const GS::Array<API_Coord3D>& run : runs) {
			for (const API_Coord3D& c : run) {
				(*memo.meshLevelCoords)[at].c = c;
				(*memo.meshLevelCoords)[at].vertexID = at + 1;
				++at;
			}
			(*memo.meshLevelEnds)[which++] = at;
		}
	}

	GSErrCode failure = NoError;
	GS::UniString failureText;
	err = ACAPI_CallUndoableCommand ("Create terrain mesh", [&] () -> GSErrCode {
		GSErrCode step = ACAPI_Element_Create (&element, &memo);
		if (step != NoError) {
			failure = step;
			failureText = "Archicad refused to create the mesh.";
			return step;
		}
		GS::UniString identifier;
		if (parameters.Get ("elementId", identifier) && !identifier.IsEmpty ()) {
			step = ACAPI_Database (APIDb_ChangeElementInfoStringID, &element.header.guid, &identifier);
			if (step != NoError) {
				failure = step;
				failureText = "The mesh was created but would not take its element ID.";
				return step;
			}
		}
		return NoError;
	});
	ACAPI_DisposeElemMemoHdls (&memo);
	if (err != NoError) {
		return Failed (failureText.IsEmpty () ? GS::UniString ("Failed to create the mesh.") : failureText,
					   failure != NoError ? failure : err);
	}

	GS::ObjectState result = Succeeded ();
	result.Add ("guid", APIGuidToString (element.header.guid));
	return result;
}


// -- CreateSlabs ----------------------------------------------------------------

namespace {

// The slab's polygon, laid out the way `BuildPolygon` lays out a fill's:
// one-based, each contour closed by repeating its first point.
GSErrCode BuildSlabPolygon (const GS::Array<Contour>& contours, API_SlabType& slab, API_ElementMemo& memo)
{
	Int32 total = 0;
	for (const Contour& contour : contours) {
		total += static_cast<Int32> (contour.GetSize ()) + 1;
	}
	slab.poly.nCoords = total;
	slab.poly.nSubPolys = static_cast<Int32> (contours.GetSize ());
	slab.poly.nArcs = 0;

	memo.coords = reinterpret_cast<API_Coord**> (BMAllocateHandle ((total + 1) * sizeof (API_Coord), ALLOCATE_CLEAR, 0));
	memo.pends = reinterpret_cast<Int32**> (BMAllocateHandle ((slab.poly.nSubPolys + 1) * sizeof (Int32), ALLOCATE_CLEAR, 0));
	if (memo.coords == nullptr || memo.pends == nullptr) {
		return APIERR_MEMFULL;
	}
	Int32 at = 0;
	Int32 which = 0;
	for (const Contour& contour : contours) {
		for (const API_Coord& point : contour) {
			(*memo.coords)[++at] = point;
		}
		(*memo.coords)[++at] = contour[0];
		(*memo.pends)[++which] = at;
	}
	return NoError;
}

}		// namespace


GS::String CreateSlabsCommand::GetName () const			{ return "CreateSlabs"; }

GS::Optional<GS::UniString> CreateSlabsCommand::GetInputParametersSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"slabs": {
				"type": "array",
				"items": {
					"type": "object",
					"properties": {
						"contours": {
							"type": "array",
							"description": "The outline, outer contour first, every contour after it a hole. Do not repeat the first point at the end.",
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
						"level": { "type": "number", "description": "Metres above the home storey of the reference plane." },
						"thickness": { "type": "number" },
						"referencePlane": { "type": "string", "enum": [ "bottom", "top" ], "description": "Which face the level names. Bottom by default: the slab stands up from the ground it is put on." },
						"layerIndex": { "type": "integer" },
						"floorIndex": { "type": "integer" },
						"elementId": { "type": "string" }
					},
					"required": [ "contours" ]
				},
				"minItems": 1
			}
		},
		"required": [ "slabs" ],
		"additionalProperties": false
	})");
}

GS::Optional<GS::UniString> CreateSlabsCommand::GetResponseSchema () const
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

GS::ObjectState CreateSlabsCommand::Execute (const GS::ObjectState& parameters,
											 GS::ProcessControl& /*processControl*/) const
{
	GS::Array<GS::ObjectState> wanted;
	if (!parameters.Get ("slabs", wanted) || wanted.IsEmpty ()) {
		return Failed ("Nothing to make: 'slabs' is empty.", APIERR_BADPARS);
	}

	GS::Array<API_Guid> made;
	GS::UniString why;
	GSErrCode failure = NoError;
	GS::UniString failureText;

	const GSErrCode err = ACAPI_CallUndoableCommand ("Model the neighbours", [&] () -> GSErrCode {
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
			element.header.type = API_SlabID;
			GSErrCode step = ACAPI_Element_GetDefaults (&element, &memo);
			if (step != NoError) {
				failure = step;
				failureText = "Could not read the Slab tool's defaults.";
				return step;
			}
			ACAPI_DisposeElemMemoHdls (&memo);
			memo = {};

			ReadInt (one, "layerIndex", element.header.layer);
			ReadShort (one, "floorIndex", element.header.floorInd);
			double level = 0.0;
			if (one.Get ("level", level)) {
				element.slab.level = level;
			}
			double thickness = 0.0;
			if (one.Get ("thickness", thickness) && thickness > 0.0) {
				element.slab.thickness = thickness;
			}
			GS::UniString plane;
			element.slab.referencePlaneLocation = APISlabRefPlane_Bottom;
			if (one.Get ("referencePlane", plane) && plane == "top") {
				element.slab.referencePlaneLocation = APISlabRefPlane_Top;
			}
			// The slab's own offset from its reference plane, which the tool's
			// defaults may carry, would move it off the ground it was put on.
			element.slab.offsetFromTop = 0.0;

			step = BuildSlabPolygon (contours, element.slab, memo);
			if (step != NoError) {
				ACAPI_DisposeElemMemoHdls (&memo);
				failure = step;
				failureText = "Ran out of memory laying out a slab's outline.";
				return step;
			}

			step = ACAPI_Element_Create (&element, &memo);
			ACAPI_DisposeElemMemoHdls (&memo);
			if (step != NoError) {
				failure = step;
				failureText = "Archicad refused to create a slab.";
				return step;
			}

			GS::UniString identifier;
			if (one.Get ("elementId", identifier) && !identifier.IsEmpty ()) {
				step = ACAPI_Database (APIDb_ChangeElementInfoStringID, &element.header.guid, &identifier);
				if (step != NoError) {
					failure = step;
					failureText = "A slab was created but would not take its element ID.";
					return step;
				}
			}
			made.Push (element.header.guid);
		}
		return NoError;
	});

	if (err != NoError) {
		return Failed (failureText.IsEmpty () ? GS::UniString ("Failed to create the slabs.") : failureText,
					   failure != NoError ? failure : err);
	}

	GS::Array<GS::ObjectState> elements;
	for (const API_Guid& guid : made) {
		GS::ObjectState entry;
		entry.Add ("guid", APIGuidToString (guid));
		elements.Push (entry);
	}

	GS::ObjectState result = Succeeded ();
	result.Add ("elements", elements);
	return result;
}

}		// namespace Loriini
