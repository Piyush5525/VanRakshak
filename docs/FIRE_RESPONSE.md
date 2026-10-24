# Fire detection and response workflow

**Live satellite hotspot → fire incident → verification → spread forecast → impact → nearest station and road route → authorised Telegram alert → IVR call → live event log → resolution.** One incident record (`fire_incidents`) drives every panel; every operation writes to one event log (`fire_events`).

Open the live map (`/live`), switch the right panel to **🔥 Fire response**, or click a hotspot and press **Investigate as fire incident**.

## What is genuinely connected

| Piece | Status | Notes |
|---|---|---|
| NASA FIRMS hotspots (VIIRS/MODIS) | **Live-verified** | Real feed, India outline filter, 10-minute cache. Each hotspot shows satellite, time, age and a CURRENT (≤3 h) / DELAYED (≤24 h) / HISTORICAL label. Refreshes and failures are logged. |
| Incident from a hotspot | **Works** | The server only accepts a hotspot that is in its own current FIRMS cache, so a "live" incident cannot be fabricated. Same detection again returns the same incident (no duplicate). Manual reports are possible and labelled. |
| Verification workflow | **Works** | `DETECTED → NEEDS_VERIFICATION → CONFIRMED → DISPATCH_REQUESTED → DISPATCHED → MONITORING → RESOLVED`, plus `REJECTED / CANCELLED / FAILED / INCONCLUSIVE`. Invalid jumps are refused. Reviewers verify; officers/admins authorise. |
| Fire-spread forecast (3/6/12 h) | **Works, prototype, not validated** | McArthur Mk5 rate of spread + Anderson ellipse + Monte-Carlo (300 runs), with real Open-Meteo weather, Copernicus DEM slope and WorldCover (water / built-up / bare cells cannot burn). Output is a probability footprint ("likely ≥50% of runs", "possible ≥10%"), reproducible from a stored seed. Missing weather → `UNAVAILABLE`; missing DEM/land cover → `DEGRADED`. |
| Impact panel | **Works** | Potential exposure area (never called burned area), nearby settlements from OSM (inside footprint / downwind), modelled ambient PM2.5/PM10 (CAMS), carbon only from operator-supplied burned area and fuel load (CO₂ only, with a range). Demo mode uses labelled illustrative values. No monitoring-station feed is configured. |
| Stations and routing | **Live-verified** | Stations from OpenStreetMap and SerpAPI/Google Maps (unverified, source shown, phones masked). Road routes by Mapbox (SerpAPI fallback); the shortlist uses straight-line distance but every reported distance/time is road-network; hazards recorded in the system block or penalise routes. Operator can pick another station. |
| Map images for Telegram | **Works** | Incident / route / forecast maps from the incident's own data on Mapbox satellite tiles, with a burned-in label (DEMONSTRATION; "predicted risk footprint, not a fire perimeter"). Schematic fallback if Mapbox fails. A PDF report is attached. |
| Telegram | **Built; real send NOT exercised** | Bot token + chat id are configured. Real sends need `confirm_real` plus a confirmation dialog; simulation builds the same message and files but sends nothing. Duplicate sends are suppressed. "SENT" = Telegram returned a message id; delivery to a person is never claimed. Button presses (Acknowledge etc.) need `TELEGRAM_POLLING=true` or a webhook and are limited to authorised chats; polling is off by default because it would compete with any other app using the same bot. |
| IVR / voice | **Simulated works; real not exercised; Dimension unavailable** | The repository contains **no Dimension API integration or credentials** and none was invented: selecting it reports that clearly. Twilio is implemented (approved-numbers whitelist, explicit confirmation, bounded retries, provider status polling) but untested against Twilio. Simulated calls are labelled `[SIMULATED]`. Keypad acknowledgement is not enabled. |
| Unified event log | **Works** | Polled every 3 s from `fire_events`; filters for live detections, incident, forecast, routing, Telegram, IVR, errors, demo. Failed, retried, skipped and simulated entries are distinct. Secrets are never logged. |
| Demo mode | **Works** | Run / Stop / Reset. Synthetic coordinates (configurable), illustrative dry/windy weather (or live weather), everything labelled DEMO. Reset deletes only demo records. |

## Running it

```powershell
uvicorn app.main:app --port 8000      # then open http://localhost:8000/live
```
Demo: **Fire response → Run demo incident**. It creates `DEMO-001`, runs the forecast, looks up stations, routes, builds the impact panel, and confirms it as part of the scenario. Then **Authorise** (Overview), and use **Comms → Preview (simulated)** and **Simulated call**. Configure the demo point and weather with `PUT /api/v1/fire/demo/config`.

## Environment variables (names only)
`FIRMS_API_KEY`, `MAPBOX_API`, `SERP_API`, `ROUTING_PROVIDER`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_POLLING`, `TELEGRAM_AUTHORISED_CHAT_IDS`, `CALLBACK_SECRET` (webhook), `PUBLIC_BASE_URL` (HTTPS needed for app links in Telegram), `IVR_PROVIDER` (`omnidimension` | `twilio` | `simulated`), `OMNIDIM_API_KEY`, `OMNIDIM_AGENT_ID`, `OMNIDIM_FROM_NUMBER_ID`, `IVR_RESPONSE_NUMBER`, `IVR_RESPONSE_LABEL`, `IVR_APPROVED_NUMBERS`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`.

## Acceptance results (what was actually run)
* **A, live detection:** real FIRMS feed (856 hotspots in India), incident `VF-001` created from the strongest hotspot (6.1 h old, labelled DELAYED, 12 nearby passes), duplicate create returned the same incident, a fabricated hotspot was rejected (422).
* **B, demo:** forecast (3/6/12 h median potential area 34 / 135 / 513 ha with illustrative weather), 4 candidate stations compared, route by Mapbox, impact panel, three map images and a PDF built, simulated Telegram and IVR exercised, complete event log.
* **C, Telegram real send: not performed.** Only the logic is tested (mocked transport). A real send needs your go-ahead.
* **D, IVR:** simulated walked QUEUED → INITIATED → RINGING → ANSWERED → COMPLETED, labelled simulated; real call correctly refused without configuration, approved number or confirmation. No real call placed.
* **E, routing:** real Mapbox routes over road network; unreachable stations and blocked routes are shown as failures.
* **F, consistency:** duplicate Telegram/incident requests suppressed; failures appear as failed; one incident id across map, panels and log.
* **G, regression:** the full suite passes.

## Known limitations
* The spread model is a prototype built on an Australian eucalypt-forest formula. **No historical validation of its accuracy has been done**; uncertainty bands only reflect the input spreads used. Spotting, suppression, fuel-type mapping and wind shifts are not modelled.
* The potential exposure area is not burned area; no fire perimeter is available from point hotspots.
* Station data is unverified public mapping data (OSM/Google); availability, contacts and access are unknown. Routes are typical driving times, not live traffic, and do not know about closures unless a hazard was recorded.
* Air quality is modelled ambient data at about 11 km; it does not show smoke from this fire.
* FIRMS has a transaction quota and a processing latency of about 3 hours: "current" is never "instant".
* OmniDimension calls use `POST /calls/dispatch` (agent id, destination number, `call_context` containing the full `message` and incident details) and follow the call through `/calls/logs`. The agent must be configured in the OmniDimension dashboard to read `{{message}}`. The call-log response shape is not fully documented, so if the provider returns no status the screen says it was dispatched and does not claim it finished.

## Current behaviour (live map)
* The map is clear until something is chosen. Left panel switches: **NASA fire hotspots** (dots), **Heat zones** (density of hotspots), **Tree cover history**. Choosing an incident draws only that incident (pin, 3/6/12 h footprint, route).
* **Fire** tab lists incidents. *Add example incidents* creates five scenario incidents (Satpura, Bandipur, Corbett, Similipal, Nallamala) with assumed weather and real terrain, land cover, stations and Mapbox routes; they carry a small "Scenario" tag and are removed with *Remove examples*. Real incidents come from the NASA layer (click a hotspot → Investigate).
* **Alert** tab: *Send to Telegram* and *Call now* each ask once, then show a live progress card (preparing → sending → accepted; placing → ringing → answered → finished). Only milestones are recorded under **Record** (opened, confirmed, forecast ready, route, sent, call placed/ringing/answered/completed/failed).
* **Phone call**: the call reads out the place name (Mapbox reverse geocoding), coordinates, detection time and fire power, confirmation state, predicted size and wind, nearby settlements, nearest station with distance, compass direction and road time, and the first route instruction. *What the call says* shows the exact text; *Hear it* plays it in the browser. Real calls need `IVR_PROVIDER=twilio`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `IVR_RESPONSE_NUMBER` (who is called) and `IVR_APPROVED_NUMBERS` (numbers calls are allowed to reach). Without them *Call now* stays disabled and says what is missing.
* Scenario incidents say "This is a test scenario" in the outgoing Telegram message and call, so a real recipient is not sent to a non-existent fire. Real NASA-based incidents carry no such line.

## Phone calls via OmniDimension
Set in `.env`: `IVR_PROVIDER=omnidimension`, `OMNIDIM_API_KEY`, `OMNIDIM_AGENT_ID` (the voice agent), `IVR_RESPONSE_NUMBER` (international format) and optionally `OMNIDIM_FROM_NUMBER_ID`. The configured response number is the only number a call may reach (or the `IVR_APPROVED_NUMBERS` list when you set one). Restart the server after editing `.env`.
