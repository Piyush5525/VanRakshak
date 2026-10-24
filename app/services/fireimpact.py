"""Environmental impact panel for a fire incident: potential exposure area, nearby places, modelled air quality, carbon.

Rules this module follows:
  * a forecast risk footprint is NOT burned area and is never reported as such;
  * air quality is modelled ambient data (CAMS via Open-Meteo), never a plume or a measurement of this fire;
  * carbon needs burned area AND fuel load; without them nothing is calculated (demo mode uses labelled illustrative values).
"""
import math
from datetime import datetime, timezone
from typing import Any

import httpx
from shapely.geometry import Point, shape

from app.services import fireops
from app.services.osm import OsmUnavailable, fetch

EF_CO2_G_PER_KG = {"value": 1643, "range": [1520, 1700], "source": "Akagi et al. 2011, tropical forest (verify before citing)"}
ILLUSTRATIVE = {"fuel_load_t_ha": 12.0, "completeness": [0.2, 0.5]}


def bearing(lat1, lon1, lat2, lon2) -> float:
    p1, p2, dl = map(math.radians, (lat1, lat2, lon2 - lon1))
    y = math.sin(dl) * math.cos(p2); x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def km(lat1, lon1, lat2, lon2) -> float:
    p = math.pi / 180
    h = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def carbon(area_ha: float, fuel_load_t_ha: float, completeness: list[float], basis: str, measured_inputs: bool) -> dict[str, Any]:
    """CO2 only (CH4, CO and aerosols excluded). t CO2 = area x fuel load x completeness x EF."""
    ef = EF_CO2_G_PER_KG
    f = lambda c, e: round(area_ha * fuel_load_t_ha * c * e / 1000.0, 1)  # noqa: E731  t DM * (g/kg == kg/t) -> t CO2
    lo, mid, hi = f(completeness[0], ef["range"][0]), f(sum(completeness) / 2, ef["value"]), f(completeness[1], ef["range"][1])
    return {"status": "CALCULATED", "evidence_class": "modelled" if measured_inputs else "illustrative", "pollutant": "CO2 only (not CO2-equivalent; CH4, CO and aerosols excluded)", "tonnes_co2": {"low": lo, "central": mid, "high": hi},
            "method": "area x fuel load x combustion completeness x emission factor (Seiler & Crutzen style)", "inputs": {"area_ha": round(area_ha, 1), "area_basis": basis, "fuel_load_t_dm_per_ha": fuel_load_t_ha, "combustion_completeness_range": completeness, "emission_factor_g_co2_per_kg_dm": ef},
            "assumptions": ["Surface-fire completeness range is an assumption; crown fires and peat/duff burning would be higher.", "Fuel load is a single value, not mapped.", "Area is " + basis],
            "uncertainty": "Low-to-high span combines only the completeness and emission-factor ranges; fuel-load and area uncertainty are NOT included, so the true range is wider."}


def nearby_places(lat: float, lon: float, footprint: Any | None, spread_toward: float | None) -> dict[str, Any]:
    try:
        els = fetch(f'[out:json][timeout:25];(node["place"~"^(city|town|village|hamlet)$"](around:25000,{lat},{lon});way["highway"~"^(motorway|trunk|primary|secondary)$"](around:12000,{lat},{lon}););out center tags 200;')
    except OsmUnavailable as exc:
        return {"status": "unavailable", "reason": str(exc)}
    places, roads = [], 0
    for e in els:
        t = e.get("tags", {})
        if "place" in t and e["type"] == "node":
            d = km(lat, lon, e["lat"], e["lon"]); b = bearing(lat, lon, e["lat"], e["lon"])
            inside = bool(footprint is not None and footprint.contains(Point(e["lon"], e["lat"])))
            diff = abs((b - spread_toward + 180) % 360 - 180) if spread_toward is not None else None
            places.append({"name": t.get("name") or "(unnamed)", "type": t["place"], "distance_km": round(d, 1), "bearing_deg": round(b), "inside_possible_footprint": inside, "downwind": bool(diff is not None and diff <= 30 and d <= 25)})
        elif "highway" in t:
            roads += 1
    places.sort(key=lambda p: p["distance_km"])
    return {"status": "ok", "source": "OpenStreetMap contributors (ODbL); mapping is incomplete", "settlements": places[:25], "major_road_segments_within_12km": roads}


def air_quality(lat: float, lon: float, wind: dict[str, Any] | None) -> dict[str, Any]:
    h, err = None, None
    for _ in range(2):
        try:
            r = httpx.get("https://air-quality-api.open-meteo.com/v1/air-quality", params={"latitude": round(lat, 2), "longitude": round(lon, 2), "hourly": "pm2_5,pm10", "past_days": 1, "forecast_days": 1, "timezone": "UTC"}, timeout=25)
            r.raise_for_status(); h = r.json()["hourly"]
            break
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            err = exc
    if h is None:
        return {"status": "unavailable", "reason": f"Air-quality model unavailable ({type(err).__name__})"}
    nowh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00"); i = h["time"].index(nowh) if nowh in h["time"] else len(h["time"]) // 2
    mx = lambda k: max([v for v in h[k] if v is not None] or [None])  # noqa: E731
    return {"status": "ok", "evidence_class": "modelled", "kind": "Ambient modelled air quality at the incident location (Copernicus CAMS via Open-Meteo). Not smoke from this fire.", "as_of": h["time"][i] + "Z", "pm2_5_ug_m3": {"now": h["pm2_5"][i], "max_24h": mx("pm2_5")}, "pm10_ug_m3": {"now": h["pm10"][i], "max_24h": mx("pm10")}, "wind": wind,
            "measured_stations": "No monitoring-station feed is configured (OpenAQ needs an API key), so no measured values are shown.", "attribution": "Air quality data by Open-Meteo.com / Copernicus CAMS (CC BY 4.0); about 11 km resolution."}


def build(incident_id: str, area_ha: float | None = None, fuel_load_t_ha: float | None = None) -> dict[str, Any]:
    inc = fireops.get(incident_id)
    demo = inc["is_demo"]
    fc = inc["forecast"] or {}
    imp: dict[str, Any] = {"generated_at": datetime.now(timezone.utc).isoformat(), "demo": demo, "labels": {"observed": "OBSERVED", "modelled": "MODELLED", "estimated": "ESTIMATED", "demo": "DEMO"}}
    det = inc.get("detection") or {}
    imp["observed"] = {"evidence_class": "observed" if not demo else "demo", "hotspots": det.get("hotspot_history") or [{"lat": inc["lat"], "lon": inc["lon"], "observed_at": inc["observed_at"], "frp_mw": det.get("frp_mw")}], "actual_fire_perimeter": "Not available. Satellite hotspots are points; no burned-area perimeter has been mapped.", "burned_area_ha": None}
    foot, toward = None, None
    if fc.get("horizons"):
        imp["potential_exposure"] = {"evidence_class": "estimated", "label": "POTENTIAL EXPOSURE AREA (predicted risk footprint), not burned area", "by_horizon_h": {h: {"area_ha_p50": v["area_ha"]["p50"], "area_ha_p10_p90": [v["area_ha"]["p10"], v["area_ha"]["p90"]], "likely_footprint_ha": v["contour_area_ha"]["likely_50"], "possible_footprint_ha": v["contour_area_ha"]["possible_10"]} for h, v in fc["horizons"].items()}, "forecast_quality": fc.get("status")}
        v12 = fc["horizons"]["12"]; toward = v12["spread_toward_deg"]
        if v12["contours"].get("possible_10"):
            foot = shape(v12["contours"]["possible_10"])
        imp["land_cover"] = (fc["inputs"].get("land_cover") or {})
    else:
        imp["potential_exposure"] = {"status": "NOT_AVAILABLE", "reason": "Generate the forecast first (or it was unavailable)."}
    places = nearby_places(inc["lat"], inc["lon"], foot, toward)
    imp["nearby"] = places
    wind = {"speed_kmh": fc["horizons"]["12"]["wind_kmh"], "from_deg": fc["horizons"]["12"]["wind_from_deg"], "toward_deg": toward} if fc.get("horizons") else None
    aq = air_quality(inc["lat"], inc["lon"], wind)
    if places.get("status") == "ok":
        aq["potentially_downwind_settlements"] = [p for p in places["settlements"] if p["downwind"]][:10]
        aq["downwind_note"] = "Settlements within 25 km and within +-30 degrees of the 12-hour mean spread direction. This marks where smoke could drift if the fire grew; it is not a concentration estimate."
    imp["air_quality"] = aq
    # carbon
    pot = imp["potential_exposure"].get("by_horizon_h", {}).get("12")
    if area_ha is not None and fuel_load_t_ha is not None:
        imp["carbon"] = carbon(area_ha, fuel_load_t_ha, ILLUSTRATIVE["completeness"], "supplied by the operator (e.g. field estimate); not derived from satellite hotspots", True)
    elif demo and pot:
        imp["carbon"] = carbon(pot["area_ha_p50"], ILLUSTRATIVE["fuel_load_t_ha"], ILLUSTRATIVE["completeness"], "the scenario 12-hour potential exposure area, treated as if it burned (illustration only)", False)
        imp["carbon"]["evidence_class"] = "illustrative"; imp["carbon"]["label"] = "SCENARIO VALUES (assumed)"
    else:
        imp["carbon"] = {"status": "NOT_ENOUGH_INFORMATION", "reason": "Carbon needs a burned area and a fuel load. NASA FIRMS hotspot counts or fire power are not a valid basis. Enter a field-estimated burned area and fuel load to calculate.", "evidence_class": "not_available"}
    fireops.patch(incident_id, impact=imp)
    fireops.log_event(incident_id, "impact_generated", "impact", "ok", f"Impact panel generated (places: {places['status']}, air: {aq['status']}, carbon: {imp['carbon']['status']})", is_demo=demo)
    return imp
