"""External data fetching: Brreg facts and page text.

Every call is wrapped so failures are predictable, and every result
carries its source URL and retrieval timestamp. The source-selection
ladder lives at the bottom of this file.
"""
import logging
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from .config import settings
from .models import Fact, SourceStatus
from .verify import verify_text, verify_url
from .websearch import search_web

logger = logging.getLogger("app.sources")

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fact(fid: str, label: str, value: str, url: str) -> Fact:
    return Fact(fact_id=fid, source="brreg", source_url=url,
                label=label, value=value, retrieved_at=_now())


async def fetch_brreg(client: httpx.AsyncClient, orgnr: str) -> tuple[list[Fact], SourceStatus]:
    """Fetch base facts from Enhetsregisteret. Never raises: failures come
    back as a failed SourceStatus and an empty fact list."""
    url = f"{settings.brreg_base_url}/enheter/{orgnr}"
    status = SourceStatus(source="brreg", url=url, status="ok")
    try:
        resp = await client.get(url, headers={"Accept": "application/json"},
                                timeout=settings.request_timeout_seconds)
        if resp.status_code == 404:
            status.status = "unavailable"
            status.detail = "Unknown organisation number"
            return [], status
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException:
        status.status = "failed"
        status.detail = "Brreg API timed out"
    except Exception as exc:
        status.status = "failed"
        status.detail = f"Brreg API error: {exc}"
    if status.status != "ok":
        logger.warning("brreg_failed orgnr=%s detail=%s", orgnr, status.detail)
        return [], status

    facts = [_fact("brreg:orgnr", "Organisation number",
                   str(data.get("organisasjonsnummer") or orgnr), url)]
    if data.get("navn"):
        facts.append(_fact("brreg:name", "Company name", data["navn"], url))
    nace = data.get("naeringskode1") or {}
    if nace.get("kode") or nace.get("beskrivelse"):
        facts.append(_fact("brreg:nace", "Industry (NACE code)",
                           f"{nace.get('kode', '')} {nace.get('beskrivelse', '')}".strip(), url))
    orgform = (data.get("organisasjonsform") or {}).get("beskrivelse")
    if orgform:
        facts.append(_fact("brreg:orgform", "Organisation form", orgform, url))
    poststed = (data.get("forretningsadresse") or {}).get("poststed")
    if poststed:
        facts.append(_fact("brreg:place", "Registered place", poststed, url))
    facts.append(_fact("brreg:website", "Registered website",
                       data.get("hjemmeside") or "(none registered)", url))
    if data.get("antallAnsatte") is not None:
        facts.append(_fact("brreg:employees", "Registered employees",
                           str(data["antallAnsatte"]), url))
    return facts, status


_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_FIREFOX_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0"


async def _fetch_html(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """GET a page and return its HTML, streamed with a hard byte cap (a
    page is content we do not control, so it must not exhaust memory).
    Some sites block non-browser clients with 403; retry once with an
    alternate browser identity. Returns (html, error), error empty on
    success. Never raises."""
    last_error = ""
    for user_agent in (_BROWSER_UA, _FIREFOX_UA):
        try:
            chunks, received = [], 0
            async with client.stream("GET", url,
                                     timeout=settings.request_timeout_seconds,
                                     follow_redirects=True,
                                     headers={"User-Agent": user_agent,
                                              "Accept": "text/html",
                                              "Accept-Language": "en"}) as resp:
                if resp.status_code == 403:
                    last_error = "Page blocked the request (403)"
                    logger.info("page_blocked_403 url=%s", url)
                    continue
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    received += len(chunk)
                    if received > 2_000_000:
                        return "", "Page response too large"
                    chunks.append(chunk)
                charset = resp.charset_encoding or "utf-8"
            return b"".join(chunks).decode(charset, errors="replace"), ""
        except httpx.TimeoutException:
            return "", "Page timed out"
        except Exception as exc:
            last_error = f"Page error: {exc}"
    return "", last_error


def _extract_text(html: str, limit: int = 4000) -> str:
    """Readable text from a page: scripts, styles and navigation removed,
    capped at `limit` words (no full crawler by design)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "svg"]):
        tag.decompose()
    return " ".join(" ".join(soup.get_text(" ").split()).split()[:limit])


async def fetch_page_text(client: httpx.AsyncClient, page_url: str,
                          missing_detail: str = "No URL to fetch") -> tuple[str, SourceStatus]:
    """Fetch one page and extract its readable text. Used for the
    registered homepage and search-result URLs alike. A missing or
    failing page is reported in the status, never raised."""
    status = SourceStatus(source="website", url=page_url or "(none)", status="ok")
    if not page_url or not page_url.startswith(("http://", "https://")):
        status.status = "unavailable"
        status.detail = missing_detail
        return "", status
    html, error = await _fetch_html(client, page_url)
    if error:
        status.status = "failed"
        status.detail = error
        return "", status
    text = _extract_text(html)
    if not text:
        status.status = "unavailable"
        status.detail = "No readable text extracted"
    return text, status


# Words almost every company name contains. Removed before domain
# matching, so a generic word cannot match half the internet.
_GENERIC_TOKENS = {"group", "holding", "asa", "as", "ab", "sa",
                   "norge", "norway", "international", "offshore"}


def _company_tokens(name: str) -> list[str]:
    """Distinctive tokens of the company name, ascii-folded ('Test
    Company AS' -> ['test']).

    The rule is general: a search URL is eligible only when its domain
    carries one of the company's own name tokens. That single rule keeps
    social media, encyclopedias, aggregators and directories out with no
    blocklist to maintain. Companies with no distinctive token (rare)
    can still qualify through verification: their page must contain the
    organisation number itself.
    """
    folded = (name.lower()
              .replace("\u00f8", "o").replace("\u00e6", "ae").replace("\u00e5", "a"))
    return [t for t in folded.split()
            if len(t) >= 3 and t not in _GENERIC_TOKENS]


def _domain_of(url: str) -> str:
    """The domain part of a URL, lowercase. The path is never considered:
    a company name inside some other site's path must not make that site
    look like the company's own."""
    return url.lower().split("//", 1)[-1].split("/", 1)[0]


def _is_third_party(url: str, tokens: list[str]) -> bool:
    """True when a URL cannot be the company's own site: its domain
    carries none of the company's name tokens."""
    if not tokens:
        return False          # no distinctive tokens: let verification decide
    return not any(t in _domain_of(url) for t in tokens)


def _url_rank(url: str, tokens: list[str]) -> int:
    """0 = the company's own domain, 1 = no distinctive tokens available
    (verification decides), 2 = third-party page (skipped)."""
    if not tokens:
        return 1
    return 0 if any(t in _domain_of(url) for t in tokens) else 2


# Phrases that only appear in cookie-consent and privacy banners. They
# are boilerplate on nearly every modern site and carry no information
# about the business, so they are removed before the text is used.
_CONSENT_PHRASES = ("we value your privacy", "cookie", "consent",
                    "accept all", "reject all", "customize",
                    "manage preferences", "privacy policy",
                    "always active", "necessary", "functional",
                    "analytics", "marketing", "duration", "session",
                    "advertisement", "uncategorized",
                    "description is currently not available",
                    "no description available", "close navigate")


def _strip_consent_boilerplate(text: str) -> str:
    """Remove consent-banner and cookie-table lines from fetched text.

    Rule kept general: a line is dropped when it contains a consent or
    cookie-category phrase and is short. Long lines that merely mention
    'privacy policy' in passing survive."""
    kept = []
    for line in text.splitlines():
        low = line.lower()
        if len(low) < 300 and any(p in low for p in _CONSENT_PHRASES):
            continue
        kept.append(line)
    return " ".join(" ".join(kept).split())


async def _reader_text(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """Fetch the live page through the Jina Reader service.

    Some sites block cloud IPs outright; Reader fetches from its own
    infrastructure with browser rendering, so it often succeeds where a
    direct fetch is refused. Free tier, no key needed. Returns (text,
    error), error empty on success. Never raises."""
    try:
        resp = await client.get(f"https://r.jina.ai/{url}",
                                headers={"Accept": "text/plain"},
                                timeout=settings.request_timeout_seconds)
        if resp.status_code != 200:
            logger.info("reader_unavailable url=%s status=%s", url, resp.status_code)
            return "", f"Reader service returned {resp.status_code}"

        def _noise(line: str) -> bool:
            t = line.strip()
            if t.startswith("![") and "](" in t:
                return True                      # image-only line
            return len(t) <= 8 and ("[x]" in t or "[ ]" in t)

        body = _strip_consent_boilerplate(
            "\n".join(ln for ln in resp.text.splitlines() if not _noise(ln)))
        if len(body) < 200:
            return "", "Reader returned too little useful text"
        return body, ""
    except httpx.TimeoutException:
        return "", "Reader service timed out"
    except Exception as exc:
        logger.info("reader_error url=%s error=%s", url, exc)
        return "", f"Reader service error: {exc}"


def _search_text_fact(url: str, text: str) -> Fact:
    """A verified search page as a citable fact. The value is a compact
    summary line; the full text travels to the LLM separately. The
    websearch fact_id keeps the origin visible in every citation:
    content from a searched site is never cited as brreg."""
    return Fact(fact_id="websearch:text", source="websearch", source_url=url,
                label="Website text",
                value=f"Excerpt from {url} ({len(text)} characters, verified via web search)",
                retrieved_at=_now())


async def _use_page(client: httpx.AsyncClient, url: str, facts: list[Fact],
                    statuses: list[SourceStatus]) -> tuple[str, Fact] | None:
    """Verify one URL and, if it passes, fetch and return its text with
    the citable fact. A rejected or unreadable page is logged in the
    statuses and returns None."""
    verified, passed, reason = await verify_url(client, url, facts)
    if not verified:
        statuses.append(SourceStatus(source="websearch", url=url,
                                     status="unavailable", detail=f"URL rejected: {reason}"))
        return None
    text, page_status = await fetch_page_text(client, url)
    statuses.append(page_status)
    if page_status.status == "ok" and text:
        logger.info("websearch_verified url=%s checks=%s", url, passed)
        return text, _search_text_fact(url, text)
    return None


async def get_unstructured_source(client: httpx.AsyncClient, facts: list[Fact],
                                  homepage_url: str
                                  ) -> tuple[str, list[SourceStatus], str, "Fact | None"]:
    """Resolve the unstructured source for one company.

    Returns (text, statuses, citable_fact_id, text_fact).

    Ladder, best first: the registered homepage (cited through the
    brreg:website fact), then search results on the company's own domain,
    verified against the registry facts and, when the site blocks the
    direct fetch, fetched through the Reader service (cited as
    websearch:text). Every outcome ends in a status the pipeline can
    show; never raises.
    """
    text, status = await fetch_page_text(
        client, homepage_url, missing_detail="No website registered in Brreg")
    if status.status == "ok":
        return text, [status], "brreg:website", None

    if settings.search_provider == "none" or not settings.search_api_key:
        return "", [status], "", None          # registry facts only

    name = next((f.value for f in facts if f.fact_id == "brreg:name"), "")
    place = next((f.value for f in facts if f.fact_id == "brreg:place"), "")
    if not name:
        return "", [status], "", None

    query = f'"{name}" {place}'.strip()
    results, search_status = await search_web(client, query)
    statuses = [search_status]
    logger.info("websearch_fallback query=%s results=%s status=%s",
                query, len(results), search_status.status)

    tokens = _company_tokens(name)
    results.sort(key=lambda r: _url_rank(r.get("url", ""), tokens))

    own_urls = []
    for result in results:
        url = result.get("url", "")
        if _is_third_party(url, tokens):
            statuses.append(SourceStatus(source="websearch", url=url,
                                         status="unavailable",
                                         detail="Not the company's own site: skipped (only the company's domain is used)"))
            continue
        own_urls.append(url)
        hit = await _use_page(client, url, facts, statuses)
        if hit:
            return hit[0], statuses, "websearch:text", hit[1]

        # The direct fetch failed (blocked, timeout): try the same URL
        # through the Reader service before moving to the next candidate.
        reader_text, reader_error = await _reader_text(client, url)
        if reader_text:
            verified, passed, reason = verify_text(url, reader_text, facts)
            if verified:
                statuses.append(SourceStatus(source="websearch", url=url,
                                             status="ok",
                                             detail="Fetched live via reader service"))
                logger.info("reader_verified url=%s checks=%s", url, passed)
                return reader_text, statuses, "websearch:text", _search_text_fact(url, reader_text)
            statuses.append(SourceStatus(source="websearch", url=url,
                                         status="unavailable", detail=f"URL rejected: {reason}"))
        else:
            statuses.append(SourceStatus(source="websearch", url=url,
                                         status="unavailable",
                                         detail=f"Reader service failed: {reader_error}"))

    statuses.append(SourceStatus(source="websearch", url=query,
                                 status="unavailable", detail="No verified URL"))
    return "", statuses, "", None
