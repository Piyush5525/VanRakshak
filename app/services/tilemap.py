"""A satellite picture built from free Esri World Imagery tiles, used when Mapbox's static-image key is missing or refused.
Draws the lost area, route lines and pins on top. Returns None when the tiles cannot be fetched (no stock image is substituted)."""
import io
import math
from concurrent.futures import ThreadPoolExecutor

import httpx
from PIL import Image, ImageDraw

TILE = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"


def _px(lon: float, lat: float, z: int) -> tuple[float, float]:
    n = 256 * 2 ** z
    return (lon + 180) / 360 * n, (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n


def render(points: list[tuple[float, float]], polys: list[list[list[float]]] = (), lines: list[tuple[list[list[float]], str]] = (), pins: list[tuple[float, float, str]] = (), size: tuple[int, int] = (1200, 900), max_zoom: int = 17) -> Image.Image | None:
    """points are (lon, lat) that must all be visible; polys are rings of [lon, lat]; lines are ([[lon, lat]...], '#rrggbb'); pins are (lon, lat, '#rrggbb')."""
    w, h = size
    lons, lats = [p[0] for p in points], [p[1] for p in points]
    z = max_zoom
    while z > 3:
        x0, y1 = _px(min(lons), min(lats), z)
        x1, y0 = _px(max(lons), max(lats), z)
        if x1 - x0 <= w * 0.8 and y1 - y0 <= h * 0.8:
            break
        z -= 1
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    left, top = cx - w / 2, cy - h / 2
    tx0, tx1, ty0, ty1 = int(left // 256), int((left + w) // 256), int(top // 256), int((top + h) // 256)
    jobs = [(x, y) for x in range(tx0, tx1 + 1) for y in range(ty0, ty1 + 1)]
    if len(jobs) > 40:
        return None
    img = Image.new("RGB", (w, h), (30, 40, 35))
    got = 0

    def fetch(xy):
        try:
            r = httpx.get(TILE.format(z=z, x=xy[0], y=xy[1]), timeout=20, headers={"User-Agent": "VanRakshak/1.0"})
            return xy, Image.open(io.BytesIO(r.content)).convert("RGB") if r.status_code == 200 else None
        except Exception:  # noqa: BLE001
            return xy, None

    with ThreadPoolExecutor(8) as ex:
        for (x, y), tile in ex.map(fetch, jobs):
            if tile:
                img.paste(tile, (int(x * 256 - left), int(y * 256 - top)))
                got += 1
    if not got:
        return None
    d = ImageDraw.Draw(img, "RGBA")
    to = lambda lon, lat: tuple(v - o for v, o in zip(_px(lon, lat, z), (left, top)))  # noqa: E731
    for ring in polys:
        pts = [to(*c) for c in ring]
        d.polygon(pts, fill=(255, 59, 48, 90), outline=(255, 59, 48, 255))
        d.line(pts + pts[:1], fill=(255, 59, 48, 255), width=3)
    for coords, col in lines:
        rgb = tuple(int(col[i:i + 2], 16) for i in (1, 3, 5))
        d.line([to(*c) for c in coords], fill=rgb + (235,), width=6, joint="curve")
    for lon, lat, col in pins:
        x, y = to(lon, lat)
        rgb = tuple(int(col[i:i + 2], 16) for i in (1, 3, 5))
        d.ellipse([x - 13, y - 13, x + 13, y + 13], fill=rgb + (255,), outline=(255, 255, 255, 255), width=4)
    return img
