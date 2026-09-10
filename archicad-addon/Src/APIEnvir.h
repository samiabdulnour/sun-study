// Platform settings, before anything from the API is included.
//
// Every add-on carries its own copy of this. It looks like it belongs to the
// Development Kit and it does not: `APIEnvir.h` lives in each example's own
// `Src` folder, not in the kit's `Inc`, so an add-on that includes it without
// having one fails on the very first line of the very first file with
// `C1083: Cannot open include file`.
//
// What it actually does matters here. `WINDOWS` is defined from the compiler's
// own `_MSC_VER`, and `AddOnMain.cpp` guards its `ShellExecuteW` call on
// exactly that symbol -- so without this file that guard would silently take
// the "not Windows" branch on a Windows build, and the menu item would report
// that starting the app is not wired up on this platform.

#ifndef LORIINI_APIENVIR_H
#define LORIINI_APIENVIR_H

#if defined (_MSC_VER)
	#if !defined (WINDOWS)
		#define WINDOWS
	#endif
#endif

#if defined (WINDOWS)
	#include "Win32Interface.hpp"
#endif

#if !defined (ACExtension)
	#define ACExtension
#endif

#endif
