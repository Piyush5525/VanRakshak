# Data sources

| Source | Use | Access | Notes |
|---|---|---|---|
| Sentinel-2 L2A (Copernicus, via Planetary Computer `sentinel-2-l2a`) | NIR/SWIR, SCL cloud mask | public, no key | free and open Copernicus licence; cite "Contains modified Copernicus Sentinel data" |
| Sentinel-1 RTC (`sentinel-1-rtc`) | VV/VH backscatter | public, no key | terrain-corrected; Microsoft processing; one orbit direction used |
| ESA WorldCover 2021 (`esa-worldcover`) | baseline tree-cover mask | public, no key | CC-BY 4.0 (ESA WorldCover); 2021 epoch; accuracy varies by biome |
| Copernicus DEM GLO-30 (`cop-dem-glo-30`) | terrain, slope, 3D scene | public, no key (per-asset signed URLs) | ESA/DLR; cite "Contains modified Copernicus data" |
| Open-Meteo forecast, archive, air quality | rainfall, climatology, ambient PM2.5 | public, no key | CC BY 4.0; model/reanalysis (~10 km), not gauges; CAMS air quality is ambient, not site emissions |
| GLAD-L forest alerts (UMD/GLAD via Global Forest Watch tile cache) | **reference dates only** for scoring the alert service | public tile cache, no key | Landsat-based, daily dates from 2021, covers parts of India; an independent product, not field truth; cite Hansen et al. / GLAD (UMD) and GFW |
| NASA FIRMS | active-fire detections | free MAP_KEY (`FIRMS_API_KEY`) | verified live: 398 VIIRS detections returned for central India over 2 days; cite NASA FIRMS |
| OpenStreetMap tiles | basemap in the dashboard | public | requires attribution; heavy use needs your own tile server |
| OpenStreetMap via Overpass | nearest road / village / waterway / protected area for impact assessment | public mirrors (overpass.openstreetmap.fr first) | ODbL; mapping incomplete; mirrors are intermittently slow, so context is best-effort and reports `unavailable` |
| Mapbox | Directions routing (primary); satellite basemap toggle | `MAPBOX_API` (public `pk.` token only is ever sent to the browser) | verified live; usage is metered by Mapbox |
| SerpAPI | Google Maps driving distance/time (routing fallback, no geometry); web leads for government records | `SERP_API` | verified live; paid per search; leads are not records |
| OSRM | optional third routing provider | self-hosted | not used by default |
| Government authorization records | matching | manual entry/import | see `AUTHORIZATION_VERIFICATION.md` |

Planetary Computer terms and Copernicus licence conditions should be reviewed before operational use; this build did not perform a legal licence review. Forecast and energy modules use only data stored in the system or supplied by users; no external training dataset is bundled. Weather, terrain, elevation, rainfall, curated biodiversity layers and biomass are **not** ingested; OSM road/settlement/waterway context is fetched per incident, not stored as a layer.
