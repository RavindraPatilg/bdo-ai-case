"""Application settings, read from environment variables only.

Secrets (API keys) never live in the code. See .env.example for the
variables.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    llm_provider: str = "openai"          # "openai" | "mock" (offline tests/demo)
    openai_api_key: str = ""
    openai_model: str = "openai/gpt-oss-20b"
    openai_base_url: str = ""             # optional: any OpenAI-compatible endpoint (e.g. Groq)
    app_access_token: str = ""            # optional: if set, /analyze requires X-Access-Token
    rate_limit_per_hour: int = 10         # LLM cost protection: per-IP analyses/hour
    environment: str = "production"
    database_path: str = "analyses.db"
    brreg_base_url: str = "https://data.brreg.no/enhetsregisteret/api"
    request_timeout_seconds: float = 15.0
    search_provider: str = "none"         # "none" | "serper" (web-search fallback when Brreg has no homepage)
    search_api_key: str = ""              # search API key - env only, never in code
    search_max_results: int = 3           # search result URLs checked per company

    model_config = SettingsConfigDict(env_file=".env")


settings = Settings()
