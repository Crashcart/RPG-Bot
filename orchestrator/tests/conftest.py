"""
Stub heavy runtime dependencies so orchestrator modules can be imported
during testing without a live PostgreSQL / Redis / ChromaDB stack.
"""
import sys
from unittest.mock import MagicMock


def _stub(name: str) -> None:
    """Register a MagicMock for *name* and all its parent packages if missing."""
    parts = name.split(".")
    for i in range(1, len(parts) + 1):
        full = ".".join(parts[:i])
        if full not in sys.modules:
            m = MagicMock()
            m.__name__ = full
            m.__path__ = []
            sys.modules[full] = m
    # Wire child as attribute on parent
    if len(parts) > 1:
        parent = sys.modules.get(".".join(parts[:-1]))
        child = sys.modules[name]
        if parent is not None:
            setattr(parent, parts[-1], child)


_STUBS = [
    "asyncpg",
    "redis",
    "redis.asyncio",
    "redis.asyncio.client",
    "chromadb",
    "chromadb.config",
    "chromadb.utils",
    "chromadb.utils.embedding_functions",
    "anthropic",
    "httpx",
    "pymupdf",
    "passlib",
    "passlib.context",
    "itsdangerous",
    "fastapi",
    "fastapi.staticfiles",
    "fastapi.templating",
    "fastapi.responses",
    "fastapi.middleware",
    "fastapi.middleware.cors",
    "openai",
    "elevenlabs",
]

for _s in _STUBS:
    _stub(_s)

# Provide dummy env vars so pydantic-settings can instantiate Settings
# without a live .env file or Docker environment.
import os
_TEST_ENVS = {
    "POSTGRES_PASSWORD": "test",
    "REDIS_PASSWORD": "test",
    "GEMINI_API_KEY": "test",
    "DISCORD_BOT_TOKEN": "test",
    "DISCORD_APPLICATION_ID": "test",
    "SESSION_SECRET_KEY": "test-secret-key-for-unit-tests-only",
    "LAVALINK_PASSWORD": "test",
}
for _k, _v in _TEST_ENVS.items():
    os.environ.setdefault(_k, _v)
