"""
Test environment setup for orchestrator unit tests.

Sets required env vars before any service modules are imported so that
pydantic-settings can initialize Settings() without a live .env file.
"""

import os

os.environ.setdefault("POSTGRES_PASSWORD",    "test-secret")
os.environ.setdefault("REDIS_PASSWORD",       "test-secret")
os.environ.setdefault("GEMINI_API_KEY",       "test-key")
os.environ.setdefault("DISCORD_BOT_TOKEN",    "test-token")
os.environ.setdefault("DISCORD_APPLICATION_ID", "123456789")
os.environ.setdefault("LAVALINK_PASSWORD",    "test-secret")
os.environ.setdefault("SESSION_SECRET_KEY",   "test-session-key")
