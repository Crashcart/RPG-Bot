"""
Test environment bootstrap. Sets required env vars before any orchestrator
modules are imported, preventing pydantic-settings validation errors that
occur when services instantiate Settings() at module level.
"""
import os

os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("GEMINI_API_KEY", "test")
os.environ.setdefault("DISCORD_BOT_TOKEN", "test")
os.environ.setdefault("DISCORD_APPLICATION_ID", "123456789")
os.environ.setdefault("LAVALINK_PASSWORD", "test")
os.environ.setdefault("SESSION_SECRET_KEY", "test-secret-key-for-testing-only")
