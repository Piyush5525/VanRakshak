"""AR-ready scene description for a hypothetical simulation.

The backend emits a georeferenced scene in a local East-North-Up frame (metres) around an anchor.
An AR client (WebXR / ARCore / ARKit) places the anchor with GPS + compass or a geo-anchor and
draws the layers. Nothing here is imagery of reality: every layer is simulated and the response
carries a mandatory on-screen label.
"""
import math
from typing import Any

from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from app.core.geometry import geodesic_area_hectares, to_shape

LABEL = "SIMULATED SCENARIO - NOT AN OBSERVATION OR A FORECAST"
MAX_VERTICES = 5000


def _enu(lon0: float, lat0: float):
    kx = 111_320.0 * math.cos(math.radians(lat0))
    ky = 110_540.0
    return lambda lon, lat: (round((lon - lon0) * kx, 2), round((lat - lat0) * ky, 2))


def _rings(geom: BaseGeometry, to_local, tolerance_m: float) -> list[dict[str, Any]]:
    polys = []
    for part in getattr(geom, "geoms", [geom]):
        if part.geom_type != "Polygon" or part.is_empty:
            continue
        part = part.simplify(tolerance_m / 111_000, preserve_topology=True)
        polys.append({"exterior": [to_local(x, y) for x, y in part.exterior.coords], "holes": [[to_local(x, y) for x, y in r.coords] for r in part.interiors]})
    return polys


def build_scene(simulation: dict[str, Any], region: dict[str, Any]) -> dict[str, Any]:
    result = simulation["result"] if "result" in simulation else simulation
    forest = to_shape(region["geometry"])
    loss = to_shape(result["geometry"])
    remaining = forest.difference(loss)
    anchor = loss.centroid
    to_local = _enu(anchor.x, anchor.y)
    # coarser simplification for large areas keeps the payload mobile-friendly
    tol = 2.0 if geodesic_area_hectares(forest) < 200 else 15.0
    layers = [
        {"id": "initial_forest", "kind": "area", "style": {"color": "#2e7d32", "opacity": 0.25, "extrude_m": 0.0}, "polygons": _rings(forest, to_local, tol), "description": "Region polygon assumed to be forest (not a verified forest mask)"},
        {"id": "remaining_forest", "kind": "area", "style": {"color": "#2e7d32", "opacity": 0.5, "extrude_m": 0.0}, "polygons": _rings(remaining, to_local, tol), "description": "Forest left if the hypothetical clearing happened"},
        {"id": "simulated_loss", "kind": "area", "style": {"color": "#d32f2f", "opacity": 0.6, "extrude_m": 1.0}, "polygons": _rings(loss, to_local, 1.0), "description": "Hypothetical clearing drawn by the user"},
    ]
    if sum(len(p["exterior"]) for l in layers for p in l["polygons"]) > MAX_VERTICES:
        raise ValueError("Scene too detailed for AR; use a smaller region or simpler polygon")
    return {
        "schema": "vananetra-ar-scene/1",
        "mandatory_label": LABEL,
        "claim_type": "hypothetical_assumption",
        "is_observation": False,
        "is_forecast": False,
        "simulation_id": result["id"],
        "horizon_days": result.get("horizon_days"),
        "anchor": {"lon": round(anchor.x, 7), "lat": round(anchor.y, 7), "altitude": None, "altitude_note": "No terrain model is ingested; the client should place the scene on the detected ground plane or terrain."},
        "frame": {"type": "ENU", "units": "metres", "x": "east", "y": "north", "z": "up", "approximation": "equirectangular around the anchor; accurate to well under 1% within ~10 km"},
        "layers": layers,
        "metrics": result.get("fragmentation"),
        "assumptions": result.get("assumptions"),
        "uncertainty": result.get("uncertainty"),
        "client_requirements": ["Show mandatory_label persistently on screen", "GPS/compass anchoring is typically 3-10 m inaccurate; do not use for boundary decisions", "Do not export screenshots without the label"],
    }
