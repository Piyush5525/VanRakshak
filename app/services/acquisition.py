"""Cached, provenance-recording acquisition of Sentinel-1 / Sentinel-2 composites for a region."""
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.db.database import connection, get_one, insert, now
from app.services.satellite import AcquisitionConfig, Composite, PlanetaryComputer, ProviderError, file_checksum

SENSORS = ("sentinel-2", "sentinel-1", "esa-worldcover")
_provider: PlanetaryComputer | None = None


def provider() -> PlanetaryComputer:
    global _provider
    if _provider is None:
        _provider = PlanetaryComputer()
    return _provider


def _key(region_geometry: dict, sensor: str, centre: date, cfg: AcquisitionConfig, orbit_state: str | None) -> str:
    raw = json.dumps([region_geometry, sensor, centre.isoformat(), cfg.__dict__, orbit_state], sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:24]


def get_composite(region: dict[str, Any], sensor: str, centre: date, cfg: AcquisitionConfig, orbit_state: str | None = None, actor: str = "system") -> dict[str, Any]:
    """Return the observation record for (region, sensor, centre, config), acquiring it if not cached."""
    if sensor not in SENSORS:
        raise ValueError(f"Unsupported sensor {sensor}")
    key = _key(region["geometry"], sensor, centre, cfg, orbit_state)
    obs_id = f"obs-{key}"
    existing = get_one("observations", "id", obs_id)
    if existing and existing["status"] == "OK" and Path(existing["provenance"].get("file", "")).is_file():
        existing["provenance"]["cache_hit"] = True
        return existing

    out = Path(get_settings().raster_dir) / f"{obs_id}_{ {'sentinel-1': 's1', 'sentinel-2': 's2'}.get(sensor, 'forest') }.tif"
    pc = provider()
    if sensor == "sentinel-2":
        comp: Composite = pc.sentinel2_composite(region["geometry"], centre, cfg, out)
    elif sensor == "sentinel-1":
        comp = pc.sentinel1_composite(region["geometry"], centre, cfg, out, orbit_state)
    else:
        comp = pc.worldcover_forest(region["geometry"], cfg, out)

    times = sorted(s["acquired_at"] for s in comp.scenes)
    quality: dict[str, Any] = {"valid_pixel_fraction": comp.valid_pixel_fraction, "cloud_fraction": round(1 - comp.valid_pixel_fraction, 4) if sensor == "sentinel-2" and comp.status == "OK" else None, "scene_count": len(comp.scenes)}
    warnings = list(comp.warnings)
    if comp.status == "OK" and comp.valid_pixel_fraction < 0.6:
        warnings.append("Low valid-pixel coverage; change detection may be withheld")
    record = {
        "id": obs_id,
        "region_id": region["id"],
        "provider": pc.name,
        "source_scene_ids": [s["scene_id"] for s in comp.scenes],
        "acquisition_start": times[0] if times else None,
        "acquisition_end": times[-1] if times else None,
        "status": comp.status,
        "quality": quality,
        "provenance": {"sensor": sensor, "requested_centre_date": centre.isoformat(), "processing": comp.processing, "scenes": comp.scenes, "warnings": warnings, "file": comp.path, "checksum_sha256": file_checksum(comp.path) if comp.path else None, "processed_at": now(), "requested_by": actor, "cache_hit": False},
        "created_at": now(),
    }
    with connection() as conn:
        conn.execute("DELETE FROM observations WHERE id = ?", (obs_id,))
    insert("observations", record)
    for s in comp.scenes:
        with connection() as conn:
            conn.execute("INSERT OR IGNORE INTO scenes (id, provider, collection, scene_id, acquired_at, bbox, properties, retrieved_at) VALUES (?,?,?,?,?,?,?,?)", (f"{pc.name}:{s['scene_id']}", pc.name, comp.processing.get("collection", sensor), s["scene_id"], s["acquired_at"], json.dumps(s.get("bbox")), json.dumps(s), now()))
    return record


def acquire_pair(region: dict[str, Any], baseline: datetime, comparison: datetime, cfg: AcquisitionConfig, actor: str) -> dict[str, dict[str, Any]]:
    """Seasonally comparable composites for both dates and both sensors (S1 forced to one orbit direction)."""
    if baseline >= comparison:
        raise ValueError("baseline_date must be earlier than comparison_date")
    out: dict[str, dict[str, Any]] = {}
    for label, when in (("before", baseline), ("after", comparison)):
        out[f"{label}_s2"] = get_composite(region, "sentinel-2", when.date(), cfg, actor=actor)
    out["before_s1"] = get_composite(region, "sentinel-1", baseline.date(), cfg, actor=actor)
    orbit = out["before_s1"]["provenance"]["processing"].get("orbit_state")
    out["after_s1"] = get_composite(region, "sentinel-1", comparison.date(), cfg, orbit_state=orbit, actor=actor)
    out["forest"] = get_composite(region, "esa-worldcover", baseline.date(), cfg, actor=actor)
    return out
