# API reference

Generated from the live OpenAPI schema (`/openapi.json`; interactive docs at `/docs`). All routes are under `/api/v1` unless shown. Identity: `Authorization: Bearer <jwt>` (`AUTH_MODE=jwt`) or, in development only, `X-Role`. Roles: admin, reviewer, officer (forest officer / ranger), analyst, read_only. Errors use `{"detail": ...}`: validation 422, role 403, invalid transition or unmet precondition 409, provider unavailable 503/502, rate limit 429.

Long-running work returns `{job_id}`; poll `GET /jobs/{job_id}`.

| Method | Path | Summary |
|---|---|---|
| POST | `/api/v1/alerts/runs` | Start Alert Run |
| GET | `/api/v1/alerts/runs` | List Alert Runs |
| GET | `/api/v1/alerts/runs/{run_id}` | Get Alert Run |
| GET | `/api/v1/alerts/runs/{run_id}/alerts` | Run Alerts |
| POST | `/api/v1/alerts/runs/{run_id}/evaluate` | Evaluate Alert Run |
| GET | `/api/v1/alerts/runs/{run_id}/evaluations` | Run Evaluations |
| GET | `/api/v1/alerts/runs/{run_id}/export.csv` | Export Csv |
| GET | `/api/v1/alerts/runs/{run_id}/export.geojson` | Export Geojson |
| GET | `/api/v1/alerts/runs/{run_id}/top` | Run Top |
| GET | `/api/v1/ar/forest-scenes/{region_id}` | Forest Scene |
| GET | `/api/v1/audit/events` | Audit Events |
| POST | `/api/v1/authorizations/ingest` | Ingest Authorization |
| POST | `/api/v1/authorizations/match-assessment` | Authorization Match |
| GET | `/api/v1/authorizations/match-assessment/{assessment_id}` | Get Authorization Assessment |
| GET | `/api/v1/authorizations/search` | Search Authorizations |
| POST | `/api/v1/authorizations/web-leads` | Authorization Web Leads |
| POST | `/api/v1/buildings/energy-predictions` | Create Energy Prediction |
| GET | `/api/v1/buildings/energy-predictions/{prediction_id}` | Get Energy Prediction |
| POST | `/api/v1/buildings/scenarios` | Create Building Scenario |
| GET | `/api/v1/buildings/scenarios/{scenario_id}` | Get Building Scenario |
| GET | `/api/v1/dashboard/summary` | Dashboard Summary |
| GET | `/api/v1/data-sources` | Data Sources |
| POST | `/api/v1/environment/assessments` | Create Environment Assessment |
| GET | `/api/v1/environment/assessments` | List Environment Assessments |
| GET | `/api/v1/environment/assessments/{assessment_id}` | Get Environment Assessment |
| GET | `/api/v1/environment/assessments/{assessment_id}/report.csv` | Environment Csv |
| GET | `/api/v1/environment/assessments/{assessment_id}/report.pdf` | Environment Pdf |
| GET | `/api/v1/evaluations` | Evaluations |
| GET | `/api/v1/evaluations/{evaluation_id}` | Evaluation |
| GET | `/api/v1/evidence/{evidence_id}/download` | Download Evidence |
| POST | `/api/v1/forest/change-detection` | Request Change Detection |
| GET | `/api/v1/forest/change-detection/{run_id}` | Get Change Run |
| GET | `/api/v1/forest/changes` | List Changes |
| GET | `/api/v1/forest/changes/{change_id}` | Get Change |
| GET | `/api/v1/forest/changes/{change_id}/evidence` | Change Evidence |
| POST | `/api/v1/forest/forecasts` | Request Forecast |
| POST | `/api/v1/forest/forecasts/train` | Train Forecast Model |
| GET | `/api/v1/forest/forecasts/{forecast_id}` | Get Forecast |
| GET | `/api/v1/forest/forecasts/{forecast_id}/layers` | Forecast Layers |
| GET | `/api/v1/forest/history` | Forest History |
| POST | `/api/v1/forest/history/build` | Build History |
| GET | `/api/v1/forest/history/{region_id}/frames` | History Frames |
| POST | `/api/v1/forest/projections` | Create Projection |
| GET | `/api/v1/forest/projections` | List Projections |
| GET | `/api/v1/forest/projections/{projection_id}/report.pdf` | Projection Pdf |
| POST | `/api/v1/forest/simulations` | Create Simulation |
| GET | `/api/v1/forest/simulations/{simulation_id}` | Get Simulation |
| GET | `/api/v1/forest/simulations/{simulation_id}/ar` | Simulation Ar Scene |
| GET | `/api/v1/incidents` | List Incidents |
| POST | `/api/v1/incidents` | Create Incident |
| GET | `/api/v1/incidents/{incident_id}` | Incident |
| POST | `/api/v1/incidents/{incident_id}/assign` | Assign Incident |
| GET | `/api/v1/incidents/{incident_id}/case` | Case Detail |
| POST | `/api/v1/incidents/{incident_id}/close` | Close Incident |
| POST | `/api/v1/incidents/{incident_id}/dispatch` | Dispatch Incident |
| POST | `/api/v1/incidents/{incident_id}/evidence` | Upload Evidence |
| GET | `/api/v1/incidents/{incident_id}/evidence` | List Evidence |
| POST | `/api/v1/incidents/{incident_id}/field-verification` | Field Verification |
| POST | `/api/v1/incidents/{incident_id}/impact-assessment` | Create Impact Assessment |
| GET | `/api/v1/incidents/{incident_id}/impact-assessment` | Get Impact Assessment |
| POST | `/api/v1/incidents/{incident_id}/reports` | Create Report |
| POST | `/api/v1/incidents/{incident_id}/review` | Review Incident |
| POST | `/api/v1/industrial/sites` | Create Site |
| GET | `/api/v1/industrial/sites` | List Sites |
| POST | `/api/v1/industrial/sites/from-change/{change_id}` | Site From Change |
| POST | `/api/v1/industrial/sites/{site_id}/assess` | Assess Site |
| GET | `/api/v1/industrial/sites/{site_id}/report.pdf` | Site Pdf |
| GET | `/api/v1/jobs/{job_id}` | Get Job |
| POST | `/api/v1/navigation/hazards` | Create Hazard |
| GET | `/api/v1/navigation/hazards` | List Hazards |
| POST | `/api/v1/navigation/hazards/{hazard_id}/resolve` | Resolve Hazard |
| POST | `/api/v1/navigation/hazards/{hazard_id}/verify` | Verify Hazard |
| POST | `/api/v1/navigation/routes` | Plan Route |
| POST | `/api/v1/navigation/sessions` | Start Session |
| GET | `/api/v1/navigation/sessions/{session_id}` | Get Session |
| POST | `/api/v1/navigation/sessions/{session_id}/end` | End Session |
| POST | `/api/v1/navigation/sessions/{session_id}/position` | Update Position |
| POST | `/api/v1/navigation/sessions/{session_id}/reroute` | Reroute |
| POST | `/api/v1/notifications/telegram/callback` | Telegram Callback |
| POST | `/api/v1/notifications/telegram/test` | Telegram Test |
| POST | `/api/v1/notifications/voice/gather` | Voice Gather |
| POST | `/api/v1/notifications/voice/status` | Voice Status |
| POST | `/api/v1/notifications/voice/test` | Voice Test |
| GET | `/api/v1/notifications/{notification_id}` | Get Notification |
| GET | `/api/v1/observations/{observation_id}/preview-meta` | Preview Meta |
| GET | `/api/v1/observations/{observation_id}/preview.png` | Preview Png |
| POST | `/api/v1/regions` | Create Region |
| GET | `/api/v1/regions` | List Regions |
| GET | `/api/v1/regions/{region_id}` | Get Region |
| PATCH | `/api/v1/regions/{region_id}` | Patch Region |
| POST | `/api/v1/regions/{region_id}/observations` | Create Observation |
| GET | `/api/v1/regions/{region_id}/observations` | List Observations |
| GET | `/api/v1/regions/{region_id}/observations-ok` | Region Observations |
| GET | `/api/v1/reports` | List Reports |
| GET | `/api/v1/reports/{report_id}` | Get Report |
| GET | `/api/v1/reports/{report_id}/download` | Download Report |
| POST | `/api/v1/routes/inspection` | Inspection Route |
| POST | `/api/v1/routes/patrol` | Patrol Route |
| POST | `/api/v1/stations` | Create Station |
| GET | `/api/v1/stations` | List Stations |
| GET | `/api/v1/system/capabilities` | Capabilities |
| GET | `/api/v1/system/client-config` | Client Config |
| GET | `/api/v1/system/models` | Models |
| GET | `/api/v1/system/providers` | Providers |
| GET | `/api/v1/wildfires` | List Wildfires |
| GET | `/api/v1/wildfires/correlations` | Fire Correlations |
| POST | `/api/v1/wildfires/ingest` | Ingest Wildfire |
| POST | `/api/v1/wildfires/ingest-firms` | Ingest Firms |
| POST | `/api/v1/wildfires/vision/analyze` | Vision Analyze |
| GET | `/api/v1/wildfires/vision/status` | Vision Status |
| GET | `/api/v1/wildfires/{wildfire_id}` | Get Wildfire |
| POST | `/api/v1/wildfires/{wildfire_id}/create-incident` | Fire Incident |
| POST | `/api/v1/wildfires/{wildfire_id}/review` | Review Wildfire |
| GET | `/health` | Health |
| GET | `/ready` | Ready |
