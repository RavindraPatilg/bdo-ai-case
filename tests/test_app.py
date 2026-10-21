"""Automated tests that run fully offline.

External services (Brreg, website, web search, LLM) are mocked, so the
suite is deterministic, fast and costs nothing. Five tests, one per
case requirement that matters:
1. Input check - reject typos before any external call or LLM cost
2. Registry - parse the facts, fail predictably
3. Trust - the LLM answer is validated before use
4. Pipeline - facts in, assessment out, saved
5. Cache - a repeat never triggers a new LLM call
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import httpx
from httpx import AsyncClient, Response

from app import store, sources
from app.main import app
from app.models import Fact, SourceStatus
from app.sources import fetch_brreg

COMPANY = "Test Company AS"
PLACE = "Oslo"
WEBSITE = "https://www.testcompany.no/"


def make_fact(fid: str, value: str) -> Fact:
    return Fact(fact_id=fid, source="brreg", source_url="https://x",
                label=fid, value=value, retrieved_at=datetime.now(timezone.utc))


def _valid_orgnr(seed: str) -> str:
    """Build a 9-digit orgnr that passes the MOD11 check (deterministic)."""
    weights = [3, 2, 7, 6, 5, 4, 3, 2]
    digits = [int(c) for c in seed[:8]]
    total = sum(d * w for d, w in zip(digits, weights))
    r = total % 11
    check = 0 if r == 0 else 11 - r
    return "".join(str(d) for d in digits) + str(check)


def _bad_orgnr(seed: str) -> str:
    """A valid orgnr with the last digit changed: always fails MOD11."""
    orgnr = _valid_orgnr(seed)
    wrong = "1" if orgnr[-1] != "1" else "2"
    return orgnr[:-1] + wrong


def _facts(orgnr: str, website: str) -> list[Fact]:
    return [make_fact("brreg:name", COMPANY),
            make_fact("brreg:nace", "01.01 Growing of crops"),
            make_fact("brreg:orgform", "Aksjeselskap"),
            make_fact("brreg:place", PLACE),
            make_fact("brreg:website", website),
            make_fact("brreg:orgnr", orgnr)]


async def fake_brreg_ok(client, orgnr):
    return _facts(orgnr, WEBSITE), SourceStatus(source="brreg", url="x", status="ok")


async def fake_brreg_fail(client, orgnr):
    return [], SourceStatus(source="brreg", url="x", status="failed", detail="timeout")


async def fake_site_ok(client, url, missing_detail=""):
    return f"{COMPANY} provides its services through this page.", SourceStatus(
        source="website", url=url, status="ok")


MOCK_LLM = {"use_cases": [
    {"title": "Predictive maintenance", "description": "Use sensor history to predict failures.",
     "expected_value": "Fewer stoppages.", "based_on_fact_ids": ["brreg:name", "brreg:nace"],
     "is_assessment": True},
    {"title": "Document intake", "description": "Automate classification of operational documents.",
     "expected_value": "Hours saved weekly.", "based_on_fact_ids": ["brreg:orgform"],
     "is_assessment": True}],
    "limitations": ["Limited facts in demo mode."]}


async def fake_llm_ok(orgnr, facts, text, website_fact_id=""):
    return MOCK_LLM, "mock-model"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Each test gets its own SQLite file: portable and isolated, and no
    database is ever deleted while a connection holds it open (Windows)."""
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "test_analyses.db"))
    from fastapi.testclient import TestClient
    with TestClient(app) as test_client:
        yield test_client
    if store._conn is not None:
        store._conn.close()
    store._conn = None


# ---- 1. Input check: reject typos before any external call or LLM cost ----

def test_1_rejects_bad_check_digit(client):
    r = client.post("/analyze", json={"orgnr": _bad_orgnr("11111111")})
    assert r.status_code == 400
    assert "check digit" in r.json()["detail"]


# ---- 2. Registry: parse the facts, fail predictably ----

async def test_2_brreg_parses_and_fails_predictably():
    c = AsyncClient()
    c.get = AsyncMock(return_value=Response(
        200, json={"organisasjonsnummer": _valid_orgnr("22222222"), "navn": COMPANY,
                   "hjemmeside": WEBSITE},
        request=httpx.Request("GET", "https://x")))
    facts, status = await fetch_brreg(c, _valid_orgnr("22222222"))
    assert status.status == "ok"
    assert {"brreg:orgnr", "brreg:name", "brreg:website"} <= {f.fact_id for f in facts}

    c.get = AsyncMock(side_effect=httpx.TimeoutException("t"))
    facts, status = await fetch_brreg(c, _valid_orgnr("22222222"))
    assert facts == [] and status.status == "failed" and "timed out" in status.detail


# ---- 3. Trust: the LLM answer is validated before use ----

async def test_3_llm_output_rejects_unknown_fact_ids():
    from app.models import validate_llm_output
    good = {"title": "Valid title", "description": "long enough description here",
            "expected_value": "some value", "based_on_fact_ids": ["brreg:name"],
            "is_assessment": True}
    bad = {"use_cases": [good, dict(good, based_on_fact_ids=["brreg:madeup"])],
           "limitations": ["none"]}
    with pytest.raises(ValueError, match="unknown fact"):
        validate_llm_output(bad, {"brreg:name"})


# ---- 4. Pipeline: facts in, assessment out, saved ----

def test_4_analyze_end_to_end_with_mocks(client):
    with patch("app.main.fetch_brreg", fake_brreg_ok), \
         patch("app.sources.fetch_page_text", fake_site_ok), \
         patch("app.main.analyse_with_llm", fake_llm_ok):
        r = client.post("/analyze", json={"orgnr": _valid_orgnr("11111111")})
    assert r.status_code == 200
    d = r.json()
    assert d["cached"] is False
    assert d["company_name"] == COMPANY
    assert d["model"] == "mock-model"
    assert len(d["use_cases"]) == 2
    assert all(uc["is_assessment"] is True for uc in d["use_cases"])
    assert {"brreg:name"} <= {f["fact_id"] for f in d["facts"]}


# ---- 5. Cache: a repeat never triggers a new LLM call ----

def test_5_serves_cached_second_call(client):
    with patch("app.main.fetch_brreg", fake_brreg_ok), \
         patch("app.sources.fetch_page_text", fake_site_ok), \
         patch("app.main.analyse_with_llm", fake_llm_ok):
        client.post("/analyze", json={"orgnr": _valid_orgnr("33333333")})
        r2 = client.post("/analyze", json={"orgnr": _valid_orgnr("33333333")})
    assert r2.status_code == 200
    assert r2.json()["cached"] is True
