"""Escalate a person-reported incident: tell the response chat on Telegram and phone the response contact, with what is known.

The same two judgements are offered everywhere in the product: Escalate (send everything now) or False alarm (close it).
Nothing is claimed beyond what the providers say: 'sent' means the provider accepted it.
"""
import html as _html
from typing import Any

from app.core.config import get_settings
from app.db.database import audit, now, update
from app.services import firecomms, fireivr, geoinfo

settings = get_settings()
KINDS = {"cutting": "illegal tree cutting", "fire": "a forest fire", "encroachment": "encroachment on forest land", "pollution": "industrial pollution", "other": "an incident"}


def _where(inc: dict[str, Any]) -> tuple[float, float]:
    g = inc["geometry"]
    if g["type"] == "Point":
        return g["coordinates"][1], g["coordinates"][0]
    from shapely.geometry import shape
    c = shape(g).representative_point()
    return c.y, c.x


def texts(inc: dict[str, Any]) -> tuple[str, str]:
    lat, lon = _where(inc)
    kind = KINDS.get((inc.get("evidence") or {}).get("kind"), "an incident")
    pl = geoinfo.place(lat, lon)
    near = f"near {pl['name']}" if pl else "in a forest area"
    note = (inc.get("evidence") or {}).get("note")
    tg = "\n".join(filter(None, [f"<b>VanRakshak report: {_html.escape(inc['title'])}</b>", f"A person reported {kind} {_html.escape(near)}.", f"Location: {lat:.5f}, {lon:.5f}", f"Map: https://www.google.com/maps?q={lat},{lon}", f"Note: {_html.escape(note)}" if note else None, "This is a report from a person, not a verified finding. Please check it."]))
    say = " ".join([f"Hello. This is a VanRakshak alert. Please listen carefully. A report of {kind} has been made {near}.", f"The location is {abs(lat):.3f} degrees {'north' if lat >= 0 else 'south'}, {abs(lon):.3f} degrees {'east' if lon >= 0 else 'west'}.", "The report comes from a person and has not been verified. The map is in the VanRakshak app. Please check it and respond."])
    return tg, say


def escalate(inc: dict[str, Any], actor: str) -> dict[str, Any]:
    tg_text, say = texts(inc)
    lat, lon = _where(inc)
    out: dict[str, Any] = {"telegram": None, "call": None}
    cfg = firecomms.configured()
    if not (cfg["token"] and cfg["chat_id"]):
        out["telegram"] = {"status": "NOT_CONFIGURED", "error": "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing"}
    else:
        try:
            r = firecomms._call("sendMessage", {"chat_id": settings.telegram_chat_id, "text": tg_text, "parse_mode": "HTML", "disable_web_page_preview": "true"})
            try:
                firecomms._call("sendLocation", {"chat_id": settings.telegram_chat_id, "latitude": lat, "longitude": lon})
            except firecomms.TelegramError:
                pass
            out["telegram"] = {"status": "SENT", "message_id": r["message_id"]}
        except firecomms.TelegramError as exc:
            out["telegram"] = {"status": "FAILED", "error": str(exc)[:160]}
    st = fireivr.status()
    if not st["real_ready"]:
        out["call"] = {"status": "NOT_CONFIGURED", "error": "Phone calls are not set up: " + ", ".join(st["missing"])}
    else:
        res = fireivr.dial(say, {"reference": inc["id"][:8], "latitude": lat, "longitude": lon, "kind": "reported incident"})
        out["call"] = {"status": "INITIATED", "call_id": res["call_id"], "provider": res["provider"]} if res["ok"] else {"status": "FAILED", "error": res["error"]}
    ok = any(v and v["status"] in ("SENT", "INITIATED") for v in out.values())
    ev = dict(inc.get("evidence") or {})
    ev["escalation"] = {"at": now(), "by": actor, **{k: v for k, v in out.items()}}
    update("incidents", "id", inc["id"], {"evidence": ev, **({"status": "DISPATCHED"} if ok else {}), "updated_at": now()})
    audit(actor, "incident_escalated", "incident", inc["id"], {"telegram": out["telegram"]["status"], "call": out["call"]["status"]})
    out["script"] = say
    return out
