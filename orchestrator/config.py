"""Ironclad GM – Centralised configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    postgres_db:       str = "ironclad"
    postgres_user:     str = "ironclad"
    postgres_password: str
    db_host:           str = "ironclad-db"
    db_port:           int = 5432

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.db_host}:{self.db_port}/{self.postgres_db}"
        )

    @property
    def database_dsn(self) -> str:
        """asyncpg-native DSN (no driver prefix)."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.db_host}:{self.db_port}/{self.postgres_db}"
        )

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_host:     str = "ironclad-cache"
    redis_port:     int = 6379
    redis_password: str
    session_ttl_seconds: int = 3600

    # ── Ollama ────────────────────────────────────────────────────────────────
    ollama_host:  str = "http://ironclad-ollama:11434"
    ollama_model: str = "mistral:7b-instruct"
    ollama_timeout_seconds: int = 60

    # ── Gemini API ────────────────────────────────────────────────────────────
    gemini_api_key: str
    gemini_model:   str = "gemini-1.5-pro"

    # ── Anthropic Claude API ──────────────────────────────────────────────────
    # Set CLAUDE_API_KEY and optionally CLOUD_PROVIDER=claude to use Claude as
    # the Tier 1 storyteller instead of Gemini.  Gemini remains the default.
    claude_api_key: str = ""
    claude_model:   str = "claude-sonnet-4-6"
    # cloud_provider: "gemini" (default) | "claude"
    cloud_provider: str = "gemini"

    # ── ChromaDB ──────────────────────────────────────────────────────────────
    chroma_host: str = "ironclad-chroma"
    chroma_port: int = 8000

    # ── Media Proxy ───────────────────────────────────────────────────────────
    media_proxy_url: str = "http://media-asset-proxy:8001"

    # ── Web Search (optional — DuckDuckGo used as free fallback) ─────────────
    # Set SERPAPI_KEY for full web results via SerpAPI, or leave blank for
    # DuckDuckGo Instant Answers (no key required).
    serpapi_key: str = ""

    # ── Disk Agency (AI file sandbox) ─────────────────────────────────────────
    # Directory the GM AI can write world artifacts to (maps, session notes…)
    world_data_dir: str = "/app/world_data"

    # ── App ───────────────────────────────────────────────────────────────────
    log_level:          str = "INFO"
    session_secret_key: str = "change-me-to-a-long-random-string"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
