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

}		// namespace


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
				"description": "Each Drawing element and where its centre goes, in metres on the layout.",
				"items": {
					"type": "object",
					"properties": {
						"guid": { "type": "string" },
						"x": { "type": "number" },
						"y": { "type": "number" }
					},
					"required": [ "guid", "x", "y" ],
					"additionalProperties": false
				}
			},
			"fitFrame": { "type": "boolean", "description": "Drop the clip polygon so the frame follows the drawing's own extent. Default true." },
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

	bool fitFrame = true;
	bool autoUpdate = true;
	parameters.Get ("fitFrame", fitFrame);
	parameters.Get ("autoUpdate", autoUpdate);

	GS::Optional<VisitDatabase> visit;
	GS::ObjectState layout;
	if (parameters.Get ("layoutDatabaseId", layout)) {
		GS::UniString guid;
		if (!layout.Get ("guid", guid)) {
			return Failed ("layoutDatabaseId needs a guid.", APIERR_BADPARS);
		}
		visit.New (APIGuidFromString (guid.ToCStr ().Get ()));
		if (!visit->moved) {
			return Failed ("Archicad refused to make that layout current.", APIERR_BADDATABASE);
		}
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

			API_Element element = {};
			element.header.guid = APIGuidFromString (guid.ToCStr ().Get ());
			GSErrCode one = ACAPI_Element_Get (&element);
			if (one == NoError && element.header.type != API_DrawingID) {
				one = APIERR_BADELEMENTTYPE;
			}
			if (one == NoError) {
				API_Element mask;
				ACAPI_ELEMENT_MASK_CLEAR (mask);

				// The centre, so a position computed as the middle of a cell
				// lands the drawing in the middle of the cell whatever size
				// it turns out to be once regenerated. Bottom-left is what
				// `CreateDrawings` leaves, and it puts a drawing that grows
				// up and to the right of wherever it was meant to sit.
				element.drawing.anchorPoint = APIAnc_MM;
				element.drawing.useOwnOrigoAsAnchor = false;
				element.drawing.pos.x = x;
				element.drawing.pos.y = y;
				ACAPI_ELEMENT_MASK_SET (mask, API_DrawingType, anchorPoint);
				ACAPI_ELEMENT_MASK_SET (mask, API_DrawingType, useOwnOrigoAsAnchor);
				ACAPI_ELEMENT_MASK_SET (mask, API_DrawingType, pos);

				if (fitFrame) {
					element.drawing.isCutWithFrame = false;
					ACAPI_ELEMENT_MASK_SET (mask, API_DrawingType, isCutWithFrame);
				}
				if (autoUpdate) {
					element.drawing.manualUpdate = false;
					ACAPI_ELEMENT_MASK_SET (mask, API_DrawingType, manualUpdate);
				}
				one = ACAPI_Element_Change (&element, &mask, nullptr, 0, true);
			}

			if (one == NoError) {
				++arranged;
			} else {
				GS::ObjectState failure;
				failure.Add ("guid", guid);
				failure.Add ("code", static_cast<Int32> (one));
				failures.Push (failure);
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
