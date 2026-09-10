#include "Support.hpp"

#include <cstring>

namespace Loriini {

GS::String CommandNamespace ()
{
	return "Loriini";
}


void Command::OnResponseValidationFailed (const GS::ObjectState& /*response*/) const
{
	// Written to the report window because there is nowhere else for it to go:
	// the response has already been handed back by the time this is called, so
	// it cannot be turned into an error the caller sees.
	ACAPI_WriteReport ("Loriini: the command '" + GS::UniString (GetName ().ToCStr ()) +
					   "' returned something its own schema does not allow. This is a bug "
					   "in the add-on rather than in the request.", false);
}


GS::ObjectState Failed (const GS::UniString& what, GSErrCode code)
{
	GS::ObjectState error;
	error.Add ("message", what);
	error.Add ("code", static_cast<Int32> (code));

	GS::ObjectState result;
	result.Add ("success", false);
	result.Add ("error", error);
	return result;
}


GS::ObjectState Succeeded ()
{
	GS::ObjectState result;
	result.Add ("success", true);
	return result;
}


void CopyName (const GS::UniString& from, GS::uchar_t* into)
{
	GS::ucsncpy (into, from.ToUStr ().Get (), API_UniLongNameLen - 1);
}


void CopyAttributeName (const GS::UniString& from, API_Attr_Head& into, GS::UniString& held)
{
	const GS::UniString::CStrPtr narrow = from.ToCStr ();
	std::strncpy (into.name, narrow.Get (), API_AttrNameLen - 1);
	into.name[API_AttrNameLen - 1] = 0;

	held = from;
	into.uniStringNamePtr = &held;
}


bool ReadInt (const GS::ObjectState& from, const char* name, Int32& into)
{
	Int32 value = 0;
	if (!from.Get (name, value)) {
		return false;
	}
	into = value;
	return true;
}


bool ReadShort (const GS::ObjectState& from, const char* name, short& into)
{
	Int32 value = 0;
	if (!from.Get (name, value)) {
		return false;
	}
	into = static_cast<short> (value);
	return true;
}


bool ReadColour (const GS::ObjectState& from, const char* name, API_RGBColor& into, GS::UniString& why)
{
	GS::ObjectState colour;
	if (!from.Get (name, colour)) {
		return false;
	}

	double red = 0.0, green = 0.0, blue = 0.0;
	if (!colour.Get ("red", red) || !colour.Get ("green", green) || !colour.Get ("blue", blue)) {
		why = GS::UniString (name) + " needs all three of red, green and blue.";
		return false;
	}

	// Refused rather than clamped. 0 to 255 passed into a 0 to 1 field does
	// not look wrong, it looks white, and a diagram drawn entirely in white
	// is a diagram nobody questions until it is on a sheet.
	const bool inRange = red >= 0.0 && red <= 1.0 &&
						 green >= 0.0 && green <= 1.0 &&
						 blue >= 0.0 && blue <= 1.0;
	if (!inRange) {
		why = GS::UniString (name) + " takes 0 to 1 for each channel, not 0 to 255.";
		return false;
	}

	into.f_red = red;
	into.f_green = green;
	into.f_blue = blue;
	return true;
}

}		// namespace Loriini
