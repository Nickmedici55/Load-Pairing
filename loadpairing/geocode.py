"""ZIP coordinates, needed only by the offline mileage fallback.

Coordinates live in the ``location`` table. They can arrive three ways:

* a CSV the dispatcher supplies (``zip,lat,lon``, extra columns ignored),
* ``pgeocode`` if it happens to be installed,
* typed in by hand with ``locations set-coords``.

A real routing source never needs any of this -- it is only how the
great-circle fallback gets something to measure.
"""

from __future__ import annotations

import csv
import math
from typing import Iterable, Optional

EARTH_RADIUS_MILES = 3958.7613

#: Road miles run longer than the straight line. 1.20 is the usual circuity
#: factor for regional truckload and is what the offline estimate applies.
CIRCUITY = 1.20


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def estimated_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance inflated by the circuity factor."""
    return round(haversine_miles(lat1, lon1, lat2, lon2) * CIRCUITY, 1)


def read_centroids(path: str) -> dict[str, tuple[float, float]]:
    """Read a ``zip,lat,lon`` CSV. A header row is optional."""
    centroids: dict[str, tuple[float, float]] = {}
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.reader(handle):
            if len(row) < 3:
                continue
            zip_code = "".join(ch for ch in row[0] if ch.isdigit())[:5]
            if not zip_code:
                continue
            try:
                centroids[zip_code.zfill(5)] = (float(row[1]), float(row[2]))
            except ValueError:
                continue                       # the header row, or a bad cell
    return centroids


def pgeocode_centroids(zips: Iterable[str], country: str = "US") -> dict[str, tuple[float, float]]:
    """Look ZIPs up with ``pgeocode`` when it is installed; empty dict if not."""
    try:
        import pgeocode
    except ImportError:
        return {}

    nominatim = pgeocode.Nominatim(country)
    found: dict[str, tuple[float, float]] = {}
    for zip_code in zips:
        record = nominatim.query_postal_code(zip_code)
        lat, lon = record.get("latitude"), record.get("longitude")
        if lat is not None and lon is not None and not (_isnan(lat) or _isnan(lon)):
            found[zip_code] = (float(lat), float(lon))
    return found


def _isnan(value) -> bool:
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return True


def fill_coordinates(store, centroids: Optional[dict[str, tuple[float, float]]] = None) -> int:
    """Write coordinates into any ``location`` row still missing them."""
    missing = [loc.zip for loc in store.locations() if loc.lat is None or loc.lon is None]
    if not missing:
        return 0

    known = dict(centroids or {})
    unresolved = [zip_code for zip_code in missing if zip_code not in known]
    if unresolved:
        known.update(pgeocode_centroids(unresolved))

    filled = 0
    for zip_code in missing:
        if zip_code in known:
            lat, lon = known[zip_code]
            store.set_coordinates(zip_code, lat, lon)
            filled += 1
    return filled
