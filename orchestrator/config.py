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

    # ── Multimedia ────────────────────────────────────────────────────────────
    # ElevenLabs: SFX generation + optional TTS provider
    elevenlabs_api_key: str = ""
    # ComfyUI: local image generation (runs as a separate Docker service)
    comfyui_url: str = "http://comfyui:8188"
    # Stability AI: cloud image generation alternative
    stability_ai_key: str = ""

    # ── Cloud Adjudication (OpenAI-compatible providers) ──────────────────────
    # adjudication_provider is managed at runtime via system_settings in the DB.
    # Set these API keys here; switch the active provider via White Portal → Settings.
    groq_api_key:       str = ""
    groq_model:         str = "llama-3.3-70b-versatile"
    openrouter_api_key: str = ""
    openrouter_model:   str = "meta-llama/llama-3.3-70b-instruct"
    together_api_key:   str = ""
    together_model:     str = "meta-llama/Llama-3.3-70B-Instruct-Turbo"

    # ── OpenAI (DALL-E 3 image gen + TTS alternative) ─────────────────────────
    openai_api_key:   str = ""
    openai_tts_model: str = "tts-1"
    openai_tts_voice: str = "onyx"

    # ── SillyTavern (external OpenAI-compatible frontend proxy) ───────────────
    # SillyTavern is NOT installed as part of this stack.
    # Point sillytavern_url at an existing SillyTavern instance.
    # Typical endpoint: http://<host>:8000/api/openai/v1
    # Leave empty to disable.  No API key required by default.
    sillytavern_url:      str = ""   # base URL including /api/openai/v1
    sillytavern_model:    str = ""   # optional model hint; leave blank to use ST's active model
    sillytavern_api_key:  str = ""   # optional — only if ST has API key protection enabled

    # ── Aetheris Storage Paths (TDR §2) ──────────────────────────────────────
    # Root data directory — shared volume mounted at /app/data
    world_data_dir: str = "/app/data"
    # SQLite vault (RealityWall scribe_core.db lives here)
    vault_dir:      str = "/app/data/vault"
    # Structured log output directory
    logs_dir:       str = "/app/logs"
    # GFS backup target (Janitor writes here)
    backups_dir:    str = "/app/backups"

    # ── ABES — Autonomous Background Entity Simulation ────────────────────────
    # Interval (seconds) between world-tick sweeps.  Default: 3600 (1 hour).
    abes_tick_interval_seconds: int = 3600
    # Global Discord webhook URL for critical ABES push notifications.
    # Per-campaign overrides are stored in global_settings.abes_webhook_url.
    abes_webhook_url: str = ""

    # ── App ───────────────────────────────────────────────────────────────────
    log_level:          str = "INFO"
    session_secret_key: str = "change-me-to-a-long-random-string"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
