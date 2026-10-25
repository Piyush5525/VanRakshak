"""New construction and land-use change over the last 365 days, from Sentinel-2 satellite images.

How a change is found (all real imagery, nothing simulated):
  * Four cloud-masked composites of the same place: one year ago (before), now (after) and two in between.
  * A place counts as changed only if it was green (or dark bare soil) a year ago and has stayed non-green / brighter in EVERY later
    composite. A field that is harvested and then grows a new crop turns green again in between, so it is not reported.
  * Changed pixels are joined into patches and drawn as polygons. Each patch is classified from the shape and from OpenStreetMap
    (roads, buildings, land use). When OpenStreetMap has nothing there the class comes from the shape alone and says so.
  * The impact (vegetation and tree cover lost, hard surface added, carbon, what is nearby) is measured from the same images and from
    ESA WorldCover. General effects are listed separately and marked as typical, not measured.

Limits, stated plainly: Sentinel-2 pixels are 10 m (20 m for big areas), so a narrow road or a single house can be missed;
construction that replaces older buildings or hard surface shows little spectral change; a patch is a satellite detection, not a survey.
"""
import hashlib
import io
import json
import math
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from pyproj import Transformer
from rasterio import features as rio_features
from rasterio.enums import Resampling
from scipy import ndimage
from shapely.geometry import LineString, Point, Polygon, box, mapping, shape
from shapely.ops import transform as shp_transform, unary_union
from shapely.strtree import STRtree

from app.core.geometry import to_shape
from app.db.database import audit, connection, decode_row, get_one, insert, now, update
from app.services import osm
from app.services.acquisition import provider
from app.services.casestudies import ASSUMPTIONS
from app.services.satellite import S2_CLOUD_CLASSES, AcquisitionConfig, ProviderError, make_grid

DATA = Path(os.environ.get("INFRA_DIR") or Path(__file__).resolve().parent.parent.parent / "data" / "infra")
HISTORY_DAYS = 365
HI_RES_KM2 = 130.0     # up to this area the images are read at 10 m, above it at 20 m
MAX_AREA_KM2 = 450.0
MIN_AREA_KM2 = 0.3

# class -> (label, colour, adds hard surface?)
CLASSES: dict[str, dict[str, Any]] = {
    "highway": {"label": "Highway / main road", "color": "#d9480f", "hard": True},
    "road": {"label": "Road", "color": "#ff922b", "hard": True},
    "building": {"label": "Buildings", "color": "#1c7ed6", "hard": True},
    "industrial": {"label": "Industrial / commercial", "color": "#9c36b5", "hard": True},
    "construction": {"label": "Construction site", "color": "#fab005", "hard": True},
    "quarry": {"label": "Quarry / mining", "color": "#8d6e63", "hard": False},
    "energy": {"label": "Solar / energy", "color": "#12b886", "hard": True},
    "cleared": {"label": "Cleared land (use unknown)", "color": "#868e96", "hard": False},
}
ROAD_WIDTH_M = {"motorway": 20, "trunk": 18, "primary": 14, "secondary": 11, "tertiary": 9, "unclassified": 7, "residential": 7, "service": 5, "track": 5}
WC = {10: "tree cover", 20: "shrub", 30: "grass", 40: "cropland", 50: "built-up", 60: "bare ground", 70: "snow", 80: "water", 90: "wetland", 95: "mangrove", 100: "moss"}

PRESETS: dict[str, dict[str, Any]] = {
    "jaipur-sanganer": {"name": "Jaipur south-west: Sanganer, Bagru, Mahindra World City", "bbox": [75.62, 26.78, 75.70, 26.85]},
    "jaipur-jagatpura": {"name": "Jaipur south: Jagatpura, Sitapura, Tonk Road", "bbox": [75.80, 26.74, 75.89, 26.82]},
    "jaipur-kukas": {"name": "Jaipur north: Delhi Road, Kukas, Amer", "bbox": [75.82, 26.96, 75.92, 27.04]},
}


class InfraError(ValueError):
    pass


@dataclass
class Params:
    green: float = 0.30          # NDVI above this = vegetated
    gone: float = 0.28           # NDVI never above this afterwards = no longer green
    min_obs: int = 2             # the "after" side needs the final image and at least one in-between image
    bright_gain: float = 0.035   # brighter by this much (reflectance) ...
    ndbi_gain: float = 0.02      # ... and more built-up-like (SWIR/NIR) by this much, in at least two images
    bright_floor: float = 0.16
    min_ha: float = 0.25


def area_km2(bbox: list[float]) -> float:
    w, s, e, n = bbox
    return abs(e - w) * 111.32 * math.cos(math.radians((s + n) / 2)) * abs(n - s) * 110.57


def validate(bbox: list[float], polygon: dict[str, Any] | None = None) -> tuple[list[float], float, float]:
    if len(bbox) != 4 or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in bbox):
        raise InfraError("bbox must be [west, south, east, north]")
    w, s, e, n = (float(x) for x in bbox)
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise InfraError("The area is not valid")
    km2 = area_km2([w, s, e, n])
    if polygon:
        from app.core.geometry import geodesic_area_hectares

        km2 = geodesic_area_hectares(to_shape(polygon)) / 100.0
    if km2 < MIN_AREA_KM2:
        raise InfraError(f"Choose an area of at least {MIN_AREA_KM2} km²")
    if km2 > MAX_AREA_KM2:
        raise InfraError(f"That area is {km2:.0f} km². The limit is {MAX_AREA_KM2:.0f} km²; draw a smaller one")
    from app.services import boundary

    if not boundary.contains((s + n) / 2, (w + e) / 2):
        raise InfraError("The centre of this area is outside India")
    return [w, s, e, n], km2, (10.0 if km2 <= HI_RES_KM2 else 20.0)


def dates(today: date | None = None) -> dict[str, Any]:
    """The four moments compared. The same time of year is used for before and after so that seasons do not look like change."""
    t = today or date.today()
    after = t - timedelta(days=20)
    return {"after": after, "before": after - timedelta(days=HISTORY_DAYS), "mids": [after - timedelta(days=75), after - timedelta(days=215)], "half": 22}


# ---------------------------------------------------------------- imagery
def _cache_path(key: str) -> Path:
    return DATA / "cache" / f"{key}.npz"


def composite(bbox: list[float], res: float, centre: date, half: int, n: int, max_cloud: float, polygon: dict | None = None) -> dict[str, Any]:
    """Median of the clearest Sentinel-2 images around `centre`: bands B02 B03 B04 B08 B11 as float32 (NaN where cloud or no image)."""
    geom = mapping(box(*bbox))
    cfg = AcquisitionConfig(resolution_m=res, max_pixels=9_000_000)
    grid = make_grid(geom, cfg)
    key = hashlib.sha1(json.dumps([bbox, res, centre.isoformat(), half, n, max_cloud]).encode()).hexdigest()[:24]
    f = _cache_path(key)
    if f.is_file():
        z = np.load(f, allow_pickle=False)
        return {"bands": {k: z[k].astype("float32") for k in ("B02", "B03", "B04", "B08", "B11")}, "scenes": json.loads(str(z["scenes"])), "grid": grid, "cached": True}
    from datetime import timedelta as td

    pc = provider()
    items = pc.search("sentinel-2-l2a", bbox, centre - td(days=half), centre + td(days=half), {"eo:cloud_cover": {"lt": max_cloud}}, limit=80)
    items = sorted(items, key=lambda i: i["properties"].get("eo:cloud_cover", 100))[:n]
    if not items:
        return {"bands": None, "scenes": [], "grid": grid, "cached": False}

    def band(it, name):
        a = it["assets"][name]
        meta = (a.get("raster:bands") or [{}])[0]
        d = pc._read_band("sentinel-2-l2a", a, grid, Resampling.bilinear)
        return np.ma.masked_less_equal(d, 0).astype("float32") * meta.get("scale", 1e-4) + meta.get("offset", 0.0)

    def read(it):
        try:
            with ThreadPoolExecutor(6) as ex:
                fs = {k: ex.submit(band, it, k) for k in ("B02", "B03", "B04", "B08", "B11")}
                fscl = ex.submit(pc._read_band, "sentinel-2-l2a", it["assets"]["SCL"], grid, Resampling.nearest)
                out = {k: v.result() for k, v in fs.items()}
                scl = fscl.result()
        except Exception:  # noqa: BLE001 - one bad image must not sink the composite
            return None
        bad = np.isin(scl.filled(0), S2_CLOUD_CLASSES)
        return {k: np.where(np.ma.getmaskarray(v) | bad, np.nan, v.filled(np.nan)).astype("float32") for k, v in out.items()}, it

    with ThreadPoolExecutor(2) as ex:
        got = [g for g in ex.map(read, items) if g]
    if not got:
        return {"bands": None, "scenes": [], "grid": grid, "cached": False}
    with np.errstate(all="ignore"):
        bands = {k: np.nanmedian(np.stack([g[0][k] for g in got]), axis=0).astype("float32") for k in ("B02", "B03", "B04", "B08", "B11")}
    scenes = [{"id": g[1]["id"], "date": g[1]["properties"]["datetime"][:10], "cloud": round(g[1]["properties"].get("eo:cloud_cover", 0))} for g in got]
    f.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(f, scenes=np.array(json.dumps(scenes)), **{k: v.astype("float16") for k, v in bands.items()})
    return {"bands": bands, "scenes": scenes, "grid": grid, "cached": False}


def indices(b: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    e = 1e-6
    return {"ndvi": (b["B08"] - b["B04"]) / (b["B08"] + b["B04"] + e), "ndbi": (b["B11"] - b["B08"]) / (b["B11"] + b["B08"] + e),
            "mndwi": (b["B03"] - b["B11"]) / (b["B03"] + b["B11"] + e), "bright": (b["B02"] + b["B03"] + b["B04"]) / 3}


def detect(before: dict[str, np.ndarray], afters: list[dict[str, np.ndarray]], p: Params = Params()) -> dict[str, np.ndarray]:
    """Masks of changed ground. `afters[0]` is the newest image; the rest are in-between images (may contain NaN)."""
    ib = indices(before)
    ia = [indices(a) for a in afters]
    ndvi_stack = np.stack([i["ndvi"] for i in ia])
    with np.errstate(all="ignore"):
        after_max = np.nanmax(np.where(np.isfinite(ndvi_stack), ndvi_stack, -9), axis=0)
    n_obs = np.sum(np.isfinite(ndvi_stack), axis=0)
    valid = np.isfinite(ib["ndvi"]) & np.isfinite(ia[0]["ndvi"]) & (n_obs >= p.min_obs)
    water = (ib["mndwi"] > 0) | np.any(np.stack([np.nan_to_num(i["mndwi"], nan=-1) > 0 for i in ia]), axis=0)
    veg = valid & ~water & (ib["ndvi"] > p.green) & (after_max < p.gone)
    gain = np.stack([(np.nan_to_num(i["bright"], nan=0) - ib["bright"] > p.bright_gain) & (np.nan_to_num(i["ndbi"], nan=-1) - ib["ndbi"] > p.ndbi_gain) for i in ia])
    brightest_ok = np.nanmin(np.stack([np.where(np.isfinite(i["bright"]), i["bright"], np.inf) for i in ia]), axis=0) > p.bright_floor
    bare = valid & ~water & (ib["ndvi"] < p.gone) & (after_max < p.gone) & (gain.sum(axis=0) >= 2) & gain[0] & brightest_ok
    return {"veg": veg, "bare": bare & ~veg, "ndvi_before": ib["ndvi"], "ndvi_after": ia[0]["ndvi"], "bright_gain": np.nan_to_num(ia[0]["bright"], nan=0) - ib["bright"], "valid": valid}


# ---------------------------------------------------------------- patches
def patches_from(masks: dict[str, np.ndarray], grid, p: Params, inside: np.ndarray | None = None) -> tuple[np.ndarray, list[dict[str, Any]]]:
    m = masks["veg"] | masks["bare"]
    if inside is not None:
        m &= inside
    m = ndimage.binary_closing(ndimage.binary_opening(m, iterations=1), iterations=1)
    lab, n = ndimage.label(m)
    if n == 0:
        return lab, []
    min_px = max(4, int(math.ceil(p.min_ha * 1e4 / (grid.resolution ** 2))))
    sizes = ndimage.sum(m, lab, range(1, n + 1))
    keep = [i + 1 for i, s in enumerate(sizes) if s >= min_px]
    to_ll = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True).transform
    out: list[dict[str, Any]] = []
    slices = ndimage.find_objects(lab)
    px_ha = grid.resolution ** 2 / 1e4
    for i in keep:
        sl = slices[i - 1]
        sub = lab[sl] == i
        pm = {"veg": int((masks["veg"][sl] & sub).sum()), "bare": int((masks["bare"][sl] & sub).sum())}
        polys = []
        for g, v in rio_features.shapes(sub.astype("uint8"), mask=sub, transform=grid.transform * grid.transform.__class__.translation(sl[1].start, sl[0].start)):
            if v == 1:
                polys.append(shape(g))
        if not polys:
            continue
        poly = max(polys, key=lambda q: q.area).simplify(grid.resolution * 0.4)
        if poly.is_empty:
            continue
        out.append({"label": i, "px": int(sub.sum()), "area_ha": round(float(sub.sum()) * px_ha, 2), "poly_utm": poly, "poly_ll": shp_transform(to_ll, poly), "slice": sl, "src": "veg" if pm["veg"] >= pm["bare"] else "bare"})
    out.sort(key=lambda q: -q["px"])
    return lab, out


# ---------------------------------------------------------------- OpenStreetMap context
OSM_QUERY = """[out:json][timeout:60][maxsize:300000000];
(way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|service|track)$"]({b});
 way["building"]({b});
 way["landuse"~"^(industrial|commercial|retail|residential|construction|quarry|brownfield|landfill|forest)$"]({b});
 way["power"~"^(plant|generator)$"]({b});
 way["natural"~"^(water|wood)$"]({b});
 way["waterway"~"^(river|canal|stream)$"]({b}););
out geom qt;"""


def osm_features(bbox: list[float], to_utm) -> dict[str, Any] | None:
    """OpenStreetMap objects in the area, in metres. None when OpenStreetMap cannot be reached (classification then uses shape only)."""
    w, s, e, n = bbox
    try:
        els = osm.fetch(OSM_QUERY.format(b=f"{s - 0.01},{w - 0.01},{n + 0.01},{e + 0.01}"))
    except osm.OsmUnavailable:
        return None
    feats: dict[str, list[tuple[Any, dict[str, str]]]] = {"road": [], "building": [], "landuse": [], "power": [], "water": [], "forest": []}
    for el in els:
        g, tags = el.get("geometry"), el.get("tags", {})
        if el.get("type") != "way" or not g or len(g) < 2:
            continue
        pts = [to_utm(q["lon"], q["lat"]) for q in g if q]
        try:
            if "highway" in tags:
                feats["road"].append((LineString(pts), tags))
            elif "waterway" in tags:
                feats["water"].append((LineString(pts), tags))
            elif len(pts) >= 4:
                poly = Polygon(pts)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                if poly.is_empty:
                    continue
                if "building" in tags:
                    feats["building"].append((poly, tags))
                elif tags.get("natural") == "water":
                    feats["water"].append((poly, tags))
                elif tags.get("natural") == "wood" or tags.get("landuse") == "forest":
                    feats["forest"].append((poly, tags))
                elif "power" in tags:
                    feats["power"].append((poly, tags))
                elif "landuse" in tags:
                    feats["landuse"].append((poly, tags))
        except Exception:  # noqa: BLE001 - one odd geometry must not stop the others
            continue
    return build_ctx(feats) | {"count": len(els)}


def build_ctx(feats: dict[str, list[tuple[Any, dict[str, str]]]]) -> dict[str, Any]:
    """Index OpenStreetMap objects (already in metres) by kind so overlaps and distances are quick."""
    kinds = ("road", "building", "landuse", "power", "water", "forest")
    return {k: ([x[0] for x in feats.get(k, [])], [x[1] for x in feats.get(k, [])], STRtree([x[0] for x in feats[k]]) if feats.get(k) else None) for k in kinds}


def _overlap(poly, ctx_kind, ctx, buffer_by=None, pick=None) -> tuple[float, dict[str, str] | None]:
    """Share of `poly` covered by objects of a kind, and the tags of the biggest overlap."""
    geoms, tags, tree = ctx[ctx_kind]
    if tree is None:
        return 0.0, None
    best, best_tags, parts = 0.0, None, []
    for i in tree.query(poly):
        g = geoms[int(i)]
        if buffer_by:
            g = g.buffer(buffer_by(tags[int(i)]))
        if pick and not pick(tags[int(i)]):
            continue
        a = poly.intersection(g).area
        if a > 0:
            parts.append(g)
            if a > best:
                best, best_tags = a, tags[int(i)]
    if not parts:
        return 0.0, None
    return poly.intersection(unary_union(parts)).area / max(poly.area, 1.0), best_tags


def shape_of(poly) -> dict[str, float]:
    mrr = poly.minimum_rotated_rectangle
    xs, ys = mrr.exterior.coords.xy
    sides = sorted([math.hypot(xs[1] - xs[0], ys[1] - ys[0]), math.hypot(xs[2] - xs[1], ys[2] - ys[1])])
    width, length = max(sides[0], 1.0), max(sides[1], 1.0)
    return {"length_m": round(length), "width_m": round(width), "aspect": round(length / width, 1), "fill": round(poly.area / max(mrr.area, 1.0), 2)}


def classify(poly_utm, ctx: dict[str, Any] | None, spectral: dict[str, float]) -> dict[str, Any]:
    """Class, where it came from, and a name if OpenStreetMap has one. Order matters: the most specific evidence wins."""
    sh = shape_of(poly_utm)
    if ctx:
        f, t = _overlap(poly_utm, "landuse", ctx, pick=lambda g: g.get("landuse") == "construction")
        if f >= 0.2:
            return {"cls": "construction", "source": "OpenStreetMap: landuse=construction", "name": (t or {}).get("name"), **sh}
        f, t = _overlap(poly_utm, "landuse", ctx, pick=lambda g: g.get("landuse") in ("quarry", "landfill", "brownfield"))
        if f >= 0.2:
            return {"cls": "quarry", "source": f"OpenStreetMap: landuse={(t or {}).get('landuse')}", "name": (t or {}).get("name"), **sh}
        f, t = _overlap(poly_utm, "power", ctx)
        if f >= 0.2:
            return {"cls": "energy", "source": f"OpenStreetMap: power={(t or {}).get('power')}", "name": (t or {}).get("name"), **sh}
        f, t = _overlap(poly_utm, "landuse", ctx, pick=lambda g: g.get("landuse") in ("industrial", "commercial", "retail"))
        fb, tb = _overlap(poly_utm, "building", ctx, pick=lambda g: g.get("building") in ("industrial", "commercial", "retail", "warehouse", "factory"))
        if f >= 0.25 or fb >= 0.2:
            tt = t if f >= fb else tb
            return {"cls": "industrial", "source": "OpenStreetMap: " + (f"landuse={tt.get('landuse')}" if f >= fb else f"building={tt.get('building')}"), "name": (tt or {}).get("name"), **sh}
        fr, tr = _overlap(poly_utm, "road", ctx, buffer_by=lambda g: ROAD_WIDTH_M.get(g.get("highway"), 6) / 2 + 6)
        if fr >= 0.3 and (sh["aspect"] >= 2.5 or fr >= 0.6):
            major = (tr or {}).get("highway") in ("motorway", "trunk", "primary")
            return {"cls": "highway" if major else "road", "source": f"OpenStreetMap: highway={(tr or {}).get('highway')}", "name": (tr or {}).get("name") or (tr or {}).get("ref"), **sh}
        fbld, tb2 = _overlap(poly_utm, "building", ctx)
        fres, tres = _overlap(poly_utm, "landuse", ctx, pick=lambda g: g.get("landuse") == "residential")
        if fbld >= 0.15 or fres >= 0.3:
            return {"cls": "building", "source": "OpenStreetMap: " + ("building" if fbld >= 0.15 else "landuse=residential"), "name": (tb2 or tres or {}).get("name"), **sh}
    if (sh["aspect"] >= 5 and sh["width_m"] <= 45) or (sh["aspect"] >= 3.5 and sh["length_m"] >= 150 and sh["width_m"] <= 120) or (sh["aspect"] >= 3.5 and sh["length_m"] >= 400 and sh["width_m"] <= 200):
        return {"cls": "road", "source": "shape: a long narrow strip, not yet in OpenStreetMap", "name": None, **sh}
    # grey and bright is how a new roof, concrete or tarmac looks; orange or brown is bare soil and stays "cleared"
    if poly_utm.area <= 3e4 and spectral.get("sat_after", 1) <= 0.145 and spectral.get("bright_after", 0) >= 0.20:
        return {"cls": "building", "source": "satellite: bright grey surface (roof or concrete), not yet in OpenStreetMap", "name": None, **sh}
    return {"cls": "cleared", "source": "satellite only: vegetation or soil changed, use unknown", "name": None, **sh}


# ---------------------------------------------------------------- impact
def near(poly, ctx, kind) -> dict[str, Any] | None:
    if not ctx or ctx[kind][2] is None:
        return None
    geoms, tags, tree = ctx[kind]
    i = int(tree.nearest(poly))
    return {"distance_m": round(poly.distance(geoms[i])), "name": tags[i].get("name")}


def impact_of(c: dict[str, Any], arrays: dict[str, np.ndarray], wc: np.ndarray | None, grid, ctx: dict[str, Any] | None, cls: str) -> dict[str, Any]:
    sl, lab_id = c["slice"], c["label"]
    sub = arrays["lab"][sl] == lab_id
    px_ha = grid.resolution ** 2 / 1e4
    nb, na = arrays["ndvi_before"][sl][sub], arrays["ndvi_after"][sl][sub]
    veg_ha = round(float((nb > 0.30).sum()) * px_ha, 2)
    out: dict[str, Any] = {"area_ha": c["area_ha"], "vegetation_lost_ha": veg_ha, "ndvi_before": round(float(np.nanmean(nb)), 2), "ndvi_after": round(float(np.nanmean(na)), 2)}
    if wc is not None:
        w = wc[sl][sub]
        out["land_before"] = {WC.get(int(k), str(int(k))): round(float((w == k).sum()) * px_ha, 2) for k in np.unique(w) if k > 0}
        trees = round(float((w == 10).sum()) * px_ha, 2)
        out["tree_cover_lost_ha"] = trees
        dens = ASSUMPTIONS["carbon_density_t_c_per_ha"]
        out["carbon_t_co2"] = {k: round(trees * dens[k] * 3.67) for k in ("low", "central", "high")} if trees > 0 else None
    out["hard_surface_added_ha"] = c["area_ha"] if CLASSES[cls]["hard"] else 0.0
    if ctx:
        out["nearest_water"] = near(c["poly_utm"], ctx, "water")
        out["nearest_forest"] = near(c["poly_utm"], ctx, "forest")
    return out


def effects(cls: str, imp: dict[str, Any], sh: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """Plain-language effects. `measured` comes from the images; `typical` is general knowledge about this kind of change."""
    m, t = [], []
    a = imp["area_ha"]
    m.append({"icon": "📐", "text": f"{a:.2f} ha changed" + (f", about {sh['length_m']} m long and {sh['width_m']} m wide" if cls in ("road", "highway") else "")})
    if imp.get("vegetation_lost_ha", 0) >= 0.05:
        m.append({"icon": "🌿", "text": f"{imp['vegetation_lost_ha']:.2f} ha of green vegetation is gone (greenness {imp['ndvi_before']:.2f} → {imp['ndvi_after']:.2f})"})
    if imp.get("tree_cover_lost_ha", 0) > 0:
        c = imp.get("carbon_t_co2")
        m.append({"icon": "🌳", "text": f"{imp['tree_cover_lost_ha']:.2f} ha was mapped as tree cover (ESA WorldCover 2021)" + (f"; about {c['central']:,} t CO₂ stored in it (assumed density, range {c['low']:,} to {c['high']:,})" if c else "")})
    if imp.get("hard_surface_added_ha", 0) > 0:
        m.append({"icon": "🧱", "text": f"{imp['hard_surface_added_ha']:.2f} ha of hard surface added"})
    for k, lab in (("nearest_water", "water"), ("nearest_forest", "forest or wood")):
        v = imp.get(k)
        if v:
            m.append({"icon": "💧" if k == "nearest_water" else "🌲", "text": f"Nearest mapped {lab}: {v['distance_m']:,} m" + (f" ({v['name']})" if v.get("name") else "")})
    if CLASSES[cls]["hard"]:
        t.append({"icon": "🌧", "text": "Hard surfaces let less rain soak in, so more runs off and less recharges groundwater."})
        t.append({"icon": "🌡", "text": "Roofs and tarmac hold more heat than soil and plants, which warms the surrounding area."})
    if imp.get("vegetation_lost_ha", 0) >= 0.05:
        t.append({"icon": "🐦", "text": "Removing vegetation reduces habitat and the shade and cooling it gave."})
    if cls in ("road", "highway"):
        t.append({"icon": "🛣", "text": "A new road cuts habitat into pieces and brings traffic noise and easier access to nearby land."})
    if cls == "quarry":
        t.append({"icon": "💨", "text": "Quarries raise dust and noise and can change local drainage."})
    if cls == "cleared":
        t.append({"icon": "❔", "text": "The ground was cleared or changed but no use is visible yet; it needs a check on the ground."})
    return {"measured": m, "typical": t}


# ---------------------------------------------------------------- the run
class Cancelled(Exception):
    pass


def _step(run_id: str, steps: list[dict], msg: str, frac: float | None = None, status: str = "ok") -> None:
    with connection() as conn:
        row = conn.execute("SELECT status FROM infra_runs WHERE id = ?", (run_id,)).fetchone()
    if row and row[0] == "CANCELLED":
        raise Cancelled()
    steps.append({"t": now(), "status": status, "message": msg})
    update("infra_runs", "id", run_id, {"status": "RUNNING", "result": {"steps": steps[-40:]}, **({"progress": frac} if frac is not None else {})})


def stretch(rgbs: list[np.ndarray]) -> list[np.ndarray]:
    """True-colour 8-bit images, stretched with the same limits so before and after are comparable."""
    ims = [np.nan_to_num(np.stack([b["B04"], b["B03"], b["B02"]], -1), nan=0.0) for b in rgbs]
    allv = np.concatenate([i[i > 0] for i in ims]) if any((i > 0).any() for i in ims) else np.array([0.0, 0.3])
    lo, hi = np.percentile(allv, [2, 98])
    return [(np.clip((i - lo) / max(hi - lo, 1e-3), 0, 1) ** 0.9 * 255).astype("uint8") for i in ims]


def run(run_id: str, bbox: list[float], polygon: dict[str, Any] | None, actor: str) -> dict[str, Any]:
    steps: list[dict] = []
    p = Params()
    try:
        bbox, km2, res = validate(bbox, polygon)
        d = dates()
        _step(run_id, steps, f"Comparing {d['before']} with {d['after']} over {km2:.1f} km² at {res:.0f} m (Sentinel-2)", 0.03)
        jobs = [("before", d["before"], 50), ("after", d["after"], 50)] + [(f"mid{i}", m, 60) for i, m in enumerate(d["mids"])]
        done = {"n": 0}

        def one(j):
            name, c, cc = j
            r = composite(bbox, res, c, d["half"], 4, cc, polygon)
            done["n"] += 1
            if r["bands"] is None:
                _step(run_id, steps, f"No clear images around {c} ({name})", status="warning")
            else:
                _step(run_id, steps, f"Image set {done['n']} of {len(jobs)} ready: {name} ({len(r['scenes'])} scene(s) around {c})" + (" (cached)" if r["cached"] else ""), round(0.05 + 0.65 * done["n"] / len(jobs), 3))
            return name, r

        results = {}
        with ThreadPoolExecutor(2) as ex:
            for name, r in ex.map(one, jobs):
                results[name] = r
        if results["before"]["bands"] is None or results["after"]["bands"] is None:
            raise InfraError("There were not enough clear satellite images for the before or the after date. Try again later or another area.")
        grid = results["before"]["grid"]
        mids = [results[k]["bands"] for k in sorted(k for k in results if k.startswith("mid")) if results[k]["bands"] is not None]
        _step(run_id, steps, "Looking for ground that was green or dark and is now cleared or built over", 0.72)
        masks = detect(results["before"]["bands"], [results["after"]["bands"]] + mids, p)
        inside = None
        if polygon:
            inside = rio_features.rasterize([(shp_transform(Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True).transform, to_shape(polygon)), 1)], out_shape=(grid.height, grid.width), transform=grid.transform, fill=0, dtype="uint8").astype(bool)
        lab, found = patches_from(masks, grid, p, inside)
        _step(run_id, steps, f"{len(found)} changed patch(es) found; reading OpenStreetMap for roads, buildings and land use", 0.8)
        to_utm = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True).transform
        ctx = osm_features(bbox, to_utm)
        if ctx is None:
            _step(run_id, steps, "OpenStreetMap could not be reached, so classes come from the shape and the images only", status="warning")
        try:
            wc = provider().worldcover_classes(mapping(box(*bbox)), grid)
        except ProviderError:
            wc = None
            _step(run_id, steps, "Land-cover map unavailable; tree cover before is not reported", status="warning")
        ia = indices(results["after"]["bands"])
        ba = results["after"]["bands"]
        sat_after = (ba["B04"] - ba["B02"]) / (ba["B04"] + ba["B02"] + 1e-6)
        arrays = {"lab": lab, "ndvi_before": masks["ndvi_before"], "ndvi_after": masks["ndvi_after"]}
        found = found[:600]
        rows = []
        for k, c in enumerate(found, 1):
            sl = c["slice"]
            sub = lab[sl] == c["label"]
            spectral = {"ndbi_after": float(np.nanmean(ia["ndbi"][sl][sub])), "bright_after": float(np.nanmean(ia["bright"][sl][sub])), "sat_after": float(np.nanmean(sat_after[sl][sub]))}
            cl = classify(c["poly_utm"], ctx, spectral)
            imp = impact_of(c, arrays, wc, grid, ctx, cl["cls"])
            ef = effects(cl["cls"], imp, cl)
            cen = c["poly_ll"].representative_point()
            rows.append({"id": str(uuid.uuid4()), "run_id": run_id, "code": f"IN-{k:04d}", "cls": cl["cls"], "cls_source": cl["source"], "name": cl["name"], "geometry": mapping(c["poly_ll"]), "lat": round(cen.y, 6), "lon": round(cen.x, 6), "area_ha": c["area_ha"],
                         "evidence": {"detected_as": "vegetation lost" if c["src"] == "veg" else "darker or bare ground became brighter", "length_m": cl["length_m"], "width_m": cl["width_m"], "aspect": cl["aspect"], "pixels": c["px"], "resolution_m": res,
                                      "before": str(d["before"]), "after": str(d["after"]), "scenes": {k2: v["scenes"] for k2, v in results.items()}},
                         "impact": {**imp, "effects": ef}, "status": "OPEN", "note": None, "created_at": now()})
        # pictures for before / after, kept with the run
        rgb_b, rgb_a = stretch([results["before"]["bands"], results["after"]["bands"]])
        DATA.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(DATA / f"{run_id}.npz", before=rgb_b, after=rgb_a, transform=np.array(list(grid.transform)[:6]), crs=np.array(grid.crs), res=np.array(res))
        with connection() as conn:
            conn.execute("DELETE FROM infra_changes WHERE run_id = ?", (run_id,))
        for r in rows:
            insert("infra_changes", r)
        by = {}
        for r in rows:
            e = by.setdefault(r["cls"], {"n": 0, "ha": 0.0})
            e["n"] += 1
            e["ha"] = round(e["ha"] + r["area_ha"], 2)
        summary = {"patches": len(rows), "total_ha": round(sum(r["area_ha"] for r in rows), 1), "vegetation_lost_ha": round(sum(r["impact"]["vegetation_lost_ha"] for r in rows), 1), "tree_cover_lost_ha": round(sum(r["impact"].get("tree_cover_lost_ha", 0) for r in rows), 1),
                   "hard_surface_ha": round(sum(r["impact"]["hard_surface_added_ha"] for r in rows), 1), "by_class": by, "area_km2": round(km2, 1), "resolution_m": res, "before": str(d["before"]), "after": str(d["after"]), "osm": "ok" if ctx else "unavailable", "scenes": {k: [s["date"] for s in v["scenes"]] for k, v in results.items()}}
        _step(run_id, steps, f"Done: {len(rows)} changes, {summary['total_ha']} ha in total", 1.0)
        update("infra_runs", "id", run_id, {"status": "COMPLETE", "progress": 1.0, "result": {"steps": steps[-40:], "summary": summary}, "finished_at": now()})
        audit(actor, "infra_scan_completed", "infra_run", run_id, {"patches": len(rows)})
        return summary
    except Cancelled:
        update("infra_runs", "id", run_id, {"status": "CANCELLED", "result": {"steps": steps[-40:], "error": "Cancelled"}, "finished_at": now()})
        return {"cancelled": True}
    except Exception as exc:  # noqa: BLE001 - the failure is stored and shown, never swallowed
        msg = str(exc) if isinstance(exc, (InfraError, ProviderError)) else f"{type(exc).__name__}: {exc}"
        update("infra_runs", "id", run_id, {"status": "FAILED", "result": {"steps": steps[-40:], "error": msg}, "finished_at": now()})
        raise


# ---------------------------------------------------------------- reading results
def _load_arrays(run_id: str):
    f = DATA / f"{run_id}.npz"
    if not f.is_file():
        return None
    z = np.load(f, allow_pickle=False)
    from affine import Affine

    return {"before": z["before"], "after": z["after"], "transform": Affine(*[float(x) for x in z["transform"]]), "crs": str(z["crs"]), "res": float(z["res"])}


def _font(size: int):
    for n in ("arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _px_ring(geom_ll: dict, tr, crs: str) -> list[tuple[float, float]]:
    to = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    g = shp_transform(to, shape(geom_ll))
    inv = ~tr
    ring = g.exterior.coords if g.geom_type == "Polygon" else max(g.geoms, key=lambda q: q.area).exterior.coords
    return [inv * (x, y) for x, y in ring]


def change_images(change: dict[str, Any], run_id: str, width: int = 520) -> dict[str, Image.Image] | None:
    """Before and after crops around one change with its outline, true colour from the same Sentinel-2 composites."""
    arr = _load_arrays(run_id)
    if not arr:
        return None
    ring = _px_ring(change["geometry"], arr["transform"], arr["crs"])
    xs, ys = [q[0] for q in ring], [q[1] for q in ring]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    half = max(max(xs) - min(xs), max(ys) - min(ys)) * 0.5 + max(14, 0.9 * max(max(xs) - min(xs), max(ys) - min(ys)))
    h, w = arr["before"].shape[:2]
    x0, x1, y0, y1 = int(max(0, cx - half)), int(min(w, cx + half)), int(max(0, cy - half)), int(min(h, cy + half))
    out = {}
    for k in ("before", "after"):
        im = Image.fromarray(arr[k][y0:y1, x0:x1]).convert("RGB")
        scale = max(1, width / max(im.width, 1))
        im = im.resize((int(im.width * scale), int(im.height * scale)), Image.BICUBIC)
        d = ImageDraw.Draw(im, "RGBA")
        col = tuple(int(CLASSES[change["cls"]]["color"][i:i + 2], 16) for i in (1, 3, 5))
        pts = [((x - x0) * scale, (y - y0) * scale) for x, y in ring]
        d.line(pts + pts[:1], fill=col + (255,), width=3)
        out[k] = im
    return out


def compare_image(change: dict[str, Any], run_id: str, run: dict[str, Any] | None = None) -> bytes | None:
    ims = change_images(change, run_id)
    if not ims:
        return None
    b, a = ims["before"], ims["after"]
    f = _font(20)
    s = (run or {}).get("result", {}).get("summary", {})
    out = Image.new("RGB", (b.width + a.width + 6, max(b.height, a.height)), (255, 255, 255))
    out.paste(b, (0, 0)); out.paste(a, (b.width + 6, 0))
    d = ImageDraw.Draw(out, "RGBA")
    for x, label in ((8, f"Before · {s.get('before', 'a year ago')}"), (b.width + 14, f"After · {s.get('after', 'now')}")):
        tw = int(d.textlength(label, font=f))
        d.rounded_rectangle([x, 8, x + tw + 16, 40], 7, fill=(15, 35, 25, 220)); d.text((x + 8, 12), label, fill=(255, 255, 255), font=f)
    buf = io.BytesIO(); out.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def overview_image(run_id: str, changes: list[dict[str, Any]], max_w: int = 1400) -> bytes | None:
    arr = _load_arrays(run_id)
    if not arr:
        return None
    im = Image.fromarray(arr["after"]).convert("RGB")
    sc = min(1.0, max_w / im.width) if im.width > max_w else max(1.0, min(2.0, max_w / im.width))
    im = im.resize((int(im.width * sc), int(im.height * sc)), Image.BICUBIC)
    d = ImageDraw.Draw(im, "RGBA")
    for c in changes:
        if c.get("status") == "NOT_A_CHANGE":
            continue
        col = tuple(int(CLASSES[c["cls"]]["color"][i:i + 2], 16) for i in (1, 3, 5))
        pts = [(x * sc, y * sc) for x, y in _px_ring(c["geometry"], arr["transform"], arr["crs"])]
        d.polygon(pts, fill=col + (110,)); d.line(pts + pts[:1], fill=col + (255,), width=2)
    f = _font(16)
    used = sorted({c["cls"] for c in changes if c.get("status") != "NOT_A_CHANGE"})
    y = 10
    for k in used:
        label = CLASSES[k]["label"]
        tw = int(d.textlength(label, font=f)) + 34
        d.rounded_rectangle([10, y, 10 + tw, y + 26], 6, fill=(15, 35, 25, 215))
        d.rectangle([18, y + 7, 30, y + 19], fill=tuple(int(CLASSES[k]["color"][i:i + 2], 16) for i in (1, 3, 5)) + (255,))
        d.text((38, y + 4), label, fill=(255, 255, 255), font=f); y += 31
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=86)
    return buf.getvalue()


def public_change(c: dict[str, Any], full: bool = True) -> dict[str, Any]:
    out = {"id": c["id"], "run_id": c["run_id"], "code": c["code"], "cls": c["cls"], "cls_label": CLASSES[c["cls"]]["label"], "color": CLASSES[c["cls"]]["color"], "class_source": c["cls_source"], "name": c["name"], "lat": c["lat"], "lon": c["lon"], "area_ha": c["area_ha"], "status": c["status"], "note": c["note"], "geometry": c["geometry"]}
    if full:
        out["evidence"] = c["evidence"]
        out["impact"] = c["impact"]
    else:
        imp = c["impact"] or {}
        out["impact"] = {k: imp.get(k) for k in ("vegetation_lost_ha", "tree_cover_lost_ha", "hard_surface_added_ha")}
        out["length_m"] = (c["evidence"] or {}).get("length_m")
    return out


def changes_of(run_id: str) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = [decode_row(dict(r)) for r in conn.execute("SELECT * FROM infra_changes WHERE run_id = ? ORDER BY area_ha DESC", (run_id,))]
    return rows
