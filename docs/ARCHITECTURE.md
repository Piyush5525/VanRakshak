# Architecture

Single FastAPI process, SQLite persistence, in-process job pool, and a dependency-free browser console (ES modules, Leaflet, Chart.js, three.js) served by the same process at `/app` (landing page at `/`). Modules communicate through shared IDs and JSON records; each claim carries a `claim_type` (`observation`, `candidate_inference`, `forecast_estimate`, `hypothetical_assumption`, `inferred_consequence`, `human_decision`).

```
frontend (static, Leaflet)  ->  app/main.py (routes, guard)  ->  app/ops_routes.py (stations, routing, dispatch, webhooks)
                                      |
   services/satellite.py      Planetary Computer STAC: Sentinel-2, Sentinel-1, WorldCover composites
   services/acquisition.py    cache + provenance (observations, scenes tables)
   services/changedetect.py   dNBR candidates + Sentinel-1 support, polygons, areas
   services/analysis.py       job entry points, candidate-incident rule, simulation, impact, authorization match
   services/geoanalysis.py    fragmentation, authorization matching states
   services/forecasting.py    risk model, time-split training, registry, evaluation
   services/wildfire.py       FIRMS active-fire ingest (separate from burned-area)
   services/energy.py         meter-history profile; user-assumption scenarios
   services/routing.py        Mapbox / SerpAPI / OSRM providers, nearest station, patrol ordering
   services/notify.py         Telegram/Twilio, idempotency, status ladder, signature checks
   services/reports.py        JSON evidence package + PDF with before/after imagery
   domain/state_machine.py    incident transitions
   db/database.py             SQLite helpers + versioned migrations (migrations/sqlite)
   services/stacks.py         per-scene Sentinel-2 NBR / Sentinel-1 VH stacks, cached by month
   services/alerts.py         monthly alert engine: confirmation, loss dating, fusion, patches, ranking
   services/alert_eval.py     GLAD-L reference loading and scoring (P@K, date error, radar gain, Phase 1 F1 check)
   services/terrain.py        Copernicus DEM elevation grid, slope statistics, 3D mesh payload
   services/weather.py        Open-Meteo rainfall and climatology
   services/environment.py    environmental assessment (measured / computed / screening / not_available)
   services/projection.py     historical-rate projection + hypothetical scenarios
   services/navigation.py     hazard-aware route evaluation, session progress, next-step logic
   services/industrial.py     site registry + screening assessment
   services/firevision.py     ONNX fire/smoke inference interface, temporal filter, event correlation
   services/forestscene.py    3D/AR scene payload (DEM + WorldCover + detection timeline)
   services/osm.py            OpenStreetMap (Overpass) context
   services/leads.py          SerpAPI web leads (never treated as records)
   services/previews.py       WGS84 PNG previews of stored composites for map overlays
   services/datasources.py    live source-health checks
   rakshak_routes.py          routes for the modules above; ops_routes.py for stations/routing/dispatch/webhooks
   core/                      config, security (JWT), middleware (correlation id, rate limit, logs), geometry
   frontend/                  index.html shell, js/main.js router, js/pages/*.js (14 pages), js/core.js, js/map.js, js/device.js
```

## Module map against the product brief
1 acquisition: done (S1, S2, WorldCover). 2 change detection: rule-based baseline. 3 forecasting: trainable spatial risk model (data-gated) plus historical-rate projection with hypothetical scenarios. 4 wildfire: FIRMS ingestion (live-verified), camera detection interface (no model bundled), correlation. 5 impact: DEM slope, rainfall, OSM context, fragmentation; biodiversity, carbon, watershed are `not_available`. 6 authorization: manual ingest, matching, SerpAPI leads. 7 energy: meter-history/scenario. 8 routing: Mapbox (primary), SerpAPI (fallback), optional OSRM, hazard evaluation, guided navigation sessions. 9 notifications: Telegram, email, Twilio adapters. 10 lifecycle/audit: done. 11 playback/simulation: frames from real observations; user-defined simulation. 12 reports/evaluations: done.

## Deliberate deviations from the brief
* **SQLite, not PostGIS**; no spatial index (spatial work is done in Python/shapely). Fine for a pilot; not for large catalogues.
* **In-process jobs, not Celery/Redis**; jobs are lost if the process restarts.
* **Filesystem storage**, not object storage; the storage path is fixed by `RASTER_DIR`/`REPORT_OUTPUT_DIR`.
* **Planetary Computer instead of Google Earth Engine** (also used for the DEM and WorldCover), because no Earth Engine credentials are available.
* No containers, by request.
