"""Shared test configuration: every test runs fully offline.

The provider is forced to "mock" for all tests, independent of any .env
file, so the suite never calls external services and behaves identically
on every machine.
"""
import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def disable_rate_limiter():
    """The rate limiter is production cost protection; in the offline test
    suite every request comes from the same 'testclient' IP, so an enabled
    limiter would cap the whole suite at 10 analyses."""
    from app.main import limiter
    limiter.enabled = False
    yield
    limiter.enabled = True


@pytest.fixture(autouse=True)
def force_offline_provider(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "openai_api_key", "offline-test-key")
    # Defence in depth: if a test ever re-enables a real provider by
    # mistake, point it at an unresolvable domain so the call fails fast
    # and locally instead of reaching any external service.
    monkeypatch.setattr(settings, "openai_base_url", "https://offline.invalid")
