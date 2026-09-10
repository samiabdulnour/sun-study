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
// The second is a menu, so Loriini is a thing in the interface rather than a
// window a colleague has to go and find in the Start menu. That matters more
// than it sounds: a tool nobody can see from inside the project they are
// working on is a tool that gets used once. The menu does not run the study
// itself -- the analysis lives in Python, where it is tested -- it starts the
// app and gets out of the way.

#include "Commands.hpp"
#include "DrawingCommands.hpp"
#include "ProjectCommands.hpp"

#include "APIEnvir.h"
#include "ACAPinc.h"

#include <functional>

// RSGetIndString. The dev kit example reaches it through its own
// APICommon.h, which is example scaffolding rather than part of the kit.
#include "RS.hpp"

#include "FileSystem.hpp"
#include "Location.hpp"
#include "Name.hpp"
#include "UniString.hpp"

#if defined (WINDOWS)
	#include <windows.h>
	#include <shellapi.h>
#endif

// 'STR#' resources. Their numbers are the add-on's own and appear in the .grc
// files beside them; changing one here without changing it there produces a
// menu with no words in it and no error anywhere.
#define LORIINI_ADDON_NAME		32000
#define LORIINI_MENU_STRINGS	32500

// Menu items, in the order the .grc lists them. Named rather than numbered at
// the point of use, because a `case 2:` in a switch is how the wrong item
// ends up doing the right thing after somebody inserts a separator.
enum MenuItem {
	OpenLoriini = 1
};

// What the app is called on disk. Looked for beside the add-on itself, which
// is where the install instructions put it: one folder holding the .apx and
// the .exe, so moving Loriini to another workstation is copying a folder.
static const char* APP_FILE_NAME = "Loriini.exe";


namespace {

// The folder this .apx is loaded from.
//
// Asked of Archicad rather than assumed, because there is no fixed place: an
// office without administrator rights registers its add-on folder by hand in
// the Add-On Manager, and on this practice's workstations that is a folder
// under Documents rather than anything in Program Files.
bool OwnFolder (IO::Location& folder)
{
	IO::Location ownFile;
	if (ACAPI_GetOwnLocation (&ownFile) != NoError) {
		return false;
	}
	folder = ownFile;
	return folder.DeleteLastLocalName () == NoError;
}


// Starts the Loriini window, or says why it could not.
//
// Deliberately not silent on failure. The commonest way this goes wrong is
// the .exe not having been copied next to the .apx, and a menu item that does
// nothing at all when clicked reads as a broken add-on rather than as a
// missing file.
void OpenTheApp ()
{
	IO::Location folder;
	if (!OwnFolder (folder)) {
		ACAPI_WriteReport ("Loriini: could not work out where the add-on is installed.", true);
		return;
	}

	IO::Location application = folder;
	application.AppendToLocal (IO::Name (APP_FILE_NAME));

	bool exists = false;
	if (application.IsEmpty () || IO::fileSystem.Contains (application, &exists) != NoError || !exists) {
		const GS::UniString path = application.ToDisplayText ();
		ACAPI_WriteReport ("Loriini: " + path + " is not there. Copy Loriini.exe into the same "
						   "folder as the add-on.", true);
		return;
	}

#if defined (WINDOWS)
	const GS::UniString path = application.ToDisplayText ();
	const HINSTANCE started = ShellExecuteW (nullptr, L"open",
											 reinterpret_cast<LPCWSTR> (path.ToUStr ().Get ()),
											 nullptr, nullptr, SW_SHOWNORMAL);
	// ShellExecute returns a value above 32 on success. The convention is
	// odd and worth naming rather than leaving as a bare number.
	const bool ok = reinterpret_cast<INT_PTR> (started) > 32;
	if (!ok) {
		ACAPI_WriteReport ("Loriini: Windows refused to start " + path, true);
	}
#else
	ACAPI_WriteReport ("Loriini: starting the app is only wired up on Windows.", true);
#endif
}

}		// namespace


// -----------------------------------------------------------------------------
// MenuCommandHandler
// -----------------------------------------------------------------------------

GSErrCode __ACENV_CALL MenuCommandHandler (const API_MenuParams* menuParams)
{
	if (menuParams->menuItemRef.menuResID != LORIINI_MENU_STRINGS) {
		return NoError;
	}

	switch (menuParams->menuItemRef.itemIndex) {
		case OpenLoriini:
			OpenTheApp ();
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
	RSGetIndString (&envir->addOnInfo.name,        LORIINI_ADDON_NAME, 1, ACAPI_GetOwnResModule ());
	RSGetIndString (&envir->addOnInfo.description, LORIINI_ADDON_NAME, 2, ACAPI_GetOwnResModule ());

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
	return ACAPI_Register_Menu (LORIINI_MENU_STRINGS, 0, MenuCode_UserDef,
								MenuFlag_SeparatorBefore);
}


// -----------------------------------------------------------------------------
// Initialize
// -----------------------------------------------------------------------------

GSErrCode __ACENV_CALL Initialize (void)
{
	GSErrCode err = ACAPI_Install_MenuHandler (LORIINI_MENU_STRINGS, MenuCommandHandler);
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
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::GetCurrentDatabaseCommand> ()); },
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::SetCurrentDatabaseCommand> ()); },
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::CreateWorksheetCommand> ()); },
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::ActivateLayerCombinationCommand> ()); },
		[] { return ACAPI_Install_AddOnCommandHandler (GS::NewOwned<Loriini::ModifyLayersCommand> ()); },
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
