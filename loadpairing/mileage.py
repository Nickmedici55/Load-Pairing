"""Lane mileage: a permanent cache in front of a truck-legal routing source.

These loads run Class 8 into Lake Placid, Massena and Plattsburgh NY, where
car routing and truck routing diverge meaningfully, so the mileage source
matters. PC*Miler is preferred where a license exists; Google and HERE are
there for shops without one; the great-circle fallback is what runs offline.

Every lane fetched is written to the ``lane`` table and never fetched again.
Rows written by the fallback carry ``source = 'estimated'`` so real miles can
be backfilled later without disturbing anything else.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional, Protocol

from .db import Lane, Location, Store
from .geocode import estimated_miles

ESTIMATED = "estimated"
HTTP_TIMEOUT_SECONDS = 20


class RoutingError(Exception):
    """Raised when a routing source cannot answer for a lane."""


class Router(Protocol):
    name: str

    def miles(self, origin: Location, destination: Location) -> float: ...


@dataclass
class EstimatedRouter:
    """Offline fallback: great-circle distance times a circuity factor."""

    name: str = ESTIMATED

    def miles(self, origin: Location, destination: Location) -> float:
        if None in (origin.lat, origin.lon, destination.lat, destination.lon):
            missing = origin.zip if origin.lat is None or origin.lon is None else destination.zip
            raise RoutingError(
                f"no coordinates on file for {missing}; either set a routing API key "
                "(PCMILER_API_KEY, GOOGLE_MAPS_API_KEY or HERE_API_KEY) or load ZIP coordinates"
            )
        return estimated_miles(origin.lat, origin.lon, destination.lat, destination.lon)


@dataclass
class PCMilerRouter:
    """PC*Miler Web Services, practical miles on a truck-legal route."""

    api_key: str
    name: str = "pcmiler"
    base_url: str = "https://pcmiler.alk.com/apis/rest/v1.0/Service.svc/route/routeReports"
    region: str = "NA"

    def miles(self, origin: Location, destination: Location) -> float:
        query = urllib.parse.urlencode(
            {
                "stops": f"{origin.zip};{destination.zip}",
                "reports": "Mileage",
                "region": self.region,
                "vehType": "Truck",
                "routeType": "Practical",
                "dataset": "Current",
            }
        )
        payload = _get_json(f"{self.base_url}?{query}", headers={"Authorization": self.api_key})
        try:
            return float(payload[0]["TMiles"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RoutingError(f"unexpected PC*Miler response for {origin.zip}->{destination.zip}") from exc


@dataclass
class GoogleRouter:
    """Google Distance Matrix. Car routing -- fine for flat New England lanes,
    less so for the Adirondack runs."""

    api_key: str
    name: str = "google"
    base_url: str = "https://maps.googleapis.com/maps/api/distancematrix/json"

    def miles(self, origin: Location, destination: Location) -> float:
        query = urllib.parse.urlencode(
            {
                "origins": f"{origin.zip},US",
                "destinations": f"{destination.zip},US",
                "units": "imperial",
                "key": self.api_key,
            }
        )
        payload = _get_json(f"{self.base_url}?{query}")
        try:
            element = payload["rows"][0]["elements"][0]
            if element.get("status") != "OK":
                raise RoutingError(f"Google returned {element.get('status')}")
            return round(element["distance"]["value"] / 1609.344, 1)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RoutingError(f"unexpected Google response for {origin.zip}->{destination.zip}") from exc


@dataclass
class HereRouter:
    """HERE Routing v8 with a truck profile."""

    api_key: str
    name: str = "here"
    base_url: str = "https://router.hereapi.com/v8/routes"

    def miles(self, origin: Location, destination: Location) -> float:
        if None in (origin.lat, origin.lon, destination.lat, destination.lon):
            raise RoutingError("HERE routing needs coordinates for both ZIPs")
        query = urllib.parse.urlencode(
            {
                "transportMode": "truck",
                "origin": f"{origin.lat},{origin.lon}",
                "destination": f"{destination.lat},{destination.lon}",
                "return": "summary",
                "apiKey": self.api_key,
            }
        )
        payload = _get_json(f"{self.base_url}?{query}")
        try:
            meters = payload["routes"][0]["sections"][0]["summary"]["length"]
            return round(meters / 1609.344, 1)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RoutingError(f"unexpected HERE response for {origin.zip}->{destination.zip}") from exc


def _get_json(url: str, headers: Optional[dict[str, str]] = None):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except OSError as exc:
        raise RoutingError(f"routing request failed: {exc}") from exc


def router_from_env(name: str = "auto") -> Router:
    """Pick a routing source.

    ``auto`` uses whichever API key is present -- PC*Miler first -- and falls
    back to the offline estimate.
    """
    keys = {
        "pcmiler": os.environ.get("PCMILER_API_KEY"),
        "google": os.environ.get("GOOGLE_MAPS_API_KEY"),
        "here": os.environ.get("HERE_API_KEY"),
    }
    routers = {"pcmiler": PCMilerRouter, "google": GoogleRouter, "here": HereRouter}

    if name in routers:
        key = keys[name]
        if not key:
            raise RoutingError(f"{name} routing needs its API key in the environment")
        return routers[name](api_key=key)
    if name == ESTIMATED:
        return EstimatedRouter()
    if name != "auto":
        raise RoutingError(f"unknown routing source {name!r}")

    for candidate in ("pcmiler", "google", "here"):
        if keys[candidate]:
            return routers[candidate](api_key=keys[candidate])
    return EstimatedRouter()


class MileageService:
    """Reads the lane cache, fetches what is missing, writes it back."""

    def __init__(self, store: Store, router: Router, fallback: Optional[Router] = None):
        self.store = store
        self.router = router
        self.fallback = fallback if fallback is not None else EstimatedRouter()
        self._lanes = {(lane.from_zip, lane.to_zip): lane for lane in store.lanes()}
        self._locations = {loc.zip: loc for loc in store.locations()}
        self.fetched = 0
        self.failures: dict[tuple[str, str], str] = {}

    def refresh(self) -> None:
        self._locations = {loc.zip: loc for loc in self.store.locations()}

    def miles(self, from_zip: str, to_zip: str) -> tuple[float, str]:
        """Miles for one lane, fetching and caching on a miss."""
        if from_zip == to_zip:
            return 0.0, "same-zip"

        cached = self._lanes.get((from_zip, to_zip))
        if cached is not None:
            return cached.miles, cached.source

        origin = self._locations.get(from_zip) or Location(zip=from_zip)
        destination = self._locations.get(to_zip) or Location(zip=to_zip)

        lane = self._fetch(origin, destination)
        self._lanes[(from_zip, to_zip)] = lane
        self.store.save_lane(lane)
        self.fetched += 1
        return lane.miles, lane.source

    def _fetch(self, origin: Location, destination: Location) -> Lane:
        try:
            miles = self.router.miles(origin, destination)
            return Lane(origin.zip, destination.zip, round(float(miles), 1), self.router.name)
        except RoutingError as exc:
            if self.fallback is None or self.router.name == self.fallback.name:
                raise
            self.failures[(origin.zip, destination.zip)] = str(exc)
            miles = self.fallback.miles(origin, destination)
            return Lane(origin.zip, destination.zip, round(float(miles), 1), self.fallback.name)
