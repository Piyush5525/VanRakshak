"""Live map backend: current NASA FIRMS fires for India, ambient air quality, and user-entered site/emission details.

Nothing here is stored except what a person enters on an incident. Fire data is fetched live and cached briefly.
"""
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings
from app.core.security import Role, require_roles
from app.db.database import audit, get_one, now, update
from app.services import boundary, fireops
from app.services.wildfire import FirmsUnavailable, fetch_firms

settings = get_settings()
api = APIRouter(prefix=settings.api_prefix)
READ = (Role.ADMIN, Role.REVIEWER, Role.OFFICER, Role.ANALYST, Role.READ_ONLY)

INDIA_BBOX = (68.1, 6.5, 97.5, 37.1)  # west, south, east, north (a bounding box: includes parts of neighbouring countries)
SOURCES = {"VIIRS_SNPP_NRT": "VIIRS S-NPP (375 m)", "VIIRS_NOAA20_NRT": "VIIRS NOAA-20 (375 m)", "VIIRS_NOAA21_NRT": "VIIRS NOAA-21 (375 m)", "MODIS_NRT": "MODIS (1 km)"}
CACHE_SECONDS = 600
_cache: dict[tuple[str, int, bool], dict[str, Any]] = {}
_lock = threading.Lock()
_air_cache: dict[tuple[float, float], tuple[float, dict]] = {}


@api.get("/live/fires")
def live_fires(days: int = Query(1, ge=1, le=3), source: str = "VIIRS_SNPP_NRT", force: bool = False, india_only: bool = True, role: Role = Depends(require_roles(*READ))):
    """Current thermal anomalies over India from NASA FIRMS (cached 10 minutes; each call to FIRMS counts against a quota)."""
    if source not in SOURCES:
        raise HTTPException(status_code=422, detail=f"source must be one of {sorted(SOURCES)}")
    key = (source, days, india_only)
    with _lock:
        hit = _cache.get(key)
        if hit and not force and time.time() - hit["at"] < CACHE_SECONDS:
            return {**hit["body"], "cached": True}
    try:
        rows = fetch_firms(settings, INDIA_BBOX, source, days)
    except FirmsUnavailable as exc:
        fireops.log_event(None, "feed_refresh_failed", "nasa_firms", "failed", f"NASA FIRMS refresh failed ({source}, {days} d): {exc}", error=str(exc))
        if hit:  # serve the last good copy rather than nothing, and say so
            return {**hit["body"], "cached": True, "stale": True, "warning": f"Refresh failed ({exc}); showing data fetched {hit['body']['fetched_at']}"}
        raise HTTPException(status_code=503, detail=str(exc))
    inside_note = "India bounding box; includes parts of neighbouring countries"
    total = len(rows)
    if india_only:
        try:
            rows = [r for r in rows if boundary.contains(r["lat"], r["lon"])]
            inside_note = f"Only hotspots inside the India outline ({boundary.SOURCE}); {total - len(rows)} outside it were hidden"
        except RuntimeError as exc:
            inside_note = f"India outline unavailable ({exc}); showing the whole bounding box"
    body = {
        "source": source, "source_name": SOURCES[source], "days": days, "fetched_at": datetime.now(timezone.utc).isoformat(), "count": len(rows),
        "bbox": INDIA_BBOX, "bbox_note": inside_note, "total_in_bbox": total,
        "attribution": "NASA FIRMS (LANCE). Thermal anomalies, not confirmed wildfires: agricultural burning, kilns, flares and industrial heat also appear.",
        "fires": [[round(r["lat"], 4), round(r["lon"], 4), int(r["observed_at"].timestamp()), r["confidence"], r["frp_mw"], (r["satellite"] or "")[:3], r["daynight"]] for r in rows],
        "fields": ["lat", "lon", "observed_epoch_s", "confidence_0_1", "frp_mw", "satellite", "day_night"],
        "cached": False,
    }
    with _lock:
        _cache[key] = {"at": time.time(), "body": body}
    fireops.log_event(None, "feed_refreshed", "nasa_firms", "ok", f"NASA FIRMS feed refreshed: {body['count']} hotspots in India ({SOURCES[source]}, last {days * 24} h)", details={"source": source, "days": days, "count": body["count"]})
    return body


@api.get("/live/boundary")
def live_boundary(role: Role = Depends(require_roles(*READ))):
    try:
        return boundary.geojson()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@api.get("/live/air")
def live_air(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180), role: Role = Depends(require_roles(*READ))):
    """Ambient modelled air quality at a point (Open-Meteo / Copernicus CAMS). Not an emission measurement."""
    k = (round(lat, 2), round(lon, 2))
    hit = _air_cache.get(k)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    try:
        r = httpx.get("https://air-quality-api.open-meteo.com/v1/air-quality", params={"latitude": k[0], "longitude": k[1], "hourly": "pm2_5,pm10,nitrogen_dioxide,sulphur_dioxide", "past_days": 1, "forecast_days": 1, "timezone": "UTC"}, timeout=25)
        r.raise_for_status()
        h = r.json()["hourly"]
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=503, detail=f"Air-quality service unavailable: {type(exc).__name__}")
    nowh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")
    idx = h["time"].index(nowh) if nowh in h["time"] else max(0, len(h["time"]) // 2)

    def stat(name):
        vals = [v for v in h[name] if v is not None]
        return {"now": h[name][idx], "max_24h": round(max(vals), 1) if vals else None, "unit": "ug/m3"}

    body = {"claim_type": "observation_third_party_model", "kind": "ambient modelled air quality, NOT facility emissions", "as_of": h["time"][idx] + "Z", "pm2_5": stat("pm2_5"), "pm10": stat("pm10"), "no2": stat("nitrogen_dioxide"), "so2": stat("sulphur_dioxide"), "attribution": "Air quality data by Open-Meteo.com, Copernicus CAMS (CC BY 4.0). Model output at about 11 km; local concentrations differ."}
    _air_cache[k] = (time.time(), body)
    return body


class Emission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pollutant: str = Field(min_length=1, max_length=60)
    value: float = Field(ge=0, le=1e9)
    unit: str = Field(min_length=1, max_length=30)
    source: str = Field(min_length=2, max_length=200, description="Where this figure comes from (permit, monitoring report, operator disclosure...)")
    as_of: str | None = Field(default=None, max_length=30)


class SiteInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=160)
    kind: str = Field(default="factory", max_length=40)
    note: str | None = Field(default=None, max_length=1000)
    emissions: list[Emission] = Field(default_factory=list, max_length=20)


@api.post("/incidents/{incident_id}/site")
def set_site(incident_id: str, payload: SiteInfo, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.ANALYST))):
    """Attach an industrial site and any reported emission figures. Stored exactly as entered and labelled unverified."""
    inc = get_one("incidents", "id", incident_id)
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")
    site = {**payload.model_dump(), "claim_type": "user_supplied_unverified", "entered_by_role": role.value, "entered_at": now()}
    update("incidents", "id", incident_id, {"evidence": {**(inc["evidence"] or {}), "site": site}, "updated_at": now()})
    audit(role.value, "incident_site_recorded", "incident", incident_id, {"site": payload.name, "emission_figures": len(payload.emissions)})
    return site


@api.delete("/incidents/{incident_id}/site")
def clear_site(incident_id: str, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER))):
    inc = get_one("incidents", "id", incident_id)
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")
    ev = {k: v for k, v in (inc["evidence"] or {}).items() if k != "site"}
    update("incidents", "id", incident_id, {"evidence": ev, "updated_at": now()})
    audit(role.value, "incident_site_removed", "incident", incident_id, {})
    return {"removed": True}
