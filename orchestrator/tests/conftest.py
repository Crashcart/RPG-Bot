"""
Shared pytest configuration.

Sets required environment variables before any imports touch pydantic-settings,
so the test suite runs without a live .env file or real infrastructure.
"""

import os

# Minimal env vars required by pydantic Settings to initialise without error
os.environ.setdefault("POSTGRES_PASSWORD",  "test-pg-password")
os.environ.setdefault("REDIS_PASSWORD",     "test-redis-password")
os.environ.setdefault("GEMINI_API_KEY",     "test-gemini-key")
os.environ.setdefault("DISCORD_BOT_TOKEN",  "test-discord-token")
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-key-32-chars-padding!")
os.environ.setdefault("LAVALINK_PASSWORD",  "test-lavalink-password")
