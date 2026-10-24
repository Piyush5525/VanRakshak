"""Prototype probabilistic fire-spread RISK footprint for 3, 6 and 12 hours.

THIS IS NOT A FIRE PERIMETER and it is NOT validated. It answers: "if a fire were burning at this hotspot,
where could it plausibly have spread to, given the forecast weather and terrain?"

Model (all published formulas; parameters that had to be assumed are listed in `ASSUMPTIONS` and returned):
  * Head rate of spread: McArthur Mk5 forest meter  R = 0.0012 * FFDI * w  (km/h, w = fuel load t/ha),
    FFDI = 2 exp(-0.45 + 0.987 ln DF - 0.0345 RH + 0.0338 T + 0.0234 V).  Developed for Australian dry
    eucalypt forest, NOT calibrated for Indian forests.
  * Slope: McArthur rule of thumb, rate changes by exp(0.069 * slope_degrees) up/down the spread direction.
  * Shape: Anderson (1983) length-to-breadth ratio from wind speed; head-to-back ratio from the ellipse.
  * Uncertainty: Monte-Carlo over wind speed, wind direction, drought factor and a model-error multiplier.
  * Fuel: cells in water, built-up, snow or bare ground (ESA WorldCover) are excluded; everything else is
    treated as burnable at the same rate (a strong simplification).
"""
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
from pyproj import Transformer
from rasterio import features
from rasterio.transform import from_origin
from rasterio.warp import transform_geom
from shapely.geometry import shape as shp_shape
from shapely.ops import unary_union

MODEL = "mcarthur-mk5-anderson-ellipse-montecarlo"
VERSION = "prototype-1"
HORIZONS = (3, 6, 12)
NON_BURNABLE = {50, 60, 70, 80}  # built-up, bare/sparse, snow/ice, water

ASSUMPTIONS = [
    "Drought factor is derived from 7-day and 24-hour rainfall (10 with no rain in 7 days, minus 1 per 5 mm, 1 after >=20 mm in 24 h). This is an assumption, not a Keetch-Byram calculation.",
    "Fuel load is a single value (default 12 t/ha) applied everywhere; real fuel varies by forest type, season and litter.",
    "Mid-flame wind is taken as 0.4 x the 10 m wind (typical forest reduction), used only for the ellipse shape.",
    "Wind for each horizon is the vector mean of the hourly forecast from now to that horizon; shifts in wind direction during the period are not followed.",
    "Slope effect uses the average slope over the first 2 km along the mean spread direction from a 30 m elevation model.",
    "Spotting, crown fire, fire suppression, firebreaks and roads as barriers are not modelled.",
    "Cells classified as water, built-up, bare ground or snow cannot burn; all other cover is treated as burnable at the same rate.",
]
LIMITATIONS = [
    "A risk footprint, not an observed or predicted fire perimeter. Probability is the share of Monte-Carlo runs in which the cell was reached.",
    "No historical validation of accuracy has been performed for Indian forests.",
    "Uncertainty ranges reflect only the input spreads chosen here; unmodelled processes can make real fires larger, smaller or oriented differently.",
]


@dataclass(frozen=True)
class SpreadParams:
    fuel_load_t_ha: float = 12.0
    wind_sigma: float = 0.25        # lognormal sigma of wind speed
    dir_sigma_deg: float = 20.0     # std of wind direction
    ros_sigma: float = 0.40         # lognormal sigma of model-error multiplier on rate of spread
    df_sigma: float = 1.5
    n_runs: int = 300
    cell_m: float = 150.0
    midflame_factor: float = 0.4
    max_ros_kmh: float = 3.0
    max_extent_km: float = 30.0


# ------------------------------------------------------------------------------ physics
def drought_factor(rain_7d_mm: float, rain_24h_mm: float) -> float:
    if rain_24h_mm >= 20:
        return 1.0
    return float(np.clip(10 - rain_7d_mm / 5.0, 1.0, 10.0))


def ffdi(df: np.ndarray | float, temp_c: float, rh: float, wind_kmh: np.ndarray | float) -> np.ndarray | float:
    return 2.0 * np.exp(-0.45 + 0.987 * np.log(np.clip(df, 1, 10)) - 0.0345 * rh + 0.0338 * temp_c + 0.0234 * wind_kmh)


def head_ros_kmh(ffdi_v, fuel_load: float, slope_deg: float, cap: float):
    return np.minimum(0.0012 * ffdi_v * fuel_load * math.exp(0.069 * slope_deg), cap)


def length_breadth(wind_kmh, midflame: float):
    u = np.asarray(wind_kmh) * 0.621371 * midflame  # mph
    return np.maximum(0.936 * np.exp(0.2566 * u) + 0.461 * np.exp(-0.1548 * u) - 0.397, 1.0)


def wind_mean(speeds_kmh: list[float], dirs_from_deg: list[float]) -> tuple[float, float]:
    """Vector-mean wind: returns (speed km/h, direction it blows FROM, degrees)."""
    th = np.radians(dirs_from_deg)
    u = np.mean(np.asarray(speeds_kmh) * np.sin(th)); v = np.mean(np.asarray(speeds_kmh) * np.cos(th))  # components toward... (from-convention)
    return float(math.hypot(u, v)), float((math.degrees(math.atan2(u, v)) + 360) % 360)


def seed_for(incident_id: str, inputs: dict[str, Any]) -> int:
    return int(hashlib.sha256((incident_id + json.dumps(inputs, sort_keys=True, default=str)).encode()).hexdigest()[:8], 16)


# ------------------------------------------------------------------------------ simulation
def simulate(lat: float, lon: float, inputs: dict[str, Any], params: SpreadParams, seed: int, landcover: np.ndarray | None = None, lc_transform=None) -> dict[str, Any]:
    """inputs: {'horizons': {3:{wind_kmh, wind_from_deg, temp_c, rh}, ...}, 'rain_7d_mm', 'rain_24h_mm', 'slope_deg' (callable bearing->deg or float)}.

    `landcover`, if given, is a class raster aligned to the returned grid (see `make_grid`).
    """
    rng = np.random.default_rng(seed)
    df0 = drought_factor(inputs["rain_7d_mm"], inputs["rain_24h_mm"])
    g = make_grid(lat, lon, inputs, params)
    n, cell, half_km, transform = g["n"], g["cell"], g["half_km"], g["transform"]
    ys = (np.arange(n) - n // 2) * cell
    X, Y = np.meshgrid(ys, -ys)  # row 0 = north
    burnable = np.ones((n, n), dtype=bool)
    if landcover is not None and landcover.shape == (n, n):
        burnable = ~np.isin(landcover, list(NON_BURNABLE))
    burnable[n // 2, n // 2] = True  # the detection itself
    out: dict[str, Any] = {}
    for h, w in inputs["horizons"].items():
        h = int(h)
        speeds = w["wind_kmh"] * np.exp(rng.normal(0, params.wind_sigma, params.n_runs) - params.wind_sigma ** 2 / 2)
        dirs = (w["wind_from_deg"] + rng.normal(0, params.dir_sigma_deg, params.n_runs)) % 360
        dfs = np.clip(df0 + rng.normal(0, params.df_sigma, params.n_runs), 1, 10)
        mult = np.exp(rng.normal(0, params.ros_sigma, params.n_runs) - params.ros_sigma ** 2 / 2)
        prob = np.zeros((n, n)); areas = np.zeros(params.n_runs); heads = np.zeros(params.n_runs)
        mean_bearing = _spread_bearing(w["wind_from_deg"])
        slope_mean = inputs["slope_deg"](mean_bearing) if callable(inputs["slope_deg"]) else inputs["slope_deg"]
        for i in range(params.n_runs):
            F = ffdi(dfs[i], w["temp_c"], w["rh"], speeds[i])
            R = float(head_ros_kmh(F, params.fuel_load_t_ha, slope_mean, params.max_ros_kmh)) * mult[i]
            R = min(R, params.max_ros_kmh)
            lb = float(length_breadth(speeds[i], params.midflame_factor))
            hb = (lb + math.sqrt(lb * lb - 1)) / (lb - math.sqrt(lb * lb - 1)) if lb > 1 else 1.0
            head = R * h * 1000.0; back = head / hb
            a = (head + back) / 2; c = (head - back) / 2; b = a / lb
            th = math.radians(_spread_bearing(dirs[i]))
            along = X * math.sin(th) + Y * math.cos(th); cross = X * math.cos(th) - Y * math.sin(th)
            inside = (((along - c) / max(a, 1.0)) ** 2 + (cross / max(b, 1.0)) ** 2) <= 1.0
            m = inside & burnable
            m[n // 2, n // 2] = True
            prob += m
            areas[i] = m.sum() * cell * cell / 1e4
            heads[i] = head / 1000.0
        prob /= params.n_runs
        res = {"wind_kmh": round(w["wind_kmh"], 1), "wind_from_deg": round(w["wind_from_deg"], 0), "spread_toward_deg": round(mean_bearing, 0), "temp_c": round(w["temp_c"], 1), "rh_percent": round(w["rh"], 0), "slope_deg_along_spread": round(slope_mean, 1), "drought_factor": round(df0, 1),
               "head_distance_km": {"p10": round(float(np.percentile(heads, 10)), 2), "p50": round(float(np.percentile(heads, 50)), 2), "p90": round(float(np.percentile(heads, 90)), 2)},
               "area_ha": {"p10": round(float(np.percentile(areas, 10)), 1), "p50": round(float(np.percentile(areas, 50)), 1), "p90": round(float(np.percentile(areas, 90)), 1)}}
        contours, contour_area = {}, {}
        for name, thr in (("likely_50", 0.5), ("possible_10", 0.1)):
            mask = (prob >= thr).astype("uint8")
            polys = [shp_shape(transform_geom(_utm(lon, lat), "EPSG:4326", g)) for g, v in features.shapes(mask, mask=mask > 0, transform=transform)]
            geom = unary_union(polys).simplify(0.0003) if polys else None
            contours[name] = json.loads(json.dumps(geom.__geo_interface__)) if geom is not None and not geom.is_empty else None
            contour_area[name] = round(float((prob >= thr).sum() * cell * cell / 1e4), 1)
        res["contours"], res["contour_area_ha"] = contours, contour_area
        out[str(h)] = res
    grid_info = {"size": n, "cell_m": round(cell, 1), "half_extent_km": half_km, "utm": _utm(lon, lat), "transform": [transform.a, transform.b, transform.c, transform.d, transform.e, transform.f]}
    return {"horizons": out, "grid": grid_info, "drought_factor": round(df0, 1)}


def make_grid(lat: float, lon: float, inputs: dict[str, Any], params: SpreadParams) -> dict[str, Any]:
    """Grid geometry that `simulate` will use (so land cover can be fetched on exactly the same cells)."""
    df0 = drought_factor(inputs["rain_7d_mm"], inputs["rain_24h_mm"])
    est = 0.0
    for h, w in inputs["horizons"].items():
        slope = inputs["slope_deg"](_spread_bearing(w["wind_from_deg"])) if callable(inputs["slope_deg"]) else inputs["slope_deg"]
        r = float(head_ros_kmh(ffdi(df0, w["temp_c"], w["rh"], w["wind_kmh"]), params.fuel_load_t_ha, slope, params.max_ros_kmh))
        est = max(est, r * int(h) * 1.5)
    half_km = float(np.clip(est, 2.0, params.max_extent_km))
    cell = max(params.cell_m, half_km * 2000 / 400)
    n = int(2 * half_km * 1000 / cell) + 1
    x0, y0 = Transformer.from_crs("EPSG:4326", _utm(lon, lat), always_xy=True).transform(lon, lat)
    return {"n": n, "cell": cell, "crs": _utm(lon, lat), "transform": from_origin(x0 - (n // 2) * cell - cell / 2, y0 + (n // 2) * cell + cell / 2, cell, cell), "half_km": half_km}


def _spread_bearing(from_deg: float) -> float:
    return (from_deg + 180.0) % 360.0


def _utm(lon: float, lat: float) -> str:
    return f"EPSG:{(32600 if lat >= 0 else 32700) + int((lon + 180) // 6) + 1}"
