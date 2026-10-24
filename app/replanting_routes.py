"""Backend support for the bundled replanting intelligence experience.

The replanting UI is intentionally self-contained under ``/replanting`` so the
standalone Replantio project can be removed after this integration.  These two
small endpoints keep the browser experience complete without exposing the
Overpass request directly to every visitor.
"""

import os
from typing import Any, cast

import httpx
from fastapi import APIRouter, HTTPException, Request, Response


router = APIRouter()

OVERPASS_ENDPOINTS = (
    "https://overpass.osm.ch/api/interpreter",
    "https://osm-overpass.gs.mil/overpass/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
)
LAND_TAGS = "brownfield|greenfield|meadow|grass|village_green|allotments"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _land_query(bbox: list[float]) -> str:
    south, west, north, east = (f"{value:.5f}" for value in bbox)
    return (
        f"[out:json][timeout:15];"
        f"(way[\"landuse\"~\"^({LAND_TAGS})$\"]({south},{west},{north},{east});"
        f"way[\"abandoned:landuse\"~\"^({LAND_TAGS})$\"]({south},{west},{north},{east}););"
        "out geom 80;"
    )


def _validate_bbox(raw: Any) -> list[float]:
    bbox = [_number(value) for value in raw] if isinstance(raw, list) else None
    if bbox is None or len(bbox) != 4 or any(value is None for value in bbox):
        raise HTTPException(status_code=400, detail="bbox must be [south, west, north, east]")
    values = cast(list[float], bbox)
    south, west, north, east = values
    if south >= north or west >= east or south < -90 or north > 90 or west < -180 or east > 180:
        raise HTTPException(status_code=400, detail="bbox is outside the supported city-scale range")
    if north - south > 0.25 or east - west > 0.25:
        raise HTTPException(status_code=400, detail="bbox is outside the supported city-scale range")
    return values


@router.post("/replanting/api/land")
async def replanting_land(request: Request):
    """Proxy the replanting land radar to an Overpass mirror."""

    if os.environ.get("OSM_DISABLED", "").lower() == "true":
        raise HTTPException(status_code=503, detail="land scan unavailable")
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    bbox = _validate_bbox(payload.get("bbox"))
    query = _land_query(bbox)
    last_error = "no Overpass endpoint available"

    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0), headers={"User-Agent": "VanRakshak/1.0 land radar"}) as client:
        for endpoint in OVERPASS_ENDPOINTS:
            try:
                result = await client.post(endpoint, data={"data": query})
                result.raise_for_status()
                if "json" not in result.headers.get("content-type", ""):
                    last_error = "Overpass returned non-JSON"
                    continue
                data = result.json()
                if data.get("remark"):
                    last_error = str(data["remark"])
                    continue
                return data
            except (httpx.HTTPError, ValueError) as exc:
                last_error = str(exc) or last_error

    raise HTTPException(status_code=503, detail=f"land scan unavailable: {last_error}")


@router.post("/replanting/api/feedback", status_code=204)
async def replanting_feedback(request: Request):
    """Accept optional product feedback without making storage mandatory.

    If Supabase credentials are configured, the same feedback row used by the
    standalone app is persisted.  Otherwise the UI still receives a successful
    response, keeping the integrated demo independent of private credentials.
    """

    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip() or len(message) > 2000:
        raise HTTPException(status_code=400, detail="message required (1-2000 chars)")
    if payload.get("website"):
        return Response(status_code=204)

    supabase_url = os.environ.get("REPLANTING_SUPABASE_URL") or os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("REPLANTING_SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
    if supabase_url and supabase_key:
        row = {
            "message": message.strip(),
            "rating": payload.get("rating") if isinstance(payload.get("rating"), int) and 1 <= payload["rating"] <= 5 else None,
            "cc": str(payload.get("cc", ""))[:2] or None,
            "city": str(payload.get("city", ""))[:80] or None,
            "ha": round(float(payload["ha"]), 1) if isinstance(payload.get("ha"), (int, float)) else None,
            "lang": str(payload.get("lang", ""))[:5] or None,
            "hash": str(payload.get("hash", ""))[:2000] or None,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            stored = await client.post(
                f"{supabase_url.rstrip('/')}/rest/v1/replantio_feedback",
                headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}", "Content-Type": "application/json", "Prefer": "return=minimal"},
                json=row,
            )
        if not stored.is_success:
            raise HTTPException(status_code=502, detail="feedback storage failed")
    return Response(status_code=204)
