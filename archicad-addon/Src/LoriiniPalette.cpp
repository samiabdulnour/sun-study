#include "LoriiniPalette.hpp"

#include "Launch.hpp"
#include "ResourceIds.hpp"

// GSGuid2APIGuid. ACAPinc.h does not reach it -- the same reason Support.hpp
// includes this header and says so.
#include "API_Guid.hpp"

// Its own guid, and it must never change: Archicad remembers where a modeless
// window was left by this number, and a new one loses every colleague's
// palette position.
const GS::Guid LoriiniPalette::paletteGuid ("{108BF235-887D-47B0-93AF-9A68EA5BCD4E}");

GS::Ref<LoriiniPalette> LoriiniPalette::instance;


LoriiniPalette::LoriiniPalette ()
	: DG::Palette (ACAPI_GetOwnResModule (), ID_PALETTE, ACAPI_GetOwnResModule (), paletteGuid)
	, openButton (GetReference (), ID_PALETTE_ITEM_OPEN)
	, facadeButton (GetReference (), ID_PALETTE_ITEM_FACADE)
	, apartmentsButton (GetReference (), ID_PALETTE_ITEM_APARTMENTS)
	, communalButton (GetReference (), ID_PALETTE_ITEM_COMMUNAL)
	, shadowButton (GetReference (), ID_PALETTE_ITEM_SHADOW)
	, viewsButton (GetReference (), ID_PALETTE_ITEM_VIEWS)
	, siteButton (GetReference (), ID_PALETTE_ITEM_SITE)
{
	Attach (*this);
	AttachToAllItems (*this);

	BeginEventProcessing ();
}


LoriiniPalette::~LoriiniPalette ()
{
	DetachFromAllItems (*this);
	EndEventProcessing ();
}


bool LoriiniPalette::HasInstance ()
{
	return instance != nullptr;
}


LoriiniPalette& LoriiniPalette::Instance ()
{
	if (!HasInstance ()) {
		instance = new LoriiniPalette ();
	}
	return *instance;
}


void LoriiniPalette::Show ()
{
	DG::Palette::Show ();
	SetMenuItemCheckedState (true);
}


void LoriiniPalette::Hide ()
{
	DG::Palette::Hide ();
	SetMenuItemCheckedState (false);
}


void LoriiniPalette::SetMenuItemCheckedState (bool isChecked)
{
	API_MenuItemRef	itemRef = {};
	GSFlags			itemFlags = {};

	itemRef.menuResID = ID_PALETTE_MENU;
	itemRef.itemIndex = ID_PALETTE_MENU_SHOW;

	// The Archicad 26 spelling. Newer kits have ACAPI_MenuItem_GetMenuItemFlags
	// for the same thing; this add-on is compiled against 26 only, so the
	// older call is used directly rather than behind a version macro.
	ACAPI_Interface (APIIo_GetMenuItemFlagsID, &itemRef, &itemFlags);
	if (isChecked) {
		itemFlags |= API_MenuItemChecked;
	} else {
		itemFlags &= ~API_MenuItemChecked;
	}
	ACAPI_Interface (APIIo_SetMenuItemFlagsID, &itemRef, &itemFlags);
}


void LoriiniPalette::PanelCloseRequested (const DG::PanelCloseRequestEvent&, bool* accepted)
{
	Hide ();
	*accepted = true;
}


void LoriiniPalette::ButtonClicked (const DG::ButtonClickEvent& ev)
{
	// The names are the ones `Window.ask_for` in the Python side knows. They
	// are matched there case-insensitively and an unknown one is ignored, so a
	// typo here costs the preselection and not the launch.
	if (ev.GetSource () == &openButton) {
		Loriini::StartTheApp (GS::EmptyUniString);
	} else if (ev.GetSource () == &facadeButton) {
		Loriini::StartTheApp ("facade");
	} else if (ev.GetSource () == &apartmentsButton) {
		Loriini::StartTheApp ("apartments");
	} else if (ev.GetSource () == &communalButton) {
		Loriini::StartTheApp ("communal");
	} else if (ev.GetSource () == &shadowButton) {
		Loriini::StartTheApp ("shadow");
	} else if (ev.GetSource () == &viewsButton) {
		Loriini::StartTheApp ("views");
	} else if (ev.GetSource () == &siteButton) {
		Loriini::StartTheApp ("site");
	}
}


GSErrCode LoriiniPalette::PaletteControlCallBack (Int32, API_PaletteMessageID messageID, GS::IntPtr param)
{
	switch (messageID) {
		case APIPalMsg_OpenPalette:
			Instance ().Show ();
			break;

		case APIPalMsg_ClosePalette:
			if (!HasInstance ()) {
				break;
			}
			Instance ().Hide ();
			break;

		// Archicad hides every add-on palette while it shows something of its
		// own, then asks for them back. Both halves are needed or the palette
		// disappears for the rest of the session.
		case APIPalMsg_HidePalette_Begin:
			if (HasInstance () && Instance ().IsVisible ()) {
				Instance ().Hide ();
			}
			break;

		case APIPalMsg_HidePalette_End:
			if (HasInstance () && !Instance ().IsVisible ()) {
				Instance ().Show ();
			}
			break;

		case APIPalMsg_IsPaletteVisible:
			*(reinterpret_cast<bool*> (param)) = HasInstance () && Instance ().IsVisible ();
			break;

		default:
			break;
	}

	return NoError;
}


GSErrCode LoriiniPalette::RegisterPaletteControlCallBack ()
{
	// Enabled everywhere a study is worth starting from, which is everywhere
	// a colleague works: the plan, the sections, the 3D window, a layout. The
	// list is spelled out rather than defaulted because a palette that
	// vanishes in the 3D window reads as a crash.
	return ACAPI_RegisterModelessWindow (
					GS::CalculateHashValue (paletteGuid),
					PaletteControlCallBack,
					API_PalEnabled_FloorPlan + API_PalEnabled_Section + API_PalEnabled_Elevation +
					API_PalEnabled_InteriorElevation + API_PalEnabled_3D + API_PalEnabled_Detail +
					API_PalEnabled_Worksheet + API_PalEnabled_Layout + API_PalEnabled_DocumentFrom3D,
					GSGuid2APIGuid (paletteGuid));
}
