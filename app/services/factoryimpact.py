"""Factory impact screening: where a factory goes, what it replaces and reaches, and how that builds up over 7 / 30 / 90 days.

Everything shown is computed from open data around the chosen point (ESA WorldCover land cover, OpenStreetMap rivers, villages and
reserves, a year of Open-Meteo wind) plus the factory details the user supplies or reads from a document. Nothing is invented:
a missing source is reported as unavailable, and the 7/30/90-day figures are a SCENARIO whose assumptions are returned with the result.
This is a screening aid, not an environmental impact assessment.
"""
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
import numpy as np
from pyproj import Transformer
from rasterio import features as rio_features
from rasterio.transform import from_origin
from shapely import set_precision
from shapely.affinity import translate
from shapely.geometry import Point, box, mapping, shape as shp
from shapely.ops import transform, unary_union

from app.services import casestudies
from app.services.acquisition import provider
from app.services.osm import OsmUnavailable, fetch
from app.services.satellite import Grid

N, CELL = 500, 20.0          # a 10 km x 10 km window at 20 m
HALF = N * CELL / 2
TREE = 10
EDGE_M = 300
HORIZONS = (7, 30, 90)

# Relative screening profiles by factory type. These say which kinds of effect a type of plant typically has; they are not measurements.
PROFILES: dict[str, dict[str, Any]] = {
    "cement": {"label": "Cement plant", "air": "high", "water": "low", "noise": "high", "carbon": "high", "footprint_ha": 40, "stack_m": 90},
    "steel": {"label": "Steel / sponge iron", "air": "high", "water": "high", "noise": "high", "carbon": "high", "footprint_ha": 60, "stack_m": 60},
    "thermal_power": {"label": "Thermal power plant", "air": "high", "water": "high", "noise": "medium", "carbon": "high", "footprint_ha": 120, "stack_m": 220},
    "chemical": {"label": "Chemical / pharma", "air": "medium", "water": "high", "noise": "medium", "carbon": "medium", "footprint_ha": 25, "stack_m": 40},
    "textile": {"label": "Textile / dyeing", "air": "low", "water": "high", "noise": "low", "carbon": "low", "footprint_ha": 10, "stack_m": 30},
    "paper": {"label": "Paper / pulp", "air": "medium", "water": "high", "noise": "medium", "carbon": "medium", "footprint_ha": 40, "stack_m": 60},
    "food": {"label": "Food / agro processing", "air": "low", "water": "medium", "noise": "low", "carbon": "low", "footprint_ha": 8, "stack_m": 25},
    "mining": {"label": "Mine / quarry", "air": "high", "water": "medium", "noise": "high", "carbon": "medium", "footprint_ha": 80, "stack_m": 0},
    "other": {"label": "Other industry", "air": "medium", "water": "medium", "noise": "medium", "carbon": "medium", "footprint_ha": 15, "stack_m": 30},
}
LEVELS = ["low", "medium", "high"]


def kinds() -> dict[str, str]:
    return {k: v["label"] for k, v in PROFILES.items()}


def _bump(level: str, by: int) -> str:
    return LEVELS[max(0, min(2, LEVELS.index(level) + by))]


def _utm(lat: float, lon: float) -> str:
    return f"EPSG:{(32600 if lat >= 0 else 32700) + int((lon + 180) // 6) + 1}"


def _wind(lat: float, lon: float) -> dict[str, Any]:
    """A year of hourly wind: how often it blows FROM each of 8 directions, and the prevailing one."""
    end = date.today() - timedelta(days=6)
    try:
        r = httpx.get("https://archive-api.open-meteo.com/v1/archive", params={"latitude": round(lat, 3), "longitude": round(lon, 3), "start_date": (end - timedelta(days=364)).isoformat(), "end_date": end.isoformat(), "hourly": "wind_speed_10m,wind_direction_10m", "timezone": "UTC", "wind_speed_unit": "kmh"}, timeout=40)
        r.raise_for_status()
        h = r.json()["hourly"]
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return {"status": "unavailable", "reason": f"Wind history unavailable ({type(exc).__name__})"}
    sp = np.array([v if v is not None else np.nan for v in h["wind_speed_10m"]], dtype=float)
    di = np.array([v if v is not None else np.nan for v in h["wind_direction_10m"]], dtype=float)
    ok = np.isfinite(sp) & np.isfinite(di) & (sp >= 3)   # calm hours carry no direction
    if ok.sum() < 200:
        return {"status": "unavailable", "reason": "Too little wind data"}
    sector = (((di[ok] + 22.5) // 45) % 8).astype(int)
    freq = np.bincount(sector, minlength=8) / sector.size
    k = int(freq.argmax())
    return {"status": "ok", "source": "Open-Meteo archive (ERA5), last 365 days", "from_deg": k * 45.0, "from_name": casestudies._compass(k * 45.0), "mean_kmh": round(float(sp[ok].mean()), 1), "rose": [{"from": casestudies._compass(i * 45.0), "pct": round(100 * float(f), 1)} for i, f in enumerate(freq)], "share_prevailing_pct": round(100 * float(freq[k]), 1)}


def _osm(query: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    try:
        return fetch(query), None
    except OsmUnavailable as exc:
        return None, str(exc)


def assess(inputs: dict[str, Any]) -> dict[str, Any]:
    lat, lon = float(inputs["lat"]), float(inputs["lon"])
    kind = inputs.get("kind") if inputs.get("kind") in PROFILES else "other"
    prof = PROFILES[kind]
    foot_ha = float(inputs.get("footprint_ha") or prof["footprint_ha"])
    foot_assumed = not inputs.get("footprint_ha")
    crs = _utm(lat, lon)
    to_utm, to_ll = Transformer.from_crs("EPSG:4326", crs, always_xy=True), Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    x0, y0 = to_utm.transform(lon, lat)
    transform_ = from_origin(x0 - HALF, y0 + HALF, CELL, CELL)
    window = shp(mapping(box(*(to_ll.transform(x0 - HALF, y0 - HALF) + to_ll.transform(x0 + HALF, y0 + HALF)))))
    sources, notes = {}, []

    # ---- land cover around the point
    try:
        lc = provider().worldcover_classes(mapping(window), Grid(crs, transform_, N, N, CELL))
        sources["land_cover"] = "ESA WorldCover 2021 (10 m), resampled to 20 m"
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "reason": f"Land cover could not be read for this place: {exc}", "inputs": inputs}
    yy, xx = np.mgrid[0:N, 0:N]
    dist = np.hypot((xx + 0.5) * CELL - HALF, HALF - (yy + 0.5) * CELL)
    tree = lc == TREE
    side = math.sqrt(foot_ha * 1e4)
    foot = box(-side / 2, -side / 2, side / 2, side / 2)

    def mask(geom) -> np.ndarray:
        g = translate(geom, x0, y0)
        return rio_features.rasterize([(g, 1)], out_shape=(N, N), transform=transform_, fill=0, dtype="uint8").astype(bool)

    ha = lambda m: round(float(m.sum()) * CELL * CELL / 1e4, 1)  # noqa: E731
    m_foot = mask(foot)
    forest_in_foot = tree & m_foot
    cleared_ha = ha(forest_in_foot)
    footprint_classes = {int(k): round(float((lc[m_foot] == k).mean()), 3) for k in np.unique(lc[m_foot])} if m_foot.any() else {}
    m_edge = mask(foot.buffer(EDGE_M)) & ~m_foot
    edge_ha_full = ha(tree & m_edge)
    forest_3km = ha(tree & (dist <= 3000))
    share = {r: round(100 * float(tree[dist <= r].mean()), 1) for r in (500, 1000, 3000)}
    out_foot = tree & ~m_foot
    near_edge = float(np.hypot(*(np.argwhere(out_foot) - np.array([N / 2, N / 2])).T).min() * CELL - side / 2) if out_foot.any() else None
    near_edge = max(0.0, near_edge) if near_edge is not None else None

    # ---- wind and smoke zones
    wind = _wind(lat, lon)
    plumes, plume_stats = [], None
    if wind["status"] == "ok":
        stack = float(inputs.get("stack_height_m") or prof["stack_m"])
        scale = 1.0 + min(stack, 250) / 250.0      # taller stacks carry further; a screening factor, not a dispersion model
        plumes = [p for p in casestudies._plumes([(0.0, 0.0)], wind["from_deg"])]
        plumes = [transform(lambda x, y, z=None, s=scale: (x * (1 + (s - 1) * 0.5), y * (1 + (s - 1) * 0.5)), p) for p in plumes] if scale != 1 else plumes
        plume_stats = [{"label": lbl, "forest_ha": ha(tree & mask(p) & ~m_foot), "area_ha": ha(mask(p))} for lbl, p in zip(("closest ring", "middle ring", "outer ring"), plumes)]
        sources["wind"] = wind["source"]
    else:
        notes.append(wind["reason"])

    # ---- OpenStreetMap: rivers, villages, protected areas (each may be unavailable)
    rv, rv_err = _osm(f'[out:json][timeout:25];(way["waterway"~"river|stream|canal"](around:3000,{lat},{lon}););out geom 80;')
    rivers, river_min = [], None
    if rv is not None:
        for e in rv:
            pts = [(p["lon"], p["lat"]) for p in e.get("geometry", [])]
            if len(pts) < 2:
                continue
            line = shp({"type": "LineString", "coordinates": pts})
            d = transform(to_utm.transform, line).distance(Point(x0, y0)) - side / 2
            rivers.append({"name": e.get("tags", {}).get("name"), "kind": e.get("tags", {}).get("waterway"), "distance_m": round(max(d, 0)), "line": mapping(line.simplify(0.0001))})
        rivers.sort(key=lambda r: r["distance_m"])
        river_min = rivers[0]["distance_m"] if rivers else None
        sources["rivers"] = "OpenStreetMap contributors (ODbL); mapping is incomplete"
    water_cells = (lc == 80) | (lc == 90)
    wb_dist = float(np.hypot(*(np.argwhere(water_cells) - np.array([N / 2, N / 2])).T).min() * CELL) if water_cells.any() else None
    vl, vl_err = _osm(f'[out:json][timeout:25];(node["place"~"village|hamlet|town|suburb|city"](around:5000,{lat},{lon}););out 60;')
    villages = []
    if vl is not None:
        for e in vl:
            x, y = to_utm.transform(e["lon"], e["lat"])
            d = math.hypot(x - x0, y - y0)
            bearing = (math.degrees(math.atan2(x - x0, y - y0)) + 360) % 360
            downwind = wind["status"] == "ok" and min(abs((bearing - ((wind["from_deg"] + 180) % 360) + 180) % 360 - 180), 360) <= 45
            villages.append({"name": e.get("tags", {}).get("name") or "Unnamed place", "lat": e["lat"], "lon": e["lon"], "distance_m": round(d), "downwind": bool(downwind)})
        villages.sort(key=lambda v: v["distance_m"])
        sources["villages"] = "OpenStreetMap contributors (ODbL); mapping is incomplete"
    pr, pr_err = _osm(f'[out:json][timeout:25];(nwr["boundary"="protected_area"](around:10000,{lat},{lon});nwr["leisure"="nature_reserve"](around:10000,{lat},{lon}););out center tags 20;')
    reserves = []
    if pr is not None:
        for e in pr:
            c = e.get("center") or {"lat": e.get("lat"), "lon": e.get("lon")}
            if c.get("lat") is None:
                continue
            x, y = to_utm.transform(c["lon"], c["lat"])
            reserves.append({"name": e.get("tags", {}).get("name") or "Protected area (unnamed)", "distance_m": round(math.hypot(x - x0, y - y0)), "lat": c["lat"], "lon": c["lon"]})
        reserves.sort(key=lambda r: r["distance_m"])
        sources["protected_areas"] = "OpenStreetMap contributors (ODbL); distance is to the centre of the mapped area, so it is approximate"

    # ---- the numbers behind the effects
    dens = casestudies.ASSUMPTIONS["carbon_density_t_c_per_ha"]
    carbon = {k: round(cleared_ha * dens[k] * 3.67) for k in ("low", "central", "high")}
    near_vill = [v for v in villages if v["distance_m"] <= 3000]
    down_vill = [v for v in near_vill if v["downwind"]]
    mid_forest = plume_stats[1]["forest_ha"] if plume_stats else None

    def lvl_forest():
        if cleared_ha >= 5 or (foot_ha and cleared_ha / foot_ha >= 0.25):
            return "high"
        return "medium" if cleared_ha > 0 or edge_ha_full >= 20 else "low"

    air = prof["air"]
    if mid_forest is not None and mid_forest >= 100:
        air = _bump(air, 1)
    if down_vill:
        air = _bump(air, 1)
    if mid_forest is not None and mid_forest < 5 and not down_vill:
        air = _bump(air, -1)
    water = prof["water"]
    if river_min is not None:
        water = _bump(water, 1 if river_min <= 500 else -1 if river_min > 2000 else 0)
    habitat = "low"
    if reserves and reserves[0]["distance_m"] <= 1000 or (near_edge is not None and near_edge <= 100 and edge_ha_full >= 20):
        habitat = "high"
    elif (reserves and reserves[0]["distance_m"] <= 5000) or share[1000] >= 30 or cleared_ha > 0:
        habitat = "medium"
    people = "low" if not near_vill else ("high" if down_vill and prof["air"] != "low" or len([v for v in near_vill if v["distance_m"] <= 1000]) >= 2 else "medium")
    carbon_lvl = "low" if cleared_ha < 1 else "medium" if cleared_ha < 20 else "high"
    num = lambda v: "unknown" if v is None else v  # noqa: E731
    effects = [
        {"area": "Forest", "icon": "🌲", "level": lvl_forest(), "title": f"{cleared_ha} ha of forest would be cleared" if cleared_ha else "No forest inside the site", "detail": f"{round(100 * (cleared_ha / foot_ha if foot_ha else 0))}% of the {foot_ha:g} ha site is tree cover today (ESA WorldCover). {edge_ha_full} ha more forest lies within {EDGE_M} m of the site and would feel the edge effect."},
        {"area": "Habitat", "icon": "🦌", "level": habitat, "title": (f"Protected area about {round(reserves[0]['distance_m'] / 1000, 1)} km away" if reserves else ("Forest begins " + (f"{round(near_edge)} m" if near_edge is not None else "farther than 5 km") + " from the site")), "detail": f"Tree cover is {share[500]}% within 500 m, {share[1000]}% within 1 km and {share[3000]}% within 3 km ({forest_3km} ha within 3 km)." + ("" if pr is not None else " Protected-area data was unavailable, so it is not counted.")},
        {"area": "Air", "icon": "🌫", "level": air, "title": (f"Prevailing wind from the {wind['from_name']}; {mid_forest} ha of forest in the 3 km downwind zone" if mid_forest is not None else "Wind data unavailable"), "detail": f"{prof['label']} sites typically have {prof['air']} air-emission concern. Smoke zones are drawn downwind and are illustrative, not a dispersion model." + (f" {len(down_vill)} village(s) lie downwind within 3 km." if down_vill else "")},
        {"area": "Water", "icon": "💧", "level": water, "title": (f"Nearest mapped river {river_min} m away" if river_min is not None else ("No mapped river within 3 km" if rv is not None else "River data unavailable")), "detail": f"{prof['label']} sites typically have {prof['water']} waste-water concern." + (f" A lake or wetland is {round(wb_dist)} m away (WorldCover)." if wb_dist is not None and wb_dist < 3000 else "")},
        {"area": "People", "icon": "🏘", "level": people, "title": (f"{len(near_vill)} settlement(s) within 3 km" if vl is not None else "Village data unavailable"), "detail": (", ".join(f"{v['name']} ({round(v['distance_m'] / 1000, 1)} km{', downwind' if v['downwind'] else ''})" for v in near_vill[:4]) or "None mapped nearby (OpenStreetMap coverage is incomplete).")},
        {"area": "Carbon", "icon": "🌍", "level": carbon_lvl, "title": f"About {carbon['central']:,} t CO₂ stored in the cleared trees" if cleared_ha else "No stored carbon lost", "detail": f"Range {carbon['low']:,}–{carbon['high']:,} t CO₂, assuming 70–150 t C/ha. Measure the real stand before quoting it."},
    ]
    layers = {
        "footprint": mapping(transform(to_ll.transform, translate(foot, x0, y0))),
        "edge": mapping(set_precision(transform(to_ll.transform, translate(foot.buffer(EDGE_M).difference(foot), x0, y0)), 1e-5)),
        "plumes": [mapping(set_precision(transform(to_ll.transform, translate(p, x0, y0)), 1e-5)) for p in plumes],
        "forest": _forest_polys(tree & (dist <= 3500), transform_, to_ll),
        "rivers": [{"name": r["name"], "distance_m": r["distance_m"], "line": r["line"]} for r in rivers[:6]],
        "villages": villages[:12], "reserves": reserves[:4],
    }
    projection = _projection(cleared_ha, edge_ha_full, plume_stats, prof, inputs, carbon, river_min, bool(down_vill))
    return {"status": "ok", "inputs": {**inputs, "kind": kind, "kind_label": prof["label"], "footprint_ha": foot_ha, "footprint_assumed": foot_assumed},
            "site": {"lat": lat, "lon": lon, "footprint_ha": foot_ha, "land_cover_in_footprint": footprint_classes, "forest_in_footprint_ha": cleared_ha, "edge_zone_forest_ha": edge_ha_full, "forest_within_3km_ha": forest_3km, "tree_share_pct": share, "nearest_forest_m": near_edge},
            "wind": wind, "smoke_zones": plume_stats, "carbon_t_co2": carbon, "effects": effects, "projection": projection, "layers": layers,
            "sources": sources, "unavailable": {k: v for k, v in (("rivers", rv_err), ("villages", vl_err), ("protected_areas", pr_err)) if v}, "notes": notes,
            "assumptions": {**casestudies.ASSUMPTIONS, "profiles_note": "Air/water concern by factory type is a general screening profile, not a measurement of this site.", "footprint_note": "The site is drawn as a square of the stated area centred on the chosen point. Replace it with the real boundary when you have it.", "scenario": projection["assumptions"]},
            "notice": "Screening estimate from open data. It is not an environmental impact assessment and says nothing about legality. A real assessment needs site surveys, the project's own data and the regulator's process.",
            "generated_at": datetime.now(timezone.utc).isoformat()}


def _forest_polys(mask_: np.ndarray, tr, to_ll) -> dict[str, Any] | None:
    polys = []
    for g, _ in rio_features.shapes(mask_.astype("uint8"), mask=mask_, transform=tr):
        p = shp(g)
        if p.area < 4e4:      # under 4 ha: not worth drawing
            continue
        p = p.simplify(CELL * 0.8, preserve_topology=True)
        polys.append(transform(to_ll.transform, p))
    if not polys:
        return None
    u = set_precision(unary_union(polys), 1e-5)
    return mapping(u) if not u.is_empty else None


def _projection(cleared_ha: float, edge_ha: float, plume_stats, prof, inputs, carbon, river_min, villages_downwind) -> dict[str, Any]:
    """What has happened by day 7, 30 and 90 after work starts. A scenario: the assumptions are returned and can be changed."""
    clear_days = max(1, int(inputs.get("clearing_days") or 45))
    start_op = max(0, int(inputs.get("operation_start_day") or 60))
    mid = plume_stats[1]["forest_ha"] if plume_stats else None
    rows = []
    for d in HORIZONS:
        cleared_frac = min(1.0, d / clear_days)
        edge_frac = min(1.0, d / 90.0)
        op_days = max(0, d - start_op)
        operating = op_days > 0
        rows.append({
            "day": d,
            "cleared_ha": round(cleared_ha * cleared_frac, 1), "cleared_pct": round(100 * cleared_frac),
            "edge_affected_ha": round(edge_ha * edge_frac, 1),
            "carbon_t_co2": round(carbon["central"] * cleared_frac),
            "operating": operating, "operating_days": op_days,
            "smoke_forest_ha": (round(mid * min(1.0, op_days / 30.0), 1) if (mid is not None and operating) else 0.0),
            "water_pathway": ("open" if (operating and river_min is not None and river_min <= 2000) else "not yet running" if not operating else "no river close by"),
            "phase": "Clearing and building" if not operating else "Running",
            "headline": (f"{round(cleared_ha * cleared_frac, 1)} ha cleared" + (", plant running" if operating else ", building")),
        })
    rows_note = f"Scenario: clearing takes {clear_days} days; operation starts on day {start_op}; edge effects grow to full width by day 90; smoke reaches the mapped downwind zone 30 days after start."
    return {"horizons": rows, "assumptions": {"clearing_days": clear_days, "operation_start_day": start_op, "edge_full_by_day": 90, "smoke_full_after_days": 30, "note": rows_note, "emissions": "Emission quantities are not projected unless you supply rates; no pollutant concentration is predicted."}}
