# Forest monitoring method (as implemented)

## Acquisition (`app/services/satellite.py`, `acquisition.py`)
Source: Microsoft Planetary Computer public STAC (no account or key; short-lived read tokens are fetched automatically).

* **Sentinel-2 L2A**: bands B08 (NIR) and B12 (SWIR), scaled with the per-asset `raster:bands` scale/offset. Mask from the scene classification layer: classes 0 nodata, 1 saturated, 3 cloud shadow, 8/9 cloud, 10 cirrus. Scenes in a +/- N day window (default 15 via UI, 30 default API, max 90) with cloud cover below a threshold; up to 6 least-cloudy scenes; per-pixel **median of clear observations**. Pixels with no clear observation are flagged, not filled.
* **Sentinel-1 RTC**: radiometrically terrain-corrected gamma0, IW mode, VV and VH, converted to dB after a linear-power median. **One orbit direction** is used for both dates to avoid ascending/descending geometry differences.
* **ESA WorldCover 2021** class 10 (tree cover) as the baseline forest mask.
* All layers are warped onto one UTM grid at 20 m. Region size is capped (4 million pixels, about 1,600 km2 at 20 m).
* Each composite is an observation record: scene IDs, acquisition window, processing time, valid-pixel fraction, processing config, warnings, file checksum. Composites are cached by (region, sensor, date, config).

Acquisition time (scene datetimes), processing time (`processed_at`) and detection time (`created_at` of the change record) are stored separately.

## Detection (`changedetect.py`)
Rule-based candidate generator, **not a trained model**:
1. Baseline forest = WorldCover tree cover AND baseline NBR >= 0.15 (vegetation still present at the baseline date; WorldCover is 2021). Without a mask, an NBR threshold of 0.35 is used and flagged as weak. On the Wayanad test area dry-season NBR had median 0.29, which is why the fixed-threshold fallback is discouraged.
2. Candidate pixel: baseline forest AND NBR drop >= 0.25, clear in both dates, then 2x2 morphological opening.
3. Polygonise, drop patches below 0.1 ha, ellipsoidal (WGS84) area.
4. **Sentinel-1 support** per patch: share of valid pixels where VH fell by at least 1.5 dB. Labels: `optical_radar_agree` (>=50%), `optical_radar_disagree`, `optical_only_radar_insufficient`. Radar never creates or deletes candidates; it informs review.
5. Withheld (`INSUFFICIENT_EVIDENCE`) if valid coverage is below 60% or the forest mask is empty.
6. Seasonal check: baseline and comparison more than 45 days apart in the annual cycle produce a warning.

Scores are uncalibrated dNBR magnitudes and are labelled so. A candidate case is opened for human review only if total area >= 0.5 ha and at least one patch is optical+radar agreeing. Thresholds are configuration, not validated operating points.

## Not done / unverified
No accuracy assessment against reference labels has been run; fire, harvest, plantation rotation and seasonal change are not distinguished; thresholds were not tuned; no trained segmentation model is shipped. See `MODEL_EVALUATION.md`.
