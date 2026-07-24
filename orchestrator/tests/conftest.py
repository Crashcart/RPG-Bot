"""Shared pytest fixtures and environment stubs.

Stubs all required env vars so pydantic-settings initialises
without a live .env file during unit tests.
"""

from __future__ import annotations

import os


def pytest_configure(config):  # noqa: ARG001
    os.environ.setdefault("POSTGRES_PASSWORD", "test-stub")
    os.environ.setdefault("REDIS_PASSWORD", "test-stub")
    os.environ.setdefault("GEMINI_API_KEY", "test-stub")
