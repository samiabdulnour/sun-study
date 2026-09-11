"""Site and context analysis from open data, for one NSW street address.

What an office prepares by hand at the start of a DA -- the context analysis,
the site analysis and the development summary -- fetched from the NSW
Spatial Services, ePlanning, Transport for NSW and OpenStreetMap endpoints
instead of screenshots, and handed to the ``archicad`` package to draw.

This layer fetches and curates. It knows nothing about Archicad: a bundle is
plain geometry in longitude and latitude, plus the categories the office's
legend uses, and ``archicad.site_analysis`` is what turns it into fills and
lines in a worksheet. The split is the same one the sun study makes between
``core`` and ``archicad``: what can be tested without a licence is kept apart
from what cannot.

Ported from the office's ``au-site-analysis`` generator, endpoint for endpoint,
so the two agree about where every line on the sheet comes from.
"""
