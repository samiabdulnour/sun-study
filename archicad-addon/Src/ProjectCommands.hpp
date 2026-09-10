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

#include "APIEnvir.h"
#include "ACAPinc.h"

namespace Loriini {

#define LORIINI_COMMAND(ClassName, OnMainThread)                                            \
	class ClassName : public API_AddOnCommand {                                             \
	public:                                                                                 \
		virtual GS::String					GetName () const override;                      \
		virtual GS::String					GetNamespace () const override;                 \
		virtual GS::Optional<GS::UniString>	GetInputParametersSchema () const override;     \
		virtual GS::Optional<GS::UniString>	GetResponseSchema () const override;            \
		virtual API_AddOnCommandExecutionPolicy GetExecutionPolicy () const override         \
		{                                                                                   \
			return OnMainThread;                                                            \
		}                                                                                   \
		virtual GS::ObjectState				Execute (const GS::ObjectState& parameters,     \
													 GS::ProcessControl& processControl) const override; \
	};

LORIINI_COMMAND (GetCurrentDatabaseCommand, API_AddOnCommandExecutionPolicy::ScheduleForExecutionOnMainThread)
LORIINI_COMMAND (SetCurrentDatabaseCommand, API_AddOnCommandExecutionPolicy::ScheduleForExecutionOnMainThread)
LORIINI_COMMAND (CreateWorksheetCommand, API_AddOnCommandExecutionPolicy::ScheduleForExecutionOnMainThread)
LORIINI_COMMAND (ActivateLayerCombinationCommand, API_AddOnCommandExecutionPolicy::ScheduleForExecutionOnMainThread)
LORIINI_COMMAND (ModifyLayersCommand, API_AddOnCommandExecutionPolicy::ScheduleForExecutionOnMainThread)

#undef LORIINI_COMMAND

}		// namespace Loriini

#endif
