"""Small geography helpers used to make spoken and written alerts understandable: place names and compass directions.

Place names come from Mapbox reverse geocoding (MAPBOX_API) with OpenStreetMap Nominatim as a fallback. If neither answers,
`place()` returns None and callers simply leave the place out; nothing is invented.
"""
import math
from typing import Any

import httpx

from app.core.config import get_settings

settings = get_settings()
_cache: dict[str, dict[str, Any] | None] = {}
COMPASS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]


def place(lat: float, lon: float) -> dict[str, Any] | None:
    """{'name': 'Kalpetta, Wayanad, Kerala', 'locality', 'district', 'state'} or None."""
    key = f"{lat:.3f},{lon:.3f}"
    if key in _cache and _cache[key] is not None:
        return _cache[key]
    out = _mapbox(lat, lon) or _nominatim(lat, lon)
    _cache[key] = out
    return out


def _mapbox(lat: float, lon: float) -> dict[str, Any] | None:
    if not settings.mapbox_api:
        return None
    try:
        r = httpx.get(f"https://api.mapbox.com/geocoding/v5/mapbox.places/{lon},{lat}.json", params={"access_token": settings.mapbox_api, "types": "locality,place,district,region", "limit": 1, "language": "en"}, timeout=12)
        if r.status_code != 200:
            return None
        feats = r.json().get("features") or []
        if not feats:
            return None
        f = feats[0]
        names = {"locality": None, "place": None, "district": None, "region": None}
        names[f["place_type"][0]] = f["text"]
        for c in f.get("context") or []:
            kind = c["id"].split(".")[0]
            if kind in names and not names[kind]:
                names[kind] = c["text"]
        parts = [p for p in (names["locality"] or names["place"], names["district"], names["region"]) if p]
        return {"name": ", ".join(dict.fromkeys(parts)), "locality": names["locality"] or names["place"], "district": names["district"], "state": names["region"]} if parts else None
    except (httpx.HTTPError, ValueError, KeyError):
        return None


def _nominatim(lat: float, lon: float) -> dict[str, Any] | None:
    try:
        r = httpx.get("https://nominatim.openstreetmap.org/reverse", params={"format": "jsonv2", "zoom": 10, "lat": lat, "lon": lon}, headers={"User-Agent": "VanRakshak/1.0"}, timeout=12)
        a = r.json().get("address") or {}
        loc = a.get("village") or a.get("town") or a.get("city") or a.get("suburb")
        dist = a.get("county") or a.get("state_district")
        parts = [p for p in (loc, dist, a.get("state")) if p]
        return {"name": ", ".join(dict.fromkeys(parts)), "locality": loc, "district": dist, "state": a.get("state")} if parts else None
    except (httpx.HTTPError, ValueError):
        return None


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass(deg: float) -> str:
    return COMPASS[int((deg + 22.5) // 45) % 8]


def direction_from(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """Compass direction of point 2 as seen from point 1 ('north-east')."""
    return compass(bearing_deg(lat1, lon1, lat2, lon2))


def say_minutes(m: float) -> str:
    m = int(round(m))
    if m < 60:
        return f"{m} minutes"
    h, r = divmod(m, 60)
    return f"{h} hour{'s' if h > 1 else ''}" + (f" {r} minutes" if r else "")
