"""OpenStreetMap context (Overpass API): roads, settlements, waterways, protected areas near a geometry.

Data (c) OpenStreetMap contributors, ODbL. OSM completeness varies, especially for forest tracks and
village boundaries, so "none found" means "none mapped", never "none exist".
"""
import time
from typing import Any

import httpx
from pyproj import Transformer
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import transform as shp_transform

from app.core.geometry import to_shape

OVERPASS_URLS = ("https://overpass.openstreetmap.fr/api/interpreter", "https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter")
ATTRIBUTION = "Map data (c) OpenStreetMap contributors, ODbL 1.0"
MAX_SPAN_DEG = 0.5  # reject very large query areas
BUFFER_DEG = 0.03   # about 3 km search margin


class OsmUnavailable(RuntimeError):
    pass


def _query_features(bbox) -> str:
    box = f"{bbox[1]},{bbox[0]},{bbox[3]},{bbox[2]}"
    return f"""[out:json][timeout:25];
(way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|track)$"]({box});
 node["place"~"^(city|town|village|hamlet)$"]({box});
 way["waterway"~"^(river|stream|canal)$"]({box}););
out geom tags 600;"""


def _query_areas(centre) -> str:
    lon, lat = centre
    return f"""[out:json][timeout:25];
is_in({lat},{lon})->.a;
(relation(pivot.a)["boundary"="protected_area"]; way(pivot.a)["boundary"="protected_area"];
 relation(pivot.a)["leisure"="nature_reserve"]; way(pivot.a)["leisure"="nature_reserve"];);
out tags;"""


def fetch(query: str, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    client = client or httpx.Client(timeout=httpx.Timeout(30, connect=8), headers={"User-Agent": "VanaNetra/0.2 (forest monitoring)"})
    last: Exception | None = None
    for url in OVERPASS_URLS:
        try:
            r = client.post(url, data={"data": query})
            r.raise_for_status()
            data = r.json()
            if str(data.get("remark", "")).startswith("runtime error"):
                raise ValueError("overpass runtime error")  # mirror cannot answer; try the next one
            return data.get("elements", [])
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
    raise OsmUnavailable(f"Overpass unavailable: {type(last).__name__}")


def context(geometry: dict[str, Any], client: httpx.Client | None = None) -> dict[str, Any]:
    geom = to_shape(geometry)
    minx, miny, maxx, maxy = geom.bounds
    if (maxx - minx) > MAX_SPAN_DEG or (maxy - miny) > MAX_SPAN_DEG:
        return {"status": "skipped", "reason": "Geometry too large for an OSM context query", "attribution": ATTRIBUTION}
    c = geom.centroid
    elements = fetch(_query_features((minx - BUFFER_DEG, miny - BUFFER_DEG, maxx + BUFFER_DEG, maxy + BUFFER_DEG)), client)
    try:
        area_elements = fetch(_query_areas((c.x, c.y)), client)
        areas_status = "ok"
    except OsmUnavailable as exc:
        area_elements, areas_status = [], f"unavailable: {exc}"
    proj = Transformer.from_crs("EPSG:4326", f"+proj=laea +lat_0={c.y} +lon_0={c.x} +datum=WGS84", always_xy=True)
    target = shp_transform(proj.transform, geom)
    nearest: dict[str, tuple[float, str | None, str | None]] = {}

    def consider(kind: str, shape, name: str | None, sub: str | None):
        d = target.distance(shp_transform(proj.transform, shape))
        if kind not in nearest or d < nearest[kind][0]:
            nearest[kind] = (d, name, sub)

    protected = []
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name")
        if el["type"] == "node":
            consider("settlement", Point(el["lon"], el["lat"]), name, tags.get("place"))
        elif el["type"] == "way" and el.get("geometry"):
            pts = [(p["lon"], p["lat"]) for p in el["geometry"]]
            if "highway" in tags and len(pts) > 1:
                consider("road", LineString(pts), name, tags["highway"])
            elif "waterway" in tags and len(pts) > 1:
                consider("waterway", LineString(pts), name, tags["waterway"])
        pass
    for el in area_elements:
        tags = el.get("tags", {})
        name = tags.get("name")
        if "boundary" in tags or "leisure" in tags:
            protected.append({"name": name, "kind": tags.get("boundary") or tags.get("leisure"), "designation": tags.get("protect_class") or tags.get("designation")})
    out: dict[str, Any] = {"status": "ok", "claim_type": "observation_third_party", "attribution": ATTRIBUTION, "search_margin_km": 3, "caveat": "OSM mapping is incomplete; absence of a feature here means none mapped.", "elements_returned": len(elements), "possibly_truncated": len(elements) >= 600}
    for kind in ("road", "settlement", "waterway"):
        if kind in nearest:
            d, name, sub = nearest[kind]
            out[f"nearest_{kind}"] = {"distance_m": round(d), "name": name, "type": sub}
        else:
            out[f"nearest_{kind}"] = None
    seen, unique = set(), []
    for p in protected:
        key = (p["name"], p["kind"])
        if key not in seen:
            seen.add(key); unique.append(p)
    out["protected_areas_status"] = areas_status
    out["protected_areas_containing_centroid"] = unique if areas_status == "ok" else None
    out["protected_area_note"] = "Only areas containing the geometry centroid are listed; edge overlaps are not detected."
    return out
