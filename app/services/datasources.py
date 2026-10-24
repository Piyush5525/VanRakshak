"""Data-source health: cheap live checks, cached for 5 minutes. Never returns keys."""
import time
from typing import Any

import httpx

from app.core.config import Settings
from app.db.database import connection

_cache: dict[str, Any] = {"at": 0.0, "value": None}


def _timed(fn):
    t = time.perf_counter()
    try:
        ok, detail = fn()
        if not ok:
            detail = "check failed (non-200 response); dependent features may be degraded"
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"{type(exc).__name__}"
    return {"reachable": ok, "detail": detail, "latency_ms": round((time.perf_counter() - t) * 1000)}


def check(settings: Settings, force: bool = False) -> dict[str, Any]:
    if not force and _cache["value"] and time.time() - _cache["at"] < 300:
        return {**_cache["value"], "cached": True}
    c = httpx.Client(timeout=15)
    src: list[dict[str, Any]] = []

    def add(name: str, purpose: str, configured: bool, fn=None, note: str | None = None):
        row = {"name": name, "purpose": purpose, "configured": configured}
        row.update(_timed(fn) if (fn and configured) else {"reachable": None, "detail": note or ("not configured" if not configured else "not checked")})
        src.append(row)

    add("Microsoft Planetary Computer (Sentinel-1, Sentinel-2, WorldCover, DEM)", "satellite data", True, lambda: (c.get("https://planetarycomputer.microsoft.com/api/stac/v1/collections/sentinel-2-l2a").status_code == 200, "STAC collection reachable"))
    add("NASA FIRMS", "active fire", bool(settings.firms_api_key), lambda: (c.get("https://firms.modaps.eosdis.nasa.gov/mapserver/mapkey_status/", params={"MAP_KEY": settings.firms_api_key}).status_code == 200, "key accepted by status endpoint"))
    add("Open-Meteo", "rainfall, air quality", True, lambda: (c.get("https://api.open-meteo.com/v1/forecast", params={"latitude": 11.6, "longitude": 76.0, "daily": "precipitation_sum", "forecast_days": 1}).status_code == 200, "API reachable"))
    add("Mapbox Directions", "routing (primary)", bool(settings.mapbox_api), lambda: (c.get("https://api.mapbox.com/directions/v5/mapbox/driving/76.08,11.60;76.07,11.61", params={"access_token": settings.mapbox_api}).status_code == 200, "token accepted"))
    add("SerpAPI", "routing fallback, web leads", bool(settings.serp_api), lambda: (c.get("https://serpapi.com/account.json", params={"api_key": settings.serp_api}).status_code == 200, "key accepted (account endpoint, no search used)"))
    add("OpenStreetMap Overpass", "roads, waterways, protected areas", True, lambda: (c.get("https://overpass.openstreetmap.fr/api/status").status_code == 200, "mirror status endpoint reachable"))
    add("OpenStreetMap tiles", "basemap", True, lambda: (c.get("https://tile.openstreetmap.org/0/0/0.png").status_code == 200, "tile server reachable"))
    add("Telegram Bot API", "notifications", bool(settings.telegram_bot_token), lambda: (c.get(f"https://api.telegram.org/bot{settings.telegram_bot_token}/getMe").status_code == 200, "bot token valid (no message sent)"))
    add("Twilio voice", "voice alerts", settings.voice_provider == "twilio" and bool(settings.twilio_account_sid), None, "not checked (would place nothing, but needs account call)")
    add("Email (SMTP)", "email alerts", bool(settings.smtp_host), None, "not checked")
    add("Fire/smoke vision model", "camera detection", bool(settings.fire_model_path), None, "see /wildfires/vision/status")
    with connection() as conn:
        last_obs = conn.execute("SELECT MAX(created_at) FROM observations WHERE status='OK'").fetchone()[0]
        last_fire = conn.execute("SELECT MAX(created_at) FROM wildfire_observations").fetchone()[0]
        last_change = conn.execute("SELECT MAX(created_at) FROM changes").fetchone()[0]
    value = {"checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sources": src, "last_updates": {"satellite_composite": last_obs, "fire_ingest": last_fire, "change_detection": last_change}, "cached": False}
    _cache.update(at=time.time(), value=value)
    return value
