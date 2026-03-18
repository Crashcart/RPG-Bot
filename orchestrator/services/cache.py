"""Redis session cache service."""

from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as aioredis

from orchestrator.config import Settings

logger = logging.getLogger(__name__)


class CacheService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: aioredis.Redis | None = None

    async def connect(self) -> None:
        self._client = aioredis.Redis(
            host=self._settings.redis_host,
            port=self._settings.redis_port,
            password=self._settings.redis_password,
            decode_responses=True,
        )
        await self._client.ping()
        logger.info("Redis connection established.")

    async def disconnect(self) -> None:
        if self._client:
            await self._client.aclose()
            logger.info("Redis connection closed.")

    @property
    def client(self) -> aioredis.Redis:
        if self._client is None:
            raise RuntimeError("CacheService not connected.")
        return self._client

    # ── Session Management ────────────────────────────────────────────────────

    async def create_session(
        self,
        session_token: str,
        player_id: str,
        guild_id: str,
        channel_id: str,
        campaign_id: str | None = None,
        character_id: str | None = None,
    ) -> None:
        data = {
            "player_id": player_id,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "campaign_id": campaign_id or "",
            "character_id": character_id or "",
        }
        await self.client.setex(
            f"session:{session_token}",
            self._settings.session_ttl_seconds,
            json.dumps(data),
        )

    async def get_session(self, session_token: str) -> dict[str, Any] | None:
        raw = await self.client.get(f"session:{session_token}")
        if not raw:
            return None
        return json.loads(raw)

    async def refresh_session(self, session_token: str) -> None:
        await self.client.expire(
            f"session:{session_token}",
            self._settings.session_ttl_seconds,
        )

    async def delete_session(self, session_token: str) -> None:
        await self.client.delete(f"session:{session_token}")

    # ── Pipeline State Cache (prevent duplicate processing) ───────────────────

    async def set_pipeline_lock(self, intent_id: str, ttl: int = 300) -> bool:
        """Returns True if lock acquired, False if already locked (duplicate)."""
        result = await self.client.set(
            f"pipeline_lock:{intent_id}", "1", ex=ttl, nx=True
        )
        return result is not None

    async def release_pipeline_lock(self, intent_id: str) -> None:
        await self.client.delete(f"pipeline_lock:{intent_id}")

    # ── WebSocket State ───────────────────────────────────────────────────────

    async def cache_narrative(self, intent_id: str, narrative: str, ttl: int = 600) -> None:
        await self.client.setex(f"narrative:{intent_id}", ttl, narrative)

    async def get_cached_narrative(self, intent_id: str) -> str | None:
        return await self.client.get(f"narrative:{intent_id}")
