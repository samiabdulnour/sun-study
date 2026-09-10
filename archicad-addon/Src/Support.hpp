// Shared by every command: one failure shape, and the small conversions.
//
// Pulled out of Commands.cpp when the second and third groups of commands
// arrived. There is nothing clever here, and that is the point -- an error
// reported three different ways across three files is three things the Python
// side has to know about.

#if !defined (LORIINI_SUPPORT_HPP)
#define LORIINI_SUPPORT_HPP

#pragma once

#include "APIEnvir.h"
#include "ACAPinc.h"

// APIGuidToString and APIGuidFromString live here, and ACAPinc.h does not
// reach them. Central, because three of the command files report a guid.
#include "API_Guid.hpp"

#include "ObjectState.hpp"
#include "UniString.hpp"

namespace Loriini {

// The namespace every command answers under. Reached from outside as
// `API.ExecuteAddOnCommand` with `commandNamespace` set to this, exactly as
// Tapir's own commands are reached, so the Python side needs no new transport.
GS::String CommandNamespace ();

// What every command in this add-on derives from.
//
// `API_AddOnCommand` is nine pure virtuals, and six of them have an answer
// that is the same for every command here. Answering those once means a new
// command is its name, its schema and its `Execute`, and nothing else -- and
// it removes the failure that produced this class, where three unimplemented
// virtuals made all nine command classes abstract and the compiler reported it
// nine times from inside a GSRoot header, naming neither the methods nor the
// file they were missing from.
class Command : public API_AddOnCommand {
public:
	virtual GS::String						GetNamespace () const override { return CommandNamespace (); }

	// No shared `$ref` definitions. Each command's schema is written out in
	// full where it is declared, because there are nine of them rather than a
	// hundred and a reader should not have to hold two files open.
	virtual GS::Optional<GS::UniString>		GetSchemaDefinitions () const override { return GS::NoValue; }
	virtual GS::Optional<GS::UniString>		GetInputParametersSchema () const override { return GS::NoValue; }
	virtual GS::Optional<GS::UniString>		GetResponseSchema () const override { return GS::NoValue; }

	// On the main thread unless a command says otherwise. Everything here
	// either reads or writes project state, and Archicad's element and
	// database calls are not safe anywhere else.
	virtual API_AddOnCommandExecutionPolicy	GetExecutionPolicy () const override
	{
		return API_AddOnCommandExecutionPolicy::ScheduleForExecutionOnMainThread;
	}

	virtual bool							IsProcessWindowVisible () const override { return false; }

	// Says so, rather than failing quietly.
	//
	// This fires when a command's own response does not match the schema it
	// published -- our bug, not the caller's, and one that otherwise reaches
	// the Python side as a command that simply returned nothing useful.
	virtual void							OnResponseValidationFailed (const GS::ObjectState& response) const override;
};


// One shape for every failure.
//
// Archicad's own number is carried through untranslated. It is a plain
// `GSErrCode`, the dev kit's error table is what names it, and a message
// invented here would be a worse version of a number that already means
// something exact.
GS::ObjectState Failed (const GS::UniString& what, GSErrCode code);

// The bare success, for commands with nothing to report.
GS::ObjectState Succeeded ();

// Copies into one of Archicad's fixed-width name buffers, without running off
// the end of it. Every `name` and `ref` field in the API is a
// `GS::uchar_t[API_UniLongNameLen]`.
void CopyName (const GS::UniString& from, GS::uchar_t* into);

// Reads a pen index, a layer index or any other optional integer, leaving the
// target alone when the caller did not name it.
//
// Partial updates are the rule throughout this add-on: a caller changing one
// field should not have to restate the other nine, and a field left out must
// keep what the project already had rather than becoming zero.
bool ReadInt (const GS::ObjectState& from, const char* name, Int32& into);

// The same, for a short -- pen indices, floor indices and the rest.
bool ReadShort (const GS::ObjectState& from, const char* name, short& into);

// Reads `{"red": 0.98, "green": 0.79, "blue": 0.20}`, in 0 to 1 as
// `API_RGBColor` wants it.
//
// Rejects a colour outside that range rather than clamping. Somebody passing
// 0 to 255 gets an error naming the range instead of a fill that is silently
// pure white, which is the same trap `API_RGBColor` set for the surface
// reflectances in D45.
bool ReadColour (const GS::ObjectState& from, const char* name, API_RGBColor& into, GS::UniString& why);

}		// namespace Loriini

#endif
