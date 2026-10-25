# New construction and land change (last 365 days)

Shows what has been built or cleared in a chosen patch over the last year: roads, highways, buildings, industrial areas, construction sites, quarries, solar and other cleared land. Each change is a polygon coloured by type, with its impact, a before / after picture and a PDF report. Jaipur is ready to use; any area in India can be drawn.

Open the live map, switch on **🏗 New construction** (left) or open the **🏗 Built** tab (right).

## How a change is found

All from real Sentinel-2 images (Microsoft Planetary Computer). Nothing is simulated.

1. Four cloud-masked composites of the same place are made: **a year ago**, **now**, and **two in between** (about 75 and 215 days before "now"). Before and after use the same time of year so seasons do not look like change.
2. Ground is reported only if both are true:
   * **Vegetation cleared:** it was green a year ago (NDVI above 0.30) and has **never been green again** in any later image (NDVI stays under 0.28). A field that is harvested and then grows a new crop turns green in between, so it is not reported.
   * **Dark or bare ground became bright hard surface:** it was not green, and in at least two later images (including the newest) it is brighter and more built-up-like (NDBI), and stays so.
3. Water and cloud are excluded. Changed pixels are joined into patches of at least 0.25 ha and drawn as polygons.

## How a patch is classified

| Type | Colour | Decided by |
|---|---|---|
| Highway / main road | dark orange | OpenStreetMap road (motorway, trunk, primary) overlapping the patch |
| Road | orange | other OpenStreetMap road, or a long narrow strip |
| Buildings | blue | OpenStreetMap buildings or residential land use, or a bright grey surface |
| Industrial / commercial | purple | OpenStreetMap industrial, commercial or retail land use or buildings |
| Construction site | yellow | OpenStreetMap `landuse=construction` |
| Quarry / mining | brown | OpenStreetMap quarry, landfill or brownfield |
| Solar / energy | teal | OpenStreetMap power plant or generator |
| Cleared land (use unknown) | grey | the ground changed but nothing says what it is |

Every change shows **where its class came from** (OpenStreetMap tag, shape, or satellite colour). OpenStreetMap is incomplete outside cities, so many changes honestly say "use unknown".

## Impact

**Measured** (from the images and ESA WorldCover): area, vegetation lost (ha), tree cover lost (WorldCover 2021) and the carbon in it (assumed density, with a low to high range), hard surface added, greenness before and after, what the land was, and the distance to the nearest mapped water and forest.

**Typical effects** (general knowledge, marked as not measured): less rain soaking in, heat, habitat loss, road fragmentation, quarry dust.

## In the app

* **Jaipur ready-made areas:** south-west (Sanganer, Bagru, Mahindra World City), south (Jagatpura, Sitapura, Tonk Road), north (Delhi Road, Kukas, Amer).
* **Your own patch:** draw a rectangle or any shape, or use what the map shows. Up to 450 km² (10 m pixels up to 130 km², 20 m above).
* **Right side:** counts, area changed, green lost and hard surface added; type chips that switch a type on or off on the map; the list of changes, largest first.
* **One change:** drag the before / after slider, read the measured impact, open the typical effects, view or download its report, open it in Google Maps.
* **Not a change:** a detection you do not believe can be **set aside** and **put back**. Set-aside changes are left out of the map, the counts and the reports.
* **Remove a check:** removes it and everything found in it.

## Reports

* **One change:** before / after picture, what was found and how it was classified, measured impact, land before, typical effects, method, limits, and Google Maps links.
* **Whole check:** every change on one satellite image, a table by type, totals, and each change (largest 40) with before / after pictures. A GeoJSON export has all of them.

Both can be viewed inside the page or downloaded.

## API

`GET /api/v1/infra/status` · `POST /api/v1/infra/runs` (`preset`, or `bbox` and optional `polygon`) · `GET /runs`, `/runs/{id}`, `/runs/{id}/changes`, `/runs/{id}/export.geojson`, `/runs/{id}/report.pdf` · `POST /runs/{id}/cancel` · `DELETE /runs/{id}` · `GET /changes/{id}`, `/changes/{id}/image/{before|after|compare}.jpg`, `/changes/{id}/report.pdf` · `PATCH /changes/{id}` (`status`: `OPEN` or `NOT_A_CHANGE`).

## Limits

* Sentinel-2 pixels are 10 m (20 m for big areas), so narrow roads and single houses can be missed.
* Work that replaces older buildings or hard surface shows little change and is not found.
* A patch is a satellite detection, not a survey and not a finding that anything is unlawful: it needs a check on the ground.
* A fresh check of about 60 km² takes a few minutes the first time (images are cached afterwards).
* Real imagery is needed: cloud in every image of a period stops a check with a clear message.
