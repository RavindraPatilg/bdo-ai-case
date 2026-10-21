"""Verification for URLs found through web search.

A URL from a search API is only a search result, never a fact.
Before its page text may be used as a source, this module checks the
URL against the facts we already trust (the Brreg registry facts):
company name, registered place, and - the strongest proof - the
organisation number itself when the company publishes it.

Rule: at least the company name must match, plus one more signal
(place or orgnr). A URL that fails stays 'unavailable' and the
analysis continues with registry facts alone - exactly as before.
"""
import logging

import httpx

from .models import Fact

logger = logging.getLogger("app.verify")


def _as_text(value: str) -> str:
    return " ".join(str(value or "").lower().split())


def _fact(facts: list[Fact], fact_id: str) -> str:
    return _as_text(next((f.value for f in facts if f.fact_id == fact_id), ""))


def verify_text(url: str, text: str, facts: list[Fact]) -> tuple[bool, list[str], str]:
    """Check already-fetched page text against the trusted facts.

    Same rule for every source: the company name must match, plus one
    more signal (registered place or the organisation number itself).
    """
    haystack = _as_text(text) + " " + _as_text(url)
    passed: list[str] = []

    name = _fact(facts, "brreg:name")
    place = _fact(facts, "brreg:place")
    orgnr = _fact(facts, "brreg:orgnr") or ""

    if name and name.lower() in haystack:
        passed.append("company name")
    if place and place.lower() in haystack:
        passed.append("registered place")
    if orgnr and orgnr in _as_text(text).replace(" ", ""):
        passed.append("organisation number")

    verified = "company name" in passed and len(passed) >= 2
    if not verified:
        reason = "Did not match the trusted facts (checked: name, place, orgnr)"
        logger.info("websearch_verify_failed url=%s passed=%s", url, passed)
        return False, passed, reason
    logger.info("websearch_verify_ok url=%s passed=%s", url, passed)
    return True, passed, ""


async def verify_url(client: httpx.AsyncClient, url: str, facts: list[Fact]) -> tuple[bool, list[str], str]:
    """Fetch the URL's page and check it against the trusted facts.

    Returns (verified, passed_checks, fail_reason). The page text is
    fetched through the same capped extraction used for registered
    websites, so the memory guarantees stay identical.
    """
    from .sources import fetch_page_text           # local import: avoids a circular import

    text, status = await fetch_page_text(client, url)
    if status.status != "ok" or not text:
        return False, [], f"Page not readable ({status.detail or status.status})"
    return verify_text(url, text, facts)

