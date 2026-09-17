// Starting Loriini.exe, from the menu or from the palette.
//
// Pulled out of AddOnMain.cpp when the palette arrived and wanted the same
// thing the menu item already did. One copy, because the interesting part is
// not the launch -- it is the report when the .exe is not there, and two
// versions of that message would drift.

#if !defined (LORIINI_LAUNCH_HPP)
#define LORIINI_LAUNCH_HPP

#pragma once

#include "APIEnvir.h"
#include "ACAPinc.h"

#include "UniString.hpp"

namespace Loriini {

// Starts the Loriini window, or says why it could not.
//
// `study` is a study the window should open already ticked -- one of
// "facade", "apartments", "communal", "shadow", "views", "site" -- or empty
// for the window as the person last left it. It is passed as `--study <name>`
// on the command line rather than in the environment, because setting a
// variable here would set it on Archicad's own process and every child it
// ever starts afterwards.
//
// Deliberately not silent on failure. The commonest way this goes wrong is
// the .exe not having been copied next to the .apx, and a button that does
// nothing at all when clicked reads as a broken add-on rather than as a
// missing file.
void StartTheApp (const GS::UniString& study);

}		// namespace Loriini

#endif
