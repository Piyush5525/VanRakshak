# VanRakshak

Forest intelligence and response platform for India. It watches forests with **Sentinel-1 (radar)** and **Sentinel-2 (optical)** satellites, shows where tree cover is disappearing, tracks wildfires from NASA hotspots, and helps a control room check, route and alert the right people.

> Decision support only. VanRakshak never decides that something is illegal. A detection is a lead; a missing permit record means "verification required".
> The repository and database use the code name **VanaNetra**.

## Start here

```powershell
cd "E:\Project 2\VanRakshak"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env        # then add the keys you have (see below)
uvicorn app.main:app --port 8000
```

Open <http://localhost:8000> to go directly to the live map. The integrated **Replanting intelligence** workspace is at `/replanting/`. The full console is at `/app`, API docs at `/docs`.

The replanting workspace is bundled under `frontend/replanting/` with its scoring engine,
species data and assets. It is served by VanRakshak and does not depend on the separate
`replantio` folder being present.

## The live map

The map starts clear. Nothing is drawn until you switch a layer on or choose an incident.

| Where | What it does |
|---|---|
| **Left: Map layers** | Switch on **NASA fire hotspots** (dots), **Heat zones** (where hotspots cluster) or **Tree cover history**. Satellite, Map and Terrain base views at the top. |
| **🌲 Trees** | Pick 7 / 30 / 90 days or a custom range. **Check a new area**: draw a rectangle or any shape anywhere in India (up to 60 km² at 30 m, up to 240 km² at 60 m). Tree cover is shown as polygons that shrink over 90 → 30 → 7 days → today. Click a loss spot to review it, check permits, find the forest office and road route, and send an alert. |
| **🔥 Fire** | Fire incidents from NASA hotspots (click a hotspot, then Investigate) or the five example incidents. Choosing one shows its 3 / 6 / 12 hour spread footprint and the route from the nearest station. Then send a Telegram alert or place a phone call. |
| **📍 Reports** | **Check a factory** (place it, fill in details or upload its PDF, see the effect on forest, air, water and people today and after 7 / 30 / 90 days), incidents you mark by hand, and worked **case studies**. |

While something is running, a small progress card shows each step (for example placing the call → ringing → answered → finished). Each incident keeps a short record of major events only.

Details: [docs/FOREST_CHANGE.md](docs/FOREST_CHANGE.md) and [docs/FIRE_RESPONSE.md](docs/FIRE_RESPONSE.md).

## Keys (`.env`)

Never commit `.env` (it is in `.gitignore`).

| Variable | Purpose |
|---|---|
| none needed | Sentinel-1/2, WorldCover, elevation: Microsoft Planetary Computer is public |
| `FIRMS_API_KEY` | NASA active-fire hotspots (free MAP_KEY) |
| `MAPBOX_API` | Road routing and the satellite base map. Use a public `pk.` token (it is sent to the browser) |
| `SERP_API` | Forest-office and fire-station lookup, routing fallback |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram alerts |
| `IVR_PROVIDER=omnidimension`, `OMNIDIM_API_KEY`, `OMNIDIM_AGENT_ID`, `IVR_RESPONSE_NUMBER` | Phone calls through OmniDimension. The agent must be set up to read the `message` it is given |
| `SMTP_*`, `FOREST_ALERT_EMAIL_TO` | E-mail alerts (optional) |

Restart the server after editing `.env`. Real messages and calls are only sent after an explicit confirmation in the interface.

## What is verified, and what is not

| Area | Status |
|---|---|
| Sentinel-2 + Sentinel-1 acquisition, cloud masking, forest mask, 90-day change detection with loss dates and uncertainty | Live-verified on a real forest block |
| Monthly alert engine (CL-01 Phase 2: two-observation confirmation, seasonal baseline, optical + radar fusion) | Implemented and tested; accuracy not validated against ground truth (see [docs/MONTHLY_ALERTS.md](docs/MONTHLY_ALERTS.md)) |
| NASA FIRMS hotspots, Open-Meteo weather, Mapbox and SerpAPI routing | Live-verified |
| Fire spread forecast (3 / 6 / 12 h) | Prototype model, not validated for Indian forests; shows a risk footprint, not a fire perimeter |
| Telegram alerts | Built and gated; use a real send only when you intend to |
| Phone calls (OmniDimension / Twilio) | Built against the providers' documented APIs; check with your own account |
| Permit check | Compares against records loaded in the system; "no record found" does not mean illegal |
| Case studies | Illustrative worked examples, not records of real companies |

Not done: PostGIS, Celery/Redis, trained deep-learning models, PARIVESH integration, biodiversity data, native AR app. See [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## Tests

```powershell
python -m pytest -q             # unit, API, safety and browser tests
python -m pytest tests/e2e -q   # browser tests only (needs Playwright and Chrome)
```

Tests use a temporary database and blank every provider key, so they cannot spend quota or send messages.

## Docs

[Forest change](docs/FOREST_CHANGE.md) · [Fire response](docs/FIRE_RESPONSE.md) · [Factory check](docs/FACTORY_CHECK.md) · [Monthly alerts](docs/MONTHLY_ALERTS.md) · [Architecture](docs/ARCHITECTURE.md) · [API reference](docs/API_REFERENCE.md) · [AR](docs/AR.md) · [Data sources](docs/DATA_SOURCES.md) · [Security](docs/SECURITY.md) · [Deployment](docs/DEPLOYMENT.md) · [Limitations](docs/LIMITATIONS.md)
