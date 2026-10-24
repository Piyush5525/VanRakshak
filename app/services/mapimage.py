"""Incident / route / forecast map images (PNG) built from the incident's own stored data.

Primary: Mapbox Static Images with real basemap tiles. Fallback: a plain drawing (no basemap) when Mapbox is
unavailable. Every image has a burned-in label: DEMONSTRATION for demo incidents, and "predicted risk footprint,
not a fire perimeter" on forecast images.
"""
import io
import json
import math
from typing import Any
from urllib.parse import quote

import httpx
from PIL import Image, ImageDraw, ImageFont

from app.core.config import get_settings

settings = get_settings()
W, H = 800, 600


def _poly5(coords: list[list[float]]) -> str:
    out, plat, plon = [], 0, 0
    for lon, lat in coords:
        la, lo = round(lat * 1e5), round(lon * 1e5)
        for v in (la - plat, lo - plon):
            v = ~(v << 1) if v < 0 else (v << 1)
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63)); v >>= 5
            out.append(chr(v + 63))
        plat, plon = la, lo
    return "".join(out)


def _thin(coords: list[list[float]], n: int) -> list[list[float]]:
    if len(coords) <= n:
        return coords
    step = len(coords) / n
    return [coords[int(i * step)] for i in range(n)] + [coords[-1]]


def _rings(geom: dict | None, max_pts: int) -> list[list[list[float]]]:
    if not geom:
        return []
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"] if geom["type"] == "MultiPolygon" else []
    rings = [p[0] for p in polys if p]
    rings.sort(key=len, reverse=True)
    return [[[round(x, 4), round(y, 4)] for x, y in _thin(r, max_pts)] for r in rings[:4]]


def _font(size: int):
    for name in ("arial.ttf", "DejaVuSans.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _banner(img: Image.Image, lines: list[tuple[str, tuple[int, int, int]]], footer: str | None) -> bytes:
    img = img.convert("RGBA")
    d = ImageDraw.Draw(img, "RGBA")
    f = _font(22)
    h = 14 + 30 * len(lines)
    d.rectangle([0, 0, img.width, h], fill=(15, 35, 25, 215))
    for i, (t, col) in enumerate(lines):
        d.text((14, 8 + 30 * i), t, fill=col, font=f)
    if footer:
        fs = _font(15)
        d.rectangle([0, img.height - 26, img.width, img.height], fill=(15, 35, 25, 190))
        d.text((10, img.height - 22), footer, fill=(230, 240, 235), font=fs)
    out = io.BytesIO(); img.convert("RGB").save(out, "JPEG", quality=86, optimize=True)
    return out.getvalue()


def _mapbox(overlays: list[str]) -> bytes | None:
    token = settings.mapbox_api
    if not token:
        return None
    url = f"https://api.mapbox.com/styles/v1/mapbox/satellite-streets-v12/static/{','.join(overlays)}/auto/{W}x{H}@2x?padding=70&access_token={token}"
    if len(url) > 7800:
        return None
    try:
        r = httpx.get(url, timeout=40)
        return r.content if r.status_code == 200 and r.headers.get("content-type", "").startswith("image") else None
    except httpx.HTTPError:
        return None


def _fallback(points: list[tuple[float, float, str, tuple]], lines: list[list[list[float]]], polys: list[tuple[list[list[float]], tuple]]) -> Image.Image:
    xs = [p[0] for p in points] + [c[0] for l in lines for c in l] + [c[0] for pl, _ in polys for c in pl]
    ys = [p[1] for p in points] + [c[1] for l in lines for c in l] + [c[1] for pl, _ in polys for c in pl]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    pad = max(x1 - x0, y1 - y0, 0.02) * 0.12
    x0, x1, y0, y1 = x0 - pad, x1 + pad, y0 - pad, y1 + pad
    k = min((W * 2 - 40) / ((x1 - x0) * math.cos(math.radians((y0 + y1) / 2))), (H * 2 - 120) / (y1 - y0))
    sx = lambda lon: 20 + (lon - x0) * math.cos(math.radians((y0 + y1) / 2)) * k  # noqa: E731
    sy = lambda lat: H * 2 - 40 - (lat - y0) * k  # noqa: E731
    img = Image.new("RGBA", (W * 2, H * 2), (231, 238, 232, 255)); d = ImageDraw.Draw(img, "RGBA")
    for pl, col in polys:
        d.polygon([(sx(a), sy(b)) for a, b in pl], fill=col, outline=(120, 40, 20, 255))
    for l in lines:
        d.line([(sx(a), sy(b)) for a, b in l], fill=(20, 87, 60, 255), width=8)
    for lon, lat, label, col in points:
        d.ellipse([sx(lon) - 14, sy(lat) - 14, sx(lon) + 14, sy(lat) + 14], fill=col, outline=(255, 255, 255, 255), width=3)
        if label:
            d.text((sx(lon) + 18, sy(lat) - 12), label, fill=(20, 30, 25, 255), font=_font(24))
    return img


def render(kind: str, inc: dict[str, Any], horizon: str = "12") -> tuple[bytes, str]:
    """kind: incident | route | forecast. Returns (jpeg bytes, basemap_source)."""
    demo = inc["is_demo"]
    head: list[tuple[str, tuple[int, int, int]]] = [(f"VanRakshak  {inc['code']}  {inc['lat']:.4f}, {inc['lon']:.4f}", (255, 255, 255))]
    if demo:
        head.append(("SCENARIO", (255, 190, 90)))
    overlays = [f"pin-l+d9382a({inc['lon']},{inc['lat']})"]
    points = [(inc["lon"], inc["lat"], inc["code"], (217, 56, 42, 255))]
    lines, polys = [], []
    footer = "Basemap: Mapbox, OpenStreetMap, Maxar"
    if kind == "route":
        r = inc.get("route") or {}
        if r.get("status") != "OK":
            raise ValueError("No route calculated for this incident")
        st = r["station"]
        coords = _thin(r["geometry"]["coordinates"], 250)
        overlays.insert(0, f"path-5+14573c-0.9({quote(_poly5(coords), safe='')})")
        overlays.append(f"pin-l-fire-station+14573c({st['lon']},{st['lat']})")
        points.append((st["lon"], st["lat"], st["name"][:24], (20, 87, 60, 255))); lines.append(coords)
        head.append((f"Road route from {st['name'][:34]}: {r['distance_km']} km, ~{round(r['duration_min'])} min (typical, unverified access)", (190, 235, 205)))
    elif kind == "forecast":
        fc = inc.get("forecast") or {}
        hz = (fc.get("horizons") or {}).get(horizon)
        if not hz:
            raise ValueError("No forecast available for this incident")
        feats = []
        for name, col, op in (("possible_10", "#e3a14b", 0.25), ("likely_50", "#d9382a", 0.45)):
            for ring in _rings(hz["contours"].get(name), 40):
                feats.append({"type": "Feature", "properties": {"stroke": col, "stroke-width": 2, "fill": col, "fill-opacity": op}, "geometry": {"type": "Polygon", "coordinates": [ring]}})
                polys.append((ring, tuple(int(col[i:i + 2], 16) for i in (1, 3, 5)) + (int(op * 255),)))
        if feats:
            overlays.insert(0, f"geojson({quote(json.dumps({'type': 'FeatureCollection', 'features': feats}, separators=(',', ':')), safe='')})")
        head.append((f"{horizon} h PREDICTED RISK FOOTPRINT (not a fire perimeter) - prototype model", (255, 210, 120)))
        head.append((f"Median potential area {hz['area_ha']['p50']} ha (P10-P90 {hz['area_ha']['p10']}-{hz['area_ha']['p90']}); red >=50% runs, orange >=10%", (255, 255, 255)))
        if fc.get("status") != "OK":
            head.append((f"Forecast quality: {fc.get('status')}", (255, 150, 120)))
    else:
        head.append(("Satellite thermal hotspot: not a confirmed fire", (255, 210, 120)))
    png = _mapbox(overlays)
    if png:
        return _banner(Image.open(io.BytesIO(png)), head, footer), "mapbox"
    img = _fallback(points, lines, polys)
    return _banner(img, head, "No basemap available: schematic drawing from incident data"), "schematic"
