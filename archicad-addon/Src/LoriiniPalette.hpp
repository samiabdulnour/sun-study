// A strip of buttons in Archicad, one per study.
//
// Why a palette and not a menu
// ----------------------------
//
// An Archicad add-on cannot put an icon on a menu item. `ACAPI_Register_Menu`
// takes a string resource, a prompt, a menu code and flags, and nothing else;
// Tapir declares ten 'GICN' icons and every one of them is used by its
// palette, never by its menu. So a palette is the only surface in Archicad
// that can show a drawing, and this is the smallest one worth having: seven
// buttons, 28 px tall, no state of its own.
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
