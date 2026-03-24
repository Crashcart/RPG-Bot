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

                # Apply inventory deltas — full JSONB payload is duplicated as-is
                for item in delta.inventory_delta:
                    qty = item.get("quantity", 0)
                    if qty > 0:
                        # Upsert: if an item with the same name exists, add quantity;
                        # otherwise insert the full JSONB payload unchanged.
                        existing = await conn.fetchrow(
                            """
                            SELECT id, item_data FROM inventories
                            WHERE character_id = $1
                              AND item_data->>'name' = $2
                            LIMIT 1
                            FOR UPDATE
                            """,
                            UUID(delta.character_id),
                            item.get("name"),
                        )
                        if existing:
                            existing_data = (
                                json.loads(existing["item_data"])
                                if isinstance(existing["item_data"], str)
                                else dict(existing["item_data"])
                            )
                            existing_data["quantity"] = (
                                int(existing_data.get("quantity", 0)) + qty
                            )
                            await conn.execute(
                                "UPDATE inventories SET item_data = $1, updated_at = NOW() WHERE id = $2",
                                json.dumps(existing_data),
                                existing["id"],
                            )
                        else:
                            # Duplicate the full JSONB payload into the inventories table
                            await conn.execute(
                                "INSERT INTO inventories (character_id, item_data) VALUES ($1, $2)",
                                UUID(delta.character_id),
                                json.dumps(item),
                            )
                    elif qty < 0:
                        await conn.execute(
                            """
                            DELETE FROM inventories
                            WHERE id = (
                                SELECT id FROM inventories
                                WHERE character_id = $1 AND item_data->>'name' = $2
                                LIMIT 1
                            )
                            """,
                            UUID(delta.character_id),
                            item.get("name"),
                        )

                # Apply vehicle deltas within the same transaction
                for vd in delta.vehicle_deltas:
                    if not vd.vehicle_id:
                        continue
                    await self.apply_vehicle_delta(
                        conn,
                        vehicle_id=vd.vehicle_id,
                        hull_delta=vd.hull_delta,
                        subsystem_changes=[s.model_dump() for s in vd.subsystems],
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

    # ── Vehicle / Asset Queries ────────────────────────────────────────────────

    async def get_vehicles_for_campaign(self, campaign_id: str) -> list[dict[str, Any]]:
        """Return all vehicles with their subsystems for a campaign."""
        vehicle_rows = await self.pool.fetch(
            """
            SELECT id, name, asset_type, hull_integrity, max_hull_integrity, asset_data
            FROM vehicles
            WHERE campaign_id = $1
            ORDER BY name
            """,
            UUID(campaign_id),
        )
        vehicles = []
        for vr in vehicle_rows:
            vehicle_id = str(vr["id"])
            sub_rows = await self.pool.fetch(
                """
                SELECT id, subsystem_name, subsystem_type, operational_status,
                       assigned_character_id, subsystem_data
                FROM vehicle_subsystems
                WHERE vehicle_id = $1
                ORDER BY subsystem_type, subsystem_name
                """,
                vr["id"],
            )
            subsystems = [
                {
                    "subsystem_id":           str(sr["id"]),
                    "subsystem_name":         sr["subsystem_name"],
                    "subsystem_type":         sr["subsystem_type"],
                    "operational_status":     sr["operational_status"],
                    "assigned_character_id":  str(sr["assigned_character_id"]) if sr["assigned_character_id"] else None,
                    "subsystem_data":         json.loads(sr["subsystem_data"]) if isinstance(sr["subsystem_data"], str) else dict(sr["subsystem_data"]),
                }
                for sr in sub_rows
            ]
            vehicles.append({
                "vehicle_id":         vehicle_id,
                "name":               vr["name"],
                "asset_type":         vr["asset_type"],
                "hull_integrity":     vr["hull_integrity"],
                "max_hull_integrity": vr["max_hull_integrity"],
                "asset_data":         json.loads(vr["asset_data"]) if isinstance(vr["asset_data"], str) else dict(vr["asset_data"]),
                "subsystems":         subsystems,
            })
        return vehicles

    async def apply_vehicle_delta(
        self,
        conn: asyncpg.Connection,
        vehicle_id: str,
        hull_delta: int,
        subsystem_changes: list[dict],
    ) -> dict[str, Any]:
        """
        Apply hull damage/repair and subsystem mutations within an existing
        transaction.  Returns the post-commit vehicle state dict.
        Called from apply_state_delta when vehicle_deltas is non-empty.
        """
        # Update hull integrity (clamped 0..max)
        if hull_delta != 0:
            await conn.execute(
                """
                UPDATE vehicles
                SET hull_integrity = GREATEST(0, LEAST(max_hull_integrity, hull_integrity + $1)),
                    updated_at = NOW()
                WHERE id = $2
                """,
                hull_delta,
                UUID(vehicle_id),
            )

        # Apply subsystem changes
        for sc in subsystem_changes:
            name = sc.get("subsystem_name")
            if not name:
                continue
            new_status = sc.get("new_status")
            assigned   = sc.get("assigned_character_id", "__no_change__")

            if new_status and assigned == "__no_change__":
                await conn.execute(
                    """
                    UPDATE vehicle_subsystems
                    SET operational_status = $1, updated_at = NOW()
                    WHERE vehicle_id = $2 AND subsystem_name = $3
                    """,
                    new_status,
                    UUID(vehicle_id),
                    name,
                )
            elif new_status and assigned != "__no_change__":
                await conn.execute(
                    """
                    UPDATE vehicle_subsystems
                    SET operational_status = $1,
                        assigned_character_id = $2,
                        updated_at = NOW()
                    WHERE vehicle_id = $3 AND subsystem_name = $4
                    """,
                    new_status,
                    UUID(assigned) if assigned else None,
                    UUID(vehicle_id),
                    name,
                )
            elif assigned != "__no_change__":
                await conn.execute(
                    """
                    UPDATE vehicle_subsystems
                    SET assigned_character_id = $1, updated_at = NOW()
                    WHERE vehicle_id = $2 AND subsystem_name = $3
                    """,
                    UUID(assigned) if assigned else None,
                    UUID(vehicle_id),
                    name,
                )

        # Return current vehicle state
        row = await conn.fetchrow(
            "SELECT name, asset_type, hull_integrity, max_hull_integrity FROM vehicles WHERE id = $1",
            UUID(vehicle_id),
        )
        return dict(row) if row else {}

    # ── Node Registry ─────────────────────────────────────────────────────────

    async def get_all_nodes(self) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT id, node_name, node_type, host, model, priority,
                   enabled, status, last_seen, notes, roles,
                   latency_ms, latency_measured_at
            FROM node_registry
            ORDER BY priority, node_name
            """
        )
        return [
            {
                "id":                   str(r["id"]),
                "node_name":            r["node_name"],
                "node_type":            r["node_type"],
                "host":                 r["host"],
                "model":                r["model"],
                "priority":             r["priority"],
                "enabled":              r["enabled"],
                "status":               r["status"],
                "last_seen":            r["last_seen"].strftime("%Y-%m-%d %H:%M") if r["last_seen"] else "",
                "notes":                r["notes"] or "",
                "roles":                json.loads(r["roles"]) if isinstance(r["roles"], str) else list(r["roles"] or []),
                "latency_ms":           r["latency_ms"],
                "latency_measured_at":  r["latency_measured_at"].strftime("%H:%M:%S") if r["latency_measured_at"] else "",
            }
            for r in rows
        ]

    async def get_enabled_ollama_nodes(self) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT id, node_name, host, model, priority, status, roles
            FROM node_registry
            WHERE node_type = 'ollama' AND enabled = TRUE
            ORDER BY priority
            """
        )
        return [
            {
                "id":        str(r["id"]),
                "node_name": r["node_name"],
                "host":      r["host"],
                "model":     r["model"],
                "priority":  r["priority"],
                "status":    r["status"],
                "roles":     json.loads(r["roles"]) if isinstance(r["roles"], str) else list(r["roles"] or []),
            }
            for r in rows
        ]

    async def get_nodes_for_role(self, role: str) -> list[dict[str, Any]]:
        """Return enabled Ollama nodes that carry the given capability tag,
        sorted by ascending priority (best first)."""
        rows = await self.pool.fetch(
            """
            SELECT id, node_name, host, model, priority, status, roles,
                   latency_ms, voice_id
            FROM node_registry
            WHERE node_type = 'ollama'
              AND enabled   = TRUE
              AND roles @> $1::jsonb
            ORDER BY priority
            """,
            json.dumps([role]),
        )
        return [
            {
                "id":         str(r["id"]),
                "node_name":  r["node_name"],
                "host":       r["host"],
                "model":      r["model"],
                "priority":   r["priority"],
                "status":     r["status"],
                "roles":      json.loads(r["roles"]) if isinstance(r["roles"], str) else list(r["roles"] or []),
                "latency_ms": r["latency_ms"],
                "voice_id":   r["voice_id"] or "en-US-GuyNeural",
            }
            for r in rows
        ]

    async def get_nodes_for_role_by_latency(self, role: str) -> list[dict[str, Any]]:
        """
        Return enabled Ollama nodes tagged with *role*, sorted by TTFT
        (latency_ms ASC, NULLs last).  Used by the Auto-Promotion Protocol:
        the fastest currently-responding node wins, regardless of static priority.
        """
        rows = await self.pool.fetch(
            """
            SELECT id, node_name, host, model, priority, status, roles,
                   latency_ms, voice_id
            FROM node_registry
            WHERE node_type = 'ollama'
              AND enabled   = TRUE
              AND roles @> $1::jsonb
            ORDER BY latency_ms ASC NULLS LAST, priority ASC
            """,
            json.dumps([role]),
        )
        return [
            {
                "id":         str(r["id"]),
                "node_name":  r["node_name"],
                "host":       r["host"],
                "model":      r["model"],
                "priority":   r["priority"],
                "status":     r["status"],
                "roles":      json.loads(r["roles"]) if isinstance(r["roles"], str) else list(r["roles"] or []),
                "latency_ms": r["latency_ms"],
                "voice_id":   r["voice_id"] or "en-US-GuyNeural",
            }
            for r in rows
        ]

    async def update_node_latency(
        self, node_name: str, latency_ms: int
    ) -> None:
        """Record the latest TTFT benchmark result for a node."""
        await self.pool.execute(
            """
            UPDATE node_registry
            SET latency_ms          = $1,
                latency_measured_at = NOW(),
                updated_at          = NOW()
            WHERE node_name = $2
            """,
            latency_ms, node_name,
        )

    # ── System Settings ────────────────────────────────────────────────────────

    async def get_system_setting(self, key: str, default: Any = None) -> Any:
        """Fetch a global system setting by key. Returns parsed Python value."""
        row = await self.pool.fetchrow(
            "SELECT value FROM system_settings WHERE key = $1", key
        )
        if not row:
            return default
        raw = row["value"]
        if isinstance(raw, str):
            return json.loads(raw)
        # asyncpg returns JSONB as a native Python type already
        return raw

    async def set_system_setting(self, key: str, value: Any) -> None:
        """Upsert a global system setting."""
        await self.pool.execute(
            """
            INSERT INTO system_settings (key, value)
            VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value, updated_at = NOW()
            """,
            key,
            json.dumps(value),
        )

    async def upsert_node(
        self,
        node_name: str,
        node_type: str,
        host: str,
        model: str,
        priority: int,
        notes: str = "",
        roles: list[str] | None = None,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO node_registry
                (node_name, node_type, host, model, priority, notes, roles, enabled, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE, 'unknown')
            ON CONFLICT (node_name) DO UPDATE
                SET node_type  = EXCLUDED.node_type,
                    host       = EXCLUDED.host,
                    model      = EXCLUDED.model,
                    priority   = EXCLUDED.priority,
                    notes      = EXCLUDED.notes,
                    roles      = EXCLUDED.roles,
                    updated_at = NOW()
            """,
            node_name, node_type, host, model, priority, notes,
            json.dumps(roles or []),
        )

    async def update_node_status(
        self, node_name: str, status: str, last_seen: Any
    ) -> None:
        await self.pool.execute(
            """
            UPDATE node_registry
            SET status = $1, last_seen = $2, updated_at = NOW()
            WHERE node_name = $3
            """,
            status, last_seen, node_name,
        )

    async def toggle_node(self, node_id: str) -> None:
        await self.pool.execute(
            "UPDATE node_registry SET enabled = NOT enabled WHERE id = $1",
            UUID(node_id),
        )

    async def delete_node(self, node_id: str) -> None:
        await self.pool.execute(
            "DELETE FROM node_registry WHERE id = $1",
            UUID(node_id),
        )

    # ── Lore CRUD ─────────────────────────────────────────────────────────────

    async def upsert_story_fact(
        self,
        campaign_id: str,
        entity_type: str,
        entity_name: str,
        summary: str,
        detail: str = "",
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO story_context
                (campaign_id, entity_type, entity_name, summary, detail)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (campaign_id, entity_type, entity_name) DO UPDATE
                SET summary            = EXCLUDED.summary,
                    detail             = EXCLUDED.detail,
                    last_updated_at    = NOW()
            """,
            UUID(campaign_id), entity_type, entity_name, summary, detail,
        )

    async def delete_story_fact(
        self, campaign_id: str, entity_type: str, entity_name: str
    ) -> None:
        await self.pool.execute(
            """
            DELETE FROM story_context
            WHERE campaign_id = $1 AND entity_type = $2 AND entity_name = $3
            """,
            UUID(campaign_id), entity_type, entity_name,
        )

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
