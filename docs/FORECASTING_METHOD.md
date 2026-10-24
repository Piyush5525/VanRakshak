# Forecasting method

**Event**: a 0.01 degree cell (about 1.1 km) with no recorded loss in the previous 365 days receives a newly recorded loss detection with `comparison_date` in (origin, origin + horizon], horizon 7, 30 or 90 days. It is a spatial risk of a new event, not growth of a polygon.

**Features** (all computed from detections dated at or before the origin): losses in the cell neighbourhood in the last 365 days, days since the last loss, distance to the nearest earlier loss, loss area within 5 km in the last year. Roads, terrain, weather and forest edge are **not** used because no such data layers are ingested yet.

**Model**: L2-regularised logistic regression (numpy). Trained by `POST /api/v1/forest/forecasts/train` (admin) on detections stored in the system.

**Guards**
* Training refuses unless at least 40 stored detections, 6 monthly origins, 30 training positives and 10 test positives exist. With one pilot region this will not be met: the honest answer is `INSUFFICIENT_DATA`.
* Time-ordered split at 70% of origins with an **embargo equal to the horizon**, so no test label window overlaps training. A spatial hold-out is not performed and the evaluation record says so.
* Metrics on the later period: PR-AUC, PR-AUC of a "closer to last loss is riskier" baseline, precision/recall in the top 10% of cells, false-alert rate, Brier score, calibration bins, expected calibration error.
* A model is `USABLE` only if PR-AUC beats the baseline and ECE < 0.05. Otherwise it is registered as `EVALUATED_NOT_USABLE` and no surface is produced.
* Inference returns `UNAVAILABLE` (no usable model, bad checksum) or `INSUFFICIENT_INPUT` (no detection on or before the origin, newest detection older than 120 days, region far outside training coverage). A future origin is rejected.

Output records carry origin, horizon, model version/ID, evaluation ID, input freshness, score kind and limitations. Forecast layers are stored separately from observations and flagged `is_observation: false`.

**Caveat**: detections come from the candidate generator, so the forecast inherits its errors and is trained on candidates, not verified loss.
