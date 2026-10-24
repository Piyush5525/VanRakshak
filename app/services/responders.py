"""Who can respond near a place: forest offices, police stations, fire stations. All are public map data (OpenStreetMap, Google Maps via
SerpAPI) and are shown as UNVERIFIED: availability, jurisdiction and contact details are not confirmed. Phone numbers are never dialled from here.
"""
import uuid
from typing import Any

import httpx

from app.core.config import get_settings
from app.db.database import connection, decode_row, insert, now
from app.services.osm import OsmUnavailable, fetch
from app.services.routing import haversine_km

settings = get_settings()
LABEL = {"forest_office": "Forest office", "police_station": "Police station", "fire_station": "Fire station"}
ICON = {"forest_office": "🌲", "police_station": "👮", "fire_station": "🚒"}


def store(kind: str, name: str, lat: float, lon: float, source: str, ext: str, phone: str | None = None, note: str | None = None) -> bool:
    """Save a responder unless the same one (same source id, or the same kind within 150 m) is already known."""
    with connection() as conn:
        if conn.execute("SELECT 1 FROM stations WHERE source = ? AND external_ref = ?", (source, ext)).fetchone():
            return False
        for r in conn.execute("SELECT location FROM stations WHERE kind = ?", (kind,)):
            if haversine_km([lon, lat], decode_row({"location": r[0]})["location"]["coordinates"]) < 0.15:
                return False
    insert("stations", {"id": str(uuid.uuid4()), "name": name[:160], "jurisdiction": "unknown", "location": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]}, "coverage": None, "contacts": {"phone": phone} if phone else {}, "availability_note": note,
                        "created_at": now(), "kind": kind, "source": source, "verified": 0, "external_ref": ext})
    return True


def nearest(kind: str, lat: float, lon: float, limit: int = 3, max_km: float = 100.0) -> list[tuple[float, dict[str, Any]]]:
    with connection() as conn:
        rows = [decode_row(dict(r)) for r in conn.execute("SELECT * FROM stations WHERE kind = ?", (kind,))]
    scored = sorted(((haversine_km([lon, lat], r["location"]["coordinates"]), r) for r in rows if r["location"].get("type") == "Point"), key=lambda x: x[0])
    return [(d, r) for d, r in scored if d <= max_km][:limit]


def near(kind: str, lat: float, lon: float, km: float) -> bool:
    return bool(nearest(kind, lat, lon, 1, km))


def public(row: dict[str, Any], d: float | None = None, kind: str | None = None) -> dict[str, Any]:
    kind = kind or row.get("kind") or "fire_station"
    c = row["location"]["coordinates"]
    phone = (row.get("contacts") or {}).get("phone")
    return {"id": row["id"], "name": row["name"], "kind": kind, "kind_label": LABEL.get(kind, kind), "icon": ICON.get(kind, "📍"), "lat": c[1], "lon": c[0], "source": row.get("source"), "verified": bool(row.get("verified")),
            "straight_line_km": None if d is None else round(d, 1), "phone": ("…" + str(phone)[-3:]) if phone else None, "data_note": "Unverified public mapping data"}


def discover_police(lat: float, lon: float, radius_km: int = 40) -> dict[str, Any]:
    """Look up police stations near a point (Google Maps first, OpenStreetMap only if none is known close by)."""
    report: dict[str, Any] = {}
    added = 0
    if settings.serp_api:
        try:
            n = seen = 0
            r = httpx.get("https://serpapi.com/search.json", params={"engine": "google_maps", "q": "police station", "ll": f"@{lat},{lon},12z", "type": "search", "api_key": settings.serp_api}, timeout=60).json()
            for x in r.get("local_results") or []:
                gps = x.get("gps_coordinates")
                if gps and "police" in ((x.get("title") or "") + " " + (x.get("type") or "")).lower():
                    seen += 1
                    n += int(store("police_station", x["title"], gps["latitude"], gps["longitude"], "serpapi", x.get("place_id") or f"{gps['latitude']},{gps['longitude']}", x.get("phone")))
            added += n
            report["serpapi"] = {"status": "ok", "found": seen, "new": n}
        except Exception as exc:  # noqa: BLE001
            report["serpapi"] = {"status": "failed", "error": type(exc).__name__}
    else:
        report["serpapi"] = {"status": "skipped", "reason": "SERP_API not configured"}
    if not near("police_station", lat, lon, 15):
        try:
            els = fetch(f'[out:json][timeout:25];nwr["amenity"="police"](around:{radius_km * 1000},{lat},{lon});out center tags 40;')
            n = 0
            for e in els:
                la, lo = e.get("lat") or e.get("center", {}).get("lat"), e.get("lon") or e.get("center", {}).get("lon")
                t = e.get("tags", {})
                if la is not None:
                    n += int(store("police_station", t.get("name") or "Police station (unnamed)", la, lo, "osm", f"{e['type']}/{e['id']}", t.get("phone") or t.get("contact:phone")))
            added += n
            report["osm"] = {"status": "ok", "found": len(els), "new": n}
        except OsmUnavailable as exc:
            report["osm"] = {"status": "failed", "error": str(exc)}
    return {"added": added, "sources": report}
