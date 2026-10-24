"""IVR / voice call for fire incidents, behind strict safety gates.

Providers:
  simulated  default. Walks a call through queued -> initiated -> ringing -> answered -> completed, every step labelled
             SIMULATED. Nothing is dialled.
  twilio     real calls through the Twilio REST API. Needs credentials, an approved recipient and explicit confirmation.
  omnidimension  real calls through the OmniDimension voice-agent API (POST /calls/dispatch). Needs OMNIDIM_API_KEY, OMNIDIM_AGENT_ID
             and the response number. The agent must be set up in the OmniDimension dashboard to read the `message` it is given.

A call is never reported as answered unless the provider says so.
"""
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import get_settings
from app.services import fireops

settings = get_settings()
TWILIO_MAP = {"queued": "QUEUED", "ringing": "RINGING", "in-progress": "ANSWERED", "completed": "COMPLETED", "busy": "BUSY", "failed": "FAILED", "no-answer": "NO_ANSWER", "canceled": "CANCELLED"}
FINAL = {"COMPLETED", "BUSY", "FAILED", "NO_ANSWER", "CANCELLED"}


def approved_numbers() -> set[str]:
    """Numbers a real call may reach: the configured response number, or the explicit IVR_APPROVED_NUMBERS list when one is set."""
    listed = {x.strip() for x in (settings.ivr_approved_numbers or "").split(",") if x.strip()}
    return listed or ({settings.ivr_response_number} if settings.ivr_response_number else set())


OMNI_STATUS = {"completed": "COMPLETED", "complete": "COMPLETED", "answered": "ANSWERED", "in-progress": "ANSWERED", "in_progress": "ANSWERED", "ringing": "RINGING", "busy": "BUSY", "no-answer": "NO_ANSWER", "no_answer": "NO_ANSWER", "failed": "FAILED", "canceled": "CANCELLED", "cancelled": "CANCELLED"}


def missing_for(provider: str) -> list[str]:
    if provider == "omnidimension":
        return [m for m, ok in (("OMNIDIM_API_KEY", bool(settings.omnidim_api_key)), ("OMNIDIM_AGENT_ID", bool(settings.omnidim_agent_id)), ("IVR_RESPONSE_NUMBER", bool(settings.ivr_response_number))) if not ok]
    return [m for m, ok in (("TWILIO_ACCOUNT_SID", bool(settings.twilio_account_sid)), ("TWILIO_AUTH_TOKEN", bool(settings.twilio_auth_token)), ("TWILIO_FROM_NUMBER", bool(settings.twilio_from_number)), ("IVR_RESPONSE_NUMBER", bool(settings.ivr_response_number))) if not ok]


def dial(script: str, context: dict[str, Any], client: httpx.Client | None = None, sleep=time.sleep) -> dict[str, Any]:
    """Place one real call with the configured provider (up to 3 tries on transient failures). Returns
    {ok, provider, call_id, attempts, error}. Nothing is claimed beyond 'the provider accepted the request'."""
    provider, number = settings.ivr_provider, settings.ivr_response_number
    c = client or httpx.Client(timeout=30)
    out: dict[str, Any] = {"ok": False, "provider": provider, "call_id": None, "attempts": [], "error": None}
    for attempt in range(1, 4):
        t0 = time.time()
        try:
            if provider == "omnidimension":
                body: dict[str, Any] = {"agent_id": settings.omnidim_agent_id, "to_number": number, "call_context": {**context, "message": script}, "metadata": {"source": "vanrakshak", "reference": context.get("reference")}}
                if settings.omnidim_from_number_id:
                    body["from_number_id"] = settings.omnidim_from_number_id
                r = c.post(f"{settings.omnidim_base_url.rstrip('/')}/calls/dispatch", json=body, headers={"Authorization": f"Bearer {settings.omnidim_api_key}"})
                ok = r.status_code in (200, 201) and (r.json().get("success", True) if r.headers.get("content-type", "").startswith("application/json") else True)
                call_id = str(r.json().get("requestId")) if ok else None
            else:
                say = script.replace("&", " and ").replace("<", " ")
                twiml = f'<Response><Say voice="alice" language="en-IN">{say}</Say><Pause length="2"/><Say voice="alice" language="en-IN">Repeating. {say}</Say></Response>'
                r = c.post(f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Calls.json", auth=(settings.twilio_account_sid, settings.twilio_auth_token), data={"To": number, "From": settings.twilio_from_number, "Twiml": twiml})
                ok = r.status_code in (200, 201)
                call_id = r.json().get("sid") if ok else None
            out["attempts"].append({"n": attempt, "http": r.status_code, "duration_s": round(time.time() - t0, 2)})
            if ok:
                return {**out, "ok": True, "call_id": call_id}
            out["error"] = f"HTTP {r.status_code}: {r.text[:160]}"
            if r.status_code < 500 and r.status_code != 429:
                return out
        except (httpx.HTTPError, ValueError) as exc:
            out["attempts"].append({"n": attempt, "error": type(exc).__name__})
            out["error"] = type(exc).__name__
        if attempt < 3:
            sleep(2 * attempt)
    return out


def _req_id(v: Any) -> str:
    return str(v.get("id")) if isinstance(v, dict) else str(v)


def remote_detail(provider: str, call_id: str, client: httpx.Client | None = None) -> dict[str, Any] | None:
    """The provider's own view of a call: {status, duration_s, hangup}. Status is normalised (RINGING / ANSWERED / COMPLETED /
    BUSY / NO_ANSWER / FAILED); None if the provider does not know the call yet."""
    c = client or httpx.Client(timeout=20)
    try:
        if provider == "omnidimension":
            r = c.get(f"{settings.omnidim_base_url.rstrip('/')}/calls/logs", params={"page": 1, "pagesize": 30, "call_request_id": call_id}, headers={"Authorization": f"Bearer {settings.omnidim_api_key}"})
            if r.status_code != 200:
                return None
            body = r.json()
            rows = body.get("call_log_data") or body.get("data") or body.get("calls") or (body if isinstance(body, list) else [])
            for row in rows:
                if _req_id(row.get("call_request_id", row.get("requestId", ""))) == str(call_id):
                    raw = str(row.get("call_status") or row.get("status") or "").lower()
                    dur = row.get("call_duration_in_seconds")
                    try:
                        dur = round(float(dur))
                    except (TypeError, ValueError):
                        dur = None
                    st = OMNI_STATUS.get(raw) or ("COMPLETED" if row.get("call_end_time") or dur else None)
                    if st == "COMPLETED" and (dur is not None and dur < 3):
                        st = "NO_ANSWER"   # connected for under three seconds: nobody listened
                    return {"status": st, "duration_s": dur, "hangup": row.get("hangup_reason")}
            return None
        r = c.get(f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Calls/{call_id}.json", auth=(settings.twilio_account_sid, settings.twilio_auth_token))
        if r.status_code != 200:
            return None
        j = r.json()
        return {"status": TWILIO_MAP.get(j.get("status")), "duration_s": int(j["duration"]) if j.get("duration") else None, "hangup": None}
    except (httpx.HTTPError, ValueError):
        return None


def remote_status(provider: str, call_id: str, client: httpx.Client | None = None) -> str | None:
    d = remote_detail(provider, call_id, client)
    return d["status"] if d else None


def mask(n: str | None) -> str:
    return "not set" if not n else "…" + n[-3:]


def status() -> dict[str, Any]:
    p = settings.ivr_provider
    missing = missing_for(p if p in ("twilio", "omnidimension") else "omnidimension") + ([] if p in ("twilio", "omnidimension") else ["IVR_PROVIDER=omnidimension"])
    real_ready = p in ("twilio", "omnidimension") and not missing_for(p) and settings.ivr_response_number in approved_numbers()
    return {"provider": p, "simulated_available": True, "real_ready": real_ready,
            "response_contact": settings.ivr_response_label, "response_number": mask(settings.ivr_response_number), "number_approved": bool(settings.ivr_response_number and settings.ivr_response_number in approved_numbers()),
            "keypad_acknowledgement": False, "keypad_note": "Keypad acknowledgement needs a public HTTPS callback URL (PUBLIC_BASE_URL) and is not enabled.",
            "dimension": None, "missing": missing}


def _num(v: float, places: int = 3) -> str:
    return f"{v:.{places}f}"


def _ago(det: dict[str, Any]) -> str | None:
    try:
        hrs = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(str(det["observed_at"]).replace("Z", "+00:00"))).total_seconds() / 3600)
    except (KeyError, ValueError, TypeError):
        return None
    return "just now" if hrs < 0.5 else "about an hour ago" if hrs < 1.5 else f"{round(hrs)} hours ago"


def spoken(inc: dict[str, Any]) -> str:
    """The call script, written to be heard: the urgent facts first, short sentences, location and code last."""
    from app.services import geoinfo

    det, r, fc, imp = inc.get("detection") or {}, inc.get("route") or {}, inc.get("forecast") or {}, inc.get("impact") or {}
    pl = det.get("place") or geoinfo.place(inc["lat"], inc["lon"])
    if pl and not det.get("place"):
        det["place"] = pl
        fireops.patch(inc["id"], detection=det)
    lat, lon = inc["lat"], inc["lon"]
    confirmed = inc.get("status") in ("CONFIRMED", "DISPATCH_REQUESTED", "DISPATCHED", "MONITORING")
    parts = ["Hello. This is a VanRakshak fire alert. Please listen carefully."]
    when = _ago(det)
    parts.append(f"A forest fire has been detected {'near ' + pl['name'] if pl else 'in a forest area'}.")
    parts.append(("Satellites spotted it " + when + (", and the control room has confirmed it." if confirmed else ". It is still being confirmed.")) if when else ("The control room has confirmed it." if confirmed else "It is still being confirmed."))
    hz = (fc.get("horizons") or {}).get("12")
    if hz and hz.get("area_ha", {}).get("p50") is not None:
        w = ((fc.get("inputs") or {}).get("horizon_weather") or {}).get("12") or {}
        wind = f", with wind from the {geoinfo.compass(w['wind_from_deg'])}" if w.get("wind_from_deg") is not None else ""
        parts.append(f"In twelve hours it could spread to about {round(hz['area_ha']['p50'])} hectares{wind}.")
    near = [p for p in (imp.get("places") or {}).get("settlements", [])[:2] if p.get("name")]
    if near:
        parts.append("Villages close by: " + ", ".join(f"{p['name']}, {round(p['distance_km'])} kilometres away" for p in near) + ".")
    if r.get("status") == "OK":
        st = r["station"]
        parts.append(f"The nearest fire station is {st['name']}, {round(r['distance_km'])} kilometres {geoinfo.direction_from(lat, lon, st['lat'], st['lon'])} of the fire, {geoinfo.say_journey(r)}.")
    parts.append(f"The location is {_num(abs(lat))} degrees {'north' if lat >= 0 else 'south'}, {_num(abs(lon))} degrees {'east' if lon >= 0 else 'west'}. The incident code is {inc['code'].replace('-', ' ')}.")
    parts.append("Details and the route are in the VanRakshak app and on Telegram. Please respond now.")
    return " ".join(parts)


def _entry(mode: str, provider: str) -> dict[str, Any]:
    return {"id": str(uuid.uuid4())[:8], "mode": mode, "provider": provider, "simulated": mode == "simulated", "status": "REQUESTED", "provider_call_id": None, "attempts": [], "history": [], "created_at": datetime.now(timezone.utc).isoformat()}


def _save(incident_id: str, entry: dict[str, Any]) -> None:
    inc = fireops.get(incident_id)
    ivr = inc["ivr"] or {"calls": []}
    calls = [c for c in ivr["calls"] if c["id"] != entry["id"]] + [entry]
    fireops.patch(incident_id, ivr={"calls": calls})


def _set(incident_id: str, entry: dict[str, Any], status_: str, demo: bool, message: str, level: str = "ok", error: str | None = None) -> None:
    entry["status"] = status_
    entry["history"].append({"status": status_, "at": datetime.now(timezone.utc).isoformat()})
    _save(incident_id, entry)
    fireops.log_event(incident_id, f"ivr_{status_.lower()}", "ivr", level, message, ref=entry.get("provider_call_id") or entry["id"], error=error, is_demo=demo, details={"mode": entry["mode"]})


def place_call(incident_id: str, mode: str, confirm_real: bool, actor: str) -> dict[str, Any]:
    inc = fireops.get(incident_id)
    demo = inc["is_demo"]
    if inc["status"] not in ("DISPATCH_REQUESTED", "DISPATCHED", "MONITORING"):
        fireops.log_event(incident_id, "ivr_refused", "ivr", "skipped", f"No call placed: {inc['code']} is {inc['status_label']}; dispatch must be authorised first", is_demo=demo)
        raise fireops.WorkflowError("Authorise dispatch first: calls are only placed for incidents approved for response")
    provider = settings.ivr_provider
    if mode != "real":
        e = _entry("simulated", "simulated")
        e["script"] = spoken(inc)
        _set(incident_id, e, "QUEUED", demo, "[SIMULATED] Call queued (nothing is dialled)", "info")
        threading.Thread(target=_simulate, args=(incident_id, e, demo), daemon=True).start()
        return e
    if provider not in ("twilio", "omnidimension"):
        raise fireops.WorkflowError("Real calls need IVR_PROVIDER=omnidimension (or twilio).")
    miss = missing_for(provider)
    if miss:
        msg = "Phone calls are not set up: missing " + ", ".join(miss)
        fireops.log_event(incident_id, "ivr_not_configured", "ivr", "failed", msg, error="unconfigured", is_demo=demo)
        raise fireops.WorkflowError(msg)
    if not confirm_real:
        raise fireops.WorkflowError("A real call needs explicit confirmation (confirm_real=true)")
    number = settings.ivr_response_number
    if number not in approved_numbers():
        msg = f"Refused: {mask(number)} is not in IVR_APPROVED_NUMBERS."
        fireops.log_event(incident_id, "ivr_refused", "ivr", "failed", msg, error="recipient not approved", is_demo=demo)
        raise fireops.WorkflowError(msg)
    e = _entry("real", provider)
    e["script"] = spoken(inc)
    _set(incident_id, e, "REQUESTED", demo, f"Calling {settings.ivr_response_label} ({mask(number)})", "pending")
    ctx = {"reference": inc["code"], "incident": inc["code"], "latitude": inc["lat"], "longitude": inc["lon"], "place": ((inc.get("detection") or {}).get("place") or {}).get("name"), "kind": "forest fire"}
    res = dial(e["script"], ctx, sleep=lambda s_: (fireops.log_event(incident_id, "ivr_retry", "ivr", "retried", f"The call request failed; trying again", is_demo=demo), time.sleep(s_))[1])
    e["attempts"] = res["attempts"]
    if res["ok"]:
        e["provider_call_id"] = res["call_id"]
        _set(incident_id, e, "INITIATED", demo, f"Call placed with {provider}; not answered yet", "ok")
        threading.Thread(target=_poll_remote, args=(incident_id, e, demo), daemon=True).start()
        return e
    _set(incident_id, e, "FAILED", demo, f"Call request failed: {res['error']}", "failed", error=res["error"])
    return e


def _simulate(incident_id: str, e: dict[str, Any], demo: bool) -> None:
    for s, msg in (("INITIATED", "[SIMULATED] Call initiated"), ("RINGING", "[SIMULATED] Ringing"), ("ANSWERED", "[SIMULATED] Answered (simulation: no person was called)"), ("COMPLETED", "[SIMULATED] Call completed")):
        time.sleep(1.2)
        _set(incident_id, e, s, demo, msg, "info")


def refresh_calls(incident_id: str) -> None:
    """A call that was placed before a restart is no longer followed by its background thread: ask the provider once, on demand."""
    inc = fireops.get(incident_id)
    for e in ((inc or {}).get("ivr") or {}).get("calls", []):
        if e.get("simulated") or e["status"] in FINAL or not e.get("provider_call_id"):
            continue
        d = remote_detail(e["provider"], e["provider_call_id"])
        if d and d["status"] and d["status"] != e["status"]:
            if d.get("duration_s") is not None:
                e["duration_s"] = d["duration_s"]
            _set(incident_id, e, d["status"], inc["is_demo"], {"RINGING": "Phone is ringing", "ANSWERED": "Call answered", "COMPLETED": f"Call completed ({e.get('duration_s')} s)", "BUSY": "Line busy", "NO_ANSWER": "Call not answered"}.get(d["status"], f"Call {d['status'].lower()}"), "ok" if d["status"] in ("RINGING", "ANSWERED", "COMPLETED") else "failed")


def _poll_remote(incident_id: str, e: dict[str, Any], demo: bool) -> None:
    """Follow the call with the provider's own status until it ends. Short calls end in seconds, so poll often."""
    gap = 3 if e["provider"] == "omnidimension" else 6
    for _ in range(int(240 / gap)):
        time.sleep(gap)
        d = remote_detail(e["provider"], e["provider_call_id"])
        new = d["status"] if d else None
        if new and new != e["status"]:
            if d.get("duration_s") is not None:
                e["duration_s"] = d["duration_s"]
            words = {"RINGING": "Phone is ringing", "ANSWERED": "Call answered", "COMPLETED": f"Call completed ({e['duration_s']} s)" if e.get("duration_s") is not None else "Call completed", "BUSY": "Line busy", "NO_ANSWER": "Call not answered"}
            _set(incident_id, e, new, demo, words.get(new, f"Call {new.lower()}"), "ok" if new in ("RINGING", "ANSWERED", "COMPLETED") else "failed")
        if new in FINAL:
            return
