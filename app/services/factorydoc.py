"""Read a factory document (project proposal, EIA summary, permit, brochure) and pull out the facts the impact check needs.

Text is read from the PDF (no OCR: a scanned PDF with no text layer is reported as unreadable). Fields are found by plain
patterns, so each one comes back with the page and the sentence it came from and must be checked by a person before use.
Nothing is guessed silently: a field that is not found is simply absent.
"""
import re
from typing import Any

import pymupdf as fitz   # PyMuPDF

MAX_PAGES = 120
MAX_BYTES = 25 * 1024 * 1024
KEYWORDS = {
    "cement": ["cement", "clinker", "grinding unit", "limestone crusher"], "steel": ["steel", "sponge iron", "blast furnace", "rolling mill", "ferro alloy", "induction furnace"],
    "thermal_power": ["thermal power", "coal-based power", "coal based power", "boiler", "turbo generator", "mw power plant"], "chemical": ["chemical", "pharmaceutical", "api manufacturing", "pesticide", "fertili", "petrochemical", "solvent"],
    "textile": ["textile", "dyeing", "spinning mill", "denim", "garment wash"], "paper": ["paper mill", "pulp", "kraft"], "food": ["distillery", "sugar", "ethanol", "food processing", "dairy", "rice mill", "oil mill", "agro"],
    "mining": ["mining", "quarry", "mine lease", "opencast", "bauxite", "iron ore", "stone crusher"],
}
POLLUTANTS = r"(SO\s?2|SO₂|NO\s?x|NOx|PM\s?10|PM\s?2\.5|PM|SPM|CO|HCl|HF|Hg)"
UNITS = r"(mg\s?/\s?Nm\s?[3³]|mg/Nm3|µg\s?/\s?m\s?[3³]|ug/m3|mg\s?/\s?[lL]|g/s|kg/h|kg/hr|tpd)"
ACRE_HA = 0.404686


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _snip(text: str, start: int, end: int) -> str:
    a, b = max(0, start - 70), min(len(text), end + 70)
    return re.sub(r"\s+", " ", text[a:b]).strip()


def read_pdf(data: bytes) -> tuple[list[str], list[str]]:
    """(page texts, warnings)."""
    if len(data) > MAX_BYTES:
        raise ValueError(f"The PDF is larger than {MAX_BYTES // 1024 // 1024} MB")
    if not data[:5] == b"%PDF-":
        raise ValueError("That file is not a PDF")
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"The PDF could not be opened ({type(exc).__name__})") from exc
    if doc.needs_pass:
        raise ValueError("The PDF is password-protected")
    warnings = []
    pages = [doc[i].get_text() for i in range(min(len(doc), MAX_PAGES))]
    if len(doc) > MAX_PAGES:
        warnings.append(f"Only the first {MAX_PAGES} of {len(doc)} pages were read.")
    if sum(len(p.strip()) for p in pages) < 200:
        warnings.append("This PDF has almost no readable text (it may be scanned images). Enter the details by hand, or use a text version.")
    return pages, warnings


def _find(pattern: str, pages: list[str], flags=re.I | re.S):
    for i, t in enumerate(pages, 1):
        m = re.search(pattern, t, flags)
        if m:
            return i, m, t
    return None


def extract(pages: list[str]) -> dict[str, Any]:
    full = "\n".join(pages)
    fields: dict[str, Any] = {}

    def put(key: str, value: Any, page: int, text: str, m: re.Match | None, note: str | None = None):
        fields[key] = {"value": value, "page": page, "snippet": _snip(text, m.start(), m.end()) if m else None, **({"note": note} if note else {})}

    # name
    hit = _find(r"(?:project|proposed|name of (?:the )?(?:project|unit|company)|M/s\.?)\s*[:\-]?\s*([A-Z][^\n]{4,90})", pages, re.I)
    if hit:
        put("name", re.sub(r"\s+", " ", hit[1].group(1)).strip(" .:-"), hit[0], hit[2], hit[1])
    else:
        first = next((ln.strip() for ln in (pages[0].splitlines() if pages else []) if 6 <= len(ln.strip()) <= 90), None)
        if first:
            fields["name"] = {"value": first, "page": 1, "snippet": first, "note": "First line of the document; check it."}
    # type
    low = full.lower()
    score = {k: sum(low.count(w) for w in ws) for k, ws in KEYWORDS.items()}
    best = max(score, key=score.get) if score else None
    if best and score[best] >= 2:
        fields["kind"] = {"value": best, "page": None, "snippet": f"Mentions: {', '.join(w for w in KEYWORDS[best] if w in low)} ({score[best]} times)"}
    # land
    for pat, unit_to_ha, label in ((r"(?:total\s+)?(?:project\s+)?(?:plot|land|site|project)\s+area[^.\n]{0,40}?(\d[\d,\.]*)\s*(hectares?|ha\b|acres?|sq\.?\s?m(?:etres?|eters?)?)", None, "footprint_ha"),):
        hit = _find(pat, pages)
        if hit:
            v, u = _num(hit[1].group(1)), hit[1].group(2).lower()
            ha = v * ACRE_HA if u.startswith("acre") else v / 1e4 if u.startswith("sq") else v
            put(label, round(ha, 2), hit[0], hit[2], hit[1], f"Read as {hit[1].group(1)} {u}")
    hit = _find(r"forest\s+land[^.\n]{0,60}?(\d[\d,\.]*)\s*(hectares?|ha\b|acres?)", pages)
    if hit:
        v = _num(hit[1].group(1))
        put("forest_land_ha", round(v * ACRE_HA if hit[1].group(2).lower().startswith("acre") else v, 2), hit[0], hit[2], hit[1], "The document mentions forest land of this size")
    # stack, water, capacity
    hit = _find(r"(?:stack|chimney)[^.\n]{0,50}?(\d{2,3}(?:\.\d+)?)\s*(?:m\b|metres?|meters?)", pages)
    if hit:
        put("stack_height_m", _num(hit[1].group(1)), hit[0], hit[2], hit[1])
    hit = _find(r"(?:water\s+(?:requirement|consumption|demand)|fresh\s+water)[^.\n]{0,60}?(\d[\d,\.]*)\s*(KLD|m3/day|m³/day|MLD|kl/day)", pages)
    if hit:
        v = _num(hit[1].group(1)); u = hit[1].group(2).upper()
        put("water_kld", round(v * 1000 if u == "MLD" else v, 1), hit[0], hit[2], hit[1], f"Read as {hit[1].group(1)} {u}")
    caps = []
    for i, t in enumerate(pages, 1):
        for m in re.finditer(r"(\d[\d,\.]*)\s*(MTPA|TPD|TPA|MW|MLD|KLD|KTPA)\b", t):
            caps.append({"text": f"{m.group(1)} {m.group(2)}", "page": i})
            if len(caps) >= 6:
                break
    if caps:
        fields["capacity"] = {"value": "; ".join(dict.fromkeys(c["text"] for c in caps)), "page": caps[0]["page"], "snippet": None}
    # emissions
    ems = []
    for i, t in enumerate(pages, 1):
        for m in re.finditer(POLLUTANTS + r"[^\d\n]{0,40}?(\d+(?:\.\d+)?)\s*" + UNITS, t, re.I):
            ems.append({"pollutant": re.sub(r"\s", "", m.group(1)).replace("SO₂", "SO2"), "value": float(m.group(2)), "unit": re.sub(r"\s", "", m.group(3)).replace("³", "3"), "source": f"Document, page {i}", "page": i, "snippet": _snip(t, m.start(), m.end())})
            if len(ems) >= 8:
                break
    if ems:
        fields["emissions"] = {"value": ems, "page": ems[0]["page"], "snippet": None, "note": "Figures quoted in the document: they may be limits, design values or measurements; check which."}
    # coordinates
    for pat in (r"(\d{1,2}\.\d{3,})\s*°?\s*N[,;\s/]+(?:Long(?:itude)?\.?\s*[:\-]?\s*)?(\d{2,3}\.\d{3,})\s*°?\s*E", r"lat(?:itude)?\.?\s*[:\-]?\s*(\d{1,2}\.\d{3,})[^\d]{1,20}lon(?:g|gitude)?\.?\s*[:\-]?\s*(\d{2,3}\.\d{3,})"):
        hit = _find(pat, pages)
        if hit:
            put("coordinates", {"lat": float(hit[1].group(1)), "lon": float(hit[1].group(2))}, hit[0], hit[2], hit[1])
            break
    else:
        hit = _find(r"(\d{1,2})\s*°\s*(\d{1,2})\s*[′']\s*(\d{1,2}(?:\.\d+)?)\s*[″\"]?\s*N[,;\s]+(\d{2,3})\s*°\s*(\d{1,2})\s*[′']\s*(\d{1,2}(?:\.\d+)?)\s*[″\"]?\s*E", pages)
        if hit:
            g = [float(x) for x in hit[1].groups()]
            put("coordinates", {"lat": round(g[0] + g[1] / 60 + g[2] / 3600, 6), "lon": round(g[3] + g[4] / 60 + g[5] / 3600, 6)}, hit[0], hit[2], hit[1], "Converted from degrees, minutes, seconds")
    # place
    place = {}
    for key, pat in (("village", r"village\s*[:\-]?\s*([A-Z][A-Za-z ]{2,30})"), ("tehsil", r"(?:tehsil|taluka?|block)\s*[:\-]?\s*([A-Z][A-Za-z ]{2,30})"), ("district", r"district\s*[:\-]?\s*([A-Z][A-Za-z ]{2,30})"), ("state", r"state\s*[:\-]?\s*([A-Z][A-Za-z ]{2,30})")):
        hit = _find(pat, pages, re.I | re.S)
        if hit:
            place[key] = re.split(r"\s{2,}|\n", hit[1].group(1))[0].strip()
    if place:
        fields["place"] = {"value": place, "page": None, "snippet": ", ".join(f"{k} {v}" for k, v in place.items())}
    # what the document says it has
    mentions = [m for m, pat in (("Environmental Clearance", r"environmental\s+clearance"), ("Consent to Establish", r"consent\s+to\s+establish"), ("Consent to Operate", r"consent\s+to\s+operate"), ("Forest clearance", r"forest\s+(?:clearance|diversion|land)"), ("Wildlife / eco-sensitive zone", r"wild\s?life|eco-?sensitive|national park|sanctuary|tiger reserve"), ("Public hearing", r"public\s+hearing")) if re.search(pat, full, re.I)]
    return {"fields": fields, "mentions": mentions, "pages_read": len(pages), "characters": len(full)}
