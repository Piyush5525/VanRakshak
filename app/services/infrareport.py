"""PDF reports for infrastructure change: one for a single change (before / after, impact, evidence) and one for a whole check."""
from typing import Any

from app.services import geoinfo, infra, reports

METHOD = ("Four cloud-masked Sentinel-2 composites of the same place (a year ago, now, and two in between) are compared. Ground counts as changed only if it was green "
          "or dark soil a year ago and stayed non-green or brighter in every later image, so a harvested field that turns green again is not reported. "
          "Classes come from OpenStreetMap where it has the feature, otherwise from the shape and colour of the patch, and the report says which.")
LIMITS = ("Sentinel-2 pixels are 10 m (20 m for large areas), so narrow roads and single houses can be missed. Work that replaces older hard surface shows little change. "
          "A patch is a satellite detection and not a survey or a finding that anything is unlawful: check it on the ground.")


def _num(v: Any, unit: str = "") -> str | None:
    return None if v is None else f"{v}{unit}"


def _near(v: dict[str, Any] | None) -> str | None:
    if not v:
        return None
    return f"{v['distance_m']:,} m" + (f" ({v['name']})" if v.get("name") else "")


def change_pdf(change: dict[str, Any], run: dict[str, Any]) -> bytes:
    c, imp = change, change["impact"] or {}
    ev = c["evidence"] or {}
    cls = infra.CLASSES[c["cls"]]
    pl = geoinfo.place(c["lat"], c["lon"])
    summ = (run.get("result") or {}).get("summary", {})
    carbon = imp.get("carbon_t_co2")
    blocks: list[tuple[str, Any]] = []
    cmp_ = infra.compare_image(c, c["run_id"], run)
    if cmp_:
        blocks.append(("Before and after", cmp_))
    size = f"{c['area_ha']} ha" + (f", about {ev.get('length_m')} m long and {ev.get('width_m')} m wide" if ev.get("length_m") else "")
    blocks.append(("What was found", {"type": cls["label"], "how it was classified": c["cls_source"], "name on the map": c["name"], "location": (pl["name"] + " · " if pl else "") + f"{c['lat']:.5f}, {c['lon']:.5f}", "size": size,
                                      "seen as": ev.get("detected_as"), "compared": f"{summ.get('before')} and {summ.get('after')} (Sentinel-2, {ev.get('resolution_m')} m)"}))
    blocks.append(("Measured impact", {"vegetation lost": _num(imp.get("vegetation_lost_ha"), " ha"), "greenness (NDVI)": f"{imp.get('ndvi_before')} → {imp.get('ndvi_after')}", "tree cover lost": _num(imp.get("tree_cover_lost_ha"), " ha (ESA WorldCover 2021)"),
                                        "carbon in that tree cover": (f"about {carbon['central']:,} t CO2 (range {carbon['low']:,} to {carbon['high']:,}; assumed density)" if carbon else None), "hard surface added": _num(imp.get("hard_surface_added_ha"), " ha"),
                                        "nearest water": _near(imp.get("nearest_water")), "nearest forest or wood": _near(imp.get("nearest_forest"))}))
    if imp.get("land_before"):
        blocks.append(("What the land was (ESA WorldCover 2021)", {k: f"{v} ha" for k, v in imp["land_before"].items()}))
    ef = imp.get("effects") or {}
    blocks.append(("What the images show", [x["text"] for x in ef.get("measured", [])] or ["Nothing listed."]))
    blocks.append(("Typical effects of this kind of change (general knowledge, not measured here)", [x["text"] for x in ef.get("typical", [])] or ["None listed."]))
    blocks.append(("How it was detected", METHOD))
    blocks.append(("Limits", LIMITS))
    return reports.generic_pdf(f"Change {c['code']}: {cls['label']}", (pl["name"] + " · " if pl else "") + f"{c['area_ha']} ha · satellite-detected, last 365 days", blocks,
                               summary=[("Size", f"{c['area_ha']} ha"), ("Type", cls["label"]), ("Vegetation lost", f"{imp.get('vegetation_lost_ha', 0)} ha"), ("Status", "Not a change" if c["status"] == "NOT_A_CHANGE" else "Open")],
                               links=[("Open in Google Maps", f"https://www.google.com/maps?q={c['lat']},{c['lon']}"), ("Satellite view", f"https://www.google.com/maps/@{c['lat']},{c['lon']},300m/data=!3m1!1e3")])


def run_pdf(run: dict[str, Any], changes: list[dict[str, Any]], top: int = 40) -> bytes:
    summ = (run.get("result") or {}).get("summary", {})
    live = [c for c in changes if c["status"] != "NOT_A_CHANGE"]
    blocks: list[tuple[str, Any]] = []
    ov = infra.overview_image(run["id"], live)
    if ov:
        blocks.append(("All changes on the satellite image of today", ov))
    by = summ.get("by_class", {})
    blocks.append(("By type", {infra.CLASSES[k]["label"]: f"{v['n']} change(s), {v['ha']} ha" for k, v in sorted(by.items(), key=lambda x: -x[1]["ha"])}))
    blocks.append(("Totals", {"area checked": f"{summ.get('area_km2')} km² at {summ.get('resolution_m')} m", "compared": f"{summ.get('before')} and {summ.get('after')}", "vegetation lost": f"{summ.get('vegetation_lost_ha')} ha",
                              "tree cover lost (WorldCover 2021)": f"{summ.get('tree_cover_lost_ha')} ha", "hard surface added": f"{summ.get('hard_surface_ha')} ha", "OpenStreetMap": summ.get("osm")}))
    for c in live[:top]:
        imp = c["impact"] or {}
        head = f"{c['code']} · {infra.CLASSES[c['cls']]['label']} · {c['area_ha']} ha"
        img = infra.compare_image(c, run["id"], run)
        body: dict[str, Any] = {"location": f"{c['lat']:.5f}, {c['lon']:.5f}", "classified from": c["cls_source"], "name": c["name"], "vegetation lost": f"{imp.get('vegetation_lost_ha', 0)} ha",
                                "hard surface added": f"{imp.get('hard_surface_added_ha', 0)} ha", "map": f"https://www.google.com/maps?q={c['lat']},{c['lon']}"}
        if img:
            blocks.append((head, img))
            blocks.append(("Details", body))
        else:
            blocks.append((head, body))
    if len(live) > top:
        blocks.append(("Not shown", f"{len(live) - top} smaller changes are in the app and in the GeoJSON export."))
    blocks.append(("How it was detected", METHOD))
    blocks.append(("Limits", LIMITS))
    return reports.generic_pdf(f"Infrastructure change: {run.get('name') or 'checked area'}", f"{summ.get('before')} to {summ.get('after')} · {len(live)} changes · Sentinel-2", blocks,
                               summary=[("Changes", str(len(live))), ("Area changed", f"{summ.get('total_ha')} ha"), ("Vegetation lost", f"{summ.get('vegetation_lost_ha')} ha"), ("Hard surface", f"{summ.get('hard_surface_ha')} ha")])
