"""Phone side of Ranger AR Navigation.

A signed, expiring link (see services/mobilelink.py) lets a phone open /mobile/ar/<token> without a login. Every call below checks the
signature and the expiry again, and only works for the one incident the link was made for. Guidance itself reuses the existing
navigation code: the same route planning, session, position, reroute and end functions, with the same privacy rules (only the latest
position and summary figures are kept, and the position is deleted when the session ends).
"""
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import rakshak_routes as rr
from app.core.config import get_settings
from app.core.geometry import validate_geojson
from app.core.security import Role, require_roles
from app.db.database import audit, get_one
from app.services import mobilelink

settings = get_settings()
api = APIRouter(prefix=settings.api_prefix)
CREATE = (Role.ADMIN, Role.REVIEWER, Role.OFFICER)
REVIEWED = ("APPROVED_FOR_INSPECTION", "ASSIGNED", "DISPATCHED", "ACKNOWLEDGED", "FIELD_VERIFICATION_PENDING")
KINDS = {"cutting": "Illegal cutting", "fire": "Forest fire", "encroachment": "Encroachment", "pollution": "Industrial pollution", "other": "Other"}


class M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LinkIn(M):
    incident_id: str
    route_index: int | None = Field(default=0, ge=0, le=9)


class RouteIn(M):
    origin: dict[str, Any]

    @field_validator("origin")
    @classmethod
    def point(cls, v):
        if v.get("type") != "Point":
            raise ValueError("origin must be a GeoJSON Point")
        return validate_geojson(v)


class SessionIn(RouteIn):
    consent_location_use: bool


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _token(token: str, expired_ok: bool = False) -> dict[str, Any]:
    try:
        return mobilelink.verify(token, now=0 if expired_ok else None)   # ending a session must work even after the link expired, so no position is left behind
    except mobilelink.MobileLinkError as exc:
        raise HTTPException(status_code=410 if exc.code == "expired" else 401, detail=str(exc))


def _incident(data: dict[str, Any]) -> dict[str, Any]:
    inc = get_one("incidents", "id", data["incident_id"])
    if not inc:
        raise HTTPException(status_code=404, detail="The incident for this link no longer exists.")
    return inc


def _own_session(data: dict[str, Any], sid: str) -> None:
    s = get_one("nav_sessions", "id", sid)
    if not s or s["incident_id"] != data["incident_id"]:
        raise HTTPException(status_code=403, detail="This session does not belong to this link.")


@api.post("/navigation/mobile-links")
def create_link(payload: LinkIn, request: Request, role: Role = Depends(require_roles(*CREATE))):
    """A short-lived link (and QR code) that opens guidance for one incident on a ranger's phone."""
    inc = get_one("incidents", "id", payload.incident_id)
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")
    if inc["status"] not in REVIEWED:
        raise HTTPException(status_code=409, detail=f"Incident is {inc['status']}; field guidance is available only for reviewed incidents. A reviewer must approve it for inspection first.")
    token, exp = mobilelink.make(inc["id"], payload.route_index)
    base, warn = mobilelink.base_url(str(request.base_url))
    audit(role.value, "mobile_link_created", "incident", inc["id"], {"expires_at": _iso(exp)})
    return {"url": f"{base}/mobile/ar/{token}", "expires_at": _iso(exp), "expires_in_minutes": max(1, round((exp - datetime.now(timezone.utc).timestamp()) / 60)), "incident_id": inc["id"], "route_index": payload.route_index, "warning": warn, "geometry_type": inc["geometry"]["type"]}


@api.get("/mobile/ar/{token}")
def mobile_open(token: str):
    data = _token(token)
    inc = _incident(data)
    ev = inc.get("evidence") or {}
    return {"incident": {"id": inc["id"], "title": inc["title"], "status": inc["status"], "geometry": inc["geometry"], "geometry_type": inc["geometry"]["type"],
                         "evidence_type": KINDS.get(ev.get("kind")) or ("Satellite change (candidate)" if inc.get("change_id") else "Fire observation" if inc.get("wildfire_id") else "Reported incident")},
            "route_index": data["route_index"], "expires_at": _iso(data["expires_at"]), "reviewed": inc["status"] in REVIEWED}


@api.post("/mobile/ar/{token}/route")
def mobile_route(token: str, payload: RouteIn):
    data = _token(token)
    inc = _incident(data)
    return rr._plan(inc, payload.origin["coordinates"], "phone position", True)


@api.post("/mobile/ar/{token}/session")
def mobile_start(token: str, payload: SessionIn):
    data = _token(token)
    body = rr.SessionStart(incident_id=data["incident_id"], origin=payload.origin, consent_location_use=payload.consent_location_use, route_index=data["route_index"])
    try:
        return rr.start_session(body, role=Role.OFFICER)
    except IndexError:   # the route chosen on the desktop no longer exists in a fresh plan: use the recommended one
        body.route_index = None
        return rr.start_session(body, role=Role.OFFICER)


@api.post("/mobile/ar/{token}/sessions/{sid}/position")
def mobile_position(token: str, sid: str, payload: rr.PositionUpdate):
    data = _token(token)
    _own_session(data, sid)
    return rr.update_position(sid, payload, role=Role.OFFICER)


@api.post("/mobile/ar/{token}/sessions/{sid}/reroute")
def mobile_reroute(token: str, sid: str, payload: rr.PositionUpdate):
    data = _token(token)
    _own_session(data, sid)
    return rr.reroute(sid, payload, role=Role.OFFICER)


@api.post("/mobile/ar/{token}/sessions/{sid}/end")
def mobile_end(token: str, sid: str, payload: rr.SessionEnd):
    data = _token(token, expired_ok=True)
    _own_session(data, sid)
    return rr.end_session(sid, payload, role=Role.OFFICER)
