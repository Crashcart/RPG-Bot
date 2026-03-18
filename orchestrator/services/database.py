"""PostgreSQL async database service using asyncpg."""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

import asyncpg

from orchestrator.config import Settings
from orchestrator.schemas.payloads import CharacterSnapshot, CharacterStatus, StateDelta

logger = logging.getLogger(__name__)


class DatabaseService:
    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.database_dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        logger.info("Database connection pool established.")

    async def disconnect(self) -> None:
        if self._pool:
            await self._pool.close()
            logger.info("Database connection pool closed.")

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("DatabaseService not connected. Call connect() first.")
        return self._pool

    # ── Character Queries ─────────────────────────────────────────────────────

    async def get_character_by_player(
        self, player_id: str, campaign_id: str
    ) -> CharacterSnapshot | None:
        row = await self.pool.fetchrow(
            """
            SELECT id, name, system, status, stats
            FROM characters
            WHERE player_id = $1
              AND campaign_id = $2
              AND status = 'ALIVE'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            player_id,
            UUID(campaign_id),
        )
        if not row:
            return None
        return CharacterSnapshot(
            character_id=str(row["id"]),
            name=row["name"],
            system=row["system"],
            status=CharacterStatus(row["status"]),
            stats=json.loads(row["stats"]) if isinstance(row["stats"], str) else dict(row["stats"]),
        )

    async def get_character_by_id(self, character_id: str) -> CharacterSnapshot | None:
        row = await self.pool.fetchrow(
            "SELECT id, name, system, status, stats FROM characters WHERE id = $1",
            UUID(character_id),
        )
        if not row:
            return None
        return CharacterSnapshot(
            character_id=str(row["id"]),
            name=row["name"],
            system=row["system"],
            status=CharacterStatus(row["status"]),
            stats=json.loads(row["stats"]) if isinstance(row["stats"], str) else dict(row["stats"]),
        )

    async def get_inventory(self, character_id: str) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT item_data FROM inventories WHERE character_id = $1",
            UUID(character_id),
        )
        return [
            json.loads(r["item_data"]) if isinstance(r["item_data"], str) else dict(r["item_data"])
            for r in rows
        ]

    async def get_active_campaign(self, guild_id: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            "SELECT id, name, system, settings FROM campaigns WHERE guild_id = $1 AND active = TRUE LIMIT 1",
            guild_id,
        )
        if not row:
            return None
        return {
            "id": str(row["id"]),
            "name": row["name"],
            "system": row["system"],
            "settings": json.loads(row["settings"]) if isinstance(row["settings"], str) else dict(row["settings"]),
        }

    # ── State Commitment ──────────────────────────────────────────────────────

    async def apply_state_delta(self, delta: StateDelta) -> dict[str, Any]:
        """
        Apply a mechanical state delta atomically.
        Returns the post-commit character stats dict.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Fetch current stats
                row = await conn.fetchrow(
                    "SELECT stats FROM characters WHERE id = $1 FOR UPDATE",
                    UUID(delta.character_id),
                )
                if not row:
                    raise ValueError(f"Character {delta.character_id} not found.")

                stats = json.loads(row["stats"]) if isinstance(row["stats"], str) else dict(row["stats"])

                # Apply each numeric delta
                for sd in delta.stat_deltas:
                    stats[sd.stat_key] = sd.new_value

                # Update status if changed
                if delta.status_change:
                    await conn.execute(
                        "UPDATE characters SET stats = $1, status = $2, updated_at = NOW() WHERE id = $3",
                        json.dumps(stats),
                        delta.status_change.value,
                        UUID(delta.character_id),
                    )
                else:
                    await conn.execute(
                        "UPDATE characters SET stats = $1, updated_at = NOW() WHERE id = $2",
                        json.dumps(stats),
                        UUID(delta.character_id),
                    )

                # Apply inventory deltas
                for item in delta.inventory_delta:
                    qty = item.get("quantity", 0)
                    if qty > 0:
                        await conn.execute(
                            "INSERT INTO inventories (character_id, item_data) VALUES ($1, $2)",
                            UUID(delta.character_id),
                            json.dumps(item),
                        )
                    elif qty < 0:
                        # Remove by item name
                        await conn.execute(
                            "DELETE FROM inventories WHERE character_id = $1 AND item_data->>'name' = $2 LIMIT 1",
                            UUID(delta.character_id),
                            item.get("name"),
                        )

                return stats

    async def log_action(self, record: dict[str, Any]) -> None:
        await self.pool.execute(
            """
            INSERT INTO action_log
              (intent_id, campaign_id, character_id, player_id, raw_input,
               intent_payload, mechanical_payload, state_delta, narrative_summary)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            UUID(record["intent_id"]),
            UUID(record["campaign_id"]) if record.get("campaign_id") else None,
            UUID(record["character_id"]) if record.get("character_id") else None,
            record["player_id"],
            record["raw_input"],
            json.dumps(record["intent_payload"]),
            json.dumps(record.get("mechanical_payload")),
            json.dumps(record.get("state_delta")),
            record.get("narrative_summary", "")[:500],
        )

    # ── Web UI Queries ────────────────────────────────────────────────────────

    async def get_all_campaigns(self) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT c.id, c.name, c.system, c.guild_id,
                   COUNT(DISTINCT ch.id) FILTER (WHERE ch.status = 'ALIVE') AS character_count,
                   COUNT(DISTINCT sc.id) AS fact_count
            FROM campaigns c
            LEFT JOIN characters ch ON ch.campaign_id = c.id
            LEFT JOIN story_context sc ON sc.campaign_id = c.id
            WHERE c.active = TRUE
            GROUP BY c.id
            ORDER BY c.created_at DESC
            """
        )
        return [
            {
                "id":              str(r["id"]),
                "name":            r["name"],
                "system":          r["system"],
                "guild_id":        r["guild_id"],
                "character_count": r["character_count"],
                "fact_count":      r["fact_count"],
            }
            for r in rows
        ]

    async def get_dashboard_stats(self) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM campaigns WHERE active = TRUE)              AS campaigns,
                (SELECT COUNT(*) FROM characters WHERE status = 'ALIVE')         AS living,
                (SELECT COUNT(*) FROM characters WHERE status = 'DEAD')          AS dead,
                (SELECT COUNT(*) FROM rule_registry WHERE active = TRUE)         AS rule_modules,
                (SELECT COUNT(*) FROM story_context)                             AS story_facts,
                (SELECT COUNT(*) FROM action_log
                 WHERE resolved_at > NOW() - INTERVAL '1 day')                   AS actions_today
            """
        )
        return dict(row)

    async def get_recent_actions(self, limit: int = 8) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT player_id, raw_input, narrative_summary, resolved_at,
                   mechanical_payload->>'outcome' AS outcome
            FROM action_log
            ORDER BY resolved_at DESC
            LIMIT $1
            """,
            limit,
        )
        return [
            {
                "player_id":        r["player_id"],
                "raw_input":        r["raw_input"],
                "narrative_summary": r["narrative_summary"] or "",
                "resolved_at":      r["resolved_at"].strftime("%m-%d %H:%M") if r["resolved_at"] else "",
                "outcome":          r["outcome"] or "",
            }
            for r in rows
        ]

    async def get_all_rule_modules(self, campaign_id: str) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT id, module_name, module_type, chroma_collection,
                   module_data, active, loaded_at
            FROM rule_registry
            WHERE campaign_id = $1
            ORDER BY loaded_at DESC
            """,
            UUID(campaign_id),
        )
        return [
            {
                "id":               str(r["id"]),
                "module_name":      r["module_name"],
                "module_type":      r["module_type"],
                "chroma_collection": r["chroma_collection"] or "",
                "module_data":      json.loads(r["module_data"]) if isinstance(r["module_data"], str) else dict(r["module_data"]),
                "active":           r["active"],
                "loaded_at":        r["loaded_at"].strftime("%Y-%m-%d %H:%M") if r["loaded_at"] else "",
            }
            for r in rows
        ]

    async def add_rule_module(
        self,
        campaign_id: str,
        module_name: str,
        module_type: str,
        module_data: dict,
        chroma_collection: str | None = None,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO rule_registry
                (campaign_id, module_name, module_type, module_data, chroma_collection, active)
            VALUES ($1, $2, $3, $4, $5, TRUE)
            ON CONFLICT (campaign_id, module_name) DO UPDATE
                SET module_data       = EXCLUDED.module_data,
                    module_type       = EXCLUDED.module_type,
                    chroma_collection = EXCLUDED.chroma_collection,
                    active            = TRUE,
                    loaded_at         = NOW()
            """,
            UUID(campaign_id),
            module_name,
            module_type,
            json.dumps(module_data),
            chroma_collection,
        )

    async def toggle_rule_module(self, module_id: str) -> None:
        await self.pool.execute(
            "UPDATE rule_registry SET active = NOT active WHERE id = $1",
            UUID(module_id),
        )

    async def delete_rule_module(self, module_id: str) -> None:
        await self.pool.execute(
            "DELETE FROM rule_registry WHERE id = $1",
            UUID(module_id),
        )

    async def get_story_context(
        self, campaign_id: str, entity_type: str = ""
    ) -> list[dict[str, Any]]:
        if entity_type:
            rows = await self.pool.fetch(
                """
                SELECT entity_type, entity_name, summary, detail, last_updated_at
                FROM story_context
                WHERE campaign_id = $1 AND entity_type = $2
                ORDER BY entity_type, entity_name
                """,
                UUID(campaign_id), entity_type,
            )
        else:
            rows = await self.pool.fetch(
                """
                SELECT entity_type, entity_name, summary, detail, last_updated_at
                FROM story_context
                WHERE campaign_id = $1
                ORDER BY entity_type, entity_name
                """,
                UUID(campaign_id),
            )
        return [
            {
                "entity_type":    r["entity_type"],
                "entity_name":    r["entity_name"],
                "summary":        r["summary"],
                "detail":         r["detail"] or "",
                "last_updated_at": r["last_updated_at"].strftime("%Y-%m-%d %H:%M") if r["last_updated_at"] else "",
            }
            for r in rows
        ]

    async def get_action_log(
        self, campaign_id: str, outcome_filter: str = "", limit: int = 50
    ) -> list[dict[str, Any]]:
        if outcome_filter:
            rows = await self.pool.fetch(
                """
                SELECT player_id, raw_input, narrative_summary, resolved_at,
                       mechanical_payload->>'outcome' AS outcome,
                       mechanical_payload->>'roll_result' AS roll_result,
                       mechanical_payload->>'difficulty' AS difficulty
                FROM action_log
                WHERE campaign_id = $1
                  AND mechanical_payload->>'outcome' = $2
                ORDER BY resolved_at DESC
                LIMIT $3
                """,
                UUID(campaign_id), outcome_filter, limit,
            )
        else:
            rows = await self.pool.fetch(
                """
                SELECT player_id, raw_input, narrative_summary, resolved_at,
                       mechanical_payload->>'outcome' AS outcome,
                       mechanical_payload->>'roll_result' AS roll_result,
                       mechanical_payload->>'difficulty' AS difficulty
                FROM action_log
                WHERE campaign_id = $1
                ORDER BY resolved_at DESC
                LIMIT $2
                """,
                UUID(campaign_id), limit,
            )
        return [
            {
                "player_id":        r["player_id"],
                "raw_input":        r["raw_input"],
                "narrative_summary": r["narrative_summary"] or "",
                "resolved_at":      r["resolved_at"].strftime("%Y-%m-%d %H:%M:%S") if r["resolved_at"] else "",
                "outcome":          r["outcome"] or "",
                "roll_result":      r["roll_result"] or "",
                "difficulty":       r["difficulty"] or "",
            }
            for r in rows
        ]

    # ── Rule Registry ─────────────────────────────────────────────────────────

    async def get_active_rule_modules(self, campaign_id: str) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT module_name, module_type, chroma_collection, module_data
            FROM rule_registry
            WHERE campaign_id = $1 AND active = TRUE
            """,
            UUID(campaign_id),
        )
        return [
            {
                "module_name": r["module_name"],
                "module_type": r["module_type"],
                "chroma_collection": r["chroma_collection"],
                "module_data": json.loads(r["module_data"]) if isinstance(r["module_data"], str) else dict(r["module_data"]),
            }
            for r in rows
        ]
