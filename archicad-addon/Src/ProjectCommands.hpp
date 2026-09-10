// Where the tool is standing, and what it can see from there.
//
// Four commands, closing four recorded walls that all turned out to have the
// same shape: Archicad's C++ API has the call, and its JSON API never exposed
// it, so the Python side had to simulate the answer and then live with the
// simulation drifting.
//
//   GetCurrentDatabase        D63, D64. Tapir has no `GetCurrentDatabase`,
//                             `GetCurrentWindow` or `GetDatabases`, so the
//                             tool remembers where it last went and is wrong
//                             the moment anybody clicks a tab.
//
//   SetCurrentDatabase        D38, and the worksheet that cannot be left. On
//                             Archicad 26 `ChangeWindow` answers
//                             `{"success": true}` and changes nothing, for
//                             `windowType` alone, with `storyIndex`, and with
//                             a floor plan's own `databaseId`. A worksheet
//                             left in front then had to be cleared by hand.
//
//   CreateWorksheet           the same wall from the other side. Tapir's
//                             `CreateWorksheets` works, and drawing into one
//                             made in the same session fails with
//                             -2130313110 before and after `RebuildView`.
//                             Creating it and becoming current in one call is
//                             what makes it drawable.
//
//   ActivateLayerCombination  D59. There is no `ActivateLayerCombination` and
//                             no `SetLayers`, so the tool rewrites every
//                             layer with `CreateLayers` and `overwriteExisting`
//                             to imitate one. `APIEnv_ChangeCurrLayerCombID`
//                             is the call that was missing.
//
//   ModifyLayers              the other half of D59. A layer's hidden and
//                             locked state is two bits in an attribute header,
//                             and `ACAPI_Attribute_Modify` sets them without
//                             recreating the layer.

#if !defined (LORIINI_PROJECTCOMMANDS_HPP)
#define LORIINI_PROJECTCOMMANDS_HPP

#pragma once

#include "Support.hpp"

namespace Loriini {

// All five are the same shape: a name, two schemas and an `Execute`. The
// namespace, the execution policy and the rest come from `Command`.
#define LORIINI_COMMAND(ClassName)                                                          \
	class ClassName : public Command {                                                      \
	public:                                                                                 \
		virtual GS::String					GetName () const override;                      \
		virtual GS::Optional<GS::UniString>	GetInputParametersSchema () const override;     \
		virtual GS::Optional<GS::UniString>	GetResponseSchema () const override;            \
		virtual GS::ObjectState				Execute (const GS::ObjectState& parameters,     \
													 GS::ProcessControl& processControl) const override; \
	};

LORIINI_COMMAND (GetCurrentDatabaseCommand)
LORIINI_COMMAND (SetCurrentDatabaseCommand)
LORIINI_COMMAND (CreateWorksheetCommand)
LORIINI_COMMAND (ActivateLayerCombinationCommand)
LORIINI_COMMAND (ModifyLayersCommand)

#undef LORIINI_COMMAND

}		// namespace Loriini

#endif
