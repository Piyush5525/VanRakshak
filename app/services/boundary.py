"""India outline (geoBoundaries gbOpen IND ADM0, CC0), downloaded once and cached on disk.

Used only to hide hotspots that fall in neighbouring countries and to draw an outline. It is a third-party
dataset: it is not the Survey of India map and makes no statement about legal boundaries.
"""
import json
import threading
from pathlib import Path

import httpx
from shapely.geometry import shape
from shapely.prepared import prep

from app.core.config import get_settings

SOURCE = "geoBoundaries gbOpen (CC0), India ADM0"
_lock = threading.Lock()
_state: dict = {}


def _path() -> Path:
    return Path(get_settings().raster_dir).parent / "india_boundary.geojson"


def geojson() -> dict:
    p = _path()
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    last = None
    for _ in range(3):
        try:
            c = httpx.Client(timeout=60, follow_redirects=True)
            meta = c.get("https://www.geoboundaries.org/api/current/gbOpen/IND/ADM0/").json()
            fc = c.get(meta.get("simplifiedGeometryGeoJSON") or meta["gjDownloadURL"]).json()
            geom = fc["features"][0]["geometry"]
            out = {"type": "Feature", "geometry": geom, "properties": {"source": SOURCE, "licence": meta.get("boundaryLicense"), "note": "Third-party outline; not the Survey of India map."}}
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(out), encoding="utf-8")
            return out
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError(f"India boundary unavailable: {type(last).__name__}")


def contains(lat: float, lon: float) -> bool:
    with _lock:
        if "prep" not in _state:
            _state["prep"] = prep(shape(geojson()["geometry"]))
    from shapely.geometry import Point

    return _state["prep"].contains(Point(lon, lat))
