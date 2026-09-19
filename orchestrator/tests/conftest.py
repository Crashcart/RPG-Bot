"""Test configuration — stubs for heavy optional dependencies.

This conftest pre-registers lightweight stub modules in sys.modules before any
service is imported, so the test suite runs without a live Chroma, asyncpg,
Redis, or ChromaDB installation.
"""
from __future__ import annotations

import os
import sys
import types

# ── Required env vars (must be set before pydantic Settings is imported) ──────
os.environ.setdefault("POSTGRES_PASSWORD", "test_pg_pass")
os.environ.setdefault("REDIS_PASSWORD", "test_redis_pass")
os.environ.setdefault("GEMINI_API_KEY", "test_gemini_key")


def _stub(name: str) -> types.ModuleType:
    """Register an empty stub module (no-op for all attribute access)."""
    mod = sys.modules.get(name)
    if mod is not None:
        return mod
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


# ── asyncpg ───────────────────────────────────────────────────────────────────
_stub("asyncpg")

# ── chromadb + sub-modules ────────────────────────────────────────────────────
_stub("chromadb")
chroma_config = _stub("chromadb.config")
chroma_config.Settings = type("Settings", (), {})  # minimal stub class
_stub("chromadb.api")
_stub("chromadb.api.models")
_stub("chromadb.api.models.Collection")
_stub("chromadb.errors")
_stub("chromadb.utils")

# ── redis / redis.asyncio ─────────────────────────────────────────────────────
# Only stub if redis is not already properly installed.
try:
    import redis.asyncio  # noqa: F401
except Exception:
    redis_mod = _stub("redis")
    aioredis = _stub("redis.asyncio")
    aioredis.Redis = object

# ── pymupdf / fitz ────────────────────────────────────────────────────────────
_stub("fitz")

# ── passlib ───────────────────────────────────────────────────────────────────
try:
    import passlib.context  # noqa: F401
except Exception:
    passlib = _stub("passlib")
    passlib_ctx = _stub("passlib.context")
    passlib_ctx.CryptContext = object

# ── nats (tests mock this — stub prevents ImportError on import) ──────────────
nats_stub = _stub("nats")
nats_stub.connect = None  # attribute must exist for patch() to work
