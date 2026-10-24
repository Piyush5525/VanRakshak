"""Historical change-rate projection (baseline) for 7 / 30 / 90 days.

This is *extrapolation of stored candidate detections*, not a physical or validated forecast, and it
says nothing about where loss will occur (that is the separate spatial risk model). Scenarios apply
user-stated multipliers and are hypothetical.
"""
import statistics
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from app.db.database import audit, get_one, insert, list_rows, now

HORIZONS = (7, 30, 90)
SCENARIOS = {"continued_disturbance": 1.0, "effective_protection": 0.4, "restoration_focus": 0.2}  # default multipliers, user-overridable, hypothetical


def intervals(region_id: str) -> list[dict[str, Any]]:
    rows = list_rows("changes", "WHERE region_id = ? AND status IN ('CANDIDATES_FOUND','NO_CANDIDATES')", (region_id,), limit=500)
    out = []
    for r in rows:
        b, c = datetime.fromisoformat(r["baseline_date"]), datetime.fromisoformat(r["comparison_date"])
        days = (c - b).days
        if days < 20:
            continue
        out.append({"change_id": r["id"], "from": r["baseline_date"][:10], "to": r["comparison_date"][:10], "days": days, "loss_ha": r["area_hectares"], "rate_ha_per_day": r["area_hectares"] / days})
    return sorted(out, key=lambda x: x["to"])


def project(region_id: str, multipliers: dict[str, float] | None, actor: str) -> dict[str, Any]:
    if not get_one("regions", "id", region_id):
        raise ValueError("region not found")
    ivs = intervals(region_id)
    scen = {**SCENARIOS, **(multipliers or {})}
    base = {"id": str(uuid.uuid4()), "region_id": region_id, "claim_type": "projection_from_historical_rate", "is_observation": False, "is_forecast_of_location": False, "created_at": now(), "intervals_used": ivs}
    if not ivs:
        result = {**base, "status": "INSUFFICIENT_DATA", "reason": "No stored change-detection intervals for this region. Run change detection (or build a yearly history) first."}
    else:
        rates = [i["rate_ha_per_day"] for i in ivs]
        mean_rate = statistics.mean(rates)
        sd = statistics.stdev(rates) if len(rates) >= 3 else None
        projections = {}
        for name, mult in scen.items():
            per = {}
            for h in HORIZONS:
                est = mean_rate * mult * h
                per[str(h)] = {"expected_additional_loss_ha": round(est, 3), "range_ha": [round(max((mean_rate - 1.645 * sd) * mult * h, 0), 3), round((mean_rate + 1.645 * sd) * mult * h, 3)] if sd is not None else None}
            projections[name] = {"multiplier": mult, "kind": "baseline" if name == "continued_disturbance" else "hypothetical_scenario", "by_horizon_days": per}
        backtest = None
        if len(ivs) >= 4:
            hist, last = ivs[:-1], ivs[-1]
            pred = statistics.mean(i["rate_ha_per_day"] for i in hist) * last["days"]
            backtest = {"method": "mean rate of earlier intervals vs the last interval", "predicted_ha": round(pred, 3), "observed_ha": last["loss_ha"], "abs_error_ha": round(abs(pred - last["loss_ha"]), 3)}
        warnings = ["Extrapolates candidate detections from a rule-based detector; detections were not field-verified"]
        if len(ivs) < 3:
            warnings.append(f"Only {len(ivs)} interval(s): no uncertainty range can be estimated")
        if len({i["from"][5:7] for i in ivs}) > 1:
            warnings.append("Intervals start in different months; seasonality is not modelled")
        result = {**base, "status": "OK", "mean_rate_ha_per_day": round(mean_rate, 5), "rate_std_ha_per_day": round(sd, 5) if sd is not None else None, "projections": projections, "backtest": backtest if backtest else {"status": "not_possible", "reason": "needs at least 4 historical intervals"}, "warnings": warnings, "assumptions": ["Future loss rate equals the historical mean", "Scenario multipliers are user-adjustable assumptions, not model outputs"], "limitations": ["Does not predict locations", "Wildfire, weather and policy changes are not modelled"]}
    insert("projections", {"id": result["id"], "region_id": region_id, "status": result["status"], "result": result, "created_at": result["created_at"]})
    audit(actor, "projection_created", "projection", result["id"], {"status": result["status"]})
    return result


def build_history(region_id: str, dates: list[datetime], actor: str) -> dict[str, Any]:
    """Run change detection between consecutive dates (same season recommended). Long-running; call from a job."""
    from app.services.analysis import detect_change

    if len(dates) < 2 or len(dates) > 7:
        raise ValueError("Provide 2-7 ascending dates")
    ds = sorted(dates)
    out = []
    for a, b in zip(ds, ds[1:]):
        req = SimpleNamespace(region_id=region_id, baseline_date=a, comparison_date=b, provider="planetary_computer", model_version=None, threshold=0.5, before_raster=None, after_raster=None, window_days=30, max_cloud_cover=60)
        r = detect_change(req, actor)
        out.append({"from": a.date().isoformat(), "to": b.date().isoformat(), "change_id": r["id"], "status": r["status"], "area_hectares": r["area_hectares"]})
    return {"intervals": out}
