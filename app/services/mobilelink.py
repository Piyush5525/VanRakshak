"""Short-lived signed links that open the mobile AR guidance page.

A link carries only: the incident id, the chosen route number, and an expiry time, signed with HMAC-SHA256. It carries no bearer token,
no role and no session secret, and it is useless after it expires. The phone uses the link to call the /mobile/ar/{token} endpoints, which
check the signature and the expiry on every request.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Any

from app.core.config import get_settings

DEFAULT_FILE = Path(__file__).resolve().parent.parent.parent / "data" / ".mobile_link_secret"


class MobileLinkError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code   # "invalid" | "expired"


def _secret() -> bytes:
    s = get_settings()
    for v in (s.mobile_link_secret, s.jwt_secret, s.callback_secret):
        if v and len(v) >= 16:
            return v.encode()
    f = DEFAULT_FILE
    if not f.is_file():   # one random secret per installation, kept on disk so links survive a restart
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    return f.read_text(encoding="utf-8").strip().encode()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make(incident_id: str, route_index: int | None = 0, ttl_s: int | None = None, now: float | None = None) -> tuple[str, int]:
    """Returns (token, expires_at_epoch)."""
    exp = int((now if now is not None else time.time()) + (ttl_s if ttl_s is not None else get_settings().mobile_link_minutes * 60))
    payload = _b64(json.dumps({"v": 1, "i": incident_id, "r": route_index, "e": exp}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}", exp


def verify(token: str, now: float | None = None) -> dict[str, Any]:
    try:
        payload, sig = token.split(".", 1)
        want = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, want):
            raise MobileLinkError("invalid", "This link is not valid.")
        data = json.loads(_unb64(payload))
        incident, exp = str(data["i"]), int(data["e"])
    except MobileLinkError:
        raise
    except Exception as exc:  # noqa: BLE001 - any malformed token is simply "invalid"
        raise MobileLinkError("invalid", "This link is not valid.") from exc
    if (now if now is not None else time.time()) >= exp:
        raise MobileLinkError("expired", "This link has expired. Ask for a new QR code.")
    return {"incident_id": incident, "route_index": data.get("r"), "expires_at": exp}


def lan_ip() -> str | None:
    """This computer's address on the local network (no traffic is sent: connecting a UDP socket only picks the outgoing interface)."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


def base_url(request_base: str) -> tuple[str, str | None]:
    """The address a phone should use, and a warning when that address only works on the computer itself."""
    s = get_settings()
    base = (s.mobile_public_base_url or request_base).rstrip("/")
    warn = None
    if not s.mobile_public_base_url and any(h in base for h in ("localhost", "127.0.0.1", "[::1]")):
        ip = lan_ip()   # the desktop page is on localhost: hand the phone this computer's address on the same Wi-Fi instead
        if ip:
            cert = Path(__file__).resolve().parent.parent.parent / "data" / "certs" / "lan.crt"
            if cert.is_file():   # scripts/run_phone_https.py has made a certificate: phones need HTTPS for the camera and location
                base = f"https://{ip}:8443"
            else:
                port = base.rsplit(":", 1)[1] if base.count(":") >= 2 else "8000"
                base = f"http://{ip}:{port}"
    if any(h in base for h in ("localhost", "127.0.0.1", "[::1]")):
        warn = "Your phone cannot open localhost. Use your computer's LAN address or an HTTPS tunnel (set MOBILE_PUBLIC_BASE_URL)."
    elif base.startswith("http://"):
        warn = "Browsers often block the camera and location on plain http addresses other than localhost. Use HTTPS (for example a tunnel) if the phone cannot start the camera."
    return base, warn
