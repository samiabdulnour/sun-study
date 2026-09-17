#include "Launch.hpp"

#include "FileSystem.hpp"
#include "Location.hpp"
#include "Name.hpp"

#if defined (WINDOWS)
	#include <windows.h>
	#include <shellapi.h>
#endif

namespace Loriini {

namespace {

// What the app is called on disk. Looked for beside the add-on itself, which
// is where the install instructions put it: one folder holding the .apx and
// the .exe, so moving Loriini to another workstation is copying a folder.
const char* APP_FILE_NAME = "Loriini.exe";


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

}		// namespace


void StartTheApp (const GS::UniString& study)
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

	// Held in a named local rather than built inside the call: the pointer
	// handed to ShellExecuteW borrows the string's buffer, and a temporary
	// would be gone before Windows read it.
	const GS::UniString arguments = study.IsEmpty () ? GS::UniString () : "--study " + study;
	LPCWSTR parameters = nullptr;
	if (!arguments.IsEmpty ()) {
		parameters = reinterpret_cast<LPCWSTR> (arguments.ToUStr ().Get ());
	}

	const HINSTANCE started = ShellExecuteW (nullptr, L"open",
											 reinterpret_cast<LPCWSTR> (path.ToUStr ().Get ()),
											 parameters, nullptr, SW_SHOWNORMAL);
	// ShellExecute returns a value above 32 on success. The convention is
	// odd and worth naming rather than leaving as a bare number.
	const bool ok = reinterpret_cast<INT_PTR> (started) > 32;
	if (!ok) {
		ACAPI_WriteReport ("Loriini: Windows refused to start " + path, true);
	}
#else
	(void) study;
	ACAPI_WriteReport ("Loriini: starting the app is only wired up on Windows.", true);
#endif
}

}		// namespace Loriini
