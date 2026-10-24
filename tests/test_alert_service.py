import io
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from rasterio.transform import from_origin

from app.db.database import get_one, insert, now
from app.main import app
from app.services import alert_eval as E
from app.services import alerts as A

client = TestClient(app)
ADMIN = {"X-Role": "admin"}
SQUARE = {"type": "Polygon", "coordinates": [[[76.0, 11.6], [76.01, 11.6], [76.01, 11.61], [76.0, 11.61], [76.0, 11.6]]]}


def png(day_gfw, conf_b):
    a = np.zeros((256, 256, 4), dtype=np.uint8)
    a[10:12, 20:22] = [day_gfw // 255, day_gfw % 255, conf_b, 255]
    buf = io.BytesIO(); Image.fromarray(a, "RGBA").save(buf, "PNG")
    return buf.getvalue()


def test_gfw_tile_decoding_matches_documented_encoding():
    gfw_day = (datetime(2025, 7, 15).date() - E.GFW_EPOCH).days
    day, conf = E.decode_tile(png(gfw_day, 202))
    d = int(np.nanmax(day))
    assert (A.EPOCH + timedelta(days=d)).isoformat() == "2025-07-15"
    assert conf[10, 20] == 2 and conf[0, 0] == 0 and np.isnan(day[0, 0])
    assert E.decode_tile(png(gfw_day, 101))[1][10, 20] == 1


def test_tolerance_prf_known_values():
    ref = np.zeros((20, 20), bool); ref[5:8, 5:8] = True
    pred = np.zeros_like(ref); pred[5:8, 6:9] = True  # shifted by one pixel: within tolerance
    r = E.prf(pred, ref)
    assert r["precision"] == 1.0 and r["recall"] == 1.0 and r["f1"] == 1.0
    far = np.zeros_like(ref); far[15:18, 15:18] = True
    z = E.prf(far, ref)
    assert z["precision"] == 0.0 and z["recall"] == 0.0 and z["f1"] == 0.0
    assert E.prf(np.zeros_like(ref), ref)["precision"] is None  # nothing predicted: precision undefined, not 0


GRID = SimpleNamespace(transform=from_origin(500000, 1230000, 20, 20), crs="EPSG:32643", height=30, width=30, resolution=20.0)
P = A.AlertParams(min_patch_ha=0.1, seasonal=False)
D0 = (datetime(2025, 1, 1).date() - A.EPOCH).days


def pix_with(blocks):
    """blocks: list of (rows, cols, est_day, issue_day, evidence)"""
    ev = np.zeros((30, 30), np.uint8); est = np.full((30, 30), np.nan); iss = np.full((30, 30), np.nan); unc = np.full((30, 30), np.nan)
    for rows, cols, e, i, c in blocks:
        ev[rows, cols] = c; est[rows, cols] = e; iss[rows, cols] = i; unc[rows, cols] = 3.0
    return {"evidence": ev, "est_day": est, "issue_day": iss, "unc_days": unc, "forest": np.ones((30, 30), bool)}


def test_scoring_precision_at_k_date_error_and_radar_gain():
    # three alerts: two real (matching reference), one false; one real loss happens in monsoon and only radar finds it
    b1 = (slice(2, 6), slice(2, 6), D0 + 70, D0 + 76, 2)    # March, real, estimated 2 days late
    b2 = (slice(10, 14), slice(10, 14), D0 + 200, D0 + 206, 1)  # mid-July (monsoon), found with radar
    b3 = (slice(20, 24), slice(20, 24), D0 + 120, D0 + 125, 2)  # false alarm
    fused = pix_with([b1, b2, b3])
    optical = pix_with([b1, b3])  # optical alone misses the monsoon loss
    ref = np.full((30, 30), np.nan)
    ref[2:6, 2:6] = D0 + 68
    ref[10:14, 10:14] = D0 + 198
    res = E.evaluate_arrays(fused, optical, GRID, P, {"day": ref, "conf": (~np.isnan(ref)).astype(np.uint8) * 2, "source": "test"}, (D0, D0 + 364), None, top_k=50)
    f, o = res["fused_s1_s2"], res["optical_only_s2"]
    assert f["patches"]["n"] == 3 and f["patches"]["precision_at_k"]["k"] == 3  # fewer than 50 alerts -> K reduced and reported
    assert f["patches"]["precision_at_k"]["spatial"] == pytest.approx(2 / 3, abs=0.01)
    assert f["date_estimate_error"]["median_abs_error_days"] == 2.0 and f["date_estimate_error"]["median_signed_error_days"] == 2.0
    assert res["radar_gain"]["monsoon"]["recall_gain"] == pytest.approx(1.0) and o["season"]["monsoon"]["recall"] == 0.0
    assert res["radar_gain"]["monsoon"]["timely_recall_45d_gain"] == pytest.approx(1.0)
    assert res["radar_gain"]["non_monsoon"]["recall_gain"] == 0.0
    assert any("anecdotal" in w for w in res["warnings"]) and any("not field truth" in w for w in res["warnings"])
    assert res["phase1_check"]["status"] == "not_computed"


def test_phase1_guard_reports_delta_and_pass_fail():
    b1 = (slice(2, 6), slice(2, 6), D0 + 70, D0 + 76, 2)
    fused = pix_with([b1]); ref = np.full((30, 30), np.nan); ref[2:6, 2:6] = D0 + 68
    refd = {"day": ref, "conf": (~np.isnan(ref)).astype(np.uint8) * 2, "source": "t"}
    good = np.zeros((30, 30), bool); good[2:6, 2:6] = True
    r = E.evaluate_arrays(fused, fused, GRID, P, refd, (D0, D0 + 364), good)
    assert r["phase1_check"]["within_5_points"] is True and r["phase1_check"]["delta_f1_points"] == 0.0
    better = good.copy(); better[2:6, 2:6] = True  # identical; now make phase 2 worse
    worse = pix_with([(slice(2, 6), slice(2, 4), D0 + 70, D0 + 76, 2), (slice(20, 24), slice(20, 24), D0 + 70, D0 + 76, 2)])
    r2 = E.evaluate_arrays(worse, worse, GRID, P, refd, (D0, D0 + 364), good)
    assert r2["phase1_check"]["within_5_points"] is False and r2["phase1_check"]["delta_f1_points"] < -5


def synthetic_series(grid_h=24, grid_w=24):
    """Optical + radar stacks with a cut in the monsoon that optical cannot see."""
    opt_t = [datetime(2024, 10, 3, tzinfo=timezone.utc) + timedelta(days=5 * i) for i in range(90)]
    rad_t = [datetime(2024, 10, 5, tzinfo=timezone.utc) + timedelta(days=12 * i) for i in range(45)]
    rng = np.random.default_rng(2)
    blk = (slice(6, 12), slice(6, 12)); cut = datetime(2025, 7, 15, tzinfo=timezone.utc)
    o = (0.6 + rng.normal(0, 0.02, (90, grid_h, grid_w))).astype("float32")
    r = (-14.0 + rng.normal(0, 0.5, (45, grid_h, grid_w))).astype("float32")
    for i, w in enumerate(opt_t):
        if w >= cut:
            o[i][blk] = 0.15
        if datetime(2025, 6, 10, tzinfo=timezone.utc) <= w <= datetime(2025, 9, 20, tzinfo=timezone.utc):
            o[i][:] = np.nan
    for i, w in enumerate(rad_t):
        if w >= cut:
            r[i][blk] -= 6.0
    grid = SimpleNamespace(transform=from_origin(500000, 1230000, 20, 20), crs="EPSG:32643", height=grid_h, width=grid_w, resolution=20.0)
    return {"data": o, "when": opt_t, "ids": [[f"s2-{i}"] for i in range(90)], "grid": grid}, {"data": r, "when": rad_t, "ids": [[f"s1-{i}"] for i in range(45)], "grid": grid}


def wait(job_id):
    for _ in range(200):
        j = client.get(f"/api/v1/jobs/{job_id}", headers=ADMIN).json()
        if j["status"] in ("completed", "failed"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_full_alert_run_api_ranking_exports_and_evaluation(monkeypatch):
    opt, rad = synthetic_series()
    monkeypatch.setattr(A, "series", lambda geom, sensor, start, n, cfg, orbit=None, progress=None: opt if sensor == "sentinel-2" else rad)
    monkeypatch.setattr(A, "s1_orbit", lambda g, s, e: {"orbit_state": "descending", "relative_orbit": 12, "scenes_in_period": 45})
    reg = client.post("/api/v1/regions", headers=ADMIN, json={"name": "Alert service region", "jurisdiction": "Kerala", "geometry": SQUARE}).json()
    r = client.post("/api/v1/alerts/runs", headers=ADMIN, json={"region_id": reg["id"], "year": 2025, "n_months": 12, "ref_months": 3, "held_out": True, "seasonal": False})
    assert r.status_code == 200
    j = wait(r.json()["job_id"])
    assert j["status"] == "completed", j
    res = j["result"]
    assert res["summary"]["alerts"] == 1 and res["summary"]["optical_only_pixels"] > 0
    run = client.get(f"/api/v1/alerts/runs/{r.json()['run_id']}", headers=ADMIN).json()
    assert run["status"] == "COMPLETE" and run["held_out"] is True and run["params"]["digest"] == r.json()["params_digest"]
    alerts = client.get(f"/api/v1/alerts/runs/{run['id']}/alerts", headers={"X-Role": "read_only"}).json()
    assert len(alerts) == 1 and alerts[0]["monsoon"] is True and alerts[0]["evidence"] == "optical_radar_agree" and alerts[0]["rank"] == 1
    assert alerts[0]["est_date"] <= "2025-07-22" and alerts[0]["issue_month"] == "2025-08"
    top = client.get(f"/api/v1/alerts/runs/{run['id']}/top?k=50", headers=ADMIN).json()
    assert top["returned"] == 1 and "not a probability" in top["ranking"]
    assert client.get(f"/api/v1/alerts/runs/{run['id']}/alerts?month=2025-03", headers=ADMIN).json() == []
    gj = client.get(f"/api/v1/alerts/runs/{run['id']}/export.geojson", headers=ADMIN)
    assert json.loads(gj.content)["features"][0]["geometry"]["type"] in ("Polygon", "MultiPolygon") and "verify" in json.loads(gj.content)["notice"]
    assert "estimated_loss_date" in client.get(f"/api/v1/alerts/runs/{run['id']}/export.csv", headers=ADMIN).text

    # evaluation with a controlled reference (no network)
    px = np.load(E.Path(__import__("app.core.config", fromlist=["x"]).get_settings().raster_dir) / f"alertpix-{run['id']}.npz")
    ref_day = np.full((24, 24), np.nan); ref_day[6:12, 6:12] = float(np.nanmedian(px["f_est_day"][px["f_evidence"] > 0])) - 3
    monkeypatch.setattr(E, "load_reference", lambda grid, client=None: {"day": ref_day.astype("float32"), "conf": np.where(np.isnan(ref_day), 0, 2).astype("uint8"), "source": "controlled test reference"})
    ev = wait(client.post(f"/api/v1/alerts/runs/{run['id']}/evaluate", headers=ADMIN, json={}).json()["job_id"])
    assert ev["status"] == "completed", ev
    e = ev["result"]
    assert e["fused_s1_s2"]["patches"]["precision_at_k"]["spatial"] == 1.0
    g = e["radar_gain"]["monsoon"]
    assert g["fused"]["timely_recall_45d"] == 1.0 and g["optical_only"]["timely_recall_45d"] == 0.0 and g["timely_recall_45d_gain"] == pytest.approx(1.0)
    assert g["recall_gain"] == 0.0, "optical alone does eventually see the loss once the clouds clear; the gain is timeliness"
    assert e["fused_s1_s2"]["date_estimate_error"]["median_abs_error_days"] <= 10
    assert e["held_out_declared"] is True and e["params_digest"] == run["params"]["digest"]
    assert client.get(f"/api/v1/alerts/runs/{run['id']}/evaluations", headers=ADMIN).json()[0]["id"] == e["id"]


def test_alert_run_permissions_and_validation():
    reg = client.post("/api/v1/regions", headers=ADMIN, json={"name": "Alert perm region", "jurisdiction": "Kerala", "geometry": SQUARE}).json()
    body = {"region_id": reg["id"], "year": 2025}
    assert client.post("/api/v1/alerts/runs", headers={"X-Role": "read_only"}, json=body).status_code == 403
    assert client.post("/api/v1/alerts/runs", headers={"X-Role": "officer"}, json=body).status_code == 403
    assert client.post("/api/v1/alerts/runs", headers=ADMIN, json={**body, "n_months": 13}).status_code == 422
    assert client.post("/api/v1/alerts/runs", headers=ADMIN, json={"region_id": "missing", "year": 2025}).status_code == 404
    assert client.get("/api/v1/alerts/runs/missing", headers=ADMIN).status_code == 404
