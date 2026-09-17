// Every resource number this add-on uses, in one place.
//
// Pulled out of AddOnMain.cpp when the palette arrived. The numbers appear
// twice by necessity -- here, and in the .grc files beside them -- and the
// comment that used to sit on them was right about the danger: changing one
// without the other produces a menu with no words in it, or a palette with no
// buttons, and no error anywhere. A header does not fix that, but it means
// there is exactly one side to change here rather than three files to search.
//
// Resource *types* are separate number spaces, which is why 'MDID' 32500 and
// 'STR#' 32500 can both exist and do. Within a type the numbers must not
// collide, so they are grouped below by type rather than by feature.

#if !defined (LORIINI_RESOURCEIDS_HPP)
#define LORIINI_RESOURCEIDS_HPP

#pragma once

// -- 'STR#' ----------------------------------------------------------------

// The add-on's own name and description, read by CheckEnvironment.
#define ID_ADDON_INFO				32000
#define ID_ADDON_INFO_NAME				1
#define ID_ADDON_INFO_DESC				2

// The add-on's one menu, and its items in the order the 'STR#' lists them.
//
// One resource and not two. Archicad draws a submenu for every registered menu
// resource, so a second one put a second "Loriini" submenu beside the first
// with the palette hidden inside it -- which is what it looked like in
// Archicad on 17 September 2026.
#define ID_ADDON_MENU				32500
#define ID_ADDON_MENU_OPEN				1
#define ID_ADDON_MENU_PALETTE			2

// -- 'GDLG' and 'DLGH' -----------------------------------------------------

// The palette itself. The item indices are the order they are listed in the
// 'GDLG' resource, and the palette's constructor pairs each button with its
// number -- get one wrong and a button does another button's job.
#define ID_PALETTE					32510
#define ID_PALETTE_ITEM_OPEN			1
#define ID_PALETTE_ITEM_FACADE			2
#define ID_PALETTE_ITEM_APARTMENTS		3
#define ID_PALETTE_ITEM_COMMUNAL		4
#define ID_PALETTE_ITEM_SHADOW			5
#define ID_PALETTE_ITEM_VIEWS			6
#define ID_PALETTE_ITEM_SITE			7

// -- 'GICN' ----------------------------------------------------------------
//
// One per button. The images are SVG under RFIX/Images, named
// <base>_24x24.svg, and they are written by scripts/make_icons.py from the
// same coordinates as the window's own icons -- so the palette and the window
// cannot drift apart. Do not hand-edit them.

#define ID_ICON_LOGO				32520
#define ID_ICON_FACADE				32521
#define ID_ICON_APARTMENTS			32522
#define ID_ICON_COMMUNAL			32523
#define ID_ICON_SHADOW				32524
#define ID_ICON_VIEWS				32525
#define ID_ICON_SITE				32526

#endif
