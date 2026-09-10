// What a placed Drawing is, once Tapir has placed it.
//
//   ArrangeDrawings   Tapir's `CreateDrawings` puts a Drawing on a layout and
//                     nothing can then touch it: `SetDetailsOfElements` takes
//                     the magnification and refuses the rest. A Drawing made
//                     from a 3D Document arrives clipped to a placeholder frame
//                     about 59 mm square, anchored by its bottom-left corner,
//                     set to manual update -- so a sheet of seven sun eye
//                     views is seven stamps, and opening the layout does not
//                     change that. `API_DrawingType` carries all three as
//                     plain fields, and `ACAPI_Element_Change` sets them.

#if !defined (LORIINI_LAYOUTCOMMANDS_HPP)
#define LORIINI_LAYOUTCOMMANDS_HPP

#pragma once

#include "Support.hpp"

namespace Loriini {

class ArrangeDrawingsCommand : public Command {
public:
	virtual GS::String					GetName () const override;
	virtual GS::Optional<GS::UniString>	GetInputParametersSchema () const override;
	virtual GS::Optional<GS::UniString>	GetResponseSchema () const override;
	virtual GS::ObjectState				Execute (const GS::ObjectState& parameters,
												 GS::ProcessControl& processControl) const override;
};

}		// namespace Loriini

#endif
