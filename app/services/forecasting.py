"""Horizon-specific forest-loss *risk* model (7 / 30 / 90 days).

Event definition: a grid cell (0.01 deg, ~1.1 km) that had no recorded loss at the forecast origin
receives a newly recorded loss detection with comparison_date in (origin, origin + horizon].
The output is a ranked risk score; it is only called a probability if calibration passes.

Features use only information dated at or before the forecast origin (no leakage):
  recent_loss_count_365d, days_since_last_loss, distance_to_nearest_loss_km, loss_area_ha_5km_365d.
Trained exclusively from detections stored in this system; refuses to train or predict otherwise.
"""
import hashlib
import json
import math
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
from shapely.geometry import box, shape

from app.core.config import get_settings
from app.db.database import connection, get_one, insert, list_rows, now

CELL_DEG = 0.01
FEATURES = ["recent_loss_count_365d", "days_since_last_loss", "distance_to_nearest_loss_km", "loss_area_ha_5km_365d"]
MIN_TRAIN_POS, MIN_TEST_POS = 30, 10
MAX_INPUT_AGE_DAYS = 120


# ------------------------------------------------------------------ data
def region_cells(geometry: dict[str, Any], max_cells: int = 5000) -> list[tuple[float, float]]:
    geom = shape(geometry)
    minx, miny, maxx, maxy = geom.bounds
    cells = []
    x = math.floor(minx / CELL_DEG) * CELL_DEG
    while x < maxx:
        y = math.floor(miny / CELL_DEG) * CELL_DEG
        while y < maxy:
            if geom.intersects(box(x, y, x + CELL_DEG, y + CELL_DEG)):
                cells.append((round(x + CELL_DEG / 2, 5), round(y + CELL_DEG / 2, 5)))
            y += CELL_DEG
        x += CELL_DEG
    if len(cells) > max_cells:
        raise ValueError(f"Region has {len(cells)} cells; limit is {max_cells}")
    return cells


def _hav_km(lon1, lat1, lon2, lat2):
    p = math.pi / 180
    a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(a))


def loss_events(region_id: str | None = None) -> list[dict[str, Any]]:
    """Point events (centroid, area, date) from stored candidate/confirmed detections."""
    events = []
    for row in list_rows("changes", "WHERE disturbance_type = 'CANDIDATE_FOREST_LOSS'" + (" AND region_id = ?" if region_id else ""), (region_id,) if region_id else (), limit=100000):
        cg = shape(row["geometry"])
        parts = list(getattr(cg, "geoms", [cg]))
        scores = {round(p["area_hectares"], 4): p for p in row["provenance"].get("polygon_scores", [])}
        for part in parts:
            events.append({"lon": part.centroid.x, "lat": part.centroid.y, "date": datetime.fromisoformat(row["comparison_date"]), "area_ha": row["area_hectares"] / max(len(parts), 1)})
    return events


def features_at(cells: list[tuple[float, float]], events: list[dict[str, Any]], origin: datetime) -> np.ndarray:
    """Feature matrix for cells at `origin`, using events with date <= origin only."""
    past = [e for e in events if e["date"] <= origin]
    X = np.zeros((len(cells), len(FEATURES)))
    for i, (lon, lat) in enumerate(cells):
        in365 = [e for e in past if (origin - e["date"]).days <= 365]
        d = [(_hav_km(lon, lat, e["lon"], e["lat"]), e) for e in past]
        X[i, 0] = sum(1 for dist, e in d if dist <= CELL_DEG * 111 and e in in365)
        X[i, 1] = min((origin - e["date"]).days for e in past) if past else 3650
        X[i, 2] = min((dist for dist, _ in d), default=100.0)
        X[i, 3] = sum(e["area_ha"] for dist, e in d if dist <= 5 and e in in365)
    return X


def labels_for(cells, events, origin: datetime, horizon: int) -> np.ndarray:
    end = origin + timedelta(days=horizon)
    y = np.zeros(len(cells))
    for i, (lon, lat) in enumerate(cells):
        y[i] = any(origin < e["date"] <= end and abs(e["lon"] - lon) <= CELL_DEG / 2 and abs(e["lat"] - lat) <= CELL_DEG / 2 for e in events)
    return y


# ------------------------------------------------------------------ model
class Logistic:
    def __init__(self, l2: float = 1.0, iters: int = 600, lr: float = 0.1):
        self.l2, self.iters, self.lr = l2, iters, lr

    def fit(self, X: np.ndarray, y: np.ndarray) -> "Logistic":
        self.mu, self.sd = X.mean(0), X.std(0) + 1e-9
        Z = (X - self.mu) / self.sd
        w, b = np.zeros(Z.shape[1]), math.log((y.mean() + 1e-6) / (1 - y.mean() + 1e-6))
        for _ in range(self.iters):
            p = 1 / (1 + np.exp(-(Z @ w + b)))
            w -= self.lr * (Z.T @ (p - y) / len(y) + self.l2 * w / len(y))
            b -= self.lr * float((p - y).mean())
        self.w, self.b = w, b
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        return 1 / (1 + np.exp(-(((X - self.mu) / self.sd) @ self.w + self.b)))


def average_precision(y: np.ndarray, s: np.ndarray) -> float:
    if y.sum() == 0:
        return float("nan")
    order = np.argsort(-s, kind="stable")
    y = y[order]
    tp = np.cumsum(y)
    precision = tp / np.arange(1, len(y) + 1)
    return float((precision * y).sum() / y.sum())


def evaluate(y: np.ndarray, p: np.ndarray, baseline: np.ndarray, top_fraction: float = 0.1) -> dict[str, Any]:
    k = max(1, int(len(y) * top_fraction))
    top = np.argsort(-p, kind="stable")[:k]
    bins = np.linspace(0, 1, 6)
    calibration = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = (p >= lo) & ((p < hi) | (hi == 1))
        if m.any():
            calibration.append({"bin": [round(lo, 2), round(hi, 2)], "n": int(m.sum()), "mean_predicted": round(float(p[m].mean()), 4), "observed_rate": round(float(y[m].mean()), 4)})
    prec = float(y[top].mean())
    ece = sum(c["n"] * abs(c["mean_predicted"] - c["observed_rate"]) for c in calibration) / len(y)
    return {
        "n": int(len(y)), "positives": int(y.sum()), "base_rate": round(float(y.mean()), 5),
        "pr_auc": round(average_precision(y, p), 4), "baseline_pr_auc": round(average_precision(y, baseline), 4),
        "top_fraction": top_fraction, f"precision_at_top_{int(top_fraction * 100)}pct": round(prec, 4),
        f"recall_at_top_{int(top_fraction * 100)}pct": round(float(y[top].sum() / y.sum()), 4) if y.sum() else None,
        "false_alert_rate_at_top": round(1 - prec, 4), "brier": round(float(((p - y) ** 2).mean()), 5),
        "expected_calibration_error": round(float(ece), 5), "calibration": calibration,
    }


def train(region_id: str | None, horizon: int, actor: str, step_days: int = 30) -> dict[str, Any]:
    """Build a leakage-safe time split from stored detections, train, evaluate, register."""
    if horizon not in (7, 30, 90):
        raise ValueError("horizon must be 7, 30 or 90")
    events = loss_events(region_id)
    if len(events) < MIN_TRAIN_POS + MIN_TEST_POS:
        return {"status": "INSUFFICIENT_DATA", "reason": f"{len(events)} stored loss detections; at least {MIN_TRAIN_POS + MIN_TEST_POS} are required before any model is trained", "claim_type": "model_training"}
    regions = [get_one("regions", "id", region_id)] if region_id else list_rows("regions", limit=1000)
    cells = [c for r in regions if r for c in region_cells(r["geometry"])]
    cells = sorted(set(cells))
    dates = sorted(e["date"] for e in events)
    t0, t1 = dates[0] + timedelta(days=60), dates[-1] - timedelta(days=horizon)
    origins = []
    t = t0
    while t <= t1:
        origins.append(t)
        t += timedelta(days=step_days)
    if len(origins) < 6:
        return {"status": "INSUFFICIENT_DATA", "reason": "History too short for a time-aware split (fewer than 6 monthly origins)", "claim_type": "model_training"}
    cut = origins[int(len(origins) * 0.7)]
    embargo = timedelta(days=horizon)  # test origins start after cut + horizon so no label window overlaps training
    Xtr, ytr, Xte, yte = [], [], [], []
    for o in origins:
        X, y = features_at(cells, events, o), labels_for(cells, events, o, horizon)
        eligible = X[:, 0] == 0  # only cells with no loss in the last year ("newly recorded loss")
        if o + embargo <= cut:
            Xtr.append(X[eligible]); ytr.append(y[eligible])
        elif o > cut + embargo:
            Xte.append(X[eligible]); yte.append(y[eligible])
    if not Xtr or not Xte:
        return {"status": "INSUFFICIENT_DATA", "reason": "No origins left after applying the leakage embargo", "claim_type": "model_training"}
    Xtr, ytr, Xte, yte = np.vstack(Xtr), np.concatenate(ytr), np.vstack(Xte), np.concatenate(yte)
    if ytr.sum() < MIN_TRAIN_POS or yte.sum() < MIN_TEST_POS:
        return {"status": "INSUFFICIENT_DATA", "reason": f"Positives train={int(ytr.sum())} (need {MIN_TRAIN_POS}), test={int(yte.sum())} (need {MIN_TEST_POS})", "claim_type": "model_training"}
    return register(Logistic().fit(Xtr, ytr), Xtr, ytr, Xte, yte, horizon, region_id, cut, actor)


def register(model: Logistic, Xtr, ytr, Xte, yte, horizon: int, region_id: str | None, cut: datetime, actor: str) -> dict[str, Any]:
    p = model.predict(Xte)
    baseline = 1 / (1 + Xte[:, 2])  # simple baseline: closer to previous loss = riskier
    metrics = evaluate(yte, p, baseline)
    usable = bool(metrics["pr_auc"] > metrics["baseline_pr_auc"] and metrics["expected_calibration_error"] < 0.05)
    model_id = str(uuid.uuid4())
    out = Path(get_settings().model_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"risk_{horizon}d_{model_id}.json"
    path.write_text(json.dumps({"features": FEATURES, "mu": model.mu.tolist(), "sd": model.sd.tolist(), "w": model.w.tolist(), "b": model.b}))
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    training = {"region_id": region_id, "n_train": int(len(ytr)), "train_positives": int(ytr.sum()), "split_cut": cut.isoformat(), "embargo_days": horizon, "features": FEATURES, "algorithm": "L2 logistic regression", "protocol": "time-ordered split with embargo; spatial hold-out not performed"}
    insert("model_registry", {"id": model_id, "name": "forest-risk", "version": f"logreg-{horizon}d-{model_id[:8]}", "horizon_days": horizon, "artifact_path": str(path), "checksum": checksum, "training": training, "status": "USABLE" if usable else "EVALUATED_NOT_USABLE", "created_at": now()})
    insert("evaluations", {"id": str(uuid.uuid4()), "model_id": model_id, "horizon_days": horizon, "protocol": training["protocol"], "metrics": metrics, "created_at": now()})
    return {"status": "TRAINED", "model_id": model_id, "usable": usable, "metrics": metrics, "usable_rule": "PR-AUC above baseline and ECE < 0.05 on held-out later period", "claim_type": "model_training"}


# ------------------------------------------------------------------ inference
def active_model(horizon: int) -> dict[str, Any] | None:
    rows = list_rows("model_registry", "WHERE name = 'forest-risk' AND horizon_days = ? AND status = 'USABLE'", (horizon,), limit=1)
    return rows[0] if rows else None


def run_forecast(region: dict[str, Any], horizon: int, origin: datetime) -> dict[str, Any]:
    """Returns {status, ...}; never fabricates a surface."""
    model_row = active_model(horizon)
    if not model_row:
        return {"status": "UNAVAILABLE", "reason": f"No trained and evaluated {horizon}-day model is registered as usable"}
    events = loss_events(None)
    past = [e for e in events if e["date"] <= origin]
    if not past:
        return {"status": "INSUFFICIENT_INPUT", "reason": "No loss detections on or before the forecast origin"}
    age = (origin - max(e["date"] for e in past)).days
    if age > MAX_INPUT_AGE_DAYS:
        return {"status": "INSUFFICIENT_INPUT", "reason": f"Newest detection is {age} days older than the origin (limit {MAX_INPUT_AGE_DAYS})"}
    spec = json.loads(Path(model_row["artifact_path"]).read_text())
    if hashlib.sha256(Path(model_row["artifact_path"]).read_bytes()).hexdigest() != model_row["checksum"]:
        return {"status": "UNAVAILABLE", "reason": "Model artifact checksum mismatch"}
    m = Logistic(); m.mu, m.sd, m.w, m.b = np.array(spec["mu"]), np.array(spec["sd"]), np.array(spec["w"]), spec["b"]
    cells = region_cells(region["geometry"])
    X = features_at(cells, events, origin)
    if (X[:, 2] > 50).all():
        return {"status": "INSUFFICIENT_INPUT", "reason": "Region is far outside the area covered by training detections (out of distribution)"}
    risk = m.predict(X)
    ev = list_rows("evaluations", "WHERE model_id = ?", (model_row["id"],), limit=1)
    return {"status": "OK", "cells": [{"lon": c[0], "lat": c[1], "risk": round(float(r), 5)} for c, r in zip(cells, risk)], "model_id": model_row["id"], "model_version": model_row["version"], "evaluation_id": ev[0]["id"] if ev else None, "input_freshness_days": age, "score_kind": "calibrated_probability_of_new_loss_in_cell (ECE-gated)"}
