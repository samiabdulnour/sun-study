// Loriini, inside Archicad.
//
// Two jobs, and they are separate on purpose.
//
// The first is the commands, in three groups, every one of them a thing
// Archicad's C++ API can do and its JSON API never exposed:
//
//   Commands.hpp          the 3D projection and 3D Documents
//   DrawingCommands.hpp   fills, with the attributes that make them right
//   ProjectCommands.hpp   where the tool is standing, and what it can see
//
// The test for belonging here is narrow and worth restating: no JSON command
// exists for it anywhere, in Tapir or in Archicad's own API. Anything Tapir
// already does keeps going through Tapir.
//
// The second is the interface: a menu and a palette, so Loriini is a thing in
// Archicad rather than a window a colleague has to go and find in the Start
// menu. That matters more than it sounds: a tool nobody can see from inside
// the project they are working on is a tool that gets used once. Neither runs
// the study itself -- the analysis lives in Python, where it is tested -- they
// start the app and get out of the way.
//
// The palette exists because a menu item cannot carry an icon. Archicad offers
// no way to put one there, so a palette is the only surface in the application
// that can show a drawing; see LoriiniPalette.hpp.

#include "Commands.hpp"
#include "DrawingCommands.hpp"
#include "ProjectCommands.hpp"
#include "ModelCommands.hpp"
#include "LayoutCommands.hpp"

#include "Launch.hpp"
#include "LoriiniPalette.hpp"
#include "ResourceIds.hpp"

#include "APIEnvir.h"
#include "ACAPinc.h"

#include <functional>

// RSGetIndString. The dev kit example reaches it through its own
// APICommon.h, which is example scaffolding rather than part of the kit.
#include "RS.hpp"


// -----------------------------------------------------------------------------
// MenuCommandHandler
// -----------------------------------------------------------------------------

GSErrCode __ACENV_CALL MenuCommandHandler (const API_MenuParams* menuParams)
{
	// One menu, so the item index is the whole question.
	if (menuParams->menuItemRef.menuResID != ID_ADDON_MENU) {
		return NoError;
	}

	switch (menuParams->menuItemRef.itemIndex) {
		case ID_ADDON_MENU_OPEN:
			Loriini::StartTheApp (GS::EmptyUniString);
			break;

		case ID_ADDON_MENU_PALETTE:
			// A toggle. Asked of HasInstance first so that choosing it when no
			// palette has ever been opened does not build one in order to hide
			// it.
			if (LoriiniPalette::HasInstance () && LoriiniPalette::Instance ().IsVisible ()) {
				LoriiniPalette::Instance ().Hide ();
			} else {
				LoriiniPalette::Instance ().Show ();
			}
			break;

		default:
			break;
	}

	return NoError;
}


// -----------------------------------------------------------------------------
// Dependency definitions
// -----------------------------------------------------------------------------

API_AddonType __ACDLL_CALL CheckEnvironment (API_EnvirParams* envir)
{
	RSGetIndString (&envir->addOnInfo.name,        ID_ADDON_INFO, ID_ADDON_INFO_NAME, ACAPI_GetOwnResModule ());
	RSGetIndString (&envir->addOnInfo.description, ID_ADDON_INFO, ID_ADDON_INFO_DESC, ACAPI_GetOwnResModule ());

	// Preload, not Normal. The commands have to answer the moment a request
	// arrives over the JSON port, including before anybody has touched the
	// menu, and a normally-loaded add-on is not in memory until something
	// asks for it.
	return APIAddon_Preload;
}


// -----------------------------------------------------------------------------
// Interface definitions
// -----------------------------------------------------------------------------

GSErrCode __ACDLL_CALL RegisterInterface (void)
{
	// Once. Archicad draws a submenu per registered menu resource, so
	// registering a second one for the palette gave the menu bar two "Loriini"
	// submenus side by side with the palette inside the second.
	return ACAPI_Register_Menu (ID_ADDON_MENU, 0, MenuCode_UserDef,
								MenuFlag_SeparatorBefore);
}


// -----------------------------------------------------------------------------
// Initialize
// -----------------------------------------------------------------------------

GSErrCode __ACENV_CALL Initialize (void)
{
	GSErrCode err = ACAPI_Install_MenuHandler (ID_ADDON_MENU, MenuCommandHandler);
	if (err != NoError) {
		return err;
	}

	// Tells Archicad this add-on owns a modeless window. Without it the
	// palette opens and then disappears the first time the view changes.
	err = LoriiniPalette::RegisterPaletteControlCallBack ();
	if (err != NoError) {
		return err;
	}

	// Every command is checked. A handler that failed to install is a command
	// that answers "not found" over the wire, which reads on the Python side
	// as an add-on that is too old rather than one that is half loaded.
	//
	// Listed rather than called one at a time, because the list is now long
	// enough that a forgotten `if (err != NoError)` after one of them would
	// be invisible -- and a half-installed add-on is the failure that reads
	// as a version problem and sends somebody to the wrong place.
	const std::function<GSErrCode ()> installers[] = {
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::GetProjectionCommand> ()); },
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::SetProjectionCommand> ()); },
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::CreateDocumentFrom3DCommand> ()); },
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::CreateFillsCommand> ()); },
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::CreateTextsCommand> ()); },
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::PlaceFiguresCommand> ()); },
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::PlaceDrawingsCommand> ()); },
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::CreateMeshCommand> ()); },
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::CreateSlabsCommand> ()); },
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::GetCurrentDatabaseCommand> ()); },
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::SetCurrentDatabaseCommand> ()); },
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::CreateWorksheetCommand> ()); },
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::ActivateLayerCombinationCommand> ()); },
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::ModifyLayersCommand> ()); },
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::GetDrawingsCommand> ()); },
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::ArrangeDrawingsCommand> ()); },
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::Set3DFilterCommand> ()); },
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::ExportModelCommand> ()); },
	};

	for (const std::function<GSErrCode ()>& install : installers) {
		err = install ();
		if (err != NoError) {
			return err;
		}
	}

	return NoError;
}


// -----------------------------------------------------------------------------
// FreeData
// -----------------------------------------------------------------------------

GSErrCode __ACENV_CALL FreeData (void)
{
	return NoError;
}
