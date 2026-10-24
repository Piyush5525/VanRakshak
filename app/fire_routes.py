"""Fire-response API: one incident record drives forecast, impact, station/route, Telegram, IVR and the event log."""
import json
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app import live_routes
from app.core.config import get_settings
from app.core.security import Role, require_roles
from app.db.database import audit, connection, get_one, now
from app.ops_routes import router_service
from app.services import fireimpact, fireivr, fireops, firecomms, firestations, mapimage
from app.services.firecomms import OUT_DIR
from app.services.jobs import JobManager
from app.services.notify import verify_shared_secret, verify_twilio_signature

settings = get_settings()
api = APIRouter(prefix=settings.api_prefix + "/fire")
jobs = JobManager(settings.job_workers)
READ = (Role.ADMIN, Role.REVIEWER, Role.OFFICER, Role.ANALYST, Role.READ_ONLY)
CREATE = (Role.ADMIN, Role.REVIEWER, Role.OFFICER)
DISPATCH = (Role.ADMIN, Role.OFFICER)
DEMO_KEY = "fire_demo_config"
DEMO_DEFAULT = {"lat": 21.95, "lon": 78.05, "frp_mw": 38.0, "use_live_weather": False, "weather": {"wind_kmh": 24.0, "wind_from_deg": 270.0, "temp_c": 36.0, "rh": 22.0, "rain_24h_mm": 0.0, "rain_7d_mm": 0.0}, "note": "Illustrative dry, windy conditions in a forested area of central India. Everything about this incident is synthetic."}
_demo = {"running": False, "stop": False}


class M(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _inc(iid: str) -> dict[str, Any]:
    i = fireops.get(iid)
    if not i:
        raise HTTPException(status_code=404, detail="Fire incident not found")
    return i


def _job(job_id: str, **extra) -> dict[str, Any]:
    return {"job_id": job_id, "status": "queued", **extra}


def _wf(fn, *a, **k):
    try:
        return fn(*a, **k)
    except fireops.WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


def _count_stations() -> int:
    with connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM stations WHERE kind = 'fire_station'").fetchone()[0]


def public_incident(i: dict[str, Any]) -> dict[str, Any]:
    out = dict(i)
    n = out.get("notify") or {}
    out["notify"] = {**n, "telegram": [{k: v for k, v in m.items() if k != "preview"} | ({"preview": m["preview"]} if m.get("preview") else {}) for m in n.get("telegram", [])]}
    return out


# ---------------------------------------------------------------- status / events
@api.get("/status")
def fire_status(probe: bool = False, role: Role = Depends(require_roles(*READ))):
    feed = fireops.events(services=["nasa_firms"], limit=1)
    cfg = firecomms.configured()
    tg = firecomms.status() if probe else {**cfg, "chat_masked": ("…" + str(settings.telegram_chat_id)[-3:]) if settings.telegram_chat_id else None, "ready_to_send": cfg["token"] and cfg["chat_id"], "inbound_buttons_enabled": settings.telegram_polling, "probed": False}
    return {"firms": {"configured": bool(settings.firms_api_key), "last_feed_event": feed[-1] if feed else None}, "telegram": tg, "ivr": fireivr.status(), "routing": router_service.status(),
            "demo": {"running": _demo["running"]}, "stations_registered": _count_stations(),
            "forecast_model": {"name": "McArthur Mk5 + Anderson ellipse + Monte-Carlo", "status": "prototype, not validated", "horizons_h": [3, 6, 12]}}


@api.get("/events")
def fire_events(since_id: int = 0, incident_id: str | None = None, types: str | None = None, services: str | None = None, statuses: str | None = None, demo: bool | None = None, limit: int = Query(200, ge=1, le=500), role: Role = Depends(require_roles(*READ))):
    evs = fireops.events(incident_id, since_id, types.split(",") if types else None, services.split(",") if services else None, statuses.split(",") if statuses else None, demo, limit)
    return {"events": evs, "last_id": evs[-1]["id"] if evs else since_id}


# ---------------------------------------------------------------- incidents
class HotspotIn(M):
    source: Literal["firms", "manual"] = "firms"
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    observed_epoch_s: int | None = None
    note: str | None = Field(default=None, max_length=500)
    firms_source: str = "VIIRS_SNPP_NRT"


def _match_cached_hotspot(lat: float, lon: float, epoch: int | None) -> tuple[list, dict] | None:
    for (src, days, _), hit in live_routes._cache.items():
        for f in hit["body"]["fires"]:
            if abs(f[0] - lat) < 0.002 and abs(f[1] - lon) < 0.002 and (epoch is None or f[2] == epoch):
                return f, hit["body"]
    return None


def _currency(age_h: float) -> str:
    return "current" if age_h <= 3 else "delayed" if age_h <= 24 else "historical"


@api.post("/incidents")
def create_incident(payload: HotspotIn, role: Role = Depends(require_roles(*CREATE))):
    if payload.source == "firms":
        hit = _match_cached_hotspot(payload.lat, payload.lon, payload.observed_epoch_s)
        if not hit:
            raise HTTPException(status_code=422, detail="This is not a hotspot from the current NASA FIRMS feed (refresh the live map and select a hotspot). Use source=manual to record a human report instead.")
        f, body = hit
        obs = datetime.fromtimestamp(f[2], timezone.utc)
        age = (datetime.now(timezone.utc) - obs).total_seconds() / 3600
        near = []
        for g in body["fires"]:
            if abs(g[0] - f[0]) < 0.05 and abs(g[1] - f[1]) < 0.05:
                near.append({"lat": g[0], "lon": g[1], "observed_at": datetime.fromtimestamp(g[2], timezone.utc).isoformat(), "frp_mw": g[4]})
        det = {"source": "NASA FIRMS", "product": body["source"], "product_name": body["source_name"], "satellite": f[5], "daynight": f[6], "confidence": f[3], "frp_mw": f[4], "observed_at": obs.isoformat(), "age_hours_at_creation": round(age, 1), "currency": _currency(age), "feed_fetched_at": body["fetched_at"], "hotspot_history": sorted(near, key=lambda x: x["observed_at"])[-12:], "note": "Satellite thermal anomaly; not a confirmed fire. Agricultural burning, kilns and industrial heat also appear."}
        inc, created = fireops.create("nasa_firms", f[0], f[1], obs.isoformat(), det, role.value, source_detection_id=f"{body['source']}:{f[2]}:{f[0]}:{f[1]}")
    else:
        det = {"source": "manual report", "note": payload.note or "Reported by a person; no satellite detection attached", "currency": "n/a"}
        inc, created = fireops.create("manual", payload.lat, payload.lon, now(), det, role.value)
    return {"incident": public_incident(inc), "created": created}


@api.get("/incidents")
def list_incidents(include_demo: bool = True, role: Role = Depends(require_roles(*READ))):
    return [public_incident(i) for i in fireops.list_incidents(include_demo)]


@api.get("/incidents/{iid}")
def get_incident(iid: str, role: Role = Depends(require_roles(*READ))):
    _inc(iid)
    try:
        fireivr.refresh_calls(iid)
    except Exception:  # noqa: BLE001 - a provider hiccup must not break the incident view
        pass
    return public_incident(_inc(iid))


class VerifyIn(M):
    decision: Literal["confirm", "reject", "inconclusive", "needs_verification"]
    note: str = Field(default="", max_length=1000)


@api.post("/incidents/{iid}/verify")
def verify(iid: str, payload: VerifyIn, role: Role = Depends(require_roles(*CREATE))):
    inc = _inc(iid)
    target = {"confirm": "CONFIRMED", "reject": "REJECTED", "inconclusive": "INCONCLUSIVE", "needs_verification": "NEEDS_VERIFICATION"}[payload.decision]
    v = inc["verification"] or {"history": []}
    v["history"].append({"decision": payload.decision, "note": payload.note, "by_role": role.value, "at": now()})
    fireops.patch(iid, verification=v)
    return public_incident(_wf(fireops.transition, iid, target, role.value, payload.note))


@api.post("/incidents/{iid}/forecast")
def run_forecast(iid: str, role: Role = Depends(require_roles(*CREATE))):
    inc = _inc(iid)
    spec = None
    if inc["is_demo"]:
        cfg = _demo_config()
        spec = None if cfg["use_live_weather"] else cfg["weather"]
        if (inc["detection"] or {}).get("scenario"):
            spec = inc["detection"]["scenario"]["weather"]
    from app.services import firesvc

    return _job(jobs.submit("fire_forecast", lambda: firesvc.run_forecast(iid, role.value, spec)))


class ImpactIn(M):
    burned_area_ha: float | None = Field(default=None, gt=0, le=1e7)
    fuel_load_t_ha: float | None = Field(default=None, gt=0, le=500)


@api.post("/incidents/{iid}/impact")
def run_impact(iid: str, payload: ImpactIn | None = None, role: Role = Depends(require_roles(*CREATE))):
    _inc(iid)
    p = payload or ImpactIn()
    if (p.burned_area_ha is None) != (p.fuel_load_t_ha is None):
        raise HTTPException(status_code=422, detail="Provide both burned_area_ha and fuel_load_t_ha, or neither")
    return _job(jobs.submit("fire_impact", lambda: fireimpact.build(iid, p.burned_area_ha, p.fuel_load_t_ha)))


@api.post("/incidents/{iid}/stations/discover")
def discover_stations(iid: str, radius_km: int = Query(60, ge=10, le=150), role: Role = Depends(require_roles(*CREATE))):
    inc = _inc(iid)
    return _job(jobs.submit("fire_stations", lambda: firestations.discover(inc["lat"], inc["lon"], radius_km, iid, inc["is_demo"])))


class RouteIn(M):
    station_id: str | None = None


@api.post("/incidents/{iid}/route")
def run_route(iid: str, payload: RouteIn | None = None, role: Role = Depends(require_roles(*CREATE))):
    _inc(iid)
    sid = payload.station_id if payload else None
    return _job(jobs.submit("fire_route", lambda: firestations.compute_route(iid, router_service, sid)))


class NoteIn(M):
    note: str = Field(default="", max_length=1000)


@api.post("/incidents/{iid}/authorise")
def authorise(iid: str, payload: NoteIn, role: Role = Depends(require_roles(*DISPATCH))):
    inc = _inc(iid)
    if inc["status"] not in ("CONFIRMED", "FAILED", "MONITORING"):
        raise HTTPException(status_code=409, detail=f"Only a confirmed incident can be authorised for response (currently {inc['status_label']})")
    out = _wf(fireops.transition, iid, "DISPATCH_REQUESTED", role.value, payload.note or "authorised for response notification")
    fireops.log_event(iid, "dispatch_authorised", "workflow", "ok", f"Response notification authorised by {role.value}. No station has been contacted yet.", is_demo=inc["is_demo"])
    return public_incident(out)


class TelegramIn(M):
    mode: Literal["simulated", "real"] = "simulated"
    confirm_real: bool = False
    resend: bool = False
    horizon: Literal["3", "6", "12"] = "12"


@api.post("/incidents/{iid}/notify/telegram")
def notify_telegram(iid: str, payload: TelegramIn, role: Role = Depends(require_roles(*DISPATCH))):
    _inc(iid)
    p = payload
    if p.mode == "real" and not p.confirm_real:
        raise HTTPException(status_code=409, detail="A real Telegram send needs explicit confirmation (confirm_real=true)")
    return _job(jobs.submit("fire_telegram", lambda: firecomms.send_incident(iid, p.mode, p.confirm_real, role.value, p.resend, p.horizon)))


class IvrIn(M):
    mode: Literal["simulated", "real"] = "simulated"
    confirm_real: bool = False


@api.post("/incidents/{iid}/ivr")
def place_ivr(iid: str, payload: IvrIn, role: Role = Depends(require_roles(*DISPATCH))):
    inc = _inc(iid)
    if payload.mode == "real":
        done = [c for c in (inc["ivr"] or {}).get("calls", []) if c["mode"] == "real"]
        if len(done) >= 3:
            raise HTTPException(status_code=429, detail="Call limit reached for this incident (3 real calls)")
    return _wf(fireivr.place_call, iid, payload.mode, payload.confirm_real, role.value)


@api.post("/incidents/{iid}/acknowledge")
def acknowledge(iid: str, payload: NoteIn, role: Role = Depends(require_roles(*DISPATCH))):
    """An operator records that a responder has acknowledged (by phone, radio, Telegram...). Only this moves the incident to DISPATCHED."""
    inc = _inc(iid)
    n = inc["notify"] or {}
    n["acknowledged"] = {"by": f"operator ({role.value})", "at": now(), "via": "operator entry", "note": payload.note, "meaning": "Recorded by an operator; not verified by this system."}
    fireops.patch(iid, notify=n)
    fireops.log_event(iid, "acknowledged", "operator", "ok", f"Responder acknowledgement recorded by {role.value}" + (f": {payload.note}" if payload.note else ""), is_demo=inc["is_demo"])
    return public_incident(_wf(fireops.transition, iid, "DISPATCHED", role.value, "acknowledgement recorded") if inc["status"] == "DISPATCH_REQUESTED" else fireops.get(iid))


@api.post("/incidents/{iid}/field-update")
def field_update(iid: str, payload: NoteIn, role: Role = Depends(require_roles(*CREATE))):
    inc = _inc(iid)
    if not payload.note.strip():
        raise HTTPException(status_code=422, detail="Update text is required")
    ups = (inc["field_updates"] or []) + [{"at": now(), "by": role.value, "via": "web", "text": payload.note.strip()}]
    fireops.patch(iid, field_updates=ups)
    fireops.log_event(iid, "field_update", "operator", "ok", f"Field update: {payload.note.strip()[:200]}", is_demo=inc["is_demo"])
    out = fireops.get(iid)
    if inc["status"] == "DISPATCHED":
        out = fireops.transition(iid, "MONITORING", role.value, "field update received")
    return public_incident(out)


@api.post("/incidents/{iid}/resolve")
def resolve(iid: str, payload: NoteIn, role: Role = Depends(require_roles(*DISPATCH))):
    return public_incident(_wf(fireops.transition, iid, "RESOLVED", role.value, payload.note or "resolved"))


@api.post("/incidents/{iid}/cancel")
def cancel(iid: str, payload: NoteIn, role: Role = Depends(require_roles(*CREATE))):
    return public_incident(_wf(fireops.transition, iid, "CANCELLED", role.value, payload.note or "cancelled"))


# ---------------------------------------------------------------- evidence files
@api.get("/incidents/{iid}/map.png")
def map_png(iid: str, kind: Literal["incident", "route", "forecast"] = "incident", horizon: Literal["3", "6", "12"] = "12", role: Role = Depends(require_roles(*READ))):
    inc = _inc(iid)
    try:
        png, src = mapimage.render(kind, inc, horizon)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return Response(png, media_type="image/jpeg", headers={"X-Basemap": src, "Cache-Control": "no-store"})


@api.get("/incidents/{iid}/report.pdf")
def report_pdf(iid: str, role: Role = Depends(require_roles(*READ))):
    inc = _inc(iid)
    audit(role.value, "fire_report_exported", "fire_incident", iid, {})
    return Response(firecomms.report_pdf(inc), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{inc["code"]}-report.pdf"'})


@api.get("/incidents/{iid}/attachments/{filename}")
def attachment(iid: str, filename: str, role: Role = Depends(require_roles(*READ))):
    _inc(iid)
    p = (OUT_DIR / iid / Path(filename).name).resolve()
    if OUT_DIR.resolve() not in p.parents or not p.is_file():
        raise HTTPException(status_code=404, detail="Attachment not found")
    return FileResponse(p, media_type="application/pdf" if p.suffix == ".pdf" else "image/jpeg")


# ---------------------------------------------------------------- hotspot context (before an incident exists)
@api.get("/context")
def context(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180), parts: str = "stations", role: Role = Depends(require_roles(*READ))):
    want = set(parts.split(","))
    out: dict[str, Any] = {"lat": lat, "lon": lon}
    if "stations" in want:
        out["stations"] = [firestations.public(r, d) for d, r in firestations.nearby(lat, lon, limit=6)]
        out["stations_note"] = "Unverified public mapping data (OpenStreetMap, Google Maps via SerpAPI). Use 'Find stations' on an incident to refresh."
    if "terrain" in want:
        try:
            out["terrain"] = _terrain_point(lat, lon)
        except Exception as exc:  # noqa: BLE001
            out["terrain"] = {"status": "unavailable", "reason": str(exc)}
    if "osm" in want:
        from app.services.osm import OsmUnavailable, context as osm_context

        try:
            out["osm"] = osm_context({"type": "Polygon", "coordinates": [[[lon - 0.02, lat - 0.02], [lon + 0.02, lat - 0.02], [lon + 0.02, lat + 0.02], [lon - 0.02, lat + 0.02], [lon - 0.02, lat - 0.02]]]})
        except OsmUnavailable as exc:
            out["osm"] = {"status": "unavailable", "reason": str(exc)}
    return out


def _terrain_point(lat: float, lon: float) -> dict[str, Any]:
    from app.services.terrain import elevation_grid, slope_degrees
    import numpy as np
    from pyproj import Transformer

    dem = elevation_grid({"type": "Point", "coordinates": [lon, lat]}, max_dim=60, margin_deg=0.06)
    g = dem["grid"]; z = dem["heights"]
    x, y = Transformer.from_crs("EPSG:4326", g.crs, always_xy=True).transform(lon, lat)
    col, row = int((x - g.transform.c) / g.transform.a), int((y - g.transform.f) / g.transform.e)
    sl = slope_degrees(z, g.resolution)
    ok = 0 <= row < g.height and 0 <= col < g.width
    return {"status": "ok" if ok else "unavailable", "elevation_m": round(float(z[row, col]), 1) if ok else None, "slope_deg": round(float(sl[row, col]), 1) if ok else None, "source": dem["source"], "resolution_m": round(g.resolution)}


# ---------------------------------------------------------------- demo mode
def _demo_config() -> dict[str, Any]:
    with connection() as conn:
        r = conn.execute("SELECT value FROM kv WHERE key = ?", (DEMO_KEY,)).fetchone()
    return {**DEMO_DEFAULT, **(json.loads(r[0]) if r else {})}


class DemoConfig(M):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    frp_mw: float = Field(default=38.0, ge=0, le=5000)
    use_live_weather: bool = False
    wind_kmh: float = Field(default=24.0, ge=0, le=120)
    wind_from_deg: float = Field(default=270.0, ge=0, le=360)
    temp_c: float = Field(default=36.0, ge=-20, le=55)
    rh: float = Field(default=22.0, ge=1, le=100)


@api.get("/demo/config")
def demo_get(role: Role = Depends(require_roles(*READ))):
    return {**_demo_config(), "running": _demo["running"], "labels": "Every coordinate, hotspot, forecast weather, and notification in demo mode is synthetic and labelled DEMO."}


@api.put("/demo/config")
def demo_put(payload: DemoConfig, role: Role = Depends(require_roles(*CREATE))):
    cfg = {"lat": payload.lat, "lon": payload.lon, "frp_mw": payload.frp_mw, "use_live_weather": payload.use_live_weather, "weather": {"wind_kmh": payload.wind_kmh, "wind_from_deg": payload.wind_from_deg, "temp_c": payload.temp_c, "rh": payload.rh, "rain_24h_mm": 0.0, "rain_7d_mm": 0.0}, "note": DEMO_DEFAULT["note"]}
    with connection() as conn:
        conn.execute("INSERT INTO kv (key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at", (DEMO_KEY, json.dumps(cfg), now()))
    return cfg


def _demo_pipeline(iid: str, role: str) -> dict[str, Any]:
    from app.services import firesvc

    cfg = _demo_config()
    inc = fireops.get(iid)
    scn = (inc["detection"] or {}).get("scenario")
    if scn:
        cfg = {**cfg, "use_live_weather": False, "weather": scn["weather"]}
    fireops.log_event(iid, "demo_pipeline", "demo", "info", "Preparing forecast, nearest station, route and impact", is_demo=True)
    firesvc.run_forecast(iid, role, None if cfg["use_live_weather"] else cfg["weather"])
    firestations.discover(inc["lat"], inc["lon"], 60, iid, True)
    firestations.compute_route(iid, router_service)
    fireimpact.build(iid)
    cur = fireops.get(iid)
    if cur["status"] == "NEEDS_VERIFICATION":
        v = cur["verification"] or {"history": []}
        v["history"].append({"decision": "confirm", "note": "Scenario incident: confirmed by the scenario, not by a person", "by_role": "demo", "at": now()})
        fireops.patch(iid, verification=v)
        fireops.transition(iid, "CONFIRMED", "demo", "Confirmed (scenario)")
    return {"incident_id": iid, "status": fireops.get(iid)["status"]}


def _demo_simulation(iid: str) -> None:
    """Adds synthetic hotspot updates for a short time. Stoppable. Everything is labelled DEMO."""
    _demo["running"], _demo["stop"] = True, False
    try:
        cfg = _demo_config()
        for step in range(1, 7):
            for _ in range(15):
                if _demo["stop"]:
                    fireops.log_event(iid, "demo_stopped", "demo", "info", "Updates stopped", is_demo=True)
                    return
                time.sleep(1)
            inc = fireops.get(iid)
            if not inc:
                return
            det = inc["detection"] or {}
            hist = det.get("hotspot_history", [])
            hist.append({"lat": round(cfg["lat"] + 0.004 * step, 5), "lon": round(cfg["lon"] + 0.006 * step, 5), "observed_at": now(), "frp_mw": round(cfg["frp_mw"] * (1 + 0.1 * step), 1), "synthetic": True})
            det["hotspot_history"] = hist
            fireops.patch(iid, detection=det)
            fireops.log_event(iid, "demo_hotspot_update", "demo", "info", f"Hotspot update {step}/6", is_demo=True)
        fireops.log_event(iid, "demo_finished", "demo", "info", "Updates finished", is_demo=True)
    finally:
        _demo["running"] = False


@api.post("/demo/run")
def demo_run(new: bool = False, role: Role = Depends(require_roles(*CREATE))):
    cfg = _demo_config()
    existing = [i for i in fireops.list_incidents(True) if i["is_demo"] and i["status"] not in fireops.TERMINAL]
    if existing and not new:
        i = existing[0]
        fireops.log_event(i["id"], "demo_duplicate", "demo", "skipped", f"{i['code']} already exists", is_demo=True)
        return {"incident": public_incident(i), "created": False}
    tag = f"scenario-{int(time.time())}"
    det = {"source": "DEMO scenario", "product": "synthetic", "product_name": "DEMONSTRATION: synthetic detection", "satellite": "none", "confidence": None, "frp_mw": cfg["frp_mw"], "observed_at": now(), "currency": "demo", "synthetic": True, "hotspot_history": [{"lat": cfg["lat"], "lon": cfg["lon"], "observed_at": now(), "frp_mw": cfg["frp_mw"], "synthetic": True}], "note": cfg["note"]}
    inc, created = fireops.create("demo", cfg["lat"], cfg["lon"], now(), det, role.value, is_demo=True, demo_tag=tag)
    job = jobs.submit("demo_pipeline", lambda: _demo_pipeline(inc["id"], role.value))
    threading.Thread(target=_demo_simulation, args=(inc["id"],), daemon=True).start()
    return {"incident": public_incident(inc), "created": created, "job_id": job}


SCENARIOS = [
    {"slug": "satpura", "name": "Satpura Tiger Reserve, Madhya Pradesh", "lat": 22.43, "lon": 78.30, "frp": 42.0, "wind": 26.0, "from": 260.0, "temp": 37.0, "rh": 20.0},
    {"slug": "bandipur", "name": "Bandipur, Karnataka", "lat": 11.78, "lon": 76.55, "frp": 31.0, "wind": 18.0, "from": 240.0, "temp": 35.0, "rh": 24.0},
    {"slug": "corbett", "name": "Jim Corbett, Uttarakhand", "lat": 29.50, "lon": 78.85, "frp": 28.0, "wind": 22.0, "from": 300.0, "temp": 34.0, "rh": 18.0},
    {"slug": "similipal", "name": "Similipal, Odisha", "lat": 21.75, "lon": 86.35, "frp": 36.0, "wind": 20.0, "from": 200.0, "temp": 36.0, "rh": 26.0},
    {"slug": "nallamala", "name": "Nallamala forest, Andhra Pradesh", "lat": 15.95, "lon": 78.95, "frp": 33.0, "wind": 24.0, "from": 90.0, "temp": 38.0, "rh": 19.0},
]


def _scenario_batch(ids: list[str], role: str) -> dict[str, Any]:
    out = {}
    for iid in ids:
        try:
            out[iid] = _demo_pipeline(iid, role)["status"]
        except Exception as exc:  # noqa: BLE001 - one failing scenario must not stop the others
            fireops.log_event(iid, "scenario_failed", "workflow", "failed", f"Could not prepare the incident: {exc}", error=str(exc)[:200], is_demo=True)
            out[iid] = "FAILED"
    return out


@api.post("/scenarios/load")
def load_scenarios(role: Role = Depends(require_roles(*CREATE))):
    """Create the example fire incidents (idempotent). Each is prepared in the background: forecast, nearest station, route, impact."""
    made, ids = [], []
    for s_ in SCENARIOS:
        det = {"source": "Scenario", "product": "scenario", "product_name": s_["name"], "satellite": "VIIRS", "confidence": None, "frp_mw": s_["frp"], "observed_at": now(), "currency": "demo", "synthetic": True, "place": {"name": s_["name"]},
               "scenario": {"slug": s_["slug"], "name": s_["name"], "weather": {"wind_kmh": s_["wind"], "wind_from_deg": s_["from"], "temp_c": s_["temp"], "rh": s_["rh"], "rain_24h_mm": 0.0, "rain_7d_mm": 0.0}},
               "hotspot_history": [{"lat": s_["lat"], "lon": s_["lon"], "observed_at": now(), "frp_mw": s_["frp"], "synthetic": True}], "note": s_["name"]}
        inc, created = fireops.create("demo", s_["lat"], s_["lon"], now(), det, role.value, is_demo=True, demo_tag=f"scn-{s_['slug']}")
        made.append(public_incident(inc))
        if created:
            ids.append(inc["id"])
    job = jobs.submit("scenario_batch", lambda: _scenario_batch(ids, role.value)) if ids else None
    return {"incidents": made, "created": len(ids), "job_id": job}


@api.get("/incidents/{iid}/ivr/script")
def ivr_script(iid: str, role: Role = Depends(require_roles(*READ))):
    """The exact words a call would speak, so the operator can read or hear them first."""
    return {"script": fireivr.spoken(_inc(iid)), "recipient": fireivr.status()["response_contact"]}


@api.post("/demo/stop")
def demo_stop(role: Role = Depends(require_roles(*CREATE))):
    was = _demo["running"]
    _demo["stop"] = True
    fireops.log_event(None, "demo_stop_requested", "demo", "info", "[DEMO] Stop requested" + ("" if was else " (no simulation was running)"), is_demo=True)
    return {"was_running": was}


@api.post("/demo/reset")
def demo_reset(role: Role = Depends(require_roles(*CREATE))):
    _demo["stop"] = True
    res = fireops.reset_demo()
    for i in res["ids"]:
        shutil.rmtree(OUT_DIR / i, ignore_errors=True)
    audit(role.value, "fire_demo_reset", "fire_incident", None, {"removed": res["incidents_removed"]})
    return {**res, "real_incidents_untouched": len(fireops.list_incidents(False))}


# ---------------------------------------------------------------- provider webhooks
@api.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if not verify_shared_secret(settings.callback_secret, x_telegram_bot_api_secret_token):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    return firecomms.handle_update(await request.json())


@api.post("/ivr/twilio/status")
async def twilio_status(request: Request):
    form = {k: str(v) for k, v in (await request.form()).items()}
    token = settings.twilio_auth_token
    if not token or not verify_twilio_signature(token, str(request.url), form, request.headers.get("x-twilio-signature", "")):
        raise HTTPException(status_code=403, detail="Invalid provider signature")
    sid = form.get("CallSid")
    for inc in fireops.list_incidents(True, 500):
        for c in (inc["ivr"] or {}).get("calls", []):
            if c.get("provider_call_id") == sid:
                new = fireivr.TWILIO_MAP.get(form.get("CallStatus", ""), form.get("CallStatus", "").upper())
                fireivr._set(inc["id"], c, new, inc["is_demo"], f"Provider callback: call {form.get('CallStatus')}", "ok" if new in ("RINGING", "ANSWERED", "COMPLETED") else "failed" if new in fireivr.FINAL else "info")
                return {"ok": True}
    return {"ok": False, "reason": "unknown call"}
