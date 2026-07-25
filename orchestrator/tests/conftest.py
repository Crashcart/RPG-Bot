"""
Shared pytest configuration for orchestrator tests.

Stubs required environment variables so Pydantic Settings can import
the app modules without a live stack (no .env, no running containers).

Also installs lightweight mock modules for optional heavy dependencies
(asyncpg, redis, aiohttp, etc.) that are not available in the test
environment but are imported at module-load time by the services package.
"""

import os
import sys
import types

# ---------------------------------------------------------------------------
# Env var stubs (required by pydantic-settings in config.py)
# ---------------------------------------------------------------------------
os.environ.setdefault("POSTGRES_PASSWORD", "test_pg_pass")
os.environ.setdefault("REDIS_PASSWORD", "test_redis_pass")
os.environ.setdefault("GEMINI_API_KEY", "test_gemini_key")
os.environ.setdefault("DISCORD_BOT_TOKEN", "test_discord_token")
os.environ.setdefault("DISCORD_APPLICATION_ID", "123456789012345678")
os.environ.setdefault("LAVALINK_PASSWORD", "test_lavalink_pass")
os.environ.setdefault("SESSION_SECRET_KEY", "test_session_secret_key_32_chars!!")


def _stub_module(name: str, **attrs):
    """Register a no-op module under `name` if it isn't already importable."""
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    for attr, value in attrs.items():
        setattr(mod, attr, value)
    sys.modules[name] = mod
    # Also register sub-packages so dotted imports don't fail
    parts = name.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[:i])
        if parent not in sys.modules:
            sys.modules[parent] = types.ModuleType(parent)


# Heavy optional dependencies not installed in the CI/test environment
_stub_module("asyncpg")
_stub_module("asyncpg.pool")
_stub_module("redis")
_stub_module("redis.asyncio")
_stub_module("redis.asyncio.client")
_stub_module("aiohttp")
_stub_module("google.generativeai", GenerativeModel=object)
_stub_module("google")
_stub_module("google.generativeai")
_stub_module("anthropic")
_stub_module("openai")
_stub_module("chromadb")
_stub_module("chromadb.utils")
_stub_module("chromadb.utils.embedding_functions")
_stub_module("wavelink")
_stub_module("nats")
_stub_module("nats.aio")
_stub_module("nats.aio.client")
_stub_module("discord")
_stub_module("discord.ext")
_stub_module("discord.ext.commands")
