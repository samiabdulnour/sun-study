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

	// Both the string *and* the wide buffer it lends out are held in named
	// locals, which is the half the first version missed.
	//
	// `ToUStr ()` does not return a pointer into the UniString; it returns a
	// helper object that owns the wide buffer, and `Get ()` points into that.
	// Keeping only the UniString alive therefore left `parameters` dangling at
	// the semicolon, because the helper died there. It happened to be harmless
	// from the menu, which passes no study and leaves the pointer null, and
	// would have bitten the first palette button anybody pressed.
	//
	// `auto` because the helper's type is private to GS::UniString -- the same
	// reason `Support.cpp` spells `ToCStr ()`'s result that way.
	const GS::UniString arguments = study.IsEmpty () ? GS::UniString () : "--study " + study;
	const auto argumentChars = arguments.ToUStr ();
	LPCWSTR parameters = nullptr;
	if (!arguments.IsEmpty ()) {
		parameters = reinterpret_cast<LPCWSTR> (argumentChars.Get ());
	}

	// The path's buffer is held the same way, for the same reason. It was safe
	// where it stood -- a temporary lives to the end of the full expression it
	// is written in -- but two pointers into two helper objects with different
	// lifetimes is a thing to have to work out rather than read.
	const auto pathChars = path.ToUStr ();
	const HINSTANCE started = ShellExecuteW (nullptr, L"open",
											 reinterpret_cast<LPCWSTR> (pathChars.Get ()),
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
