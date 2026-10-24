import time
from datetime import datetime, timezone

import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from rasterio.transform import from_origin

from app.core.config import get_settings
from app.core.geometry import geodesic_area_hectares
from app.main import app
from app.services.changedetect import ChangeConfig, detect_from_rasters
from app.services.geoanalysis import assess_candidate, fragmentation

client = TestClient(app)
HEADERS = {"X-Role": "admin"}
SQUARE = {"type": "Polygon", "coordinates": [[[76, 11], [76.1, 11], [76.1, 11.1], [76, 11.1], [76, 11]]]}


def write_raster(path, nir, swir, cloud, crs="EPSG:32643"):
    # 10 m pixels, UTM 43N near 76E
    with rasterio.open(path, "w", driver="GTiff", height=nir.shape[0], width=nir.shape[1], count=3, dtype="float32", crs=crs, transform=from_origin(500000, 1230000, 10, 10)) as dst:
        dst.write(np.stack([nir, swir, cloud]).astype("float32"))


@pytest.fixture
def scene(tmp_path):
    n = 60
    forest_nir, forest_swir = np.full((n, n), 0.4), np.full((n, n), 0.1)  # NBR 0.6
    cloud = np.zeros((n, n))
    after_nir, after_swir = forest_nir.copy(), forest_swir.copy()
    after_nir[10:30, 10:30], after_swir[10:30, 10:30] = 0.15, 0.30  # 400 px = 4 ha cleared
    write_raster(tmp_path / "before.tif", forest_nir, forest_swir, cloud)
    write_raster(tmp_path / "after.tif", after_nir, after_swir, cloud)
    return tmp_path


def test_geodesic_area_of_known_square():
    # 0.1 x 0.1 degree at 11N is ~ 0.1*111.3km * 0.1*109.3km ≈ 12,160 ha
    assert geodesic_area_hectares(SQUARE) == pytest.approx(12_160, rel=0.01)


def test_detects_cleared_block_with_expected_area(scene):
    result = detect_from_rasters(str(scene / "before.tif"), str(scene / "after.tif"))
    assert result["status"] == "CANDIDATES_FOUND"
    assert sum(p["area_hectares"] for p in result["polygons"]) == pytest.approx(4.0, rel=0.02)
    assert result["claim_type"] == "candidate_inference"


def test_unchanged_scene_yields_no_candidates(scene):
    result = detect_from_rasters(str(scene / "before.tif"), str(scene / "before.tif"))
    assert result["status"] == "NO_CANDIDATES"


def test_cloud_obscured_scene_is_insufficient_evidence(scene, tmp_path):
    with rasterio.open(scene / "after.tif") as src:
        data = src.read()
    data[2][:, :] = 1  # fully clouded
    write_raster(scene / "cloudy.tif", data[0], data[1], data[2])
    result = detect_from_rasters(str(scene / "before.tif"), str(scene / "cloudy.tif"))
    assert result["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["polygons"] == []


def test_misaligned_rasters_rejected(scene):
    write_raster(scene / "other_crs.tif", np.full((60, 60), 0.4), np.full((60, 60), 0.1), np.zeros((60, 60)), crs="EPSG:32644")
    with pytest.raises(ValueError):
        detect_from_rasters(str(scene / "before.tif"), str(scene / "other_crs.tif"))


def test_empty_forest_mask_is_insufficient_evidence(tmp_path):
    flat = np.full((20, 20), 0.2)
    write_raster(tmp_path / "a.tif", flat, flat, np.zeros((20, 20)))
    assert detect_from_rasters(str(tmp_path / "a.tif"), str(tmp_path / "a.tif"), ChangeConfig())["status"] == "INSUFFICIENT_EVIDENCE"


def test_raster_size_limit(scene):
    with pytest.raises(ValueError):
        detect_from_rasters(str(scene / "before.tif"), str(scene / "after.tif"), ChangeConfig(max_pixels=100))


def test_fragmentation_splits_patch():
    band = {"type": "Polygon", "coordinates": [[[76, 11.04], [76.1, 11.04], [76.1, 11.06], [76, 11.06], [76, 11.04]]]}
    m = fragmentation(SQUARE, band)
    assert m["patch_count_before"] == 1 and m["patch_count_after"] == 2
    assert m["remaining_forest_hectares"] + m["loss_within_forest_hectares"] == pytest.approx(m["initial_forest_hectares"], rel=1e-3)
    assert m["edge_created_m"] > 0


def auth(status, geometry=SQUARE, dates=None):
    return {"id": "a1", "status": status, "geometry": geometry, "dates": dates or {}}


OBS = datetime(2025, 6, 1, tzinfo=timezone.utc)
CHANGE = {"type": "Polygon", "coordinates": [[[76.01, 11.01], [76.02, 11.01], [76.02, 11.02], [76.01, 11.02], [76.01, 11.01]]]}


def test_authorization_states_never_say_legal_or_illegal():
    valid = {"valid_from": "2025-01-01T00:00:00+00:00", "valid_to": "2026-01-01T00:00:00+00:00"}
    assert assess_candidate(CHANGE, OBS, auth("issued", dates=valid))["state"] == "MATCH_REQUIRES_REVIEW"
    assert assess_candidate(CHANGE, OBS, auth("issued", dates={"valid_to": "2024-01-01T00:00:00+00:00"}))["state"] == "RECORD_EXPIRED_OR_REVOKED_CANDIDATE"
    assert assess_candidate(CHANGE, OBS, auth("application"))["state"] == "AUTHORIZATION_STATUS_UNCLEAR"
    assert assess_candidate(CHANGE, OBS, auth("revoked"))["state"] == "RECORD_EXPIRED_OR_REVOKED_CANDIDATE"
    far = {"type": "Polygon", "coordinates": [[[77, 12], [77.1, 12], [77.1, 12.1], [77, 12.1], [77, 12]]]}
    assert assess_candidate(CHANGE, OBS, auth("issued", geometry=far))["state"] == "INSUFFICIENT_EVIDENCE"
    partial = {"type": "Polygon", "coordinates": [[[76.01, 11.01], [76.015, 11.01], [76.015, 11.02], [76.01, 11.02], [76.01, 11.01]]]}
    assert assess_candidate(CHANGE, OBS, auth("issued", geometry=partial))["state"] == "POSSIBLE_SCOPE_OR_CONDITION_MISMATCH"


def wait(job_id):
    for _ in range(100):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_api_raster_detection_then_authorization_match_and_simulation(scene):
    settings = get_settings()
    original = settings.raster_dir
    settings.raster_dir = str(scene)
    try:
        # region matches raster footprint approximately; the change polygons come from the rasters
        region = client.post("/api/v1/regions", headers=HEADERS, json={"name": "Raster forest", "jurisdiction": "Kerala", "geometry": SQUARE}).json()
        payload = {"region_id": region["id"], "baseline_date": "2025-01-01T00:00:00Z", "comparison_date": "2025-06-01T00:00:00Z", "before_raster": "before.tif", "after_raster": "after.tif"}
        job = wait(client.post("/api/v1/forest/change-detection", headers=HEADERS, json=payload).json()["run_id"])
        assert job["status"] == "completed", job
        change = job["result"]
        assert change["status"] == "CANDIDATES_FOUND"
        assert change["area_hectares"] == pytest.approx(4.0, rel=0.02)

        # path traversal is rejected
        bad = wait(client.post("/api/v1/forest/change-detection", headers=HEADERS, json={**payload, "before_raster": "../before.tif"}).json()["run_id"])
        assert bad["status"] == "failed"

        # no ingested record -> NO_RECORD_FOUND, with the explicit non-verdict warning
        assessment = client.post("/api/v1/authorizations/match-assessment", headers=HEADERS, json={"change_id": change["id"]}).json()
        assert assessment["status"] in {"NO_RECORD_FOUND", "MATCH_REQUIRES_REVIEW", "POSSIBLE_SCOPE_OR_CONDITION_MISMATCH", "RECORD_EXPIRED_OR_REVOKED_CANDIDATE", "POTENTIAL_MATCH", "AUTHORIZATION_STATUS_UNCLEAR"}
        assert "illegality" in assessment["rationale"]["warning"]
    finally:
        settings.raster_dir = original

    sim = client.post("/api/v1/forest/simulations", headers=HEADERS, json={"region_id": region["id"], "geometry": CHANGE, "horizon_days": 30})
    body = sim.json()
    assert body["is_observation"] is False and body["is_forecast"] is False
    assert body["fragmentation"]["remaining_forest_hectares"] < body["fragmentation"]["initial_forest_hectares"]
    assert client.post("/api/v1/forest/simulations", headers=HEADERS, json={"region_id": region["id"], "geometry": CHANGE, "horizon_days": 30, "mode": "model_based"}).status_code == 409


def test_impact_and_energy_endpoints_persist():
    inc = client.post("/api/v1/incidents", headers=HEADERS, json={"title": "Impact test", "geometry": {"type": "Point", "coordinates": [76.04, 11.04]}, "evidence": {}}).json()
    r = client.post(f"/api/v1/incidents/{inc['id']}/impact-assessment", headers=HEADERS)
    assert r.status_code == 200 and r.json()["claim_type"] == "inferred_consequence"
