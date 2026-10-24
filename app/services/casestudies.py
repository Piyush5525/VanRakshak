"""Worked example cases: what happens when a factory is built beside, or by clearing, a forest.

These are EXAMPLES. The shapes are drawn by hand around a real forest-edge location to show the method; they are not
a record of a real company or event. Every number is computed from those shapes (area, edge zone, fragmentation) or from
a stated assumption (carbon density, influence distances). Nothing is measured, and the plume rings are not a dispersion model.
"""
import math
from typing import Any

from shapely.geometry import LineString, Point, Polygon, mapping
from shapely.ops import transform, unary_union

ASSUMPTIONS = {
    "carbon_density_t_c_per_ha": {"low": 70, "central": 110, "high": 150, "note": "Assumed above-ground carbon density of tropical forest; real stands vary widely and should be measured."},
    "edge_zone_m": 300, "road_clearing_width_m": 30,
    "edge_note": "Forest within 300 m of cleared land is treated as edge-affected (light, wind, dryness, noise, invasive species). The distance is an assumption from the ecology literature, not a measurement here.",
    "plume_note": "Influence rings are 'potentially affected downwind' zones drawn for illustration. They are not a dispersion model and do not predict concentrations.",
}


def _circ(x: float, y: float, r: float) -> Polygon:
    return Point(x, y).buffer(r, 24)


def _forest(parts: list[tuple[float, float, float]]) -> Polygon:
    return unary_union([_circ(*p) for p in parts])


CASES: list[dict[str, Any]] = [
    {"id": "steel-sal", "title": "Steel plant cut into a sal forest", "where": "Saranda region, Jharkhand (example site)", "lat": 22.15, "lon": 85.45, "wind_from": 270,
     "summary": "A clearing, a plant, two chimneys, a drain to the river and a road right across a continuous forest.",
     "forest": _forest([(0, 0, 2600), (-1900, 700, 1700), (1900, -400, 1800), (200, 1700, 1500), (-300, -1900, 1500)]),
     "river": [(-3600, -1200), (-2000, -1000), (-600, -1100), (900, -900), (2200, -1400), (3800, -1300)],
     "cleared": [(-420, -200), (320, -300), (520, 180), (60, 420), (-380, 240)], "plant": [(-300, -120), (200, -180), (300, 100), (-60, 250), (-280, 140)],
     "road": [(-4000, 300), (-2500, 250), (-1200, 80), (-420, 0), (500, -60), (2000, -150), (4200, -100)], "chimneys": [(-120, 20), (90, 60)], "corridor": [(-1500, 2800), (-900, 900), (-300, 300), (500, -1200), (900, -3000)],
     "effluent": [(120, -230), (260, -700), (500, -1000)]},
    {"id": "cement-buffer", "title": "Cement unit beside a reserve buffer", "where": "Near a tiger-reserve buffer, Maharashtra (example site)", "lat": 20.20, "lon": 79.35, "wind_from": 315,
     "summary": "Built on farmland next to the forest: little forest is cleared, but dust, noise and trucks reach into it.",
     "forest": _forest([(0, 1400, 2400), (-2200, 1800, 1700), (2000, 1900, 1600), (0, 3300, 1500)]),
     "river": [(-3500, 400), (-1500, 300), (400, 600), (2000, 400), (3800, 700)],
     "cleared": [(-260, 560), (260, 520), (330, 900), (-180, 980)], "plant": [(-520, -120), (420, -160), (440, 340), (-500, 380)],
     "road": [(-3000, -600), (-1200, -420), (-300, 200), (-120, 600)], "chimneys": [(-200, 60), (150, 80)], "corridor": [(-2800, 3600), (-2500, 1900), (-2400, 500), (-2100, -800)],
     "effluent": [(0, -160), (60, -100), (220, 520)]},
    {"id": "ghats-foothill", "title": "Processing factory at the Western Ghats foothills", "where": "Wayanad foothills, Kerala (example site)", "lat": 11.60, "lon": 76.02, "wind_from": 250,
     "summary": "A small factory and an access road thread into a narrow forest strip along a river.",
     "forest": _forest([(0, 0, 1700), (-1500, 400, 1200), (1500, -200, 1300), (-200, 1500, 1100)]),
     "river": [(-3000, -700), (-1500, -600), (-200, -500), (1200, -700), (3000, -600)],
     "cleared": [(-210, 150), (150, 120), (200, 420), (-140, 470)], "plant": [(-160, 190), (110, 170), (150, 380), (-110, 410)],
     "road": [(-2600, 800), (-1500, 500), (-700, 420), (-210, 300), (500, 200), (1500, 50), (2600, -100)], "chimneys": [(-30, 290)], "corridor": [(-1200, 1600), (-400, 700), (0, -300), (300, -1800)],
     "effluent": [(0, 170), (-60, -200), (-100, -500)]},
]


def _ll(lat0: float, lon0: float):
    kx, ky = 111320.0 * math.cos(math.radians(lat0)), 110540.0
    return lambda x, y, z=None: (lon0 + x / kx, lat0 + y / ky)


def _gj(geom, f) -> dict | None:
    return None if geom is None or geom.is_empty else mapping(transform(f, geom))


def _ha(g) -> float:
    return round(g.area / 1e4, 1)


def _plumes(chimneys: list[tuple[float, float]], wind_from: float) -> list[Polygon]:
    """Three nested downwind ellipses (about 1.5, 3 and 5 km long) from the chimneys."""
    away = math.radians((wind_from + 180) % 360)
    out = []
    for length in (1500, 3000, 5000):
        cx, cy = sum(c[0] for c in chimneys) / len(chimneys), sum(c[1] for c in chimneys) / len(chimneys)
        e = Point(0, 0).buffer(1, 32)
        e = transform(lambda x, y, z=None: (x * length / 2, y * length / 4), e)
        theta = math.pi / 2 - away
        e = transform(lambda x, y, z=None: (x * math.cos(theta) - y * math.sin(theta) + cx + math.sin(away) * length / 2, x * math.sin(theta) + y * math.cos(theta) + cy + math.cos(away) * length / 2), e)
        out.append(e)
    return out


def _patches(g) -> list[Polygon]:
    return [p for p in getattr(g, "geoms", [g]) if not p.is_empty]


def build(case_id: str) -> dict[str, Any] | None:
    c = next((x for x in CASES if x["id"] == case_id), None)
    if not c:
        return None
    f = _ll(c["lat"], c["lon"])
    forest, river, corridor = c["forest"], LineString(c["river"]), LineString(c["corridor"])
    cleared, plant = Polygon(c["cleared"]).intersection(forest), Polygon(c["plant"])
    road_raw = LineString(c["road"]).intersection(forest)
    road = road_raw.buffer(ASSUMPTIONS["road_clearing_width_m"] / 2) if not road_raw.is_empty else Polygon()
    lost = unary_union([cleared, road])
    edge_r = ASSUMPTIONS["edge_zone_m"]
    plumes = _plumes([tuple(p) for p in c["chimneys"]], c["wind_from"])
    eff = LineString(c["effluent"])
    forest_ha0 = forest.area / 1e4

    def metrics(remaining, lost_now, edge_now) -> dict[str, Any]:
        ps = sorted(_patches(remaining), key=lambda p: -p.area)
        lost_ha = lost_now.area / 1e4 if lost_now is not None and not lost_now.is_empty else 0.0
        tco2 = lambda d: round(lost_ha * d * 3.67)  # noqa: E731
        k = ASSUMPTIONS["carbon_density_t_c_per_ha"]
        return {"forest_ha": _ha(remaining), "lost_ha": round(lost_ha, 1), "lost_pct": round(100 * lost_ha / forest_ha0, 2), "edge_affected_ha": _ha(edge_now) if edge_now is not None and not edge_now.is_empty else 0.0,
                "patches": len(ps), "largest_patch_ha": _ha(ps[0]) if ps else 0.0, "carbon_t_co2": {"low": tco2(k["low"]), "central": tco2(k["central"]), "high": tco2(k["high"])}}

    stages = []
    # 0 before
    stages.append({"key": "before", "label": "Before", "year": "Year 0", "caption": "An unbroken forest with a river and an animal corridor.",
                   "layers": {"forest": _gj(forest, f), "river": _gj(river, f), "corridor": _gj(corridor, f)}, "metrics": metrics(forest, None, None)})
    # 1 clearing
    rem1 = forest.difference(unary_union([cleared, road]))
    edge1 = forest.intersection(unary_union([cleared, road]).buffer(edge_r)).difference(unary_union([cleared, road]))
    stages.append({"key": "clearing", "label": "Clearing", "year": "Year 1", "caption": "Trees are felled for the site and the access road.",
                   "layers": {"forest": _gj(rem1, f), "river": _gj(river, f), "corridor": _gj(corridor, f), "cleared": _gj(cleared, f), "road": _gj(road, f), "edge": _gj(edge1, f)}, "metrics": metrics(rem1, lost, edge1)})
    # 2 construction
    stages.append({"key": "building", "label": "Building", "year": "Year 2", "caption": "The plant is built on the cleared ground.",
                   "layers": {"forest": _gj(rem1, f), "river": _gj(river, f), "corridor": _gj(corridor, f), "cleared": _gj(cleared.difference(plant), f), "road": _gj(road, f), "plant": _gj(plant, f), "edge": _gj(edge1, f)}, "metrics": metrics(rem1, lost, edge1)})
    # 3 operating: edge widens with operation, plumes + effluent
    infl = unary_union(plumes[1:2]).intersection(rem1)
    stages.append({"key": "running", "label": "Running", "year": "Year 5", "caption": "The plant is working: smoke drifts downwind and waste water runs to the river.",
                   "layers": {"forest": _gj(rem1, f), "river": _gj(river, f), "corridor": _gj(corridor, f), "cleared": _gj(cleared.difference(plant), f), "road": _gj(road, f), "plant": _gj(plant, f), "edge": _gj(edge1, f),
                              "plumes": [_gj(p, f) for p in plumes], "effluent": _gj(eff, f), "chimneys": [{"lon": f(*p)[0], "lat": f(*p)[1]} for p in c["chimneys"]]},
                   "metrics": {**metrics(rem1, lost, edge1), "downwind_forest_ha": _ha(infl)}})
    m0, m3 = stages[0]["metrics"], stages[3]["metrics"]
    crosses = corridor.intersects(unary_union([cleared, road, plant]).buffer(40))
    river_hit = eff.intersects(river.buffer(60)) or eff.distance(river) < 450
    low, hi = m3["carbon_t_co2"]["low"], m3["carbon_t_co2"]["high"]
    effects = [
        {"area": "Forest", "icon": "🌲", "level": "high" if m3["lost_pct"] > 1 else "medium", "title": f"{m3['lost_ha']} ha of forest lost", "detail": f"{m3['lost_pct']}% of this forest block, cleared for the site and the road."},
        {"area": "Habitat", "icon": "🦌", "level": "high" if m3["patches"] > m0["patches"] else "medium", "title": f"Forest broken into {m3['patches']} piece{'s' if m3['patches'] != 1 else ''}" if m3["patches"] > 1 else "Forest edge pushed inward", "detail": f"Largest unbroken patch {m0['largest_patch_ha']} → {m3['largest_patch_ha']} ha. {m3['edge_affected_ha']} ha now sits within {edge_r} m of cleared land (edge effect)."},
        {"area": "Wildlife corridor", "icon": "🐘", "level": "high" if crosses else "low", "title": "Corridor cut by the site" if crosses else "Corridor not crossed", "detail": "The dotted animal corridor passes through the cleared ground and road." if crosses else "The site stays clear of the corridor, but traffic and noise still reach it."},
        {"area": "Water", "icon": "💧", "level": "high" if river_hit else "medium", "title": "Waste-water path reaches the river" if river_hit else "Waste-water path ends near the river", "detail": "The drain from the plant runs towards the river; untreated discharge would affect fish and downstream villages."},
        {"area": "Air", "icon": "🌫", "level": "medium", "title": f"{m3.get('downwind_forest_ha', 0)} ha of forest lies in the 3 km downwind zone", "detail": f"Wind from {_compass(c['wind_from'])}: smoke and dust drift towards the forest. Rings show 1.5, 3 and 5 km (illustrative)."},
        {"area": "Carbon", "icon": "🌍", "level": "medium", "title": f"About {m3['carbon_t_co2']['central']:,} t CO₂ released", "detail": f"Range {low:,} – {hi:,} t CO₂ for the cleared area, assuming 70–150 t C/ha. Measure the real stand before quoting this."},
    ]
    return {"id": c["id"], "title": c["title"], "where": c["where"], "summary": c["summary"], "center": {"lat": c["lat"], "lon": c["lon"]}, "wind_from": c["wind_from"], "stages": stages, "effects": effects, "assumptions": ASSUMPTIONS,
            "notice": "Worked example. The shapes are illustrative and placed at a real forest-edge location; this is not a record of a real company or event."}


def _compass(deg: float) -> str:
    return ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"][int((deg + 22.5) // 45) % 8]


def listing() -> list[dict[str, Any]]:
    return [{"id": c["id"], "title": c["title"], "where": c["where"], "summary": c["summary"], "lat": c["lat"], "lon": c["lon"]} for c in CASES]
