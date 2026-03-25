"""
Ironclad GM – Campfire Mode Service
======================================
Manages the Campfire Mode state machine that pauses critical plot advancement
when key players go offline.

How it works
------------
1. The Discord bot calls POST /api/presence whenever any guild member's
   online status changes.
2. CampfireService upserts the row in player_presence and then checks
   whether all characters with an active character in the campaign are online.
3. If any character-owning player is offline, the service writes
   system_settings.campfire_mode_active = true and records which players
   are absent.
4. The /action endpoint checks is_campfire_active() before running the
   pipeline.  When campfire is ON, it runs a lightweight "downtime RP" path
   instead of the full pipeline, producing a friendly holding message.
5. When all players come back online, campfire mode is automatically lifted.

"Key player" definition
-----------------------
Any player who has an ALIVE character in the active campaign for that guild.
Players without a character are considered observers and don't block the story.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from orchestrator.config import Settings
from orchestrator.schemas.payloads import CampfireStatus

logger = logging.getLogger(__name__)


class CampfireService:
    def __init__(self, settings: Settings, pool) -> None:
        self._pool = pool

    # ── Presence Tracking ──────────────────────────────────────────────────────

    async def update_presence(
        self, player_id: str, guild_id: str, online: bool
    ) -> CampfireStatus:
        """
        Record a presence change and recalculate campfire mode for the guild.
        Returns the current CampfireStatus after the update.
        """
        await self._pool.execute(
            """
            INSERT INTO player_presence (player_id, guild_id, online, last_seen_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (player_id, guild_id) DO UPDATE
                SET online       = EXCLUDED.online,
                    last_seen_at = NOW()
            """,
            player_id, guild_id, online,
        )

        return await self._recalculate_campfire(guild_id)

    # ── Campfire State Queries ─────────────────────────────────────────────────

    async def get_status(self, guild_id: str) -> CampfireStatus:
        active  = await self._read_setting("campfire_mode_active",    False)
        absent  = await self._read_setting("campfire_absent_players", [])
        return CampfireStatus(guild_id=guild_id, active=active, absent_players=absent)

    async def is_campfire_active(self, guild_id: str) -> bool:
        return bool(await self._read_setting("campfire_mode_active", False))

    # ── Manual Controls (admin override) ──────────────────────────────────────

    async def force_campfire_on(self, guild_id: str, reason: str = "") -> None:
        await self._write_setting("campfire_mode_active", True)
        logger.info("Campfire mode manually enabled for guild %s — %s", guild_id, reason)

    async def force_campfire_off(self, guild_id: str) -> None:
        await self._write_setting("campfire_mode_active", False)
        await self._write_setting("campfire_absent_players", [])
        logger.info("Campfire mode manually disabled for guild %s", guild_id)

    # ── Private Helpers ────────────────────────────────────────────────────────

    async def _recalculate_campfire(self, guild_id: str) -> CampfireStatus:
        """
        Compare online player_presence rows against players who have an ALIVE
        character in the active campaign.  Enable campfire if any are offline.
        """
        # Get the active campaign for this guild
        campaign = await self._pool.fetchrow(
            "SELECT id FROM campaigns WHERE guild_id = $1 AND active = TRUE LIMIT 1",
            guild_id,
        )
        if not campaign:
            # No active campaign — can't evaluate campfire
            return CampfireStatus(guild_id=guild_id, active=False)

        campaign_id = campaign["id"]

        # Character-owning players in this campaign
        char_rows = await self._pool.fetch(
            """
            SELECT DISTINCT player_id
            FROM characters
            WHERE campaign_id = $1 AND status = 'ALIVE'
            """,
            campaign_id,
        )
        key_players = {r["player_id"] for r in char_rows}

        if not key_players:
            return CampfireStatus(guild_id=guild_id, active=False)

        # Which of those players are currently offline?
        presence_rows = await self._pool.fetch(
            """
            SELECT player_id, online
            FROM player_presence
            WHERE guild_id = $1 AND player_id = ANY($2::text[])
            """,
            guild_id,
            list(key_players),
        )
        known_online = {r["player_id"] for r in presence_rows if r["online"]}
        # Players with no presence row are treated as offline (unknown = absent)
        absent = [p for p in key_players if p not in known_online]

        campfire_active = len(absent) > 0

        await self._write_setting("campfire_mode_active",    campfire_active)
        await self._write_setting("campfire_absent_players", absent)

        if campfire_active:
            logger.info(
                "Campfire mode ACTIVE for guild %s — absent players: %s",
                guild_id, absent,
            )
        else:
            logger.info("Campfire mode INACTIVE — all key players online for guild %s", guild_id)

        return CampfireStatus(
            guild_id=guild_id,
            active=campfire_active,
            absent_players=absent,
        )

    async def _read_setting(self, key: str, default):
        row = await self._pool.fetchrow(
            "SELECT value FROM system_settings WHERE key = $1", key
        )
        if not row:
            return default
        raw = row["value"]
        return json.loads(raw) if isinstance(raw, str) else raw

    async def _write_setting(self, key: str, value) -> None:
        await self._pool.execute(
            """
            INSERT INTO system_settings (key, value)
            VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value, updated_at = NOW()
            """,
            key,
            json.dumps(value),
        )
