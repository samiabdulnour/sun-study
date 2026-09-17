// A strip of buttons in Archicad, one per study.
//
// Why a palette and not a menu
// ----------------------------
//
// Not, as this said until 17 September 2026, because a menu item cannot carry
// an icon. It can: Tapir's compiled menu strings end with `^32503` and
// `^32510`, its own 'GICN' ids, and they draw. That was read straight out of
// the shipped TapirAddOn_AC26_Win.apx, and it means `ACAPI_Register_Menu`
// taking no icon argument proves nothing -- the icon rides in the string.
//
// The reason that survives is smaller and still good: a menu item is one
// click into a hidden list, and seven studies read as seven words a colleague
// has to stop and parse. A palette puts all seven on screen at once as
// pictures, which is the printer driver's logic and the reason the icons were
// drawn in the first place. The menu item should get its icon too; the two are
// not in competition.
//
// What a button can honestly do
// -----------------------------
//
// Not run the study. The analysis lives in Python where it is tested, and
// `core` is kept free of Archicad on purpose. So a button starts Loriini.exe
// with the study it stands for already ticked, and the window comes up
// arranged around that one output -- which is the printer driver's own logic,
// and the same logic the icons themselves came from.
//
// The first button is the Loriini mark and opens the window as the person last
// left it, which is what the menu item has always done.

#if !defined (LORIINI_PALETTE_HPP)
#define LORIINI_PALETTE_HPP

#pragma once

#include "APIEnvir.h"
#include "ACAPinc.h"

#include "DGModule.hpp"

class LoriiniPalette final : public DG::Palette,
							 public DG::PanelObserver,
							 public DG::ButtonItemObserver
{
public:
	virtual ~LoriiniPalette ();

	// Built on first use and kept. `HasInstance` exists so the callbacks and
	// the menu handler can ask whether it is up without constructing one --
	// asking Archicad to hide a palette nobody has opened would otherwise
	// create it in order to hide it.
	static bool				HasInstance ();
	static LoriiniPalette&	Instance ();

	void					Show ();
	void					Hide ();

	// Tells Archicad this add-on owns a modeless window, which is what makes
	// the palette survive a view change and reappear where it was left.
	static GSErrCode		RegisterPaletteControlCallBack ();

private:
	// One per button, paired with its item number from the 'GDLG' resource in
	// the constructor. The order here is the order in the strip.
	DG::IconButton	openButton;
	DG::IconButton	facadeButton;
	DG::IconButton	apartmentsButton;
	DG::IconButton	communalButton;
	DG::IconButton	shadowButton;
	DG::IconButton	viewsButton;
	DG::IconButton	siteButton;

	void			SetMenuItemCheckedState (bool isChecked);

	virtual void	PanelCloseRequested (const DG::PanelCloseRequestEvent& ev, bool* accepted) override;
	virtual void	ButtonClicked (const DG::ButtonClickEvent& ev) override;

	static GSErrCode	PaletteControlCallBack (Int32 paletteId, API_PaletteMessageID messageID, GS::IntPtr param);

	static const GS::Guid				paletteGuid;
	static GS::Ref<LoriiniPalette>		instance;

	LoriiniPalette ();
};

#endif
