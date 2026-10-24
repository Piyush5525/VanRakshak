from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "RAKSHAK"
    environment: str = "development"
    database_url: str = "sqlite:///./data/vananetra.db"
    api_prefix: str = "/api/v1"
    max_geometry_vertices: int = 20_000
    job_workers: int = 2
    firms_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    voice_provider: str = "disabled"
    routing_provider: str = "unconfigured"
    report_output_dir: str = "./data/reports"
    raster_dir: str = "./data/rasters"
    model_dir: str = "./data/models"
    auth_mode: str = "dev_header"  # dev_header | jwt
    jwt_secret: str | None = None
    jwt_audience: str = "vananetra"
    rate_limit_per_minute: int = 240
    log_level: str = "INFO"
    firms_base_url: str = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
    osrm_url: str | None = None
    mapbox_api: str | None = None
    fire_model_path: str | None = None
    fire_model_meta: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    forest_alert_email_to: str | None = None
    evidence_dir: str = "./data/evidence"
    max_evidence_mb: int = 10
    serp_api: str | None = None
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_number: str | None = None
    callback_secret: str | None = None
    grid_emission_factor_kg_per_kwh: float | None = None
    allow_real_notifications: bool = False
    telegram_polling: bool = False           # opt-in: receive button presses via getUpdates (do NOT enable if another app uses this bot)
    telegram_authorised_chat_ids: str = ""    # extra chat ids (comma list) allowed to press buttons; TELEGRAM_CHAT_ID is always allowed
    ivr_provider: str = "simulated"           # simulated | twilio | omnidimension
    omnidim_api_key: str | None = None
    omnidim_agent_id: int | None = None       # the voice agent that speaks the alert
    omnidim_from_number_id: int | None = None # optional: which of your OmniDimension numbers calls out
    omnidim_base_url: str = "https://backend.omnidim.io/api/v1"
    ivr_response_number: str | None = None    # E.164 number of the response contact
    ivr_response_label: str = "Response contact"
    ivr_approved_numbers: str = ""            # optional comma list; if set, the response number must be on it
    public_base_url: str = "http://localhost:8000"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def validate_runtime(self) -> None:
        if self.is_production and self.auth_mode != "jwt":
            raise RuntimeError("Production requires AUTH_MODE=jwt")
        if self.allow_real_notifications and not (self.callback_secret and len(self.callback_secret) >= 16):
            raise RuntimeError("ALLOW_REAL_NOTIFICATIONS requires CALLBACK_SECRET (16+ chars) to authenticate inbound callbacks")
        if self.auth_mode == "jwt" and not (self.jwt_secret and len(self.jwt_secret) >= 32):
            raise RuntimeError("AUTH_MODE=jwt requires JWT_SECRET of at least 32 characters")

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    @property
    def sqlite_path(self) -> str:
        value = self.database_url.replace("sqlite:///", "", 1)
        Path(value).parent.mkdir(parents=True, exist_ok=True)
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
