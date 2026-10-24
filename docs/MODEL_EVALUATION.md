# Model evaluation status

## What has actually been measured
| Item | Result | Evidence |
|---|---|---|
| Rule-based change detector, synthetic raster fixtures | finds a 4 ha cleared block to within 2% area; no candidates on unchanged scene; withholds on full cloud / empty forest mask | `tests/test_geo.py` (deterministic, tests only) |
| Live run, Wayanad pilot block (about 4 km x 4 km, Feb 2024 vs Feb 2025, Sentinel-1+2+WorldCover) | 21 candidate patches, 5.48 ha total, most 0.16-0.48 ha; 16 optical+radar agree, 5 disagree; valid coverage 100% | one real job, 190 s. **Not validated against any reference.** The patches may be plantation harvest, selective felling or noise |
| Forecast trainer on synthetic clustered events | pipeline trains, splits with embargo, registers metrics | `tests/test_platform.py`. Synthetic data proves the code path only; it says nothing about real-world skill |

## What has not been measured
Precision/recall/IoU of change detection against labelled loss (e.g. Hansen/GLAD or field data); Sentinel-1 threshold choice; forecast skill for any horizon on real detections (insufficient history: the trainer correctly refuses); calibration; building-energy accuracy (no trained model; no metrics claimed).

## How to produce real evaluation
1. Assemble reference loss polygons for several districts and years; hold out whole districts.
2. Run the detector over them, compute polygon-level precision/recall by area, with a buffer to tolerate label resolution (Hansen is 30 m, annual).
3. Tune `dnbr_min`, `min_area_ha`, radar threshold on a development district only; report on the held-out districts.
4. For forecasting, accumulate months of verified detections, then call the training endpoint; evaluation records appear under `/api/v1/evaluations`.
