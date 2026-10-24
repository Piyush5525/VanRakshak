"""Building-energy estimation without invented benchmarks.

* Observed building: requires real metered history. Seasonal profile + empirical spread; no
  hidden model, no typical-intensity table. Without >=12 months of readings the answer is UNAVAILABLE.
* Hypothetical development: arithmetic on assumptions the user supplies (energy intensity,
  operating fraction). Emissions only with a stated emission factor. Output is labelled a scenario.
"""
import statistics
import uuid
from collections import defaultdict
from typing import Any

from fastapi import HTTPException

from app.core.config import get_settings
from app.db.database import audit, insert, now

MAX_AREA_M2 = 2_000_000


def _monthly(meter_history: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in meter_history:
        month, kwh = str(row.get("month", ""))[:7], row.get("kwh")
        if len(month) != 7 or month[4] != "-" or not isinstance(kwh, (int, float)) or kwh < 0:
            raise HTTPException(status_code=422, detail="meter_history rows need {'month': 'YYYY-MM', 'kwh': non-negative number}")
        out[month] = out.get(month, 0.0) + float(kwh)
    return out


def predict_observed(request, actor: str) -> dict[str, Any]:
    if request.floor_area_m2 > MAX_AREA_M2:
        raise HTTPException(status_code=422, detail=f"floor_area_m2 above {MAX_AREA_M2} is outside the supported range")
    monthly = _monthly(request.meter_history)
    base = {"id": str(uuid.uuid4()), "claim_type": "model_input_dependent_estimate", "features": {"building_type": request.building_type, "floor_area_m2": request.floor_area_m2, "age_years": request.age_years}, "created_at": now()}
    if len(monthly) < 12:
        result = {**base, "status": "UNAVAILABLE", "reason": f"{len(monthly)} distinct months of metered history supplied; at least 12 are required. No benchmark intensity is substituted.", "model_version": "seasonal-profile-v1"}
    else:
        by_cal: dict[str, list[float]] = defaultdict(list)
        for month, kwh in sorted(monthly.items()):
            by_cal[month[5:]].append(kwh)
        profile = {m: statistics.mean(v) for m, v in by_cal.items()}
        spread = {m: (statistics.stdev(v) if len(v) > 1 else None) for m, v in by_cal.items()}
        annual = sum(profile.get(f"{m:02d}", statistics.mean(profile.values())) for m in range(1, 13))
        interval = None
        if all(s is not None for s in spread.values()) and len(spread) == 12:
            sd = (sum(s * s for s in spread.values())) ** 0.5  # independent monthly errors
            interval = {"lower": round(max(annual - 1.645 * sd, 0), 1), "upper": round(annual + 1.645 * sd, 1), "level": "approx. 90% from inter-year spread (assumes independence)"}
        warnings = [] if interval else ["Fewer than 2 years of data: no uncertainty interval can be estimated"]
        result = {**base, "status": "ESTIMATED_FROM_METER_HISTORY", "model_version": "seasonal-profile-v1", "annual_kwh": round(annual, 1), "monthly_profile_kwh": {m: round(v, 1) for m, v in sorted(profile.items())}, "monthly_kwh": round(annual / 12, 1), "prediction_interval": interval, "peak_demand_kw": max((r.get("peak_kw") for r in request.meter_history if isinstance(r.get("peak_kw"), (int, float))), default=None), "intensity_kwh_per_m2_year": round(annual / request.floor_area_m2, 2), "history_months": len(monthly), "out_of_distribution_warning": False, "limitations": ["Assumes future usage resembles the supplied history", "No weather normalisation (no weather series supplied)"] + warnings}
    insert("energy_predictions", {"id": result["id"], "status": result["status"], "claim_type": result["claim_type"], "result": result, "created_at": result["created_at"]})
    audit(actor, "energy_prediction_created", "energy_prediction", result["id"], {"status": result["status"]})
    return result


def scenario(request, actor: str) -> dict[str, Any]:
    a = request.assumptions or {}
    if request.floor_area_m2 > MAX_AREA_M2:
        raise HTTPException(status_code=422, detail=f"floor_area_m2 above {MAX_AREA_M2} is outside the supported range")
    intensity = a.get("intensity_kwh_per_m2_year")
    if not isinstance(intensity, (int, float)) or intensity <= 0:
        raise HTTPException(status_code=422, detail="assumptions.intensity_kwh_per_m2_year (positive number, with assumptions.intensity_source) is required; the system does not invent one")
    low, high = a.get("intensity_low"), a.get("intensity_high")
    op = float(a.get("operating_fraction", 1.0))
    if not 0 < op <= 1:
        raise HTTPException(status_code=422, detail="operating_fraction must be in (0, 1]")
    annual = request.floor_area_m2 * intensity * op
    ef = a.get("emission_factor_kg_per_kwh") or get_settings().grid_emission_factor_kg_per_kwh
    result = {"id": str(uuid.uuid4()), "status": "SCENARIO_ARITHMETIC", "claim_type": "hypothetical_scenario", "is_observation": False, "is_prediction_of_real_facility": False, "annual_kwh": round(annual, 1), "monthly_kwh": round(annual / 12, 1), "range_kwh": {"lower": round(request.floor_area_m2 * low * op, 1), "upper": round(request.floor_area_m2 * high * op, 1)} if isinstance(low, (int, float)) and isinstance(high, (int, float)) else None, "assumptions": {**a, "building_type": request.building_type, "floor_area_m2": request.floor_area_m2}, "emissions": {"kg_co2e_per_year": round(annual * ef, 1), "emission_factor_kg_per_kwh": ef, "factor_source": a.get("emission_factor_source", "GRID_EMISSION_FACTOR_KG_PER_KWH setting")} if ef else {"status": "not_calculated", "reason": "No emission factor supplied or configured"}, "model_version": "scenario-arithmetic-v1", "limitations": ["Result is exactly as good as the stated intensity assumption", "Not evidence that any facility exists or will be built"], "created_at": now()}
    insert("energy_predictions", {"id": result["id"], "status": result["status"], "claim_type": result["claim_type"], "result": result, "created_at": result["created_at"]})
    audit(actor, "energy_scenario_created", "energy_prediction", result["id"], {"building_type": request.building_type})
    return result
