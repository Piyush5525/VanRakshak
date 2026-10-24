"""Fire-response incident core: creation (idempotent), state machine, unified event log.

Every operation in the fire workflow writes to `fire_events` as it really happens. Nothing here inserts a
success entry on behalf of an operation that did not run.
"""
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from app.db.database import audit, connection, decode_row, get_one, insert, now, update

TRANSITIONS: dict[str, set[str]] = {
    "DETECTED": {"NEEDS_VERIFICATION", "REJECTED", "CANCELLED"},
    "NEEDS_VERIFICATION": {"CONFIRMED", "REJECTED", "INCONCLUSIVE", "CANCELLED"},
    "INCONCLUSIVE": {"NEEDS_VERIFICATION", "CONFIRMED", "REJECTED", "CANCELLED"},
    "CONFIRMED": {"DISPATCH_REQUESTED", "MONITORING", "RESOLVED", "REJECTED", "CANCELLED"},
    "DISPATCH_REQUESTED": {"DISPATCHED", "FAILED", "CANCELLED", "MONITORING"},
    "FAILED": {"DISPATCH_REQUESTED", "CANCELLED"},
    "DISPATCHED": {"MONITORING", "RESOLVED", "CANCELLED"},
    "MONITORING": {"RESOLVED", "DISPATCH_REQUESTED", "CANCELLED"},
    "RESOLVED": set(), "REJECTED": {"NEEDS_VERIFICATION"}, "CANCELLED": {"NEEDS_VERIFICATION"},
}
TERMINAL = {"RESOLVED", "REJECTED", "CANCELLED"}
LABEL = {"DETECTED": "Detected", "NEEDS_VERIFICATION": "Needs verification", "CONFIRMED": "Confirmed", "DISPATCH_REQUESTED": "Dispatch requested", "DISPATCHED": "Dispatched (acknowledged)", "MONITORING": "Monitoring", "RESOLVED": "Resolved", "REJECTED": "Rejected", "CANCELLED": "Cancelled", "FAILED": "Notification failed", "INCONCLUSIVE": "Inconclusive"}


class WorkflowError(ValueError):
    pass


def log_event(incident_id: str | None, event_type: str, service: str, status: str, message: str, *, ref: str | None = None, error: str | None = None, correlation_id: str | None = None, is_demo: bool = False, details: dict[str, Any] | None = None) -> int:
    """Append a real event. status: ok | pending | failed | retried | info | skipped."""
    with connection() as conn:
        cur = conn.execute("INSERT INTO fire_events (ts, incident_id, event_type, service, status, message, ref, error, correlation_id, is_demo, details) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (now(), incident_id, event_type, service, status, message[:500], ref, (error or None) and str(error)[:600], correlation_id, int(is_demo), json.dumps(details) if details else None))
        return cur.lastrowid


def events(incident_id: str | None = None, since_id: int = 0, types: list[str] | None = None, services: list[str] | None = None, statuses: list[str] | None = None, demo: bool | None = None, limit: int = 200) -> list[dict[str, Any]]:
    clauses, args = ["id > ?"], [since_id]
    if incident_id:
        clauses.append("incident_id = ?"); args.append(incident_id)
    if types:
        clauses.append(f"event_type IN ({','.join('?' * len(types))})"); args += types
    if services:
        clauses.append(f"service IN ({','.join('?' * len(services))})"); args += services
    if statuses:
        clauses.append(f"status IN ({','.join('?' * len(statuses))})"); args += statuses
    if demo is not None:
        clauses.append("is_demo = ?"); args.append(int(demo))
    with connection() as conn:
        rows = conn.execute(f"SELECT * FROM fire_events WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    out = []
    for r in reversed(rows):
        d = decode_row(dict(r))
        if isinstance(d.get("details"), str):
            try:
                d["details"] = json.loads(d["details"])
            except ValueError:
                pass
        d["is_demo"] = bool(d["is_demo"])
        out.append(d)
    return out


def _next_code(demo: bool) -> str:
    prefix = "SC" if demo else "VF"
    with connection() as conn:
        rows = [r[0] for r in conn.execute("SELECT code FROM fire_incidents WHERE code LIKE ?", (f"{prefix}-%",))]
    n = max([int(c.split("-")[1]) for c in rows if c.split("-")[1].isdigit()] or [0]) + 1
    return f"{prefix}-{n:03d}"


def dedupe_key(source: str, lat: float, lon: float, observed_at: str | None, demo_tag: str | None = None) -> str:
    if demo_tag:
        return f"demo:{demo_tag}"
    hour = (observed_at or "")[:13]
    return hashlib.sha1(f"{source}|{lat:.3f}|{lon:.3f}|{hour}".encode()).hexdigest()


def hydrate(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    row = dict(row)
    row["is_demo"] = bool(row["is_demo"])
    row["status_label"] = LABEL.get(row["status"], row["status"])
    for k in ("detection", "verification", "forecast", "impact", "station", "route", "notify", "ivr", "field_updates"):
        if isinstance(row.get(k), str):
            try:
                row[k] = json.loads(row[k])
            except ValueError:
                row[k] = None
    return row


def get(incident_id: str) -> dict[str, Any] | None:
    return hydrate(get_one("fire_incidents", "id", incident_id))


def list_incidents(include_demo: bool = True, limit: int = 100) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(f"SELECT * FROM fire_incidents {'' if include_demo else 'WHERE is_demo = 0'} ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [hydrate(decode_row(dict(r))) for r in rows]


def create(source: str, lat: float, lon: float, observed_at: str | None, detection: dict[str, Any], actor: str, *, source_detection_id: str | None = None, is_demo: bool = False, demo_tag: str | None = None) -> tuple[dict[str, Any], bool]:
    """Idempotent: the same detection (or demo scenario) returns the existing incident."""
    key = dedupe_key(source, lat, lon, observed_at, demo_tag)
    existing = get_one("fire_incidents", "dedupe_key", key)
    if existing:
        log_event(existing["id"], "incident_duplicate", "workflow", "skipped", f"{existing['code']} already exists for this detection; no duplicate created", is_demo=is_demo)
        return hydrate(existing), False
    iid = str(uuid.uuid4())
    code = _next_code(is_demo)
    rec = {"id": iid, "code": code, "source": source, "source_detection_id": source_detection_id, "dedupe_key": key, "is_demo": int(is_demo), "status": "DETECTED", "lat": round(lat, 6), "lon": round(lon, 6), "observed_at": observed_at, "detection": detection, "verification": {"history": []}, "forecast": None, "impact": None, "station": None, "route": None, "notify": {"telegram": [], "history": []}, "ivr": {"calls": []}, "field_updates": [], "created_by": actor, "created_at": now(), "updated_at": now(), "closed_at": None}
    insert("fire_incidents", rec)
    log_event(iid, "incident_created", "workflow", "ok", f"Incident created: {code}" + (" (scenario)" if is_demo else ""), ref=iid, is_demo=is_demo, details={"source": source, "lat": rec["lat"], "lon": rec["lon"]})
    audit(actor, "fire_incident_created", "fire_incident", iid, {"code": code, "demo": is_demo})
    transition(iid, "NEEDS_VERIFICATION", actor, "Awaiting verification: a satellite hotspot is not a confirmed fire")
    return get(iid), True


def transition(incident_id: str, target: str, actor: str, note: str = "") -> dict[str, Any]:
    inc = get(incident_id)
    if not inc:
        raise WorkflowError("Incident not found")
    if target not in TRANSITIONS.get(inc["status"], set()):
        raise WorkflowError(f"Cannot move {inc['code']} from {inc['status']} to {target}")
    patch: dict[str, Any] = {"status": target, "updated_at": now()}
    if target in TERMINAL:
        patch["closed_at"] = now()
    elif inc["status"] in TERMINAL:
        patch["closed_at"] = None   # reopened
    update("fire_incidents", "id", incident_id, patch)
    log_event(incident_id, "status_changed", "workflow", "ok", f"{inc['code']}: {LABEL.get(inc['status'])} → {LABEL.get(target)}", is_demo=inc["is_demo"], details={"from": inc["status"], "to": target, "actor_role": actor})
    audit(actor, "fire_incident_status", "fire_incident", incident_id, {"from": inc["status"], "to": target, "note": note})
    return get(incident_id)


def patch(incident_id: str, **fields: Any) -> dict[str, Any]:
    update("fire_incidents", "id", incident_id, {**fields, "updated_at": now()})
    return get(incident_id)


def reset_demo() -> dict[str, int]:
    """Delete demonstration incidents and their events only. Real incidents are never touched."""
    with connection() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM fire_incidents WHERE is_demo = 1")]
        n_events = conn.execute("DELETE FROM fire_events WHERE is_demo = 1").rowcount
        n_inc = conn.execute("DELETE FROM fire_incidents WHERE is_demo = 1").rowcount
    return {"incidents_removed": n_inc, "events_removed": n_events, "ids": ids}
