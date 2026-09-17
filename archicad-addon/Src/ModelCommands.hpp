// The 3D model, written out as triangles and nothing else.
//
// Why this exists, when an IFC export already does it
// ---------------------------------------------------
// Because the IFC cannot be made small from here. On the reference project the
// export came to 692 MB, of which roughly nine tenths was
// `IfcPropertySingleValue` and `IfcQuantityLength` that the analysis never
// reads, and the only cure is the "Properties to export" setting inside
// Archicad's IFC translator. Nothing in the API creates or edits a translator:
// every IFC function reads, converts, or opens a dialog for a person. So an
// IFC-shaped answer always ends in "set this up by hand on every machine",
// which is not a thing that can be handed to an office.
//
// The geometry can be read directly instead. `API_BodyType` carries a `parent`
// -- the floor plan element the body was converted from -- and that header
// holds the element's guid, its layer and its storey, which is the whole of
// what the analysis needs beside the triangles themselves.
//
// What is written
// ---------------
// One record per element: guid, layer name, element type, element id, storey
// name, then its triangles as float32 xyz. Nothing else. No properties, no
// quantities, no classifications, no materials -- the analysis has never read
// any of them, and they are the entire reason the IFC was 692 MB.
//
// Zones are deliberately not written. Their outlines, names, storeys and
// categories already come over the JSON API through Tapir, which is how the
// drawing side has always read them; asking for the same fact twice is how the
// two answers get to disagree.
//
// Triangulation, and why it is not a fan over whatever Archicad gives
// -------------------------------------------------------------------
// A body's polygons are general: a wall face with a window in it is one
// polygon with an inner contour. Fanning that fills the hole, and a window
// that stops sunlight is not a detail -- the apartment penetration study is
// precisely the sunlight coming through it. So the polygons are split into
// *convex* ones first, where a fan is exact by construction.
//
// The file is a private format between this command and `ingest/native.py`,
// versioned so a mismatched pair says so rather than reading each other's
// bytes as coordinates -- which would produce a model instead of an error, and
// nothing downstream could tell.

#if !defined (LORIINI_MODELCOMMANDS_HPP)
#define LORIINI_MODELCOMMANDS_HPP

#pragma once

#include "Support.hpp"

namespace Loriini {

class ExportModelCommand : public Command {
public:
	virtual GS::String						GetName () const override;
	virtual GS::Optional<GS::UniString>		GetInputParametersSchema () const override;
	virtual GS::Optional<GS::UniString>		GetResponseSchema () const override;

	// The 3D model has to be converted before its bodies can be walked, and
	// that is main-thread work like everything else here.
	virtual API_AddOnCommandExecutionPolicy	GetExecutionPolicy () const override
	{
		return API_AddOnCommandExecutionPolicy::ScheduleForExecutionOnMainThread;
	}

	// Shown, because this one takes real time on a large project and a window
	// that looks hung is the thing people kill.
	virtual bool							IsProcessWindowVisible () const override { return true; }

	virtual GS::ObjectState					Execute (const GS::ObjectState& parameters,
													 GS::ProcessControl& processControl) const override;
};

}		// namespace Loriini

#endif
