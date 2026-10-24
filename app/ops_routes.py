"""Response coordination: stations, routing, dispatch, notification callbacks."""
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response

from app.core.config import get_settings
from app.core.geometry import to_shape
from app.core.security import Role, require_roles
from app.db.database import audit, get_one, insert, list_rows, now, update
from app.domain.state_machine import transition
from app.schemas import DispatchRequest, PatrolRequest, RouteRequest, StationCreate
from app.services.notify import NotificationBlocked, Notifier, build_message, verify_shared_secret, verify_twilio_signature
from app.services.routing import Router, nearest_station

settings = get_settings()
notifier = Notifier(settings)
router_service = Router(settings)
api = APIRouter(prefix=settings.api_prefix)


def get_incident(incident_id: str) -> dict:
    incident = get_one("incidents", "id", incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


def _point(geometry: dict) -> list[float]:
    return geometry["coordinates"] if geometry["type"] == "Point" else list(to_shape(geometry).representative_point().coords[0])


def _mask(contacts: dict) -> dict:
    return {k: "***" for k in contacts}


def _incident_message(incident: dict) -> str:
    change = get_one("changes", "id", incident["change_id"]) if incident.get("change_id") else None
    return build_message(incident, change)


# ------------------------------------------------------------------ stations
@api.post("/stations")
def create_station(payload: StationCreate, role: Role = Depends(require_roles(Role.ADMIN))):
    record = {"id": str(uuid.uuid4()), **payload.model_dump(), "created_at": now()}
    insert("stations", record)
    audit(role.value, "station_created", "station", record["id"], {"name": payload.name})
    return {**record, "contacts": _mask(record["contacts"])}


@api.get("/stations")
def list_stations(role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.OFFICER))):
    return [{**s, "contacts": _mask(s["contacts"])} for s in list_rows("stations", limit=500)]


# ------------------------------------------------------------------ routing
@api.post("/routes/inspection")
def inspection_route(payload: RouteRequest, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.OFFICER))):
    destination = payload.destination
    if payload.incident_id:
        destination = {"type": "Point", "coordinates": _point(get_incident(payload.incident_id)["geometry"])}
    if not destination:
        raise HTTPException(status_code=422, detail="Provide destination or incident_id")
    dest = _point(destination)
    origin, origin_source = payload.origin, "supplied"
    if not origin:
        nearest = nearest_station(list_rows("stations", limit=1000), dest)
        if not nearest:
            raise HTTPException(status_code=409, detail="No origin supplied and no stations are registered")
        origin = nearest[0]["location"]
        origin_source = f"nearest registered station by straight line: {nearest[0]['name']} ({nearest[1]:.1f} km); availability not known"
    result = router_service.route(origin["coordinates"], dest, payload.alternatives)
    return {**result, "origin": origin, "origin_source": origin_source, "destination": {"type": "Point", "coordinates": dest}, "destination_kind": "suspected disturbance centre / inspection access point"}


@api.post("/routes/patrol")
def patrol_route(payload: PatrolRequest, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.OFFICER))):
    stops = [_point(get_incident(i)["geometry"]) for i in payload.incident_ids]
    origin = payload.origin["coordinates"] if payload.origin else None
    if origin is None:
        nearest = nearest_station(list_rows("stations", limit=1000), stops[0])
        if not nearest:
            raise HTTPException(status_code=409, detail="No origin supplied and no stations are registered")
        origin = nearest[0]["location"]["coordinates"]
    return {**router_service.patrol(origin, stops), "incident_ids": payload.incident_ids}


# ------------------------------------------------------------------ dispatch
@api.post("/incidents/{incident_id}/dispatch")
def dispatch_incident(incident_id: str, payload: DispatchRequest, role: Role = Depends(require_roles(Role.ADMIN, Role.OFFICER))):
    item = get_incident(incident_id)
    transition(item["status"], "DISPATCHED")  # only ASSIGNED incidents (which required review) pass
    try:
        delivery = notifier.send(channel=payload.channel, recipient=payload.recipient, text=_incident_message(item), incident_id=incident_id, event="dispatch", actor=role.value, approved=True)
    except NotificationBlocked as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if delivery["status"] == "FAILED":
        audit(role.value, "dispatch_failed", "incident", incident_id, {"notification_id": delivery["id"]})
        raise HTTPException(status_code=502, detail="Provider rejected the notification; incident remains ASSIGNED")
    update("incidents", "id", incident_id, {"status": "DISPATCHED", "evidence": {**item["evidence"], "notification_id": delivery["id"]}, "updated_at": now()})
    audit(role.value, "incident_dispatched", "incident", incident_id, {"notification_id": delivery["id"], "status": delivery["status"]})
    return {"incident": get_incident(incident_id), "notification": delivery["id"], "delivery": {"status": delivery["status"], "provider_response": delivery["provider_response"], "duplicate_suppressed": delivery.get("duplicate_suppressed", False), "safe_mode": delivery["status"] == "CAPTURED_NOT_SENT"}}


# ------------------------------------------------------------------ notifications
@api.post("/notifications/telegram/test")
def telegram_test(payload: dict, role: Role = Depends(require_roles(Role.ADMIN))):
    return notifier.send(channel="telegram", recipient=str(payload.get("recipient", "")), text="VanaNetra test message - no incident attached.", incident_id=None, event=f"test-{uuid.uuid4()}", actor=role.value, approved=True)


@api.post("/notifications/voice/test")
def voice_test(payload: dict, role: Role = Depends(require_roles(Role.ADMIN))):
    return notifier.send(channel="voice", recipient=str(payload.get("recipient", "")), text="VanaNetra test call. No incident is attached.", incident_id=None, event=f"test-{uuid.uuid4()}", actor=role.value, approved=True)


@api.get("/notifications/{notification_id}")
def get_notification(notification_id: str, role: Role = Depends(require_roles(Role.ADMIN, Role.REVIEWER, Role.OFFICER))):
    record = get_one("notifications", "id", notification_id)
    if not record:
        raise HTTPException(status_code=404, detail="Notification not found")
    return record


def _ack_incident(notification: dict) -> None:
    inc_id = (notification.get("payload") or {}).get("incident_id")
    inc = get_one("incidents", "id", inc_id) if inc_id else None
    if inc and inc["status"] == "DISPATCHED":
        update("incidents", "id", inc_id, {"status": "ACKNOWLEDGED", "updated_at": now()})
        audit("recipient", "incident_acknowledged", "incident", inc_id, {"notification_id": notification["id"]})


@api.post("/notifications/telegram/callback")
def telegram_callback(body: dict, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    """Telegram webhook: needs the webhook secret AND a sender who is the notification's recipient."""
    if not verify_shared_secret(settings.callback_secret, x_telegram_bot_api_secret_token):
        raise HTTPException(status_code=403, detail="Invalid callback secret")
    cb = body.get("callback_query") or {}
    data = str(cb.get("data", ""))
    if not data.startswith("ack:"):
        return {"ok": True, "ignored": True}
    notification = get_one("notifications", "id", data[4:])
    sender = str((cb.get("from") or {}).get("id", ""))
    chat = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
    if not notification or notification["channel"] != "telegram" or notification["recipient"] not in {sender, chat}:
        raise HTTPException(status_code=403, detail="Sender is not the recipient of this notification")
    updated = notifier.acknowledge(notification["id"], sender)
    if updated:
        _ack_incident(updated)
    return {"ok": True, "acknowledged": bool(updated)}


async def _verified_form(request: Request) -> dict:
    form = {k: str(v) for k, v in (await request.form()).items()}
    token = settings.twilio_auth_token
    if not token or not verify_twilio_signature(token, str(request.url), form, request.headers.get("x-twilio-signature", "")):
        raise HTTPException(status_code=403, detail="Invalid provider signature")
    return form


@api.post("/notifications/voice/status")
async def voice_status(request: Request, nid: str):
    form = await _verified_form(request)
    record = get_one("notifications", "id", nid)
    if not record or record["channel"] != "voice":
        raise HTTPException(status_code=404, detail="Notification not found")
    call = form.get("CallStatus")
    if record["status"] == "SENT_TO_PROVIDER" and call in ("completed", "busy", "failed", "no-answer", "canceled"):
        update("notifications", "id", nid, {"status": "DELIVERED" if call == "completed" else "FAILED", "provider_response": {**(record["provider_response"] or {}), "call_status": call}})
    return {"ok": True}


@api.post("/notifications/voice/gather")
async def voice_gather(request: Request, nid: str):
    form = await _verified_form(request)
    record = get_one("notifications", "id", nid)
    if record and form.get("Digits") == "1" and record["recipient"] in {form.get("To"), form.get("Called")}:
        updated = notifier.acknowledge(nid, record["recipient"])
        if updated:
            _ack_incident(updated)
    return Response("<Response><Say>Acknowledged. Thank you.</Say></Response>", media_type="application/xml")
