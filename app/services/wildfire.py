"""NASA FIRMS active-fire ingestion (thermal anomalies). Burned-area evidence is a different record type."""
import csv
import io
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import Settings
from app.core.geometry import to_shape
from app.db.database import audit, insert, now

SOURCES = {"VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "MODIS_NRT"}


class FirmsUnavailable(RuntimeError):
    pass


def _confidence(raw: str, source: str) -> tuple[float | None, str]:
    raw = (raw or "").strip().lower()
    if source.startswith("VIIRS"):
        mapping = {"l": 0.3, "n": 0.6, "h": 0.9}
        return mapping.get(raw), raw
    try:
        return min(max(float(raw) / 100, 0.0), 1.0), raw
    except ValueError:
        return None, raw


def parse_firms_csv(text: str, source: str) -> list[dict[str, Any]]:
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        try:
            lat, lon = float(r["latitude"]), float(r["longitude"])
            hhmm = str(r.get("acq_time", "0000")).zfill(4)
            observed = datetime.strptime(f"{r['acq_date']} {hhmm}", "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue  # malformed row from an external source is skipped, not guessed at
        conf, raw = _confidence(r.get("confidence", ""), source)
        rows.append({"lat": lat, "lon": lon, "observed_at": observed, "confidence": conf, "raw_confidence": raw, "satellite": r.get("satellite"), "instrument": r.get("instrument"), "frp_mw": float(r["frp"]) if r.get("frp") not in (None, "") else None, "brightness_k": float(r.get("bright_ti4") or r.get("brightness") or 0) or None, "daynight": r.get("daynight"), "scan_km": float(r["scan"]) if r.get("scan") else None})
    return rows


def fetch_firms(settings: Settings, bbox: tuple[float, float, float, float], source: str, days: int, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    if not settings.firms_api_key:
        raise FirmsUnavailable("FIRMS_API_KEY is not configured (free MAP_KEY from firms.modaps.eosdis.nasa.gov/api)")
    if source not in SOURCES or not 1 <= days <= 5:
        raise ValueError(f"source must be one of {sorted(SOURCES)} and days 1-5")
    west, south, east, north = bbox
    url = f"{settings.firms_base_url}/{settings.firms_api_key}/{source}/{west},{south},{east},{north}/{days}"
    try:
        resp = (client or httpx.Client(timeout=60)).get(url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise FirmsUnavailable(f"FIRMS request failed: {type(exc).__name__}") from exc  # URL (with key) never echoed
    if resp.text.lstrip().lower().startswith(("invalid", "error")):
        raise FirmsUnavailable("FIRMS rejected the request (check MAP_KEY and quota)")
    return parse_firms_csv(resp.text, source)


def store_detections(rows: list[dict[str, Any]], source: str, region: dict[str, Any] | None, actor: str) -> dict[str, int]:
    inserted = duplicates = 0
    region_shape = to_shape(region["geometry"]) if region else None
    for r in rows:
        geometry = {"type": "Point", "coordinates": [r["lon"], r["lat"]]}
        evidence = {"claim_type": "observation", "observation_kind": "active_fire_thermal_anomaly", "satellite": r["satellite"], "instrument": r["instrument"], "frp_mw": r["frp_mw"], "brightness_k": r["brightness_k"], "daynight": r["daynight"], "source_confidence_raw": r["raw_confidence"], "pixel_size_km": r["scan_km"], "location_uncertainty_note": "Fire pixel centre; true fire may be anywhere inside the pixel", "retrieved_at": now(), "region_id": region["id"] if region else None, "inside_region": bool(region_shape and region_shape.contains(to_shape(geometry)))}
        try:
            insert("wildfire_observations", {"id": str(uuid.uuid4()), "geometry": geometry, "observed_at": r["observed_at"].isoformat(), "observation_type": "active_fire", "confidence": r["confidence"], "source": f"NASA FIRMS {source}", "status": "NEEDS_REVIEW", "evidence": evidence, "created_at": now()})
            inserted += 1
        except sqlite3.IntegrityError:
            duplicates += 1
    audit(actor, "wildfire_firms_ingested", "wildfire", None, {"source": source, "inserted": inserted, "duplicates": duplicates})
    return {"inserted": inserted, "duplicates": duplicates, "received": len(rows)}
