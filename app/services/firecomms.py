"""Telegram coordination channel for fire incidents.

* Real sends only when the bot token and chat id are configured AND the operator explicitly confirms the send.
* "SENT" means Telegram accepted the message and returned a message_id. It does NOT mean anyone read it,
  and it never means a station acknowledged or was dispatched.
* Simulation mode builds exactly the same message and attachments but sends nothing.
"""
import html as _html
import io
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.core.config import get_settings
from app.services import fireops, mapimage
from app.services.reports import generic_pdf

settings = get_settings()
OUT_DIR = Path("data/fire_out")


class TelegramError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def configured() -> dict[str, Any]:
    return {"token": bool(settings.telegram_bot_token), "chat_id": bool(settings.telegram_chat_id)}


def authorised_chats() -> set[str]:
    ids = {settings.telegram_chat_id} if settings.telegram_chat_id else set()
    ids |= {x.strip() for x in (settings.telegram_authorised_chat_ids or "").split(",") if x.strip()}
    return ids


def _call(method: str, data: dict[str, Any] | None = None, files: dict[str, tuple[str, bytes, str]] | None = None, client: httpx.Client | None = None) -> dict[str, Any]:
    """One Telegram Bot API call with a bounded retry on 429/5xx. The token never appears in errors."""
    if not settings.telegram_bot_token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not configured")
    c = client or httpx.Client(timeout=60)
    last: TelegramError | None = None
    for attempt in range(2):
        try:
            r = c.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}", data=data, files=files)
        except httpx.HTTPError as exc:
            last = TelegramError(f"Network error: {type(exc).__name__}")
            time.sleep(1.5)
            continue
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code == 200 and body.get("ok"):
            return body["result"]
        desc = str(body.get("description") or f"HTTP {r.status_code}")[:200]
        last = TelegramError(desc, r.status_code)
        if r.status_code == 429 and attempt == 0:
            time.sleep(min(float(body.get("parameters", {}).get("retry_after", 2)), 5))
            continue
        if r.status_code >= 500 and attempt == 0:
            time.sleep(1.5)
            continue
        break
    raise last or TelegramError("Unknown Telegram error")


def status() -> dict[str, Any]:
    cfg = configured()
    out = {**cfg, "chat_masked": ("…" + str(settings.telegram_chat_id)[-3:]) if settings.telegram_chat_id else None, "ready_to_send": cfg["token"] and cfg["chat_id"], "inbound_buttons_enabled": settings.telegram_polling, "authorised_chats": len(authorised_chats()), "bot": None}
    if cfg["token"]:
        try:
            me = _call("getMe")
            out["bot"] = {"username": me.get("username"), "name": me.get("first_name")}
        except TelegramError as exc:
            out["bot_error"] = str(exc)
    return out


def _e(v: Any) -> str:
    return _html.escape(str(v), quote=False)


def public_link() -> str | None:
    b = (settings.public_base_url or "").lower()
    return settings.public_base_url if b.startswith("https://") and "localhost" not in b else None


def compose(inc: dict[str, Any]) -> str:
    d = inc.get("detection") or {}
    lines = []
    if inc["is_demo"]:
        lines.append("ℹ️ <i>Test scenario</i>")
    lines += [f"🔥 <b>VanRakshak fire incident {_e(inc['code'])}</b>", f"Status: <b>{_e(inc['status_label'])}</b>", f"Source: {_e(inc['source'])}" + (" (scenario)" if inc["is_demo"] else " satellite thermal hotspot, not a confirmed fire")]
    if inc.get("observed_at"):
        lines.append(f"Detected: {_e(inc['observed_at'])}")
    lines.append(f"Location: <code>{inc['lat']:.5f}, {inc['lon']:.5f}</code>")
    bits = [f"{k}: {d[k]}" for k in ("satellite", "instrument") if d.get(k)]
    if d.get("frp_mw") is not None:
        bits.append(f"FRP {d['frp_mw']} MW")
    if d.get("confidence") is not None:
        bits.append(f"confidence {round(d['confidence'] * 100)}%")
    if bits:
        lines.append("Source data: " + _e(", ".join(bits)))
    fc = inc.get("forecast") or {}
    if fc.get("horizons"):
        lines.append("\n<b>Predicted risk footprint (prototype model, NOT a fire perimeter)</b>" + (" [scenario weather]" if inc["is_demo"] else ""))
        for h in ("3", "6", "12"):
            v = fc["horizons"][h]
            lines.append(f"• {h} h: median potential area {v['area_ha']['p50']} ha (P10-P90 {v['area_ha']['p10']}-{v['area_ha']['p90']}), towards {int(v['spread_toward_deg'])}°")
        if fc.get("status") != "OK":
            lines.append(f"⚠ Forecast quality: {_e(fc.get('status'))}")
    elif fc.get("status") == "UNAVAILABLE":
        lines.append("\nForecast unavailable: " + _e(fc.get("reason", "")))
    imp = inc.get("impact") or {}
    if imp.get("air_quality", {}).get("status") == "ok":
        aq = imp["air_quality"]
        lines.append(f"\nAmbient air (MODELLED, not smoke from this fire): PM2.5 {aq['pm2_5_ug_m3']['now']} µg/m³")
    r = inc.get("route") or {}
    if r.get("status") == "OK":
        lines.append(f"\n<b>Nearest station by road:</b> {_e(r['station']['name'])} ({_e(r['station']['source'])}, unverified)\n{r['distance_km']} km, about {round(r['duration_min'])} min{(' (🚗 ' + str(r['drive_km']) + ' km by road + 🚶 ' + str(r['walk_km']) + ' km on foot)') if r.get('walk_km') else ''} (typical estimate; closures and availability not verified)")
    else:
        lines.append("\nRoute: not calculated" + (f" ({_e(r.get('reason'))})" if r.get("reason") else ""))
    link = public_link()
    if link:
        lines.append(f"\nOpen in VanRakshak: {link}/live")
    lines.append("\n<i>Telegram delivery does not mean a station has acknowledged or been dispatched.</i>")
    return "\n".join(lines)[:3900]


def keyboard(inc: dict[str, Any]) -> dict[str, Any]:
    row1 = [{"text": "🗺 Map", "url": f"https://www.openstreetmap.org/?mlat={inc['lat']}&mlon={inc['lon']}#map=14/{inc['lat']}/{inc['lon']}"}]
    if (inc.get("route") or {}).get("navigation_url"):
        row1.append({"text": "🚒 Route", "url": inc["route"]["navigation_url"]})
    rows = [row1]
    if settings.telegram_polling:
        rows.append([{"text": "✅ Acknowledge", "callback_data": f"fa:{inc['id']}"}, {"text": "📈 Forecast", "callback_data": f"ff:{inc['id']}"}, {"text": "⏳ Verifying", "callback_data": f"fv:{inc['id']}"}])
    return {"inline_keyboard": rows}


def report_pdf(inc: dict[str, Any]) -> bytes:
    fc, r, imp = inc.get("forecast") or {}, inc.get("route") or {}, inc.get("impact") or {}
    blocks: list[tuple[str, Any]] = [("Incident", {"id": inc["code"], "status": inc["status_label"], "scenario": "yes" if inc["is_demo"] else "no", "source": inc["source"], "location": f"{inc['lat']}, {inc['lon']}", "observed_at": inc["observed_at"]}), ("Detection (source data)", inc.get("detection") or {})]
    if fc.get("horizons"):
        blocks.append(("Predicted risk footprint (prototype, NOT a fire perimeter)", {f"{h} h": f"median potential area {v['area_ha']['p50']} ha (P10-P90 {v['area_ha']['p10']}-{v['area_ha']['p90']}); towards {int(v['spread_toward_deg'])} deg; head {v['head_distance_km']['p50']} km" for h, v in fc["horizons"].items()}))
        blocks.append(("Forecast assumptions and limitations", fc["assumptions"] + fc["limitations"]))
    if r.get("status") == "OK":
        blocks.append(("Response route (road network)", {"station": f"{r['station']['name']} ({r['station']['source']}, unverified)", "distance": f"{r['distance_km']} km", "typical drive time": f"{round(r['duration_min'])} min", "provider": r["provider"], "calculated_at": r["calculated_at"]}))
        blocks.append(("Route caveats", r["caveats"]))
    if imp:
        if imp.get("carbon"):
            c = imp["carbon"]
            blocks.append(("Carbon (CO2 only)", {"status": c["status"], "class": c.get("evidence_class"), "t CO2 low-central-high": c.get("tonnes_co2"), "inputs": c.get("inputs"), "uncertainty": c.get("uncertainty") or c.get("reason")}))
        if imp.get("air_quality", {}).get("status") == "ok":
            blocks.append(("Air quality (modelled ambient)", {"as_of": imp["air_quality"]["as_of"], "pm2_5": imp["air_quality"]["pm2_5_ug_m3"], "pm10": imp["air_quality"]["pm10_ug_m3"], "note": imp["air_quality"]["kind"]}))
    events = fireops.events(inc["id"], limit=200)
    blocks.append(("Event log", [f"{e['ts'][11:19]}  [{e['status']}] {e['service']}: {e['message']}" for e in events]))
    return generic_pdf(f"VanRakshak fire incident {inc['code']}", "Scenario incident" if inc["is_demo"] else "Satellite hotspot incident: decision support, not a confirmed fire", blocks)


def build_attachments(inc: dict[str, Any], horizon: str = "12") -> list[dict[str, Any]]:
    """Render the evidence files. A failed render is reported, never replaced by a stock image."""
    out = []
    for kind, label in (("incident", "Incident map"), ("route", "Route map"), ("forecast", f"{horizon} h forecast footprint map")):
        try:
            png, src = mapimage.render(kind, inc, horizon)
            out.append({"kind": kind, "label": label, "filename": f"{inc['code']}-{kind}.jpg", "bytes": png, "mime": "image/jpeg", "basemap": src})
        except ValueError as exc:
            out.append({"kind": kind, "label": label, "skipped": str(exc)})
        except Exception as exc:  # noqa: BLE001
            out.append({"kind": kind, "label": label, "error": f"{type(exc).__name__}: {exc}"[:200]})
    try:
        out.append({"kind": "report", "label": "Incident report (PDF)", "filename": f"{inc['code']}-report.pdf", "bytes": report_pdf(inc), "mime": "application/pdf"})
    except Exception as exc:  # noqa: BLE001
        out.append({"kind": "report", "label": "Incident report (PDF)", "error": str(exc)[:200]})
    return out


def send_incident(incident_id: str, mode: str, confirm_real: bool, actor: str, resend: bool = False, horizon: str = "12") -> dict[str, Any]:
    inc = fireops.get(incident_id)
    demo = inc["is_demo"]
    if inc["status"] not in ("DISPATCH_REQUESTED", "DISPATCHED", "MONITORING"):
        fireops.log_event(incident_id, "telegram_refused", "telegram", "skipped", f"Telegram not sent: {inc['code']} is {inc['status_label']}; dispatch must be authorised first", is_demo=demo)
        raise fireops.WorkflowError("Authorise dispatch first: notifications are only sent for incidents approved for response")
    sim = mode != "real"
    tag = "SIMULATED " if sim else ""
    notify = inc["notify"] or {"telegram": [], "history": []}
    prior = [m for m in notify.get("telegram", []) if m["kind"] == "message" and m["mode"] == ("simulated" if sim else "real") and m["status"] in ("SENT", "SIMULATED")]
    if prior and not resend:
        fireops.log_event(incident_id, "telegram_duplicate_suppressed", "telegram", "skipped", f"{tag}Telegram message already recorded for {inc['code']}; not sent again", is_demo=demo)
        return {"duplicate_suppressed": True, "notify": notify}
    if not sim:
        cfg = configured()
        if not (cfg["token"] and cfg["chat_id"]):
            fireops.log_event(incident_id, "telegram_not_configured", "telegram", "failed", "Telegram is not configured (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)", error="unconfigured", is_demo=demo)
            raise fireops.WorkflowError("Telegram is not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        if not confirm_real:
            raise fireops.WorkflowError("A real Telegram send needs explicit confirmation (confirm_real=true)")
    text = compose(inc)
    atts = build_attachments(inc, horizon)
    for a in atts:
        if a.get("skipped") or a.get("error"):
            fireops.log_event(incident_id, "attachment_unavailable", "mapimage", "skipped" if a.get("skipped") else "failed", f"{a['label']}: {a.get('skipped') or a.get('error')}", error=a.get("error"), is_demo=demo)
        else:
            fireops.log_event(incident_id, "attachment_built", "mapimage" if a["kind"] != "report" else "report", "ok", f"{a['label']} built ({len(a['bytes']) // 1024} KB" + (f", basemap {a['basemap']}" if a.get("basemap") else "") + ")", is_demo=demo)
    entries: list[dict[str, Any]] = []
    folder = OUT_DIR / inc["id"]
    folder.mkdir(parents=True, exist_ok=True)

    def record(kind: str, status_: str, **extra):
        e = {"id": f"{inc['id'][:8]}-{len(notify['telegram']) + len(entries) + 1}", "kind": kind, "mode": "simulated" if sim else "real", "status": status_, "at": datetime.now(timezone.utc).isoformat(), **extra}
        entries.append(e)
        return e

    if sim:
        record("message", "SIMULATED", preview=text)
        fireops.log_event(incident_id, "telegram_simulated", "telegram", "info", f"SIMULATED Telegram message composed for {inc['code']} (nothing was sent)", is_demo=demo)
        for a in atts:
            if "bytes" in a:
                (folder / a["filename"]).write_bytes(a["bytes"])
                record(a["kind"], "SIMULATED", filename=a["filename"], bytes=len(a["bytes"]))
                fireops.log_event(incident_id, "telegram_attachment_simulated", "telegram", "info", f"SIMULATED attachment: {a['label']} (saved locally, not sent)", is_demo=demo)
    else:
        chat = settings.telegram_chat_id
        fireops.log_event(incident_id, "telegram_request", "telegram", "pending", f"Telegram sendMessage requested for {inc['code']}", is_demo=demo)
        t0 = time.time()
        try:
            res = _call("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true", "reply_markup": __import__("json").dumps(keyboard(inc))})
            record("message", "SENT", message_id=res["message_id"], chat_masked=f"…{str(chat)[-3:]}")
            fireops.log_event(incident_id, "telegram_sent", "telegram", "ok", f"Telegram accepted message (message_id {res['message_id']}); delivery to a person is not confirmed", ref=str(res["message_id"]), is_demo=demo, details={"duration_s": round(time.time() - t0, 2)})
        except TelegramError as exc:
            record("message", "FAILED", error=str(exc))
            fireops.log_event(incident_id, "telegram_failed", "telegram", "failed", f"Telegram rejected the message: {exc}", error=str(exc), is_demo=demo)
        if entries[-1]["status"] == "SENT":
            for a in atts:
                if "bytes" not in a:
                    continue
                method, field = ("sendDocument", "document") if a["mime"] == "application/pdf" else ("sendPhoto", "photo")
                try:
                    res = _call(method, {"chat_id": chat, "caption": (("Scenario - " if demo else "") + a["label"] + f" - {inc['code']}")[:200]}, {field: (a["filename"], a["bytes"], a["mime"])})
                    record(a["kind"], "SENT", message_id=res["message_id"], filename=a["filename"])
                    fireops.log_event(incident_id, "telegram_attachment_sent", "telegram", "ok", f"Telegram accepted attachment: {a['label']} (message_id {res['message_id']})", ref=str(res["message_id"]), is_demo=demo)
                except TelegramError as exc:
                    record(a["kind"], "FAILED", error=str(exc), filename=a["filename"])
                    fireops.log_event(incident_id, "telegram_attachment_failed", "telegram", "failed", f"Attachment {a['label']} failed: {exc}", error=str(exc), is_demo=demo)
    notify["telegram"] = notify.get("telegram", []) + entries
    fireops.patch(incident_id, notify=notify)
    return {"duplicate_suppressed": False, "entries": entries, "notify": notify}


# ---------------------------------------------------------------- inbound (buttons)
def handle_update(update: dict[str, Any]) -> dict[str, Any]:
    """Process one Telegram update. Only authorised chats can change anything."""
    cb = update.get("callback_query")
    msg = update.get("message")
    chat_id = str(((cb or {}).get("message") or {}).get("chat", {}).get("id") or (msg or {}).get("chat", {}).get("id") or "")
    if chat_id not in authorised_chats():
        fireops.log_event(None, "telegram_unauthorised", "telegram", "failed", "Ignored a Telegram action from an unauthorised chat", error="unauthorised chat")
        return {"handled": False, "reason": "unauthorised chat"}
    who = ((cb or {}).get("from") or (msg or {}).get("from") or {})
    name = who.get("first_name") or "an authorised user"
    if cb:
        data = cb.get("data", "")
        kind, _, iid = data.partition(":")
        inc = fireops.get(iid)
        if not inc:
            _answer(cb, "Incident not found")
            return {"handled": False}
        demo = inc["is_demo"]
        if kind == "fa":
            notify = inc["notify"] or {"telegram": []}
            notify["acknowledged"] = {"by": name, "at": datetime.now(timezone.utc).isoformat(), "via": "telegram", "meaning": "An authorised Telegram user pressed Acknowledge. It records receipt of the alert, not station arrival."}
            fireops.patch(iid, notify=notify)
            fireops.log_event(iid, "acknowledged", "telegram", "ok", f"Alert acknowledged by {name} via Telegram (records receipt, not arrival)", is_demo=demo)
            if inc["status"] == "DISPATCH_REQUESTED":
                fireops.transition(iid, "DISPATCHED", "telegram", f"acknowledged by {name}")
            _answer(cb, "Acknowledgement recorded")
        elif kind == "ff":
            try:
                png, _ = mapimage.render("forecast", inc, "12")
                _call("sendPhoto", {"chat_id": chat_id, "caption": "12 h predicted risk footprint (not a fire perimeter)" + (" - scenario" if demo else "")}, {"photo": (f"{inc['code']}-forecast.jpg", png, "image/jpeg")})
                fireops.log_event(iid, "forecast_sent", "telegram", "ok", "Forecast image sent on request", is_demo=demo)
            except Exception as exc:  # noqa: BLE001
                fireops.log_event(iid, "forecast_send_failed", "telegram", "failed", f"Could not send forecast: {exc}", error=str(exc), is_demo=demo)
            _answer(cb, "Sending forecast")
        elif kind == "fv":
            ups = inc.get("field_updates") or []
            ups.append({"at": datetime.now(timezone.utc).isoformat(), "by": name, "via": "telegram", "text": "Verification pending (reported via Telegram)"})
            fireops.patch(iid, field_updates=ups)
            fireops.log_event(iid, "field_update", "telegram", "ok", f"{name} marked verification pending via Telegram", is_demo=demo)
            _answer(cb, "Noted: verification pending")
        return {"handled": True}
    return {"handled": False, "reason": "no supported action"}


def _answer(cb: dict[str, Any], text: str) -> None:
    try:
        _call("answerCallbackQuery", {"callback_query_id": cb["id"], "text": text})
    except TelegramError:
        pass


_poll = {"stop": False, "thread": None}


def start_polling() -> bool:
    """Opt-in only (TELEGRAM_POLLING=true). Competes with any other consumer of this bot's updates, so it is never automatic."""
    import threading

    if not (settings.telegram_polling and settings.telegram_bot_token) or _poll["thread"]:
        return False

    def loop():
        offset = 0
        while not _poll["stop"]:
            try:
                ups = _call("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": '["callback_query","message"]'})
                for u in ups:
                    offset = u["update_id"] + 1
                    handle_update(u)
            except TelegramError as exc:
                fireops.log_event(None, "telegram_poll_error", "telegram", "failed", f"Polling error: {exc}", error=str(exc))
                time.sleep(10)

    t = threading.Thread(target=loop, daemon=True, name="telegram-poll")
    _poll["thread"] = t
    t.start()
    return True
