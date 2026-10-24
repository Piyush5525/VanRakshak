from dataclasses import dataclass
from typing import Any

from app.core.config import Settings


@dataclass
class ProviderStatus:
    name: str
    configured: bool
    usable: bool
    reason: str


class SatelliteProvider:
    name = "planetary_computer"

    def __init__(self, settings: Settings):
        self.settings = settings

    def status(self) -> ProviderStatus:
        return ProviderStatus(self.name, True, True, "Sentinel-1 RTC, Sentinel-2 L2A and ESA WorldCover via public STAC; no credentials required, network needed")

    def request_observation(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("Satellite acquisition unavailable: configure a provider adapter")


class FIRMSProvider:
    name = "nasa_firms"

    def __init__(self, settings: Settings):
        self.settings = settings

    def status(self) -> ProviderStatus:
        configured = bool(self.settings.firms_api_key)
        return ProviderStatus(self.name, configured, configured, "API key configured" if configured else "FIRMS API key not configured")


class NotificationProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    def send_test(self, channel: str, recipient: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.settings.environment != "production" or channel == "test":
            return {"status": "captured", "safe_mode": True, "message": "Notification captured by development adapter"}
        if channel == "telegram" and not self.settings.telegram_bot_token:
            raise RuntimeError("Telegram is not configured")
        if channel == "voice" and self.settings.voice_provider == "disabled":
            raise RuntimeError("Voice provider is disabled")
        return {"status": "queued", "safe_mode": False, "message": "Provider adapter contract accepted; delivery must be verified"}


class RoutingProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    def route(self, origin: dict[str, Any], destination: dict[str, Any], alternatives: bool) -> dict[str, Any]:
        if self.settings.routing_provider == "unconfigured":
            return {"status": "unavailable", "reason": "No routing provider configured", "origin": origin, "destination": destination, "alternatives": []}
        return {"status": "provider_configured", "reason": "Routing adapter contract is ready", "origin": origin, "destination": destination, "alternatives": [] if not alternatives else [{"status": "not_fetched"}]}
