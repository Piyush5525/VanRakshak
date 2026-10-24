"""Tests run against a throwaway database and directories, never the working data/ folder."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="vananetra-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["REPORT_OUTPUT_DIR"] = f"{_tmp}/reports"
os.environ["RASTER_DIR"] = f"{_tmp}/rasters"
os.environ["MODEL_DIR"] = f"{_tmp}/models"
os.environ["ENVIRONMENT"] = "test"
os.environ["ENABLE_EXAMPLES"] = "true"   # examples are synthetic: only tests may use them
os.environ["AUTH_MODE"] = "dev_header"
os.environ["RATE_LIMIT_PER_MINUTE"] = "0"
os.environ["ALLOW_REAL_NOTIFICATIONS"] = "false"
# Real provider keys live in .env for the running app; tests must never be able to use them.
for _k in ("MAPBOX_API", "SERP_API", "FIRMS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OSRM_URL", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "CALLBACK_SECRET", "OMNIDIM_API_KEY", "OMNIDIM_AGENT_ID", "OMNIDIM_FROM_NUMBER_ID", "IVR_RESPONSE_NUMBER", "ALERT_PHONE_NUMBER"):
    os.environ[_k] = ""
os.environ["ROUTING_PROVIDER"] = "unconfigured"
os.environ["VOICE_PROVIDER"] = "disabled"
# Tests must not hit the public Overpass service.
import app.services.osm as _osm
_osm.OVERPASS_URLS = ()
# Place names come from Mapbox/Nominatim; tests must not call them.
import app.services.geoinfo as _geo
_geo.place = lambda lat, lon: None
