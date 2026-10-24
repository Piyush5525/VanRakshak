# Limitations

* Change detection is a rule-based candidate generator with unvalidated thresholds; no trained model, no accuracy numbers. 20 m working resolution; patches of a few pixels are noise-prone.
* Clouds: persistent monsoon cloud can leave no clear observation; the system withholds results rather than filling gaps.
* WorldCover is a 2021 map; later clearing is only partially handled via the baseline-NBR check.
* Forecasting needs months of real detections and will report `INSUFFICIENT_DATA` until then; it uses only detection-history features.
* Impact assessment computes fragmentation against the **region polygon assumed to be forest** (not the forest mask). Runoff, biodiversity, protected-area and carbon assessments are not implemented and say `not_calculated`. No carbon figures are produced because no justified biomass model is available.
* Wildfire: FIRMS ingestion verified live, but burned-area mapping and fire-spread forecasting are not implemented, and no fire is linked automatically to forest cover or settlements.
* Authorization: manual ingest only; no PARIVESH/state integration; legal rules not encoded.
* Routing: Mapbox and SerpAPI verified live. Telegram: the bot token was verified read-only (`getMe`) but no message has been sent; Twilio voice is untested. Notification logic is covered by tests with in-test HTTP transports.
* OSM context: Overpass mirrors are intermittently slow; protected-area lookup timed out in testing and is reported as unavailable. Query results are capped (flagged when truncated).
* AR: the backend emits a georeferenced local-frame scene (`/forest/simulations/{id}/ar`). There is no AR client app, no terrain/elevation model and no glTF export yet; GPS/compass anchoring is metres-inaccurate.
* Energy: no trained building model; observed-building estimates need >=12 months of meter data; scenarios are arithmetic on user assumptions.
* Infrastructure: SQLite, in-process jobs, filesystem storage, single-process rate limiting; no PostGIS/Celery/Redis/object storage; HS256 JWT only.
* Historical playback returns real observation metadata and detected-change frames; it does not serve map tiles of each date.
* The tool supports review; it does not make legal determinations.

## Added with the RAKSHAK console
* The browser console was exercised with scripted Chrome (all pages, empty states, mobile width, live guidance with a simulated device position and a fake camera). It has not been tested on Safari, Firefox or real phones, and WebXR AR could not be tested at all.
* Terrain uses 30 m Copernicus DEM resampled to as few as 96 cells per side; tree markers are simulated; the scene shows 2021 tree cover plus detected loss, not current canopy.
* Environmental indicators: slope-only erosion screening, gridded model rainfall, OSM features (incomplete; protected-area lookup times out on busy mirrors). No hydrology, soil, species, biomass or watershed data.
* Industrial module: ambient modelled PM2.5 only; no emissions, discharge, noise or light data.
* Camera fire detection needs a user-supplied ONNX model; none is bundled. Only single frames are analysed (no live stream or video decoding).
* Projections extrapolate stored candidate detections from an unvalidated detector; with one interval no uncertainty range is possible.
* Hazard data is whatever users record; there is no automatic flood or closure feed. Rainfall advisories are screening flags only.
* Guided navigation depends on device GPS/compass quality and on provider road data; it is not certified for safety-critical use.
