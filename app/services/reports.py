"""Incident evidence package (JSON) and PDF report.

Every fact is placed in exactly one evidence class so a reader can tell them apart:
observed | inferred | forecast | simulation | official_records | user_assumptions | human_decisions
"""
import hashlib
import io
import json
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from PIL import Image, ImageDraw
from pyproj import Transformer
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image as RLImage, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from shapely.geometry import shape

from app.core.config import Settings
from app.db.database import audit, connection, decode_row, get_one, insert, list_rows, now

DISCLAIMER = "Decision-support output. Satellite detections are model-generated candidates; absence of a government record is not a finding of illegality; only a competent authority can determine lawfulness."


def _nbr_image(path: str, outline_geojson: dict | None, size: int = 360) -> bytes | None:
    try:
        with rasterio.open(path) as src:
            nir, swir, cloud = src.read([1, 2, 3])
            tf, crs = src.transform, src.crs
    except Exception:  # noqa: BLE001 - missing/unreadable raster: the report says so instead
        return None
    with np.errstate(divide="ignore", invalid="ignore"):
        nbr = np.where((nir + swir) > 0, (nir - swir) / (nir + swir), 0)
    t = np.clip((nbr + 0.1) / 0.7, 0, 1)
    rgb = np.stack([200 - 150 * t, 160 + 60 * t - 60 * t ** 2 * 0, 90 - 40 * t], -1).clip(0, 255).astype("uint8")
    rgb[:, :, 0] = (190 - 140 * t).astype("uint8"); rgb[:, :, 1] = (150 + 70 * t).astype("uint8"); rgb[:, :, 2] = (100 - 50 * t).astype("uint8")
    rgb[cloud > 0] = (235, 235, 235)
    img = Image.fromarray(rgb)
    if outline_geojson:
        draw = ImageDraw.Draw(img)
        to_crs = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        inv = ~tf
        geom = shape(outline_geojson)
        for part in getattr(geom, "geoms", [geom]):
            if part.geom_type != "Polygon":
                continue
            pts = [inv * to_crs.transform(x, y) for x, y in part.exterior.coords]
            draw.line(pts + [pts[0]], fill=(220, 30, 30), width=2)
    img.thumbnail((size, size), Image.NEAREST)
    buf = io.BytesIO(); img.save(buf, "PNG")
    return buf.getvalue()


def _mask_recipient(value: str) -> str:
    return value[:2] + "***" + value[-2:] if len(value) > 6 else "***"


def collect(incident_id: str) -> dict[str, Any]:
    incident = get_one("incidents", "id", incident_id)
    if not incident:
        raise ValueError("Incident not found")
    change = get_one("changes", "id", incident["change_id"]) if incident.get("change_id") else None
    region = get_one("regions", "id", change["region_id"]) if change and change.get("region_id") else None
    pkg: dict[str, Any] = {"observed": [], "inferred": [], "forecast": [], "simulation": [], "official_records": [], "user_assumptions": [], "human_decisions": []}
    obs_records = []
    if change:
        pkg["inferred"].append({"type": "forest_change_candidate", "change_id": change["id"], "area_hectares": change["area_hectares"], "score": change["score"], "score_note": "uncalibrated model score, not a probability", "baseline_date": change["baseline_date"], "comparison_date": change["comparison_date"], "disturbance_type": change["disturbance_type"], "status": change["status"], "method": change["provenance"].get("method"), "config": change["provenance"].get("config"), "radar": change["provenance"].get("radar"), "baseline_forest": change["provenance"].get("baseline_forest"), "polygon_evidence": change["provenance"].get("polygon_scores"), "data_quality": change["provenance"].get("data_quality"), "seasonal_warning": change["provenance"].get("seasonal_warning")})
        for key, meta in (change["provenance"].get("observations") or {}).items():
            rec = get_one("observations", "id", meta["id"])
            if rec:
                obs_records.append((key, rec))
                pkg["observed"].append({"role": key, "observation_id": rec["id"], "sensor": rec["provenance"].get("sensor"), "scenes": rec["provenance"].get("scenes"), "acquired_from": rec["acquisition_start"], "acquired_to": rec["acquisition_end"], "processed_at": rec["provenance"].get("processed_at"), "valid_pixel_fraction": rec["quality"].get("valid_pixel_fraction"), "processing": rec["provenance"].get("processing"), "checksum_sha256": rec["provenance"].get("checksum_sha256"), "warnings": rec["provenance"].get("warnings")})
        for a in list_rows("authorization_assessments", "WHERE change_id = ?", (change["id"],), limit=50):
            pkg["official_records"].append({"assessment_id": a["id"], "workflow_status": a["status"], "rationale": a["rationale"], "note": "Workflow state for human review, not a legal determination"})
        follow = [o for o in list_rows("observations", "WHERE region_id = ? AND status = 'OK'", (change["region_id"],), limit=200) if (o["acquisition_start"] or "") > change["comparison_date"]]
        for o in follow:
            pkg["observed"].append({"role": "follow_up", "observation_id": o["id"], "sensor": o["provenance"].get("sensor"), "acquired_from": o["acquisition_start"], "valid_pixel_fraction": o["quality"].get("valid_pixel_fraction")})
        for f in list_rows("forecasts", "WHERE region_id = ?", (change["region_id"],), limit=20):
            pkg["forecast"].append({"forecast_id": f["id"], "horizon_days": f["horizon_days"], "status": f["status"], "origin": f["origin"], "model_version": f["provenance"].get("model_version"), "evaluation_id": f["provenance"].get("evaluation_id"), "reason": f["provenance"].get("reason"), "limitations": f["provenance"].get("limitations")})
        for s in list_rows("simulations", "WHERE region_id = ?", (change["region_id"],), limit=20):
            pkg["simulation"].append({"simulation_id": s["id"], "horizon_days": s["horizon_days"], "label": "HYPOTHETICAL - not observed, not forecast", "assumptions": s["assumptions"], "fragmentation": (s["result"] or {}).get("fragmentation")})
    for im in list_rows("impact_assessments", "WHERE incident_id = ?", (incident_id,), limit=5):
        pkg["inferred"].append({"type": "impact_assessment", "status": im["status"], "metrics": (im["result"] or {}).get("metrics"), "assumptions": (im["result"] or {}).get("assumptions")})
    if incident.get("wildfire_id"):
        w = get_one("wildfire_observations", "id", incident["wildfire_id"])
        if w:
            pkg["observed" if w["observation_type"] == "active_fire" else "inferred"].append({"type": w["observation_type"], "source": w["source"], "observed_at": w["observed_at"], "confidence": w["confidence"], "review_status": w["status"]})
    ev0 = incident["evidence"] or {}
    if ev0.get("kind") or ev0.get("note"):
        pkg["user_assumptions"].append({"type": "incident_report_by_person", "reported_kind": ev0.get("kind"), "note": ev0.get("note"), "source": ev0.get("source"), "label": "As entered by the person who marked this incident; not verified"})
    if ev0.get("site"):
        st = ev0["site"]
        pkg["user_assumptions"].append({"type": "industrial_site", "name": st.get("name"), "kind": st.get("kind"), "note": st.get("note"), "label": "Entered by a user; not verified; does not show that this site caused anything", "entered_by_role": st.get("entered_by_role"), "entered_at": st.get("entered_at")})
        for em in st.get("emissions", []):
            pkg["user_assumptions"].append({"type": "reported_emission_figure", "pollutant": em["pollutant"], "value": f"{em['value']} {em['unit']}", "source": em["source"], "as_of": em.get("as_of"), "label": "Figure as entered with the stated source; not measured or verified by this system"})
    with connection() as conn:
        events = [decode_row(dict(r)) for r in conn.execute("SELECT * FROM audit_events WHERE resource_type='incident' AND resource_id = ? ORDER BY id", (incident_id,))]
    for e in events:
        pkg["human_decisions"].append({"at": e["created_at"], "actor_role": e["actor_role"], "action": e["action"], "details": e["details"]})
    notes = []
    nid = (incident["evidence"] or {}).get("notification_id")
    if nid and (n := get_one("notifications", "id", nid)):
        notes.append({"notification_id": n["id"], "channel": n["channel"], "recipient": _mask_recipient(n["recipient"]), "delivery_status": n["status"], "created_at": n["created_at"]})
    if (fv := (incident["evidence"] or {}).get("field_verification")):
        pkg["human_decisions"].append({"action": "field_verification", "details": fv})
    return {"incident": {k: incident[k] for k in ("id", "title", "status", "geometry", "created_at", "updated_at")}, "assignee": (incident["evidence"] or {}).get("assignee"), "evidence": pkg, "notifications": notes, "_obs": obs_records, "_change": change}


def build_report(incident_id: str, settings: Settings, actor: str) -> dict:
    data = collect(incident_id)
    obs, change = data.pop("_obs"), data.pop("_change")
    report_id, pdf_id = str(uuid.uuid4()), str(uuid.uuid4())
    out = Path(settings.report_output_dir)
    out.mkdir(parents=True, exist_ok=True)
    package = {"report_id": report_id, "generated_at": now(), "generated_by_role": actor, "disclaimer": DISCLAIMER, **data, "limitations": ["Detections are rule-based candidates (dNBR with Sentinel-1 support), not a validated classifier", "Candidate loss is not distinguished from fire, seasonal change or other conversion without review", "Government-record search covers only records ingested into this system"]}
    body = json.dumps(package, indent=2, default=str)
    package_sha = hashlib.sha256(body.encode()).hexdigest()
    json_path = out / f"{report_id}.json"
    json_path.write_text(body, encoding="utf-8")

    styles = getSampleStyleSheet()
    story: list[Any] = [Paragraph("VanaNetra - Incident evidence report", styles["Title"]), Paragraph(DISCLAIMER, styles["Italic"]), Spacer(1, 6 * mm)]
    inc = data["incident"]
    story += [Paragraph(f"Incident {inc['id']} - {inc['title']}", styles["Heading2"]), Paragraph(f"Status: <b>{inc['status']}</b> | Created {inc['created_at']} | Assignee: {data['assignee'] or 'none'}", styles["Normal"])]
    if change:
        imgs = []
        for key in ("before_s2", "after_s2"):
            rec = next((r for k, r in obs if k == key), None)
            png = _nbr_image(rec["provenance"]["file"], change["geometry"]) if rec and rec["provenance"].get("file") else None
            imgs.append(RLImage(io.BytesIO(png), width=80 * mm, height=80 * mm * 0.9, kind="proportional") if png else Paragraph("Imagery file unavailable", styles["Normal"]))
        story += [Spacer(1, 4 * mm), Paragraph("Before / after (Sentinel-2 NBR, red outline = candidate change; grey = no clear observation)", styles["Heading3"]), Table([imgs, [Paragraph(f"Baseline {change['baseline_date'][:10]}", styles["Normal"]), Paragraph(f"Comparison {change['comparison_date'][:10]}", styles["Normal"])]])]
    labels = {"observed": "Observations (satellite / sensor)", "inferred": "Model inferences (candidates, not facts)", "forecast": "Forecasts", "simulation": "Simulations (hypothetical)", "official_records": "Government-record searches", "user_assumptions": "User assumptions", "human_decisions": "Human decisions and audit trail"}
    for key, title in labels.items():
        items = data["evidence"][key]
        story.append(Paragraph(title, styles["Heading3"]))
        if not items:
            story.append(Paragraph("None recorded.", styles["Italic"]))
        for it in items:
            rows = [[Paragraph(f"<b>{k}</b>", styles["BodyText"]), Paragraph(str(v)[:600].replace("<", "&lt;"), styles["BodyText"])] for k, v in it.items() if v not in (None, [], {})]
            if rows:
                t = Table(rows, colWidths=[40 * mm, 125 * mm])
                t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
                story += [t, Spacer(1, 2 * mm)]
    if data["notifications"]:
        story.append(Paragraph("Notifications (delivery status is provider-reported only)", styles["Heading3"]))
        story += [Paragraph(str(n), styles["BodyText"]) for n in data["notifications"]]
    story += [Paragraph("Limitations", styles["Heading3"])] + [Paragraph("- " + l, styles["BodyText"]) for l in package["limitations"]]
    story.append(Paragraph(f"Evidence package SHA-256: {package_sha}", styles["Code"]))
    pdf_path = out / f"{pdf_id}.pdf"
    SimpleDocTemplate(str(pdf_path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm).build(story)

    insert("reports", {"id": report_id, "incident_id": incident_id, "format": "json", "path": str(json_path), "created_at": now()})
    insert("reports", {"id": pdf_id, "incident_id": incident_id, "format": "pdf", "path": str(pdf_path), "created_at": now()})
    audit(actor, "report_generated", "incident", incident_id, {"json_id": report_id, "pdf_id": pdf_id, "package_sha256": package_sha})
    return {"id": report_id, "pdf_id": pdf_id, "incident_id": incident_id, "package_sha256": package_sha, "artifacts": [{"format": "json", "id": report_id}, {"format": "pdf", "id": pdf_id}], "status": "generated"}


GREEN, DARK, SOFT, LINE = colors.HexColor("#0f7a4a"), colors.HexColor("#123524"), colors.HexColor("#eef5f0"), colors.HexColor("#d5e0d9")


def _flat(v: Any) -> str:
    """Show nested data as a short readable line instead of a Python dict."""
    if isinstance(v, dict):
        return "; ".join(f"{k}: {_flat(x)}" for k, x in v.items() if x not in (None, "", [], {}))
    if isinstance(v, (list, tuple)):
        return ", ".join(_flat(x) for x in v)
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


def _static_map(lat: float, lon: float, zoom: int = 15, w: int = 640, h: int = 340) -> bytes | None:
    """Satellite picture of the place with a pin (Mapbox static images). None when there is no key or the request fails."""
    from app.core.config import get_settings
    import httpx

    token = get_settings().mapbox_api
    if not token:
        return None
    try:
        r = httpx.get(f"https://api.mapbox.com/styles/v1/mapbox/satellite-streets-v12/static/pin-l+d7263d({lon},{lat})/{lon},{lat},{zoom},0/{w}x{h}@2x?access_token={token}", timeout=30)
        return r.content if r.status_code == 200 and r.headers.get("content-type", "").startswith("image") else None
    except httpx.HTTPError:
        return None


def generic_pdf(title: str, subtitle: str, blocks: list[tuple[str, Any]], summary: list[tuple[str, str]] | None = None, links: list[tuple[str, str]] | None = None, image: bytes | None = None, caption: str | None = None, images: list[tuple[bytes, str]] | None = None) -> bytes:
    """Branded A4 report: header band, key facts at a glance, optional map, clear sections, links that open, page numbers. Used by every report."""
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import KeepTogether

    base = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=base["BodyText"], fontSize=9.2, leading=12.6, textColor=DARK)
    small = ParagraphStyle("s", parent=body, fontSize=8, leading=10.5, textColor=colors.HexColor("#5b6b62"))
    h2 = ParagraphStyle("h2", parent=body, fontName="Helvetica-Bold", fontSize=11.5, leading=14, textColor=GREEN, spaceBefore=9, spaceAfter=4)
    esc = lambda v: "".join(ch for ch in _flat(v)[:1200] if ord(ch) < 0x2000 or ch in "•–—‘’“”…").strip().replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731

    def para(v, st=body):
        try:
            return Paragraph(_linkify(esc(v)), st)
        except ValueError:   # odd characters in the data must never stop the report
            return Paragraph(esc(v).replace("&", "").replace(";", "")[:1200], st)
    W = A4[0] - 36 * mm
    head = Table([[Paragraph(f'<font color="white" size="9"><b>VANRAKSHAK</b> &nbsp; forest intelligence &amp; response</font>', body)],
                  [Paragraph(f'<font color="white" size="18"><b>{esc(title)}</b></font>', ParagraphStyle("t", parent=body, leading=22))],
                  [Paragraph(f'<font color="#cfeedd" size="9.5">{esc(subtitle)}</font>', body)]], colWidths=[W])
    head.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), GREEN), ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12), ("TOPPADDING", (0, 0), (0, 0), 9), ("BOTTOMPADDING", (0, -1), (-1, -1), 10)]))
    story: list[Any] = [head, Spacer(1, 4 * mm)]
    if summary:   # key facts as cards, up to four across
        cells = [[Paragraph(f'<font size="14"><b>{esc(v)}</b></font><br/><font size="7.5" color="#5b6b62">{esc(k).upper()}</font>', ParagraphStyle("c", parent=body, leading=15, alignment=1)) for k, v in summary[:4]]]
        t = Table(cells, colWidths=[W / len(cells[0])] * len(cells[0]))
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), SOFT), ("BOX", (0, 0), (-1, -1), 0.5, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.white), ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
        story += [t, Spacer(1, 3 * mm)]
    for image, caption in ([(image, caption)] if image else []) + list(images or []):
        try:
            im = Image.open(io.BytesIO(image))
            iw = W
            story += [RLImage(io.BytesIO(image), width=iw, height=iw * im.height / im.width)]
            if caption:
                story.append(para(caption, small))
            story.append(Spacer(1, 2 * mm))
        except Exception:  # noqa: BLE001 - a bad picture must not stop the report
            pass
    if links:
        story.append(Paragraph("   ".join(f'<link href="{esc(u)}" color="#0f7a4a"><u>{esc(lbl)}</u></link>' for lbl, u in links), body))
    for heading, content in blocks:
        part: list[Any] = [Paragraph(esc(heading), h2)]
        if isinstance(content, dict):
            rows = [[para(k, ParagraphStyle("k", parent=body, fontName="Helvetica-Bold")), para(v)] for k, v in content.items() if v not in (None, "", [], {})]
            if rows:
                t = Table(rows, colWidths=[W * 0.31, W * 0.69])
                t.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, -1), SOFT), ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
                part.append(t)
            else:
                part.append(para("Nothing recorded.", small))
        elif isinstance(content, list):
            part += [para("•  " + _flat(x)) for x in content] or [para("None.", small)]
        else:
            part.append(para(content))
        story.append(KeepTogether(part[:3]) if len(part) > 3 else KeepTogether(part))
        story += part[3:]
    story += [Spacer(1, 5 * mm), para(DISCLAIMER, small)]

    def furniture(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.line(18 * mm, 12 * mm, A4[0] - 18 * mm, 12 * mm)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#5b6b62"))
        canvas.drawString(18 * mm, 8 * mm, f"VanRakshak · generated {now()[:16].replace('T', ' ')} UTC")
        canvas.drawRightString(A4[0] - 18 * mm, 8 * mm, f"Page {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=14 * mm, bottomMargin=17 * mm, title=title, author="VanRakshak").build(story, onFirstPage=furniture, onLaterPages=furniture)
    return buf.getvalue()


def _linkify(text: str) -> str:
    import re

    return re.sub(r"(https?://[^\s<\"'\]\)\}]+)", lambda m: f'<link href="{m.group(1)}" color="#0f7a4a"><u>open link</u></link>', text)


def to_csv(rows: list[dict[str, Any]], columns: list[str]) -> str:
    import csv

    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("'" + str(v) if isinstance(v, str) and v[:1] in "=+-@" else v) for k, v in r.items()})  # neutralise spreadsheet formula injection
    return out.getvalue()
