# AR Forest Explorer and guided navigation

The attached "prototype reference" mentioned in the brief was not received, so the interface follows the written requirements and the existing RAKSHAK design system rather than a specific mock-up.

## AR Forest Explorer (`#/ar-explorer`, backend `GET /api/v1/ar/forest-scenes/{region_id}`)

**Real inputs**: Copernicus DEM GLO-30 heights (about 30 m native, resampled to at most 96 cells per side), ESA WorldCover 2021 tree-cover fraction per cell (10 m classes averaged), and the region's stored candidate-loss polygons grouped by detection date.

**Not real**: individual tree positions. The client scatters up to about 14,000 markers in proportion to the density grid (seeded, repeatable) and the page states this. Markers are enlarged for visibility.

**Timeline**: reference forest (2021 map) then one item per stored detection (red = candidate loss, labelled unverified), then any hypothetical clearings. Play/pause/reset/speed and a slider; cut trees shrink and tilt as the timeline passes the detection. "Compare with reference" renders the reference (left) and the selected item (right) side by side.

**Scenario labelling** (always on screen): `REFERENCE MAP`, `CANDIDATE DETECTION — UNVERIFIED`, `HYPOTHETICAL SCENARIO — NOT A FORECAST`. The 7/30/90-day *spatial forecast* is shown only when an evaluated risk model exists (it does not yet); otherwise the page says so. Users can place a **hypothetical clearing** (radius and horizon) by clicking the terrain; it is stored as a simulation with the user's assumptions.

**Inspect**: clicking terrain shows elevation, tree density, coordinates and which category the cell is in at the current time.

**WebXR AR**: "Enter AR" appears only when `navigator.xr.isSessionSupported('immersive-ar')` is true. It uses hit-test to place a tabletop-sized model (about 0.9 m) on a surface; overlay buttons resize, rotate, play/pause and exit. This path could not be exercised in this build (no AR device); only the capability detection and 3D fallback were tested. Compatibility: Chrome on ARCore Android devices; iPhone/iPad Safari has no WebXR AR (a native ARKit companion would be needed and is not built).

Device compatibility is reported on the page (WebGL2, secure context, WebXR AR, camera, location, motion sensors).

## Guided navigation (`#/ar-navigation`, backend `/api/v1/navigation/*`)

Flow: pick a **reviewed** incident → set position (GPS permission, or manual coordinates) → route options from Mapbox (fallback SerpAPI) → each route is evaluated against recorded hazards (verified blocking hazards block; unverified or stale ones penalise; heavy-rain advisories are shown as advisories) with plain-language reasons → consent checkbox → start.

**Guidance HUD**: camera backdrop (if permitted), large arrow, next instruction and distance, remaining distance, route progress bar, on/off-route banner, GPS accuracy, hazard warnings, Recalculate, optional voice, haptic buzz when off-route, exit. Position is posted about every 3 s; the server computes progress, off-route distance (> 50 m), the next manoeuvre *ahead on the route*, and warns if a newly verified hazard blocks the route.

**Honest limits**: this is camera + compass guidance, **not world-anchored AR**. Browsers cannot reliably register an arbitrary outdoor route to the camera, so the arrow is rotated by (bearing − device heading). If compass access is denied the arrow shows the absolute bearing and the HUD says so. GPS under canopy is often 10–50 m off. Forest tracks are frequently absent from road data, so the final approach may be off-network (the API warns when a waypoint snaps more than 500 m from a road). "No recorded hazard" never means safe.

**Privacy**: location use requires explicit consent; only the latest position and summary figures are stored; the last position is deleted when the session ends; no track is kept.

**Not built**: offline map tiles, turn-by-turn from providers lacking geometry (SerpAPI gives distance/time only), wearable/native clients.
