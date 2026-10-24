from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from rasterio.transform import from_origin

from app.services import alerts as A

START = datetime(2025, 1, 1, tzinfo=timezone.utc)
REF_END = (START.date() - A.EPOCH).days
SHAPE = (24, 24)
P = A.AlertParams(min_patch_ha=0.2, seasonal=False)  # fixed pre-period baseline for these short synthetic stacks
GRID = SimpleNamespace(transform=from_origin(500000, 1230000, 20, 20), crs="EPSG:32643", height=24, width=24, resolution=20.0)
BLOCK = (slice(6, 12), slice(6, 12))  # 36 px at 20 m = 1.44 ha


def dates(first, n, step):
    return [first + timedelta(days=step * i) for i in range(n)]


OPT_T = dates(datetime(2024, 10, 3, tzinfo=timezone.utc), 90, 5)   # 5-day revisit from Oct 2024
RAD_T = dates(datetime(2024, 10, 5, tzinfo=timezone.utc), 45, 12)  # 12-day revisit


def stack(whens, base, fn=None, noise=0.0, seed=1):
    rng = np.random.default_rng(seed)
    out = np.full((len(whens), *SHAPE), base, dtype="float32") + rng.normal(0, noise, (len(whens), *SHAPE)).astype("float32")
    if fn:
        for i, w in enumerate(whens):
            fn(out[i], i, w)
    return {"data": out, "when": whens}


def utc(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


def test_confirmed_optical_loss_dated_by_midpoint_and_issued_on_confirmation():
    cut = utc(2025, 3, 10)
    opt = stack(OPT_T, 0.6, lambda a, i, w: a.__setitem__(BLOCK, 0.15) if w >= cut else None, noise=0.02)
    out = A.detect(opt, None, REF_END, REF_END, P, use_radar=False)
    m = out["evidence"] > 0
    assert m[BLOCK].all() and m.sum() == 36  # only the cut block; noise creates nothing
    first_bad = min(w for w in OPT_T if w >= cut)
    prev = max(w for w in OPT_T if w < cut)
    est = A.to_date(float(np.nanmedian(out["est_day"][m])))
    assert prev.date() <= est <= first_bad.date()
    assert float(np.nanmedian(out["unc_days"][m])) <= 3.0  # 5-day revisit -> about +-2.5 d
    second_bad = [w for w in OPT_T if w >= cut][1]
    assert A.to_date(float(np.nanmedian(out["issue_day"][m]))) == second_bad.date()  # causal: needs the 2nd observation


def test_single_dip_from_cloud_shadow_is_not_an_alert():
    opt = stack(OPT_T, 0.6, lambda a, i, w: a.__setitem__(BLOCK, 0.1) if i == 40 else None, noise=0.02)
    assert (A.detect(opt, None, REF_END, REF_END, P, use_radar=False)["evidence"] > 0).sum() == 0


def test_cloud_gap_widens_date_uncertainty_and_delays_issue():
    cut = utc(2025, 7, 1)

    def f(a, i, w):
        if w >= cut:
            a[BLOCK] = 0.15
        if utc(2025, 6, 20) <= w <= utc(2025, 7, 25):
            a[:] = np.nan  # monsoon cloud: no clear optical observation for ~5 weeks

    out = A.detect(stack(OPT_T, 0.6, f, noise=0.02), None, REF_END, REF_END, P, use_radar=False)
    m = out["evidence"] > 0
    assert m[BLOCK].all()
    assert float(np.nanmedian(out["unc_days"][m])) > 10  # last clear obs was well before the first clear changed obs
    assert A.to_date(float(np.nanmedian(out["issue_day"][m]))) > datetime(2025, 7, 25).date()


def radar_cut(day, drop):
    return lambda a, i, w: a.__setitem__(BLOCK, a[BLOCK] - drop) if w >= day else None


def test_radar_alerts_when_optical_is_blind_and_is_suppressed_when_optical_sees_nothing():
    cut = utc(2025, 7, 20)
    rad = stack(RAD_T, -14.0, radar_cut(cut, 6.0), noise=0.5)
    blind = stack(OPT_T, 0.6, lambda a, i, w: a.__setitem__(slice(None), np.nan) if utc(2025, 6, 1) <= w <= utc(2025, 9, 30) else None, noise=0.02)
    out = A.detect(blind, rad, REF_END, REF_END, P)
    assert (out["evidence"][BLOCK] == 4).mean() > 0.9, "radar must alert through a monsoon optical blackout"
    clear = stack(OPT_T, 0.6, None, noise=0.02)  # optical clear and unchanged -> radar alone is not trusted
    out2 = A.detect(clear, rad, REF_END, REF_END, P)
    assert (out2["evidence"] == 5).sum() > 20
    assert len(A.extract_patches(out2, GRID, P)) == 0  # class 5 kept out of issued alerts by default
    assert len(A.extract_patches(out2, GRID, P, include_low=True)) == 1


def test_agreeing_optical_and_radar_is_class_1():
    cut = utc(2025, 4, 2)
    opt = stack(OPT_T, 0.6, lambda a, i, w: a.__setitem__(BLOCK, 0.15) if w >= cut else None, noise=0.02)
    rad = stack(RAD_T, -14.0, radar_cut(cut, 6.0), noise=0.5)
    out = A.detect(opt, rad, REF_END, REF_END, P)
    assert (out["evidence"][BLOCK] == 1).mean() > 0.9


def test_rain_and_speckle_noise_do_not_trigger_radar_alerts():
    rng = np.random.default_rng(3)
    wet = lambda a, i, w: a.__setitem__(slice(None), a + (1.5 if w.month in (7, 8) else 0) + rng.normal(0, 0.4, SHAPE))  # wetter canopy + noise
    rad = stack(RAD_T, -14.0, wet, noise=0.8)
    out = A.detect(stack(OPT_T, 0.6, None, 0.02), rad, REF_END, REF_END, P)
    assert (out["evidence"] > 0).sum() == 0


def test_radar_gain_in_monsoon_months_on_a_known_scenario():
    cut = utc(2025, 7, 15)

    def optical(a, i, w):
        if w >= cut:
            a[BLOCK] = 0.15
        if utc(2025, 6, 10) <= w <= utc(2025, 9, 20):
            a[:] = np.nan

    opt = stack(OPT_T, 0.6, optical, 0.02)
    rad = stack(RAD_T, -14.0, radar_cut(cut, 6.0), 0.5)
    pf = A.extract_patches(A.detect(opt, rad, REF_END, REF_END, P), GRID, P)
    po = A.extract_patches(A.detect(opt, None, REF_END, REF_END, P, use_radar=False), GRID, P)
    assert len(pf) == 1 and len(po) == 1
    assert pf[0]["issue_month"] == "2025-08" and pf[0]["monsoon"] is True  # 12-day radar revisit: first changed pass late July, confirmed early August
    assert po[0]["issue_month"] >= "2025-09", "optical alone only sees the loss when the clouds clear"
    assert pf[0]["issue_month"] < po[0]["issue_month"] and pf[0]["evidence"] == "optical_radar_agree"
    assert pf[0]["date_uncertainty_days"] <= 7 and pf[0]["est_date"] <= "2025-07-20", "radar's tight bracket must date the loss, not optical's vague gap"


def test_patch_filters_ranking_and_months():
    cut1, cut2 = utc(2025, 3, 10), utc(2025, 5, 10)
    big, small, tiny = (slice(2, 9), slice(2, 9)), (slice(14, 18), slice(14, 18)), (slice(20, 21), slice(20, 21))

    def f(a, i, w):
        if w >= cut1:
            a[big] = 0.15
        if w >= cut2:
            a[small] = 0.15
            a[tiny] = 0.15

    out = A.detect(stack(OPT_T, 0.6, f, 0.02), None, REF_END, REF_END, P, use_radar=False)
    ps = A.extract_patches(out, GRID, P)
    assert len(ps) == 2  # the 1-pixel (0.04 ha) patch is below the 0.2 ha minimum
    assert ps[0]["area_ha"] > ps[1]["area_ha"] and ps[0]["rank"] == 1 and ps[0]["score"] > ps[1]["score"]
    assert {p["issue_month"] for p in ps} == {"2025-03", "2025-05"}
    assert all(p["evidence"] == "optical" and p["geometry"]["type"] in ("Polygon", "MultiPolygon") for p in ps)


def test_params_digest_changes_with_thresholds():
    assert A.AlertParams().digest() == A.AlertParams().digest() != A.AlertParams(opt_drop=0.3).digest()


def test_confirmed_onset_requires_two_consecutive_valid_observations():
    bad = np.zeros((6, 1, 1), bool)
    valid = np.ones((6, 1, 1), bool)
    bad[2] = True  # transient
    assert A.confirmed_onset(bad, valid)[0][0, 0] == -1
    bad[3] = True
    o, c, p = A.confirmed_onset(bad, valid)
    assert (o[0, 0], c[0, 0], p[0, 0]) == (2, 3, 1)
    valid[3] = False  # next obs missing (cloud) -> the following valid obs decides
    bad2 = np.zeros((6, 1, 1), bool)
    bad2[2] = bad2[4] = True
    o, c, p = A.confirmed_onset(bad2, valid)
    assert (o[0, 0], c[0, 0]) == (2, 4)


def seasonal_forest(start, n, step, fn=None, cycle=0.18, noise=0.02, seed=5, phase_doy=45):
    """NBR with a strong annual cycle (dry-season minimum around late winter), like deciduous/semi-evergreen forest."""
    whens = dates(start, n, step)
    rng = np.random.default_rng(seed)
    out = np.empty((n, *SHAPE), dtype="float32")
    for i, w in enumerate(whens):
        doy = w.timetuple().tm_yday
        out[i] = 0.6 - cycle * np.cos(2 * np.pi * (doy - phase_doy) / 365) + rng.normal(0, noise, SHAPE)
        if fn:
            fn(out[i], i, w)
    return {"data": out, "when": whens}


def test_fixed_post_monsoon_baseline_confuses_the_dry_season_with_loss_but_the_seasonal_baseline_does_not():
    short = seasonal_forest(datetime(2024, 10, 3, tzinfo=timezone.utc), 90, 5, cycle=0.25)    # baseline = Oct-Dec only (greenest part of the cycle)
    full = seasonal_forest(datetime(2024, 1, 3, tzinfo=timezone.utc), 146, 5, cycle=0.25)     # baseline = the whole prior year
    fixed = A.detect(short, None, REF_END, REF_END, A.AlertParams(seasonal=False), use_radar=False)
    seasonal = A.detect(full, None, REF_END, REF_END, A.AlertParams(seasonal=True), use_radar=False)
    assert (fixed["evidence"] > 0).sum() > 100, "a fixed baseline taken after the monsoon must flag the dry season (demonstrates the failure mode)"
    assert (seasonal["evidence"] > 0).sum() == 0


def test_seasonal_baseline_still_finds_a_real_cut_in_the_monsoon_and_dates_it():
    cut = utc(2025, 7, 20)
    full = seasonal_forest(datetime(2024, 1, 3, tzinfo=timezone.utc), 146, 5, lambda a, i, w: a.__setitem__(BLOCK, a[BLOCK] - 0.4) if w >= cut else None)
    out = A.detect(full, None, REF_END, REF_END, A.AlertParams(seasonal=True), use_radar=False)
    m = out["evidence"] > 0
    assert m[BLOCK].all() and m.sum() == 36
    est = A.to_date(float(np.nanmedian(out["est_day"][m])))
    assert utc(2025, 7, 14).date() <= est <= cut.date() + timedelta(days=5)


def test_seasonal_radar_ignores_monsoon_moisture_swings_but_flags_a_real_drop():
    def rad(day=None):
        whens = dates(datetime(2024, 1, 5, tzinfo=timezone.utc), 61, 12)
        rng = np.random.default_rng(9)
        d = np.empty((61, *SHAPE), dtype="float32")
        for i, w in enumerate(whens):
            d[i] = -14 + 2.5 * np.sin(2 * np.pi * (w.timetuple().tm_yday - 100) / 365) + rng.normal(0, 0.4, SHAPE)  # +-2.5 dB annual wetness swing
            if day and w >= day:
                d[i][BLOCK] -= 6
        return {"data": d, "when": whens}

    quiet = A.detect(seasonal_forest(datetime(2024, 1, 3, tzinfo=timezone.utc), 146, 5), rad(), REF_END, REF_END, A.AlertParams(seasonal=True))
    assert (quiet["evidence"] > 0).sum() == 0
    real = A.detect(seasonal_forest(datetime(2024, 1, 3, tzinfo=timezone.utc), 146, 5), rad(utc(2025, 8, 1)), REF_END, REF_END, A.AlertParams(seasonal=True, include_low_confidence=True))
    assert (real["evidence"][BLOCK] > 0).mean() > 0.9
