"""Study areas: find the boundary of a Spanish municipality.

:func:`find_area` looks the municipality up by name (or INE code) in a
boundaries file and returns its polygon. :func:`multimodalpy.download_network`
uses it to know where to download the networks; call it yourself to inspect
or reuse the area.

The default boundaries file covers peninsular Spain, the Balearic Islands,
Ceuta and Melilla, not the Canary Islands; pass ``boundaries_path`` for other
areas. It is downloaded once (about 50 MB) and cached.
"""

from __future__ import annotations

from ._boundaries import AmbiguousAreaError, find_area

__all__ = ["AmbiguousAreaError", "find_area"]
