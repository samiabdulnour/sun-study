// Fills, with the attributes Tapir's `CreateHatches` cannot reach.
//
// This is the command the shadow and solar diagrams are actually made of, and
// four separate workarounds in the Python side exist only because Tapir's
// version of it is thinner than the element it creates.
//
// What `CreateHatches` offers: coordinates for a single contour, layer, floor,
// three pen indices, a fill attribute, a building material, room special and
// show area. What `API_HatchType` actually carries, and this command reaches:
//
//   foregroundRGB / backgroundRGB   the colour itself, not an index into
//                                   somebody's pen table
//   a polygon with sub-contours     so a shadow can have a hole in it
//   determination                   drafting, cut or cover
//   ltypeInd, penWeight             the contour's line type and weight
//   an element ID at creation       rather than a second pass afterwards
//
// The first is the one that matters most. D27 exists because a pen index means
// nothing outside its own pen table, so the tool measures the seven reference
// band colours and then goes hunting for the nearest pen in the office
// palette -- with a one-to-one assignment, a POOR MATCH warning and an
// indistinguishability check, all of it apparatus for a colour the element can
// simply be told. On the reference project one band had no pen within 110.
//
// `APIHatch_HasFgRGBColor` and `APIHatch_HasBkgRGBColor` are what switch the
// RGB fields on; without the flag the fields are ignored and the pens are used,
// which is why they are set together here and never separately.

#if !defined (LORIINI_DRAWINGCOMMANDS_HPP)
#define LORIINI_DRAWINGCOMMANDS_HPP

#pragma once

#include "Support.hpp"

namespace Loriini {

class CreateFillsCommand : public Command {
public:
	virtual GS::String						GetName () const override;
	virtual GS::Optional<GS::UniString>		GetInputParametersSchema () const override;
	virtual GS::Optional<GS::UniString>		GetResponseSchema () const override;

	virtual API_AddOnCommandExecutionPolicy	GetExecutionPolicy () const override
	{
		return API_AddOnCommandExecutionPolicy::ScheduleForExecutionOnMainThread;
	}

	virtual bool							IsProcessWindowVisible () const override { return true; }

	virtual GS::ObjectState					Execute (const GS::ObjectState& parameters,
													 GS::ProcessControl& processControl) const override;
};

}		// namespace Loriini

#endif
