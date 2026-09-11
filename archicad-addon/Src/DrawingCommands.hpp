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


// Texts, with the box, the layer and the anchor Tapir's `CreateTexts` cannot set.
//
// Tapir's version takes a coordinate, a string, a height, a pen, a
// justification and an angle, and hands everything else to the Text tool's
// defaults. Two of those defaults decide whether the text can be read at all.
// A text box that is not `nonBreaking` wraps at its `width`, and a project
// whose Text tool was last used for a narrow wrapped note then draws every
// label one letter per line -- measured on the Kogarah solar study on 11
// September 2026, 81 labels each showing its first letter. And a Text takes
// no layer, so every label lands on the office's annotation layer and has to
// be moved (D60, D62).
//
// So: the box is non-breaking unless a width is asked for, the layer is set
// at creation, the anchor is the caller's, and the element ID goes on in the
// same call, as `CreateFills` does. Content is one paragraph and one run,
// which is what a label is.
class CreateTextsCommand : public Command {
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


// A picture on the drawing: the orthophoto under a site sheet.
//
// No command anywhere places a Figure -- not Archicad's JSON API, not Tapir --
// and a context analysis without its aerial is a diagram of nothing. The
// image comes inside the request as base64, because the add-on cannot be
// pointed at a file on the machine; the box is the picture's size on the
// drawing in metres, which for a tile of the ground is known before its
// pixel count is, and the turn is the frame's.
class PlaceFiguresCommand : public Command {
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
