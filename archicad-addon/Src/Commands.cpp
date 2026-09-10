#include "Commands.hpp"
#include "Projection.hpp"
#include "Support.hpp"

namespace Loriini {

namespace {

// The twelve numbers of a transformation matrix, in the order the header
// gives them. Reported raw and unrearranged on purpose: the whole reason
// `GetProjection` exists is to compare Archicad's own matrix against the one
// `Projection.cpp` builds, and a tidied-up version would hide exactly the
// disagreement that comparison is looking for.
GS::ObjectState MatrixState (const API_Tranmat& matrix)
{
	GS::Array<double> numbers;
	for (short index = 0; index < 12; ++index) {
		numbers.Push (matrix.tmx[index]);
	}
	GS::ObjectState state;
	state.Add ("tmx", numbers);
	return state;
}


GS::ObjectState SunState (const API_SunAngleSettings& sun)
{
	GS::ObjectState state;
	state.Add ("azimuth", sun.sunAzimuth);
	state.Add ("altitude", sun.sunAltitude);
	state.Add ("givenByDate", sun.sunPosOpt == API_SunPosition_GivenByDate);
	state.Add ("year", static_cast<Int32> (sun.year));
	state.Add ("month", static_cast<Int32> (sun.month));
	state.Add ("day", static_cast<Int32> (sun.day));
	state.Add ("hour", static_cast<Int32> (sun.hour));
	state.Add ("minute", static_cast<Int32> (sun.minute));
	state.Add ("second", static_cast<Int32> (sun.second));
	state.Add ("summerTime", sun.summerTime);
	return state;
}


// Fills a sun setting from the caller's date, leaving anything unnamed as it
// already was.
//
// Partial on purpose. A caller changing only the hour across seven runs
// should not have to restate the year each time, and a caller who states
// nothing should get the sun the project already had rather than midnight on
// the first of January.
void ApplySun (const GS::ObjectState& date, API_SunAngleSettings& sun)
{
	Int32 value = 0;
	bool flag = false;

	if (date.Get ("year", value))		sun.year = static_cast<unsigned short> (value);
	if (date.Get ("month", value))		sun.month = static_cast<unsigned short> (value);
	if (date.Get ("day", value))		sun.day = static_cast<unsigned short> (value);
	if (date.Get ("hour", value))		sun.hour = static_cast<unsigned short> (value);
	if (date.Get ("minute", value))		sun.minute = static_cast<unsigned short> (value);
	if (date.Get ("second", value))		sun.second = static_cast<unsigned short> (value);
	if (date.Get ("summerTime", flag))	sun.summerTime = flag;

	// Given a date at all, the sun is computed from it. Stated explicitly
	// rather than assumed: a project whose sun was last set by dragging the
	// angle sliders is sitting on `API_SunPosition_GivenByAngles`, and
	// writing a date into it without switching the option changes the numbers
	// in the dialog and nothing in the drawing.
	sun.sunPosOpt = API_SunPosition_GivenByDate;
}


}		// namespace


// -- GetProjection ----------------------------------------------------------

GS::String GetProjectionCommand::GetName () const			{ return "GetProjection"; }

GS::Optional<GS::UniString> GetProjectionCommand::GetInputParametersSchema () const
{
	return GS::NoValue;
}

GS::Optional<GS::UniString> GetProjectionCommand::GetResponseSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"success": { "type": "boolean" },
			"isPerspective": { "type": "boolean" },
			"azimuth": { "type": "number", "description": "Archicad's own camera azimuth, in radians, exactly as reported." },
			"projectionMode": { "type": "integer", "description": "API_AxonoPars::projMod, the preset selected in the 3D Projection Settings dialog." },
			"transformation": { "type": "object", "properties": { "tmx": { "type": "array", "items": { "type": "number" } } } },
			"inverseTransformation": { "type": "object", "properties": { "tmx": { "type": "array", "items": { "type": "number" } } } },
			"sun": { "type": "object" },
			"error": { "type": "object" }
		}
	})");
}

GS::ObjectState GetProjectionCommand::Execute (const GS::ObjectState& /*parameters*/,
											   GS::ProcessControl& /*processControl*/) const
{
	API_3DProjectionInfo info = {};
	const GSErrCode err = ACAPI_Environment (APIEnv_Get3DProjectionSetsID, &info);
	if (err != NoError) {
		return Failed ("Failed to read the 3D projection settings.", err);
	}

	GS::ObjectState result;
	result.Add ("success", true);
	result.Add ("isPerspective", info.isPersp);

	if (info.isPersp) {
		// Reported, but not the case this add-on is for. A sun eye view is a
		// parallel projection: a perspective one puts the near corner of the
		// site at a different scale from the far one, and an assessor
		// measuring off the drawing would be measuring nothing.
		result.Add ("azimuth", info.u.persp.azimuth);
		result.Add ("sun", SunState (info.u.persp.sunAngSets));
	} else {
		result.Add ("azimuth", info.u.axono.azimuth);
		result.Add ("projectionMode", static_cast<Int32> (info.u.axono.projMod));
		result.Add ("transformation", MatrixState (info.u.axono.tranmat));
		result.Add ("inverseTransformation", MatrixState (info.u.axono.invtranmat));
		result.Add ("sun", SunState (info.u.axono.sunAngSets));
	}

	return result;
}


// -- SetProjection ----------------------------------------------------------

GS::String SetProjectionCommand::GetName () const			{ return "SetProjection"; }

GS::Optional<GS::UniString> SetProjectionCommand::GetInputParametersSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"viewAzimuth": {
				"type": "number",
				"description": "Where the camera stands, in degrees clockwise from north. For a sun eye view this is the sun's own bearing."
			},
			"viewAltitude": {
				"type": "number",
				"description": "How high the camera stands, in degrees above the horizon. For a sun eye view this is the sun's own altitude."
			},
			"sun": {
				"type": "object",
				"description": "The date and time Archicad computes its own sun from. Any field left out keeps the value the project already had.",
				"properties": {
					"year": { "type": "integer" },
					"month": { "type": "integer" },
					"day": { "type": "integer" },
					"hour": { "type": "integer" },
					"minute": { "type": "integer" },
					"second": { "type": "integer" },
					"summerTime": { "type": "boolean" }
				},
				"additionalProperties": false
			}
		},
		"required": [ "viewAzimuth", "viewAltitude" ],
		"additionalProperties": false
	})");
}

GS::Optional<GS::UniString> SetProjectionCommand::GetResponseSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"success": { "type": "boolean" },
			"transformation": { "type": "object" },
			"error": { "type": "object" }
		}
	})");
}

GS::ObjectState SetProjectionCommand::Execute (const GS::ObjectState& parameters,
											   GS::ProcessControl& /*processControl*/) const
{
	double azimuth = 0.0;
	double altitude = 0.0;
	if (!parameters.Get ("viewAzimuth", azimuth) || !parameters.Get ("viewAltitude", altitude)) {
		return Failed ("viewAzimuth and viewAltitude are both required, in degrees.", APIERR_BADPARS);
	}

	// Read first, then change. The struct carries a great deal this command
	// has no opinion about -- the camera set it belongs to, the projection
	// preset, the sun option -- and building one from zero would silently
	// reset every one of them.
	API_3DProjectionInfo info = {};
	GSErrCode err = ACAPI_Environment (APIEnv_Get3DProjectionSetsID, &info);
	if (err != NoError) {
		return Failed ("Failed to read the 3D projection settings before changing them.", err);
	}

	const bool wasPerspective = info.isPersp;
	info.isPersp = false;

	// Coming from a perspective view there is no axonometric half to inherit,
	// so the union holds perspective numbers reinterpreted as parallel ones.
	// Cleared rather than trusted.
	if (wasPerspective) {
		const API_SunAngleSettings sun = info.u.persp.sunAngSets;
		info.u.axono = {};
		info.u.axono.sunAngSets = sun;
	}

	const Direction from = { azimuth, altitude };
	info.u.axono.tranmat = ViewMatrix (from);
	info.u.axono.invtranmat = InverseViewMatrix (from);

	GS::ObjectState date;
	if (parameters.Get ("sun", date)) {
		ApplySun (date, info.u.axono.sunAngSets);
	}

	// `par2` switches only the axonometric half rather than flipping the
	// window between parallel and perspective, which is what the header's
	// "switch only axono or persp" note means.
	bool axonometricOnly = true;
	err = ACAPI_Environment (APIEnv_Change3DProjectionSetsID, &info, &axonometricOnly);
	if (err != NoError) {
		return Failed ("Failed to change the 3D projection settings.", err);
	}

	GS::ObjectState result = Succeeded ();
	result.Add ("transformation", MatrixState (info.u.axono.tranmat));
	return result;
}


// -- CreateDocumentFrom3D ---------------------------------------------------

GS::String CreateDocumentFrom3DCommand::GetName () const		{ return "CreateDocumentFrom3D"; }

GS::Optional<GS::UniString> CreateDocumentFrom3DCommand::GetInputParametersSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"name": { "type": "string", "description": "The 3D Document's name in the Project Map." },
			"referenceId": { "type": "string", "description": "Its reference string, the sheet-style ID shown beside the name." },
			"viewAzimuth": { "type": "number", "description": "Where the camera stands, in degrees clockwise from north." },
			"viewAltitude": { "type": "number", "description": "How high the camera stands, in degrees above the horizon." },
			"sun": { "type": "object", "description": "The date and time this document keeps its sun at." }
		},
		"required": [ "name" ],
		"additionalProperties": false
	})");
}

GS::Optional<GS::UniString> CreateDocumentFrom3DCommand::GetResponseSchema () const
{
	return GS::UniString (R"({
		"type": "object",
		"properties": {
			"success": { "type": "boolean" },
			"databaseId": { "type": "object", "properties": { "guid": { "type": "string" } } },
			"error": { "type": "object" }
		}
	})");
}

GS::ObjectState CreateDocumentFrom3DCommand::Execute (const GS::ObjectState& parameters,
													  GS::ProcessControl& /*processControl*/) const
{
	GS::UniString name;
	if (!parameters.Get ("name", name) || name.IsEmpty ()) {
		return Failed ("A 3D Document needs a name.", APIERR_BADPARS);
	}

	GS::UniString referenceId;
	parameters.Get ("referenceId", referenceId);

	API_DatabaseInfo database = {};
	database.typeID = APIWind_DocumentFrom3DID;
	CopyName (name, database.name);
	if (!referenceId.IsEmpty ()) {
		CopyName (referenceId, database.ref);
	}

	// Undoable, because it is a change to the project a person must be able
	// to take back. A run that makes seven of these and is then abandoned
	// should not leave seven documents behind that can only be deleted by
	// hand, one at a time, in the Navigator.
	GSErrCode err = ACAPI_CallUndoableCommand ("Create 3D Document", [&] () -> GSErrCode {
		return ACAPI_Database (APIDb_NewDatabaseID, &database);
	});
	if (err != NoError) {
		return Failed ("Failed to create the 3D Document.", err);
	}

	// The projection is set afterwards, on the document that now exists. It
	// is the half that matters: a 3D Document created and left alone borrows
	// whatever the 3D window happened to show, so seven of them made in a row
	// would all be the same hour.
	double azimuth = 0.0;
	double altitude = 0.0;
	GS::ObjectState date;

	const bool hasDirection = parameters.Get ("viewAzimuth", azimuth) &&
							  parameters.Get ("viewAltitude", altitude);
	const bool hasSun = parameters.Get ("sun", date);

	if (hasDirection || hasSun) {
		API_DocumentFrom3DType settings = {};
		err = ACAPI_Environment (APIEnv_GetDocumentFrom3DSettingsID, &database.databaseUnId, &settings);
		if (err != NoError) {
			return Failed ("The 3D Document was created but its settings could not be read.", err);
		}

		if (hasDirection) {
			const Direction from = { azimuth, altitude };
			settings.projectionSetting.isPersp = false;
			settings.projectionSetting.u.axono.tranmat = ViewMatrix (from);
			settings.projectionSetting.u.axono.invtranmat = InverseViewMatrix (from);
		}
		if (hasSun) {
			ApplySun (date, settings.projectionSetting.u.axono.sunAngSets);
		}
		err = ACAPI_CallUndoableCommand ("Set 3D Document projection", [&] () -> GSErrCode {
			return ACAPI_Environment (APIEnv_ChangeDocumentFrom3DSettingsID, &database.databaseUnId, &settings);
		});
		if (err != NoError) {
			return Failed ("The 3D Document was created but its projection could not be set.", err);
		}
	}

	GS::ObjectState identifier;
	identifier.Add ("guid", APIGuidToString (database.databaseUnId.elemSetId));

	GS::ObjectState result = Succeeded ();
	result.Add ("databaseId", identifier);
	return result;
}

}		// namespace Loriini
