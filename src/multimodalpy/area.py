"""Study areas: a Spanish municipality, or a polygon of your own.

:func:`find_area` looks a municipality up by name (or INE code) in the
packaged municipal boundaries, or takes the polygon in a file of your own
(``boundaries_path``) and finds the provinces and municipalities it
overlaps. It returns the area's polygon with those lists.
:func:`multimodalpy.download_network` uses it to know where to download the
networks, and which provinces' bus and train feeds; call it yourself to
inspect or reuse the area.

The packaged boundaries cover peninsular Spain, the Balearic Islands, Ceuta
and Melilla, not the Canary Islands; an area of your own must overlap them.
They are downloaded once (about 50 MB) and cached.
"""

from __future__ import annotations

from ._boundaries import AmbiguousAreaError, find_area

__all__ = ["AmbiguousAreaError", "find_area"]
