"""Fire-station registry (OpenStreetMap + SerpAPI Google Maps, both UNVERIFIED) and road-network routing to an incident.

Station facts come from public mapping data. Nothing here is an authoritative emergency-services register;
phone numbers are stored only if a source provided one and are never used to place a call automatically.
Straight-line distance is only used to shortlist candidates; every reported distance/time is road-network routing.
"""
import math
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import get_settings
from app.db.database import connection, decode_row, get_one, insert, now
from app.services import fireops, navigation
from app.services.osm import OsmUnavailable, fetch
from app.services.routing import haversine_km

settings = get_settings()


def _mask(contacts: dict[str, Any] | None) -> dict[str, Any]:
    return {k: ("***" + str(v)[-3:] if k == "phone" and v else "set") for k, v in (contacts or {}).items()}


def public(row: dict[str, Any], dist_km: float | None = None) -> dict[str, Any]:
    c = row["location"]["coordinates"]
    return {"id": row["id"], "name": row["name"], "lat": c[1], "lon": c[0], "kind": row.get("kind"), "source": row.get("source"), "verified": bool(row.get("verified")), "availability": row.get("availability_note") or "Availability not known", "contacts": _mask(row.get("contacts")), "straight_line_km": None if dist_km is None else round(dist_km, 1), "data_note": "Unverified public mapping data" if not row.get("verified") else "Verified record"}


def _store(name: str, lat: float, lon: float, source: str, ext: str, phone: str | None, note: str | None) -> bool:
    with connection() as conn:
        hit = conn.execute("SELECT 1 FROM stations WHERE source = ? AND external_ref = ?", (source, ext)).fetchone()
        if hit:
            return False
        # skip a near-duplicate (same place already stored by another source)
        for r in conn.execute("SELECT location FROM stations WHERE kind = 'fire_station'"):
            loc = decode_row({"location": r[0]})["location"]["coordinates"]
            if haversine_km([lon, lat], loc) < 0.15:
                return False
    insert("stations", {"id": str(uuid.uuid4()), "name": name[:160], "jurisdiction": "unknown", "location": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]}, "coverage": None, "contacts": {"phone": phone} if phone else {}, "availability_note": note, "created_at": now(), "kind": "fire_station", "source": source, "verified": 0, "external_ref": ext})
    return True


def discover(lat: float, lon: float, radius_km: int = 60, incident_id: str | None = None, demo: bool = False) -> dict[str, Any]:
    """Look up stations near a point from public sources and store them (unverified). Failures are reported, not hidden."""
    added, report = 0, {}
    try:
        els = fetch(f'[out:json][timeout:25];nwr["amenity"="fire_station"](around:{radius_km * 1000},{lat},{lon});out center tags 40;')
        n = 0
        for e in els:
            la, lo = e.get("lat") or e.get("center", {}).get("lat"), e.get("lon") or e.get("center", {}).get("lon")
            t = e.get("tags", {})
            if la is None:
                continue
            n += int(_store(t.get("name") or t.get("operator") or "Fire station (unnamed)", la, lo, "osm", f"{e['type']}/{e['id']}", t.get("phone") or t.get("contact:phone"), t.get("opening_hours")))
        added += n; report["osm"] = {"status": "ok", "found": len(els), "new": n}
    except OsmUnavailable as exc:
        report["osm"] = {"status": "failed", "error": str(exc)}
    if settings.serp_api:
        try:
            r = httpx.get("https://serpapi.com/search.json", params={"engine": "google_maps", "q": "fire station", "ll": f"@{lat},{lon},11z", "type": "search", "api_key": settings.serp_api}, timeout=60).json()
            if "error" in r and not r.get("local_results"):
                raise RuntimeError(str(r["error"])[:120])
            n, seen = 0, 0
            for x in r.get("local_results") or []:
                title = (x.get("title") or "")
                types = " ".join(x.get("types") or []) + " " + (x.get("type") or "")
                gps = x.get("gps_coordinates") or {}
                if "fire" not in (title + " " + types).lower() or "police" in title.lower() or not gps:
                    continue
                seen += 1
                n += int(_store(title, gps["latitude"], gps["longitude"], "serpapi", x.get("place_id") or f"{gps['latitude']},{gps['longitude']}", x.get("phone"), x.get("hours") if isinstance(x.get("hours"), str) else None))
            added += n; report["serpapi"] = {"status": "ok", "found": seen, "new": n}
        except Exception as exc:  # noqa: BLE001
            report["serpapi"] = {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    else:
        report["serpapi"] = {"status": "skipped", "reason": "SERP_API not configured"}
    ok = any(v.get("status") == "ok" for v in report.values())
    fireops.log_event(incident_id, "stations_discovered", "station_registry", "ok" if ok else "failed", f"Station lookup: {added} new from {', '.join(k for k, v in report.items() if v.get('status') == 'ok') or 'no source'}" + ("" if ok else " (all sources failed)"), error=None if ok else str({k: v.get('error') for k, v in report.items()}), is_demo=demo, details=report)
    return {"added": added, "sources": report}


def nearby(lat: float, lon: float, limit: int = 6, max_km: float = 200) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = [decode_row(dict(r)) for r in conn.execute("SELECT * FROM stations WHERE kind = 'fire_station' OR kind IS NULL")]
    scored = [(haversine_km([lon, lat], r["location"]["coordinates"]), r) for r in rows if r["location"].get("type") == "Point"]
    return [(d, r) for d, r in sorted(scored, key=lambda x: x[0]) if d <= max_km][:limit]


def _polyline_decimate(coords: list[list[float]], max_pts: int = 400) -> list[list[float]]:
    if len(coords) <= max_pts:
        return coords
    step = len(coords) / max_pts
    return [coords[int(i * step)] for i in range(max_pts)] + [coords[-1]]


def compute_route(incident_id: str, router, station_id: str | None = None, max_candidates: int = 4) -> dict[str, Any]:
    """Road-network route(s) from candidate stations to the incident; selects the least estimated driving time."""
    inc = fireops.get(incident_id)
    demo = inc["is_demo"]
    lat, lon = inc["lat"], inc["lon"]
    t0 = datetime.now(timezone.utc)
    if station_id:
        row = get_one("stations", "id", station_id)
        if not row:
            raise fireops.WorkflowError("Station not found")
        cands = [(haversine_km([lon, lat], row["location"]["coordinates"]), row)]
    else:
        cands = nearby(lat, lon, limit=max_candidates)
    if not cands:
        res = {"status": "NO_STATIONS", "reason": "No fire station is registered near this location. Run station lookup or add one manually.", "calculated_at": t0.isoformat()}
        fireops.patch(incident_id, route=res)
        fireops.log_event(incident_id, "route_failed", "routing", "failed", res["reason"], error=res["reason"], is_demo=demo)
        return res
    if not router.available:
        res = {"status": "ROUTING_UNAVAILABLE", "reason": "No routing provider configured (ROUTING_PROVIDER, MAPBOX_API / SERP_API)", "calculated_at": t0.isoformat()}
        fireops.patch(incident_id, route=res)
        fireops.log_event(incident_id, "route_failed", "routing", "failed", res["reason"], error=res["reason"], is_demo=demo)
        return res
    fireops.log_event(incident_id, "stations_evaluated", "routing", "ok", f"Evaluating {len(cands)} candidate station(s) by road network", is_demo=demo, details={"candidates": [{"name": r["name"], "straight_line_km": round(d, 1)} for d, r in cands]})
    hazards = navigation.active_hazards()
    evaluated: list[dict[str, Any]] = []
    for d, st in cands:
        origin = st["location"]["coordinates"]
        rt = router.route(origin, [lon, lat], True)
        entry = {"station_id": st["id"], "station_name": st["name"], "straight_line_km": round(d, 1), "status": rt["status"]}
        if rt["status"] != "ok":
            entry["error"] = rt.get("reason") or str(rt.get("details"))
            fireops.log_event(incident_id, "route_candidate_failed", "routing", "failed", f"No route from {st['name']}: {entry['error']}"[:300], error=entry["error"], is_demo=demo)
            evaluated.append(entry)
            continue
        ev = navigation.evaluate_routes([rt["primary"], *rt.get("alternatives", [])], hazards)
        if ev["recommended_index"] is None:
            entry.update(status="BLOCKED", error=ev["message"])
            evaluated.append(entry)
            continue
        best = ev["routes"][ev["recommended_index"]]
        entry.update(provider=rt["provider"], distance_km=best["distance_km"], duration_min=best["duration_min"], adjusted_duration_min=best["adjusted_duration_min"], geometry=best.get("geometry"), segments=best.get("segments"), drive_km=best.get("drive_km"), walk_km=best.get("walk_km"), drive_min=best.get("drive_min"), walk_min=best.get("walk_min"), steps=(best.get("steps") or [])[:25], reasons=best["reasons"], recommendation=best["recommendation"], alternatives=[{"distance_km": r["distance_km"], "duration_min": r["duration_min"], "status": r["status"], "geometry": _poly_small(r.get("geometry"))} for r in ev["routes"] if r["index"] != ev["recommended_index"]], fallback_note=rt.get("fallback_note"))
        evaluated.append(entry)
    usable = [e for e in evaluated if e.get("distance_km") is not None]
    if not usable:
        res = {"status": "FAILED", "reason": "No road route could be calculated from any candidate station", "candidates": evaluated, "calculated_at": t0.isoformat()}
        fireops.patch(incident_id, route=res)
        fireops.log_event(incident_id, "route_failed", "routing", "failed", res["reason"], error=str(evaluated)[:400], is_demo=demo)
        return res
    best = min(usable, key=lambda e: (e.get("adjusted_duration_min") or e["duration_min"]))
    st = get_one("stations", "id", best["station_id"])
    for e in evaluated:
        e["selected"] = e["station_id"] == best["station_id"]
        if e["station_id"] != best["station_id"]:
            e.pop("geometry", None); e.pop("steps", None)
    best_geom = _poly_small(best["geometry"], 600)
    res = {"status": "OK", "objective": "minimum estimated driving time (road network)", "selected_manually": bool(station_id),
           "station": public(st, best["straight_line_km"]), "provider": best["provider"], "distance_km": best["distance_km"], "duration_min": best["duration_min"], "adjusted_duration_min": best["adjusted_duration_min"],
           "geometry": best_geom, "segments": _segments_small(best.get("segments")), "drive_km": best.get("drive_km"), "walk_km": best.get("walk_km"), "drive_min": best.get("drive_min"), "walk_min": best.get("walk_min"), "steps": best["steps"], "reasons": best["reasons"], "alternatives": best["alternatives"], "candidates": _merge_candidates([{k: v for k, v in e.items() if k not in ("geometry", "steps", "alternatives", "reasons", "segments")} for e in evaluated], (inc.get("route") or {}).get("candidates") if station_id else None),
           "calculated_at": t0.isoformat(), "duration_s": round((datetime.now(timezone.utc) - t0).total_seconds(), 1),
           "navigation_url": f"https://www.google.com/maps/dir/?api=1&origin={st['location']['coordinates'][1]},{st['location']['coordinates'][0]}&destination={lat},{lon}&travelmode=driving", "walk_url": _walk_url(best.get("segments"), lat, lon),
           "caveats": ["Road-network estimate only; typical driving time, not live traffic.", "Road closures, vehicle restrictions, terrain access, flooding and station availability are NOT verified.", "Forest tracks near the fire may be missing from road data; the final approach may be off-network.", "The station is unverified public mapping data. No dispatch has occurred."]}
    fireops.patch(incident_id, station=res["station"], route=res)
    fireops.log_event(incident_id, "route_calculated", "routing", "ok", f"Route calculated: {st['name']} -> incident, {res['distance_km']} km, ~{round(res['duration_min'])} min by road ({res['provider']})", ref=best["station_id"], is_demo=demo, details={"selected_manually": bool(station_id)})
    return res


def _merge_candidates(fresh: list[dict[str, Any]], previous: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Choosing one station must not hide the others: keep the earlier comparison and update only the stations just routed."""
    if not previous:
        return fresh
    by_id = {c["station_id"]: c for c in previous}
    for c in fresh:
        by_id[c["station_id"]] = c
    for c in by_id.values():
        c["selected"] = any(f["station_id"] == c["station_id"] and f.get("selected") for f in fresh)
    order = [c["station_id"] for c in previous] + [c["station_id"] for c in fresh if c["station_id"] not in {x["station_id"] for x in previous}]
    return [by_id[i] for i in order]


def _segments_small(segs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [{**{k: v for k, v in s.items() if k != "geometry"}, "geometry": _poly_small(s.get("geometry"), 500)} for s in (segs or [])]


def _walk_url(segs: list[dict[str, Any]] | None, lat: float, lon: float) -> str | None:
    walk = next((s for s in (segs or []) if s["mode"] == "walk" and s.get("geometry")), None)
    if not walk:
        return None
    a = walk["geometry"]["coordinates"][0]
    return f"https://www.google.com/maps/dir/?api=1&origin={a[1]},{a[0]}&destination={lat},{lon}&travelmode=walking"


def _poly_small(geom: dict | None, max_pts: int = 200) -> dict | None:
    if not geom:
        return None
    return {"type": "LineString", "coordinates": _polyline_decimate(geom["coordinates"], max_pts)}
