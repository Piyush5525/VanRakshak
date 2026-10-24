"""Web search leads for government-record discovery via SerpAPI (Google).

Results are *leads for a human to open and verify on the official portal*. They are never stored as
authorization records and never feed the matching logic.
"""
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.config import Settings

OFFICIAL_DOMAINS = ("parivesh.nic.in", "forestsclearance.nic.in", "moef.gov.in", "environmentclearance.nic.in", "indiacode.nic.in", "egazette.gov.in", ".gov.in", ".nic.in")


class LeadsUnavailable(RuntimeError):
    pass


def search(settings: Settings, query: str, num: int = 8, client: httpx.Client | None = None) -> dict[str, Any]:
    if not settings.serp_api:
        raise LeadsUnavailable("SERP_API is not configured")
    q = f"({query}) (site:parivesh.nic.in OR site:forestsclearance.nic.in OR site:environmentclearance.nic.in OR site:moef.gov.in OR site:gov.in)"
    try:
        r = (client or httpx.Client(timeout=60)).get("https://serpapi.com/search.json", params={"engine": "google", "q": q, "num": min(max(num, 1), 10), "gl": "in", "api_key": settings.serp_api})
        data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LeadsUnavailable(f"Search request failed: {type(exc).__name__}") from exc
    if "error" in data:
        raise LeadsUnavailable("Search provider returned an error")
    leads = []
    for item in data.get("organic_results", []):
        host = urlparse(item.get("link", "")).hostname or ""
        leads.append({"title": item.get("title"), "url": item.get("link"), "snippet": item.get("snippet"), "host": host, "official_domain": host.endswith(OFFICIAL_DOMAINS)})
    return {"query": query, "claim_type": "unverified_web_lead", "notice": "These are search results, not records. Open each on the official portal and enter verified details through /authorizations/ingest. Absence of results proves nothing.", "leads": leads}
