"""Notification delivery: Telegram and Twilio voice, behind safety gates.

Status ladder (never skipped, never inferred from a successful HTTP call):
  CAPTURED_NOT_SENT  - safe mode / test channel, nothing left the system
  SENT_TO_PROVIDER   - provider accepted the request (NOT proof of delivery or reading)
  DELIVERED          - provider-confirmed delivery (voice status webhook)
  ACKNOWLEDGED       - an authenticated human pressed acknowledge
  FAILED             - provider rejected / unreachable
"""
import base64
import hashlib
import hmac
import sqlite3
import uuid
from typing import Any

import httpx

from app.core.config import Settings
from app.db.database import audit, get_one, insert, now, update


class NotificationBlocked(RuntimeError):
    """A safety gate refused a real send."""


def idempotency_key(incident_id: str, channel: str, recipient: str, event: str) -> str:
    return hashlib.sha256(f"{incident_id}|{channel}|{recipient}|{event}".encode()).hexdigest()


def real_sends_enabled(settings: Settings) -> bool:
    return settings.is_production and settings.allow_real_notifications


def build_message(incident: dict[str, Any], change: dict[str, Any] | None) -> str:
    coords = incident["geometry"]
    if coords["type"] == "Point":
        point = coords["coordinates"]
    else:
        from app.core.geometry import to_shape

        point = list(to_shape(coords).representative_point().coords[0])
    lines = [f"VanaNetra incident {incident['id'][:8]} - {incident['title']}", f"Status: {incident['status']} (human-reviewed; field verification required)"]
    if point:
        lines.append(f"Location: {point[1]:.5f}, {point[0]:.5f}  https://www.openstreetmap.org/?mlat={point[1]}&mlon={point[0]}#map=16/{point[1]}/{point[0]}")
    if change:
        lines.append(f"Satellite-detected candidate change: {change['area_hectares']} ha between {change['baseline_date'][:10]} and {change['comparison_date'][:10]} (model-generated candidate, not a confirmed finding).")
    lines.append("This message does not state that any activity is unlawful.")
    return "\n".join(lines)


class Notifier:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=20)

    # -------------------------------------------------------------- core
    def send(self, *, channel: str, recipient: str, text: str, incident_id: str | None, event: str, actor: str, approved: bool) -> dict[str, Any]:
        """Create (or return the existing) notification. `approved` must be True only for reviewed, assigned incidents."""
        key = idempotency_key(incident_id or "none", channel, recipient, event)
        with_existing = self._by_key(key)
        if with_existing:
            return {**with_existing, "duplicate_suppressed": True}
        nid = str(uuid.uuid4())
        status, response = "CAPTURED_NOT_SENT", {"safe_mode": True, "message": "Captured by development adapter; nothing was sent"}
        if channel != "test" and real_sends_enabled(self.settings):
            if not approved:
                raise NotificationBlocked("Incident has not passed the review/assignment workflow")
            status, response = self._deliver(channel, recipient, text, nid)
        elif channel != "test":
            response = {"safe_mode": True, "message": "Real sends are disabled (requires ENVIRONMENT=production and ALLOW_REAL_NOTIFICATIONS=true); captured only"}
        record = {"id": nid, "channel": channel, "recipient": recipient, "status": status, "payload": {"incident_id": incident_id, "event": event, "text": text}, "provider_response": response, "created_at": now(), "idempotency_key": key}
        try:
            insert("notifications", record)
        except sqlite3.IntegrityError:  # concurrent duplicate
            return {**self._by_key(key), "duplicate_suppressed": True}
        audit(actor, "notification_created", "notification", nid, {"channel": channel, "status": status, "incident_id": incident_id})
        return {**record, "duplicate_suppressed": False}

    def _by_key(self, key: str):
        from app.db.database import connection, decode_row

        with connection() as conn:
            row = conn.execute("SELECT * FROM notifications WHERE idempotency_key = ?", (key,)).fetchone()
        return decode_row(dict(row)) if row else None

    # -------------------------------------------------------------- providers
    def _deliver(self, channel: str, recipient: str, text: str, nid: str) -> tuple[str, dict[str, Any]]:
        try:
            if channel == "telegram":
                return self._telegram(recipient, text, nid)
            if channel == "voice":
                return self._voice(recipient, text, nid)
            if channel == "email":
                return self._email(recipient, text)
        except (httpx.HTTPError, OSError) as exc:
            return "FAILED", {"error": type(exc).__name__}
        raise NotificationBlocked(f"Unknown channel {channel}")

    def _telegram(self, chat_id: str, text: str, nid: str):
        if not self.settings.telegram_bot_token:
            raise NotificationBlocked("Telegram is not configured")
        r = self.client.post(f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage", json={"chat_id": chat_id, "text": text[:3900], "disable_web_page_preview": True, "reply_markup": {"inline_keyboard": [[{"text": "Acknowledge", "callback_data": f"ack:{nid}"}]]}})
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and body.get("ok"):
            return "SENT_TO_PROVIDER", {"telegram_message_id": body["result"]["message_id"], "note": "Accepted by Telegram; delivery/reading not confirmed"}
        return "FAILED", {"http_status": r.status_code, "description": body.get("description")}

    def _email(self, to: str, text: str):
        import re
        import smtplib
        from email.message import EmailMessage

        s = self.settings
        if not (s.smtp_host and s.smtp_from):
            raise NotificationBlocked("Email is not configured (SMTP_HOST, SMTP_FROM)")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to):
            raise NotificationBlocked("Recipient is not a valid email address")
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = s.smtp_from, to, text.splitlines()[0][:120]
        msg.set_content(text)
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
            smtp.starttls()
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password or "")
            smtp.send_message(msg)
        return "SENT_TO_PROVIDER", {"note": "Accepted by the SMTP server; delivery and reading are not confirmed"}

    def _voice(self, number: str, text: str, nid: str):
        s = self.settings
        if s.voice_provider != "twilio" or not (s.twilio_account_sid and s.twilio_auth_token and s.twilio_from_number):
            raise NotificationBlocked("Voice provider is not configured")
        spoken = text.replace("\n", ". ")[:600].replace("<", " ").replace("&", " and ")
        twiml = f"<Response><Gather numDigits=\"1\" action=\"{s.public_base_url}/api/v1/notifications/voice/gather?nid={nid}\"><Say>{spoken}. Press 1 to acknowledge.</Say></Gather></Response>"
        r = self.client.post(f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Calls.json", auth=(s.twilio_account_sid, s.twilio_auth_token), data={"To": number, "From": s.twilio_from_number, "Twiml": twiml, "StatusCallback": f"{s.public_base_url}/api/v1/notifications/voice/status?nid={nid}"})
        if r.status_code in (200, 201):
            return "SENT_TO_PROVIDER", {"call_sid": r.json().get("sid"), "note": "Call requested; answer/delivery not confirmed"}
        return "FAILED", {"http_status": r.status_code}

    # -------------------------------------------------------------- inbound
    def acknowledge(self, nid: str, by: str) -> dict[str, Any] | None:
        rec = get_one("notifications", "id", nid)
        if not rec or rec["status"] in ("CAPTURED_NOT_SENT", "FAILED"):
            return None
        update("notifications", "id", nid, {"status": "ACKNOWLEDGED", "provider_response": {**(rec["provider_response"] or {}), "acknowledged_by": by, "acknowledged_at": now()}})
        audit(f"recipient:{by}", "notification_acknowledged", "notification", nid, {})
        return get_one("notifications", "id", nid)


def verify_twilio_signature(auth_token: str, url: str, params: dict[str, str], signature: str) -> bool:
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    expected = base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(expected, signature or "")


def verify_shared_secret(expected: str | None, supplied: str | None) -> bool:
    return bool(expected) and hmac.compare_digest(expected, supplied or "")
