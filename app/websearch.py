"""Web search fallback.

The brief leaves the missing-website case to the developer: not every
business has a homepage registered in Brreg. When the registry has no
homepage, this module searches for the company's site using the
company name and registered place.

Every URL returned here is only a search result, never a fact:
verify.py must confirm it belongs to the organisation before the page
text is used as a source.
"""
import logging

import httpx

from .config import settings
from .models import SourceStatus

logger = logging.getLogger("app.websearch")


async def search_web(client: httpx.AsyncClient, query: str) -> tuple[list[dict], SourceStatus]:
    """Search the web for the company's site. Never raises: every failure
    becomes a failed SourceStatus, so the pipeline stays predictable.

    Returns (results, status) where each result is a dict with 'url'
    and 'title'. At most settings.search_max_results URLs.
    """
    status = SourceStatus(source="websearch", url=query, status="ok")
    if settings.search_provider == "none" or not settings.search_api_key:
        status.status = "unavailable"
        status.detail = "Web search not configured"
        return [], status

    try:
        resp = await client.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": settings.search_api_key,
                     "Content-Type": "application/json"},
            json={"q": query, "num": settings.search_max_results},
            timeout=settings.request_timeout_seconds,
        )
        if resp.status_code == 401 or resp.status_code == 403:
            status.status = "failed"
            status.detail = "Search API rejected the key"
            return [], status
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException:
        status.status = "failed"
        status.detail = "Search API timed out"
        return [], status
    except Exception as exc:
        status.status = "failed"
        status.detail = f"Search API error: {exc}"
        return [], status

    results = _parse_results(data)
    if not results:
        status.status = "unavailable"
        status.detail = "No search results"
        return [], status
    status.detail = f"{len(results)} URL(s) from search"
    return results, status


def _parse_results(data: dict) -> list[dict]:
    """Pick the result links out of the search response."""
    out = []
    for item in data.get("organic", [])[:settings.search_max_results]:
        url = item.get("link") or ""
        if url:
            out.append({"url": url, "title": item.get("title", "")})
    return out
