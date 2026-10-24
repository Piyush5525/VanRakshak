"""Fire-response orchestration: forecast inputs (weather, terrain, land cover) -> spread risk footprint, stored on the incident."""
import math
from datetime import datetime, timezone
from typing import Any

import httpx
import numpy as np

from app.services import fireops
from app.services.acquisition import provider
from app.services.firespread import ASSUMPTIONS, HORIZONS, LIMITATIONS, MODEL, VERSION, SpreadParams, make_grid, seed_for, simulate, wind_mean
from app.services.satellite import Grid, ProviderError
from app.services.terrain import elevation_grid

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


class InputUnavailable(RuntimeError):
    pass


def gather_weather(lat: float, lon: float, client: httpx.Client | None = None) -> dict[str, Any]:
    """Hourly forecast for the next 12 h plus rainfall history, from Open-Meteo (model data, not station observations)."""
    c = client or httpx.Client(timeout=30)
    last: Exception | None = None
    for _ in range(2):
        try:
            r = c.get(OPEN_METEO, params={"latitude": lat, "longitude": lon, "hourly": "wind_speed_10m,wind_direction_10m,temperature_2m,relative_humidity_2m,precipitation", "wind_speed_unit": "kmh", "past_days": 7, "forecast_days": 2, "timezone": "UTC"})
            r.raise_for_status()
            h = r.json()["hourly"]
            break
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            last = exc
    else:
        raise InputUnavailable(f"Weather service unavailable ({type(last).__name__})")
    times = h["time"]
    now_h = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")
    i0 = times.index(now_h) if now_h in times else max(0, len(times) // 2)
    need = ("wind_speed_10m", "wind_direction_10m", "temperature_2m", "relative_humidity_2m")
    hours = []
    for k in range(0, 13):
        j = i0 + k
        if j >= len(times) or any(h[n][j] is None for n in need):
            continue
        hours.append({"t": times[j] + "Z", "wind_kmh": h["wind_speed_10m"][j], "wind_from_deg": h["wind_direction_10m"][j], "temp_c": h["temperature_2m"][j], "rh": h["relative_humidity_2m"][j]})
    if len(hours) < 6:
        raise InputUnavailable("Weather forecast has too few valid hours")
    pr = [v or 0.0 for v in h["precipitation"][: i0 + 1]]
    return {"source": "Open-Meteo forecast (numerical weather model output, ~11 km)", "fetched_at": datetime.now(timezone.utc).isoformat(), "valid_from": hours[0]["t"], "valid_to": hours[-1]["t"], "hours": hours, "rain_24h_mm": round(sum(pr[-24:]), 1), "rain_7d_mm": round(sum(pr[-168:]), 1), "attribution": "Weather data by Open-Meteo.com (CC BY 4.0)"}


def horizon_inputs(weather: dict[str, Any]) -> dict[int, dict[str, float]]:
    out = {}
    for hz in HORIZONS:
        sel = weather["hours"][: hz + 1]
        spd, frm = wind_mean([x["wind_kmh"] for x in sel], [x["wind_from_deg"] for x in sel])
        out[hz] = {"wind_kmh": spd, "wind_from_deg": frm, "temp_c": float(np.mean([x["temp_c"] for x in sel])), "rh": float(np.mean([x["rh"] for x in sel]))}
    return out


def slope_function(lat: float, lon: float) -> tuple[Any, dict[str, Any]]:
    """Returns (callable bearing_deg -> mean slope in degrees over the first 2 km, provenance)."""
    try:
        dem = elevation_grid({"type": "Point", "coordinates": [lon, lat]}, max_dim=80, margin_deg=0.12)
    except Exception as exc:  # noqa: BLE001 - degrade rather than fail
        return (lambda b: 0.0), {"available": False, "reason": f"Terrain unavailable: {exc}"}
    grid: Grid = dem["grid"]; z = dem["heights"]
    from pyproj import Transformer

    x0, y0 = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True).transform(lon, lat)

    def z_at(x: float, y: float) -> float:
        col = (x - grid.transform.c) / grid.transform.a; row = (y - grid.transform.f) / grid.transform.e
        c0, r0 = int(math.floor(col)), int(math.floor(row))
        if not (0 <= c0 < grid.width - 1 and 0 <= r0 < grid.height - 1):
            return float("nan")
        fx, fy = col - c0, row - r0
        v = z[r0, c0] * (1 - fx) * (1 - fy) + z[r0, c0 + 1] * fx * (1 - fy) + z[r0 + 1, c0] * (1 - fx) * fy + z[r0 + 1, c0 + 1] * fx * fy
        return float(v)

    def slope(bearing: float) -> float:
        th = math.radians(bearing); ds = np.arange(0, 2001, 200.0)
        zs = np.array([z_at(x0 + d * math.sin(th), y0 + d * math.cos(th)) for d in ds])
        ok = np.isfinite(zs)
        if ok.sum() < 4:
            return 0.0
        return float(math.degrees(math.atan(np.polyfit(ds[ok], zs[ok], 1)[0])))

    return slope, {"available": True, "source": dem["source"], "resolution_m": round(grid.resolution), "elevation_at_point_m": round(z_at(x0, y0), 1)}


def landcover_for(lat: float, lon: float, params: SpreadParams, inputs: dict[str, Any]) -> tuple[np.ndarray | None, dict[str, Any]]:
    g = make_grid(lat, lon, inputs, params)
    grid = Grid(g["crs"], g["transform"], g["n"], g["n"], g["cell"])
    from pyproj import Transformer
    from shapely.geometry import box

    to_wgs = Transformer.from_crs(g["crs"], "EPSG:4326", always_xy=True)
    c = g["transform"]; xs = [c.c, c.c + c.a * g["n"]]; ys = [c.f, c.f + c.e * g["n"]]
    lons, lats = zip(*[to_wgs.transform(x, y) for x in xs for y in ys])
    b = box(min(lons), min(lats), max(lons), max(lats)).__geo_interface__
    try:
        lc = provider().worldcover_classes(b, grid)
    except (ProviderError, Exception) as exc:  # noqa: BLE001
        return None, {"available": False, "reason": f"Land cover unavailable: {exc}"}
    share = {int(k): round(float((lc == k).mean()), 3) for k in np.unique(lc)}
    return lc, {"available": True, "source": "ESA WorldCover 2021 (10 m, point-sampled at the model grid)", "class_share": share}


def override_weather(spec: dict[str, Any]) -> dict[str, Any]:
    """Illustrative weather for a DEMONSTRATION: constant conditions for 13 hours. Clearly flagged, never used for real incidents."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    hours = [{"t": (now.replace(hour=0) if False else now).isoformat().replace("+00:00", "Z"), "wind_kmh": spec["wind_kmh"], "wind_from_deg": spec["wind_from_deg"], "temp_c": spec["temp_c"], "rh": spec["rh"]} for _ in range(13)]
    return {"source": "Scenario weather (set by the operator, not observed or forecast)", "fetched_at": datetime.now(timezone.utc).isoformat(), "valid_from": hours[0]["t"], "valid_to": hours[-1]["t"], "hours": hours, "rain_24h_mm": spec.get("rain_24h_mm", 0.0), "rain_7d_mm": spec.get("rain_7d_mm", 0.0), "attribution": "Demonstration scenario", "illustrative": True}


def run_forecast(incident_id: str, actor: str = "system", weather_spec: dict[str, Any] | None = None) -> dict[str, Any]:
    inc = fireops.get(incident_id)
    if not inc:
        raise fireops.WorkflowError("Incident not found")
    demo = inc["is_demo"]
    lat, lon = inc["lat"], inc["lon"]
    fireops.log_event(incident_id, "forecast_requested", "firespread", "pending", f"Forecast requested for {inc['code']} (3, 6, 12 h)", is_demo=demo)
    try:
        weather = override_weather(weather_spec) if (weather_spec and demo) else gather_weather(lat, lon)
    except InputUnavailable as exc:
        fc = {"status": "UNAVAILABLE", "reason": str(exc), "generated_at": datetime.now(timezone.utc).isoformat(), "demo": demo}
        fireops.patch(incident_id, forecast=fc)
        fireops.log_event(incident_id, "forecast_failed", "firespread", "failed", "Forecast unavailable: no weather input", error=str(exc), is_demo=demo)
        return fc
    fireops.log_event(incident_id, "forecast_inputs", "demo_weather" if weather.get("illustrative") else "open_meteo", "ok", f"{'ILLUSTRATIVE weather set' if weather.get('illustrative') else 'Weather fetched'}: wind {weather['hours'][0]['wind_kmh']} km/h from {weather['hours'][0]['wind_from_deg']}°, rain 7 d {weather['rain_7d_mm']} mm", is_demo=demo, details={"valid_from": weather["valid_from"], "valid_to": weather["valid_to"]})
    slope_fn, terrain = slope_function(lat, lon)
    fireops.log_event(incident_id, "forecast_inputs", "terrain", "ok" if terrain["available"] else "failed", "Terrain slope loaded" if terrain["available"] else terrain["reason"], error=None if terrain["available"] else terrain["reason"], is_demo=demo)
    params = SpreadParams()
    hz_in = horizon_inputs(weather)
    inputs = {"horizons": hz_in, "rain_7d_mm": weather["rain_7d_mm"], "rain_24h_mm": weather["rain_24h_mm"], "slope_deg": slope_fn}
    lc, cover = landcover_for(lat, lon, params, inputs)
    fireops.log_event(incident_id, "forecast_inputs", "landcover", "ok" if cover["available"] else "failed", "Land cover loaded" if cover["available"] else cover["reason"], error=None if cover["available"] else cover["reason"], is_demo=demo)
    repro = {"lat": lat, "lon": lon, "horizons": {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in hz_in.items()}, "rain": [weather["rain_7d_mm"], weather["rain_24h_mm"]], "params": params.__dict__}  # same inputs -> same seed -> same output
    seed = seed_for(incident_id, repro)
    sim = simulate(lat, lon, inputs, params, seed, landcover=lc)
    quality = "OK" if terrain["available"] and cover["available"] else "DEGRADED"
    notes = [] if quality == "OK" else [x["reason"] for x in (terrain, cover) if not x["available"]]
    fc = {"status": quality, "model": MODEL, "version": VERSION, "claim_type": "predicted_risk_footprint", "is_fire_perimeter": False, "demo": demo,
          "generated_at": datetime.now(timezone.utc).isoformat(), "seed": seed, "origin": {"lat": lat, "lon": lon, "observed_at": inc["observed_at"], "synthetic": demo},
          "inputs": {"weather": {k: v for k, v in weather.items() if k != "hours"}, "horizon_weather": {str(k): {kk: round(vv, 1) for kk, vv in v.items()} for k, v in hz_in.items()}, "terrain": terrain, "land_cover": cover},
          "params": {**params.__dict__}, "horizons": sim["horizons"], "grid": {k: v for k, v in sim["grid"].items() if k != "transform"}, "drought_factor": sim["drought_factor"],
          "assumptions": ASSUMPTIONS, "limitations": LIMITATIONS, "degradation_notes": notes,
          "evaluation": {"status": "not_validated", "note": "No historical accuracy test has been run for this model; see /fire/evaluation for any available proxy test."}}
    fireops.patch(incident_id, forecast=fc)
    p50 = {h: v["area_ha"]["p50"] for h, v in fc["horizons"].items()}
    fireops.log_event(incident_id, "forecast_generated", "firespread", "ok" if quality == "OK" else "info", f"Risk footprint generated ({quality}): median potential area 3 h {p50['3']} ha, 6 h {p50['6']} ha, 12 h {p50['12']} ha" , ref=f"seed:{seed}", is_demo=demo, details={"quality": quality})
    return fc
