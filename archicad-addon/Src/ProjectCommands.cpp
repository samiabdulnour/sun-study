#include "ProjectCommands.hpp"
#include "Support.hpp"

namespace Loriini {

namespace {

// Archicad's window type names, as the JSON side already spells them.
//
// Matched to Tapir's own vocabulary on purpose: the Python adapter already
// says "FloorPlan" and "Worksheet" in `ChangeWindow`, and a second spelling of
// the same idea is a second thing to get wrong.
bool ReadWindowType (const GS::UniString& name, API_WindowTypeID& into)
{
	if (name == "FloorPlan")		{ into = APIWind_FloorPlanID; return true; }
	if (name == "Worksheet")		{ into = APIWind_WorksheetID; return true; }
	if (name == "Detail")			{ into = APIWind_DetailID; return true; }
	if (name == "Layout")			{ into = APIWind_LayoutID; return true; }
	if (name == "Section")			{ into = APIWind_SectionID; return true; }
	if (name == "Elevation")		{ into = APIWind_ElevationID; return true; }
	if (name == "DocumentFrom3D")	{ into = APIWind_DocumentFrom3DID; return true; }
	if (name == "3D")				{ into = APIWind_3DModelID; return true; }
	return false;
}


GS::UniString WindowTypeName (API_WindowTypeID type)
{
	switch (type) {
		case APIWind_FloorPlanID:		return "FloorPlan";
		case APIWind_WorksheetID:		return "Worksheet";
		case APIWind_DetailID:			return "Detail";
		case APIWind_LayoutID:			return "Layout";
		case APIWind_SectionID:			return "Section";
		case APIWind_ElevationID:		return "Elevation";
		case APIWind_DocumentFrom3DID:	return "DocumentFrom3D";
		case APIWind_3DModelID:			return "3D";
		default:						return "Other";
	}
}


GS::ObjectState DatabaseState (const API_DatabaseInfo& database)
{
	GS::ObjectState identifier;
	identifier.Add ("guid", APIGuidToString (database.databaseUnId.elemSetId));

	GS::ObjectState state;
	state.Add ("databaseId", identifier);
	state.Add ("windowType", WindowTypeName (database.typeID));
	state.Add ("name", GS::UniString (database.name));
	state.Add ("referenceId", GS::UniString (database.ref));
	return state;
}

}		// namespace


// -- GetCurrentDatabase -----------------------------------------------------

GS::String GetCurrentDatabaseCommand::GetName () const		{ return "GetCurrentDatabase"; }

GS::Optional<GS::UniString> GetCurrentDatabaseCommand::GetInputParametersSchema () const
{
	return GS::NoValue;
}

GS::Optional<GS::UniString> GetCurrentDatabaseCommand::GetResponseSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"success": { "type": "boolean" },
			"databaseId": { "type": "object", "properties": { "guid": { "type": "string" } } },
			"windowType": { "type": "string" },
			"name": { "type": "string" },
			"referenceId": { "type": "string" },
			"error": { "type": "object" }
		}
	})");
}

GS::ObjectState GetCurrentDatabaseCommand::Execute (const GS::ObjectState& /*parameters*/,
													GS::ProcessControl& /*processControl*/) const
{
	API_DatabaseInfo database = {};
	const GSErrCode err = ACAPI_Database (APIDb_GetCurrentDatabaseID, &database);
	if (err != NoError) {
		return Failed ("Failed to read the current database.", err);
	}

	GS::ObjectState result = DatabaseState (database);
	result.Add ("success", true);
	return result;
}


// -- SetCurrentDatabase -----------------------------------------------------

GS::String SetCurrentDatabaseCommand::GetName () const		{ return "SetCurrentDatabase"; }

GS::Optional<GS::UniString> SetCurrentDatabaseCommand::GetInputParametersSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"databaseId": {
				"type": "object",
				"properties": { "guid": { "type": "string" } },
				"required": [ "guid" ]
			},
			"windowType": {
				"type": "string",
				"description": "Needed alongside the id: a database is identified by its type and its unique id together."
			},
			"floorIndex": {
				"type": "integer",
				"description": "For the floor plan, which storey to stand on."
			}
		},
		"additionalProperties": false
	})");
}

GS::Optional<GS::UniString> SetCurrentDatabaseCommand::GetResponseSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"success": { "type": "boolean" },
			"databaseId": { "type": "object" },
			"windowType": { "type": "string" },
			"error": { "type": "object" }
		}
	})");
}

GS::ObjectState SetCurrentDatabaseCommand::Execute (const GS::ObjectState& parameters,
													GS::ProcessControl& /*processControl*/) const
{
	// Started from where we are, so a caller naming only a floor index moves
	// storey without having to restate which database it is standing in.
	API_DatabaseInfo database = {};
	GSErrCode err = ACAPI_Database (APIDb_GetCurrentDatabaseID, &database);
	if (err != NoError) {
		return Failed ("Failed to read the current database before changing it.", err);
	}

	GS::ObjectState identifier;
	if (parameters.Get ("databaseId", identifier)) {
		GS::UniString guid;
		if (!identifier.Get ("guid", guid)) {
			return Failed ("databaseId needs a guid.", APIERR_BADPARS);
		}
		database.databaseUnId.elemSetId = APIGuidFromString (guid.ToCStr ().Get ());
	}

	GS::UniString windowType;
	if (parameters.Get ("windowType", windowType)) {
		if (!ReadWindowType (windowType, database.typeID)) {
			return Failed ("Unknown windowType " + windowType + ".", APIERR_BADPARS);
		}
	}

	Int32 floor = 0;
	if (parameters.Get ("floorIndex", floor)) {
		database.index = floor;
	}

	err = ACAPI_Database (APIDb_ChangeCurrentDatabaseID, &database);
	if (err != NoError) {
		return Failed ("Archicad refused to move to that database.", err);
	}

	// Read back rather than assumed. This command exists because Tapir's
	// `ChangeWindow` reports success and does not move (D40), so a version of
	// it that reports success without checking would be the same trap wearing
	// a different name.
	API_DatabaseInfo landed = {};
	err = ACAPI_Database (APIDb_GetCurrentDatabaseID, &landed);
	if (err != NoError) {
		return Failed ("Moved, but could not confirm where to.", err);
	}
	if (landed.databaseUnId != database.databaseUnId) {
		return Failed ("Archicad accepted the move and stayed where it was.", APIERR_GENERAL);
	}

	GS::ObjectState result = DatabaseState (landed);
	result.Add ("success", true);
	return result;
}


// -- CreateWorksheet --------------------------------------------------------

GS::String CreateWorksheetCommand::GetName () const			{ return "CreateWorksheet"; }

GS::Optional<GS::UniString> CreateWorksheetCommand::GetInputParametersSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"name": { "type": "string" },
			"referenceId": { "type": "string" },
			"makeCurrent": {
				"type": "boolean",
				"description": "Stand in the new worksheet straight away, so the same run can draw into it. Default true, which is the point of the command."
			}
		},
		"required": [ "name" ],
		"additionalProperties": false
	})");
}

GS::Optional<GS::UniString> CreateWorksheetCommand::GetResponseSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"success": { "type": "boolean" },
			"databaseId": { "type": "object", "properties": { "guid": { "type": "string" } } },
			"isCurrent": { "type": "boolean" },
			"error": { "type": "object" }
		}
	})");
}

GS::ObjectState CreateWorksheetCommand::Execute (const GS::ObjectState& parameters,
												 GS::ProcessControl& /*processControl*/) const
{
	GS::UniString name;
	if (!parameters.Get ("name", name) || name.IsEmpty ()) {
		return Failed ("A worksheet needs a name.", APIERR_BADPARS);
	}

	API_DatabaseInfo database = {};
	database.typeID = APIWind_WorksheetID;
	CopyName (name, database.name);

	GS::UniString referenceId;
	if (parameters.Get ("referenceId", referenceId) && !referenceId.IsEmpty ()) {
		CopyName (referenceId, database.ref);
	}

	GSErrCode err = ACAPI_CallUndoableCommand ("Create worksheet", [&] () -> GSErrCode {
		return ACAPI_Database (APIDb_NewDatabaseID, &database);
	});
	if (err != NoError) {
		return Failed ("Failed to create the worksheet.", err);
	}

	// Becoming current is the whole reason this exists rather than Tapir's
	// `CreateWorksheets`. A worksheet made and not entered cannot be drawn
	// into in the same session: -2130313110, before and after `RebuildView`.
	bool makeCurrent = true;
	parameters.Get ("makeCurrent", makeCurrent);

	bool isCurrent = false;
	if (makeCurrent) {
		err = ACAPI_Database (APIDb_ChangeCurrentDatabaseID, &database);
		if (err != NoError) {
			return Failed ("The worksheet was created but could not be entered, so nothing "
						   "can be drawn into it yet.", err);
		}
		isCurrent = true;
	}

	GS::ObjectState identifier;
	identifier.Add ("guid", APIGuidToString (database.databaseUnId.elemSetId));

	GS::ObjectState result = Succeeded ();
	result.Add ("databaseId", identifier);
	result.Add ("isCurrent", isCurrent);
	return result;
}


// -- ActivateLayerCombination -----------------------------------------------

GS::String ActivateLayerCombinationCommand::GetName () const		{ return "ActivateLayerCombination"; }

GS::Optional<GS::UniString> ActivateLayerCombinationCommand::GetInputParametersSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"name": { "type": "string", "description": "The layer combination's name, as Layer Settings shows it." }
		},
		"required": [ "name" ],
		"additionalProperties": false
	})");
}

GS::Optional<GS::UniString> ActivateLayerCombinationCommand::GetResponseSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"success": { "type": "boolean" },
			"index": { "type": "integer" },
			"error": { "type": "object" }
		}
	})");
}

GS::ObjectState ActivateLayerCombinationCommand::Execute (const GS::ObjectState& parameters,
														  GS::ProcessControl& /*processControl*/) const
{
	GS::UniString name;
	if (!parameters.Get ("name", name) || name.IsEmpty ()) {
		return Failed ("A layer combination needs a name.", APIERR_BADPARS);
	}

	API_Attr_Head head = {};
	head.typeID = API_LayerCombID;
	GS::UniString held;
	CopyAttributeName (name, head, held);

	GSErrCode err = ACAPI_Attribute_Search (&head);
	if (err != NoError) {
		return Failed ("No layer combination named '" + name + "' in this project.", err);
	}

	err = ACAPI_Environment (APIEnv_ChangeCurrLayerCombID, &head.index);
	if (err != NoError) {
		return Failed ("Found '" + name + "' but Archicad would not activate it.", err);
	}

	GS::ObjectState result = Succeeded ();
	result.Add ("index", static_cast<Int32> (head.index));
	return result;
}


// -- ModifyLayers -----------------------------------------------------------

GS::String ModifyLayersCommand::GetName () const		{ return "ModifyLayers"; }

GS::Optional<GS::UniString> ModifyLayersCommand::GetInputParametersSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"layers": {
				"type": "array",
				"items": {
					"type": "object",
					"properties": {
						"name": { "type": "string" },
						"isHidden": { "type": "boolean" },
						"isLocked": { "type": "boolean" },
						"isWireframe": { "type": "boolean" }
					},
					"required": [ "name" ]
				},
				"minItems": 1
			}
		},
		"required": [ "layers" ],
		"additionalProperties": false
	})");
}

GS::Optional<GS::UniString> ModifyLayersCommand::GetResponseSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"success": { "type": "boolean" },
			"changed": { "type": "integer" },
			"error": { "type": "object" }
		}
	})");
}

GS::ObjectState ModifyLayersCommand::Execute (const GS::ObjectState& parameters,
											  GS::ProcessControl& /*processControl*/) const
{
	GS::Array<GS::ObjectState> wanted;
	if (!parameters.Get ("layers", wanted) || wanted.IsEmpty ()) {
		return Failed ("Nothing to change: 'layers' is empty.", APIERR_BADPARS);
	}

	Int32 changed = 0;
	GS::UniString failureText;
	GSErrCode failure = NoError;

	const GSErrCode err = ACAPI_CallUndoableCommand ("Change layer visibility", [&] () -> GSErrCode {
		for (const GS::ObjectState& one : wanted) {
			GS::UniString name;
			if (!one.Get ("name", name) || name.IsEmpty ()) {
				failure = APIERR_BADPARS;
				failureText = "Every layer needs a name.";
				return APIERR_BADPARS;
			}

			API_Attribute layer = {};
			layer.header.typeID = API_LayerID;
			GS::UniString held;
			CopyAttributeName (name, layer.header, held);

			GSErrCode step = ACAPI_Attribute_Search (&layer.header);
			if (step != NoError) {
				failure = step;
				failureText = "No layer named '" + name + "' in this project.";
				return step;
			}
			step = ACAPI_Attribute_Get (&layer);
			if (step != NoError) {
				failure = step;
				failureText = "Found '" + name + "' but could not read it.";
				return step;
			}

			// Read, flip the named bits, write. Only what the caller asked
			// about moves: a layer's locked state is nobody's business when
			// the caller only spoke about visibility, and D59's workaround
			// rewrote all of it every time.
			bool flag = false;
			if (one.Get ("isHidden", flag)) {
				layer.header.flags = static_cast<short> (flag ? (layer.header.flags | APILay_Hidden)
														   : (layer.header.flags & ~APILay_Hidden));
			}
			if (one.Get ("isLocked", flag)) {
				layer.header.flags = static_cast<short> (flag ? (layer.header.flags | APILay_Locked)
														   : (layer.header.flags & ~APILay_Locked));
			}
			if (one.Get ("isWireframe", flag)) {
				layer.header.flags = static_cast<short> (flag ? (layer.header.flags | APILay_ForceToWire)
														   : (layer.header.flags & ~APILay_ForceToWire));
			}

			step = ACAPI_Attribute_Modify (&layer, nullptr);
			if (step != NoError) {
				failure = step;
				failureText = "Archicad refused to change layer '" + name + "'.";
				return step;
			}
			++changed;
		}
		return NoError;
	});

	if (err != NoError) {
		return Failed (failureText.IsEmpty () ? GS::UniString ("Failed to change the layers.") : failureText,
					   failure != NoError ? failure : err);
	}

	GS::ObjectState result = Succeeded ();
	result.Add ("changed", changed);
	return result;
}

}		// namespace Loriini
