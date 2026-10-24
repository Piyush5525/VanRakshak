# Reference repository audit

Method: shallow `git clone --depth 1` of each repository into an isolated scratch folder on 2026-10-24, then reading the file tree, licence files, requirements and key source files. Nothing from the repositories was executed. Reported metrics were **not** reproduced (no data or compute for it here).

| # | Repository | Commit checked | Licence evidence | Copied into VanaNetra? |
|---|---|---|---|---|
| A | `yunes-mahan/deforestation-makeathon-2026` | `170889a` (2026-04-19) | README says "MIT"; **no LICENSE file** in the tree | No |
| B | `Climate-Vision/ClimateVision` | `aab9708` (2026-08-28) | `LICENSE` file present, README states MIT | No (patterns only) |
| C | `esteban-russi/sentinel2-dl-deforestation` | `ce459ba` (2025-09-01) | No licence file, none stated in README | No |
| D | `pruthviklshetty/Forest-Loss-Detection-Carbon-Emission-Estimation` | `1ff6386` (2026-09-01) | No licence file, none stated in README | No |
| E | `akhilchibber/deforestation-detection-satellite-fusion` | `90116c8` (2026-05-19) | No licence file, none stated in README | No |

Where a licence is absent or only asserted in prose, default copyright applies and reuse rights are unclear, so no code was taken. VanaNetra's code is original; the ideas below were adopted.

## A. Makeathon pipeline
`step1`–`step6` scripts. `step3_train.py` defines `DualHeadUNet`: a UNet++ (segmentation-models-pytorch) with two 1x1 output heads (binary mask and year-of-loss), trained with Dice + Tversky (alpha 0.3, beta 0.7, false-positive-heavy) + weighted BCE. README motivation: temporal differencing (dNDVI) is more robust than static classification.
Adopted idea: bi-temporal differencing and false-positive-averse design; geometric filtering of tiny polygons. Not adopted: the network (needs competition data and weights we do not have). The README's ">55% IoU" is a competition result, not evidence for India.

## B. ClimateVision
Large FastAPI package (`src/climatevision`: api, analysis, governance, impact, inference, reports, security, workers). Uses `earthengine-api` (needs a Google Earth Engine account). Its `models/` directory holds **flooding** model runs, including one named `flooding_flood_synthetic_test`; `analytics/carbon.py` and others reference synthetic/random data. These are not forest-change models and were not reused. Adopted: module layout ideas (provider boundary, governance/audit, reports) only.

## C. sentinel2-dl-deforestation
Four numbered scripts: GEE export, U-Net, attention U-Net, change analysis; Dynamic World labels, Colombia. Requires Earth Engine. Adopted: cloud/shadow masking via the scene classification layer, and treating Dynamic World labels as coarse reference rather than ground truth (we use SCL and ESA WorldCover for the same purpose without needing Earth Engine).

## D. Forest-Loss-Detection-Carbon-Emission-Estimation
`src/{preprocessing,change_detection,carbon,eval,models}`, FastAPI `backend/serve`. Bi-temporal Sentinel-2, forest-mask comparison, 10 m pixel to hectares. Its carbon module states in code that the 3-bin NDVI to tC/ha scheme (100/150/200) is **the authors' assumption, not IPCC**. VanaNetra therefore does **not** convert area to carbon; the API returns `not_calculated` until a documented biomass model is configured. Requires Earth Engine for data.

## E. satellite-fusion
Seven notebooks (download, preprocessing, UNet training, prediction). Fuses Sentinel-1, Sentinel-2 and AlphaEarth embeddings over 2020-2025. README reports F1 56.75% and deforestation IoU 39.62% on its own split. Adopted idea: radar as a complementary channel and explicit missing-data handling. We do not use AlphaEarth embeddings. The reported numbers are the authors' and say nothing about operational accuracy here.

## Compatibility summary
All data-producing references depend on Google Earth Engine. VanaNetra instead reads Sentinel-1 RTC, Sentinel-2 L2A and ESA WorldCover from the public Microsoft Planetary Computer STAC (no key). No third-party weights, datasets or notebooks are bundled. Any future reuse needs: licence confirmation from the author, a pinned commit, and a checksum recorded here.
