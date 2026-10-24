# Deployment (no containers)

Runs directly on Python 3.12.

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # edit values
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Migrations in `migrations/sqlite/*.sql` are applied automatically at start and recorded in `schema_migrations`. Data, models, rasters and reports live under `./data` (git-ignored).

## Satellite data
No Google Earth Engine account is needed. Sentinel-1 RTC, Sentinel-2 L2A and ESA WorldCover are read from the Microsoft Planetary Computer public STAC. Needs internet. A 4 km x 4 km area took about 3 minutes for two dates and both sensors (cold cache); repeats are cached under `RASTER_DIR`. The service is occasionally slow or returns 5xx (observed once while testing); acquisition retries and then fails the job with an explicit provider error.

## Optional integrations
| Need | Variable(s) | Cost / approval |
|---|---|---|
| NASA FIRMS active fire | `FIRMS_API_KEY` (free MAP_KEY) | free registration, rate limits |
| Routing | `ROUTING_PROVIDER=mapbox,serpapi` (also `osrm`), `MAPBOX_API`, `SERP_API`, `OSRM_URL` | Mapbox and SerpAPI are metered paid services |
| Telegram | `TELEGRAM_BOT_TOKEN`, `CALLBACK_SECRET`, public HTTPS webhook | free |
| Voice | `VOICE_PROVIDER=twilio`, `TWILIO_*`, `PUBLIC_BASE_URL` | paid |
| Real sends | `ENVIRONMENT=production`, `ALLOW_REAL_NOTIFICATIONS=true` | - |
| Auth | `AUTH_MODE=jwt`, `JWT_SECRET` (32+ chars) | - |
| Email | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | mail provider |
| Camera fire detection | `FIRE_MODEL_PATH`, `FIRE_MODEL_META` | an ONNX model whose licence you verified |
| Evidence uploads | `EVIDENCE_DIR`, `MAX_EVIDENCE_MB` | local disk |

## Production checklist
`ENVIRONMENT=production`, `AUTH_MODE=jwt`, TLS reverse proxy, backups of `data/`, a persistent job queue if you need jobs to survive restarts (jobs are an in-process thread pool: in-flight jobs are lost on restart), and migration to PostgreSQL/PostGIS if multi-user concurrency or spatial indexing is needed. **PostGIS, Celery/Redis and S3-style storage are not implemented.**

## Browser features that need HTTPS
Camera, compass and WebXR require a secure context. `localhost` works for development; for phones in the field put the app behind HTTPS (a reverse proxy with a certificate) and set `PUBLIC_BASE_URL`.

## Checks
```
python -m pytest -q
python -m compileall app tests
```
