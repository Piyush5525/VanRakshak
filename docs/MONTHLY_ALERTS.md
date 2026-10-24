# Monthly forest-loss alert service (CL-01, Phase 2)

Problem statement (Phase 1, unchanged): using Sentinel-2, identify where forest loss occurred in India between two dates, despite clouds and seasonal change.

Phase 2 extension and where each item lives:

| # | Requirement | Implementation | How it is measured |
|---|---|---|---|
| 1 | Monthly alerts for a full year, including monsoon months | `services/alerts.py` (`run`), `services/stacks.py`; page **Monthly Alerts**; `POST /alerts/runs` | Run with `n_months=12`; the page shows alerts per month and the Sentinel-2 clear fraction (the monsoon dip) |
| 2 | Add radar (Sentinel-1) and show the gain during the monsoon | Radar branch of `alerts.detect`; every run also computes an optical-only variant | `radar_gain` in the evaluation: precision, eventual recall, **recall within 45 days**, and issue delay, monsoon vs other months, fused vs optical-only |
| 3 | Estimate the date of loss; median error against public alert dates | Midpoint between last unchanged and first changed observation, half-gap reported as uncertainty | `date_estimate_error` against GLAD-L alert dates (median absolute and signed error, p90, n) |
| 4 | Rank alerts; precision of the top 50 | `score` and `rank`; `GET /alerts/runs/{id}/top?k=50` | `precision_at_k` (spatial match, and spatial + date within 60 days). K is reduced and reported if fewer than 50 alerts exist |
| 5 | Keep Phase 1 F1 within 5 points | Phase 1 = the existing two-date optical detector (`changedetect.py`), unchanged | `phase1_check`: F1 of the Phase 1 map vs F1 of the union of all Phase 2 alerts, same reference, same tolerance; pass if the drop is at most 5 points |

## Method

**Data.** Per-scene Sentinel-2 L2A NBR (cloud, shadow, cirrus and saturation masked with SCL) and Sentinel-1 RTC VH backscatter in dB (one relative orbit for the whole run, so incidence geometry never changes; 3x3 spatial smoothing against speckle), both from Microsoft Planetary Computer, on one 20 m UTM grid. Same-day overlapping tiles are merged. Months are cached.

**Baseline.** The `ref_months` before January (default 3) give each pixel a median NBR, and a median and robust sigma (1.4826 x MAD, floored at 0.8 dB) for VH. A pixel needs at least 3 valid baseline observations. Forest = baseline NBR >= 0.15 (the Phase 1 forest mask from WorldCover is a documented extension; see limitations).

**Change.** Optical: NBR below baseline by 0.25. Radar: VH more than 3 robust sigma below baseline. A change is confirmed only when the *next valid observation* of the same sensor is also changed (rain, haze, shadow and speckle make single dips common). Each alert is therefore issued when its confirming observation exists and uses no later data.

**Loss date.** Midpoint between the last valid unchanged observation and the first changed one; uncertainty = half that gap (capped at 45 d). Under a monsoon cloud gap the optical bracket is wide, and when optical and radar agree the *tighter* bracket sets the date.

**Fusion (per pixel).** (1) optical and radar agree within 45 days; (2) optical only (radar had no valid pass); (3) optical, but radar saw no change (low confidence); (4) radar only while optical was blind within +-20 days; (5) radar only although optical was clear and unchanged (low confidence). Classes 3 and 5 are withheld from issued alerts unless requested. Radar never overrules a clear optical observation.

**Patches and ranking.** Alert pixels are grouped per issue month (8-connected, minimum 0.2 ha). Score = evidence weight x sqrt(area in ha) x (1 - 0.3 x date uncertainty / 60 d). It orders work; it is **not** a probability and is not calibrated.

**Frozen parameters.** All thresholds live in `AlertParams`; a run stores its parameter digest. The held-out declaration is the user's statement that the region was not used to choose them; the system records it but cannot verify it.

## Evaluation against public alert dates

Reference = GLAD-L alerts from the Global Forest Watch tile cache (UMD/GLAD, Landsat, 30 m, from 2021, covers this part of India; the Sentinel-2 GLAD and RADD products do not cover India). Tile pixels encode days since 2014-12-31 in R x 255 + G and confidence in B (>= 200 = high). Decoded at zoom 12 (about 38 m) and resampled to the run grid.

* Agreement uses a **one-pixel tolerance** (3x3) in both directions because the grids differ. Reference pixels are restricted to the run's forest mask.
* Pixel precision/recall/F1 and date error are computed over reference alerts dated inside the evaluation period.
* **Timely recall** = share of reference pixels in a season that have an alert issued within 45 days of the reference date. Eventual recall ignores timing. Radar's contribution in the monsoon shows up as timeliness (optical often sees the same loss eventually, weeks later), so both are reported.
* Precision@K uses patches ranked by score; a patch counts if any reference alert falls within one pixel of it (and, in the stricter variant, its estimated date is within 60 days of the reference).

### Honest limits of this evaluation
* GLAD-L is an alert product with its own errors; it is not field truth. A reference-free "false alarm" can be a GLAD-L omission.
* The pilot region is about 4 km x 4 km. Counts of reference patches and alerts are small, so percentages are anecdotal. The evaluation says so (`warnings`) whenever n < 30.
* A statewide, held-out evaluation needs the same code run over many tiles for a state-sized area. The service limits a region to about 1,600 km2 per run (4 million pixels at 20 m) and reads every scene, which takes hours per block; a state needs tiling and a scheduler that this build does not include.
* The 45-day timeliness window and the 60-day date window are choices, not standards.

## API
`POST /alerts/runs` -> `{run_id, job_id}`; `GET /alerts/runs`, `/alerts/runs/{id}`, `/alerts/runs/{id}/alerts?month=&evidence=`, `/alerts/runs/{id}/top?k=50`, `/alerts/runs/{id}/export.geojson|csv`; `POST /alerts/runs/{id}/evaluate` (body: optional `phase1_change_id`, `top_k`); `GET /alerts/runs/{id}/evaluations`.

## Pilot result (recorded 2026-10-24; real data, small sample)

Region: 4 km x 4 km Wayanad block, year 2025, 12 months, Sentinel-1 (one descending orbit) + Sentinel-2, reference = GLAD-L. This block is **not** a held-out state and had no monsoon-period reference loss, so items 2 and 5 below could not be demonstrated here. 30 reference pixels in 5 patches: every percentage is anecdotal.

| | Run 1: fixed Oct-Dec baseline | Run 2: seasonal baseline (full 2024) |
|---|---|---|
| Alerts issued / area | 98 / 52.6 ha | 44 / 20.0 ha |
| Issued Jan-Mar | about 50 | 31 |
| Optical-only / radar-corroborated | 96 / 2 | 41 / 3 |
| Pixel precision vs GLAD-L (1-px tolerance) | 0.8% | 1.2% |
| Pixel recall (30 reference px) | 60% | 70% |
| Precision of top 50 (spatial match) | 2% (of 50) | 0% (of 44; fewer than 50 alerts) |
| Median date error / n | 3.3 d / 17 px (p90 117 d) | 3.3 d / 10 px (p90 263 d) |
| Monsoon radar gain | not measurable (no monsoon reference loss) | not measurable; zero alerts issued Jun-Sep |
| Phase 1 two-date F1 vs all-alert F1 | 0% vs 1.6% | 0% vs 2.4% (Phase 1 F1 is 0%, so "within 5 points" is not informative) |

**Reading it honestly.** The seasonal baseline halved the alerts and raised recall, which is the intended effect, but precision against GLAD-L is still about 1%: most alerts have no GLAD-L alert near them. That is either many false alarms or small clearings that 30 m GLAD-L does not see (or both); this evaluation cannot tell which. The date error is about three days for the few matched pixels but with a long tail. Nothing here demonstrates the Phase 2 targets. What it does show is the pipeline, the evaluation and their failure modes working end to end on real data.

**Known weaknesses to address next (not done):** early-year alerts still dominate (year-to-year differences in dry-season greenness are being read as loss); there is no plantation/agriculture filter (WorldCover distinguishes tree cover from crops but plantations such as rubber and coffee are tree cover); a single baseline year cannot separate a dry year from loss; alerts have not been checked against high-resolution imagery; thresholds have not been tuned and should be tuned only on a separate tuning region, then frozen before scoring a held-out one.
