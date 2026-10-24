"""Rainfall from Open-Meteo (free, no key): recent observation/forecast and a climatology comparison."""
from datetime import date, timedelta
from typing import Any

import httpx

FORECAST = "https://api.open-meteo.com/v1/forecast"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0); reanalysis/model data, not gauge measurements"
HEAVY_RAIN_MM_DAY = 50.0  # IMD "very heavy" begins at 115.6 mm; 50 mm is used here as a screening level only


class WeatherUnavailable(RuntimeError):
    pass


def _get(client: httpx.Client, url: str, params: dict[str, Any]) -> dict[str, Any]:
    last = None
    for _ in range(2):
        try:
            r = client.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
    raise WeatherUnavailable(f"Open-Meteo request failed: {type(last).__name__}")


def rainfall_summary(lat: float, lon: float, today: date | None = None, client: httpx.Client | None = None) -> dict[str, Any]:
    client = client or httpx.Client(timeout=30)
    today = today or date.today()
    recent = _get(client, FORECAST, {"latitude": lat, "longitude": lon, "daily": "precipitation_sum", "past_days": 30, "forecast_days": 7, "timezone": "UTC"})["daily"]
    days = [(date.fromisoformat(d), v) for d, v in zip(recent["time"], recent["precipitation_sum"])]
    past = [v or 0.0 for d, v in days if d <= today]
    ahead = [v or 0.0 for d, v in days if d > today]
    last30 = sum(past[-30:])
    clim = []
    for years_back in range(1, 6):
        end = today - timedelta(days=365 * years_back)
        try:
            a = _get(client, ARCHIVE, {"latitude": lat, "longitude": lon, "start_date": (end - timedelta(days=29)).isoformat(), "end_date": end.isoformat(), "daily": "precipitation_sum", "timezone": "UTC"})["daily"]["precipitation_sum"]
            clim.append(sum(x or 0.0 for x in a))
        except WeatherUnavailable:
            continue
    mean_clim = sum(clim) / len(clim) if clim else None
    return {
        "status": "ok", "attribution": ATTRIBUTION, "claim_type": "observation_third_party_model",
        "last_7_days_mm": round(sum(past[-7:]), 1), "last_30_days_mm": round(last30, 1),
        "next_7_days_forecast_mm": round(sum(ahead), 1), "max_daily_past_30_mm": round(max(past[-30:], default=0), 1),
        "climatology_30d_mm": round(mean_clim, 1) if mean_clim is not None else None, "climatology_years": len(clim),
        "anomaly_ratio": round(last30 / mean_clim, 2) if mean_clim else None,
        "heavy_rain_in_window": max(past[-7:] + ahead[:3], default=0) >= HEAVY_RAIN_MM_DAY,
        "note": "Gridded model/reanalysis rainfall at ~10 km; local rainfall can differ materially.",
    }
