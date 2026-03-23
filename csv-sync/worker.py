"""
Ironclad GM – CSV Sync Worker
================================
Subscribes to Redis pub/sub for state_commit and vehicle_commit events.

On state_commit:
  Regenerates characters_living.csv and characters_dead.csv from the
  PostgreSQL characters table.

On vehicle_commit:
  Regenerates asset_[vehicleId].csv — one file per physical asset —
  giving players direct read access to their ship/mech component status.

Design:
  - Full-table regeneration on each event is safe at home-lab scale and
    avoids partial-write corruption.
  - Dead characters are atomically moved to characters_dead.csv.
  - CSV files are volume-mounted and accessible to players directly.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import asyncpg
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
POSTGRES_DSN = (
    f"postgresql://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
    f"@{os.environ.get('DB_HOST', 'ironclad-db')}:5432/{os.environ['POSTGRES_DB']}"
)
REDIS_HOST     = os.environ.get("REDIS_HOST", "ironclad-cache")
REDIS_PORT     = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ["REDIS_PASSWORD"]
EXPORTS_DIR    = Path(os.environ.get("EXPORTS_DIR", "/app/exports"))
CHANNEL        = "csv_sync_events"

EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

LIVING_CSV = EXPORTS_DIR / "characters_living.csv"
DEAD_CSV   = EXPORTS_DIR / "characters_dead.csv"


# ─────────────────────────────────────────────────────────────────────────────
# Character CSV helpers (unchanged behaviour)
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_characters(pool: asyncpg.Pool, status: str) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT
            c.id,
            c.player_id,
            c.name,
            c.system,
            c.status,
            c.stats,
            c.updated_at,
            cam.name AS campaign_name
        FROM characters c
        LEFT JOIN campaigns cam ON cam.id = c.campaign_id
        WHERE c.status = $1
        ORDER BY c.updated_at DESC
        """,
        status,
    )
    result = []
    for row in rows:
        stats = json.loads(row["stats"]) if isinstance(row["stats"], str) else dict(row["stats"])
        result.append({
            "id":         str(row["id"]),
            "player_id":  row["player_id"],
            "name":       row["name"],
            "system":     row["system"],
            "status":     row["status"],
            "campaign":   row["campaign_name"] or "",
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else "",
            **{f"stat_{k}": v for k, v in stats.items()},
        })
    return result


def _write_csv(path: Path, records: list[dict]) -> None:
    if not records:
        path.write_text("")
        return

    base_keys  = ["id", "player_id", "name", "system", "status", "campaign", "updated_at"]
    stat_keys  = sorted({k for r in records for k in r if k.startswith("stat_")})
    fieldnames = base_keys + stat_keys

    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    tmp_path.replace(path)
    logger.info("Written %d records to %s", len(records), path.name)


async def regenerate_character_csvs(pool: asyncpg.Pool) -> None:
    living = await fetch_characters(pool, "ALIVE")
    dead   = await fetch_characters(pool, "DEAD")
    _write_csv(LIVING_CSV, living)
    _write_csv(DEAD_CSV, dead)


# ─────────────────────────────────────────────────────────────────────────────
# Vehicle / Asset CSV helpers
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_vehicle_subsystems(pool: asyncpg.Pool, vehicle_id: str) -> list[dict]:
    """Fetch vehicle header + all subsystems, flattened for CSV."""
    vehicle_row = await pool.fetchrow(
        """
        SELECT v.id, v.name, v.asset_type,
               v.hull_integrity, v.max_hull_integrity,
               v.asset_data, v.updated_at,
               cam.name AS campaign_name
        FROM vehicles v
        LEFT JOIN campaigns cam ON cam.id = v.campaign_id
        WHERE v.id = $1
        """,
        vehicle_id,
    )
    if not vehicle_row:
        return []

    asset_data = (
        json.loads(vehicle_row["asset_data"])
        if isinstance(vehicle_row["asset_data"], str)
        else dict(vehicle_row["asset_data"])
    )

    sub_rows = await pool.fetch(
        """
        SELECT
            vs.subsystem_name,
            vs.subsystem_type,
            vs.operational_status,
            vs.subsystem_data,
            ch.name AS assigned_character_name
        FROM vehicle_subsystems vs
        LEFT JOIN characters ch ON ch.id = vs.assigned_character_id
        WHERE vs.vehicle_id = $1
        ORDER BY vs.subsystem_type, vs.subsystem_name
        """,
        vehicle_id,
    )

    records = []
    for sr in sub_rows:
        sub_data = (
            json.loads(sr["subsystem_data"])
            if isinstance(sr["subsystem_data"], str)
            else dict(sr["subsystem_data"])
        )
        records.append({
            "vehicle_id":             str(vehicle_row["id"]),
            "vehicle_name":           vehicle_row["name"],
            "asset_type":             vehicle_row["asset_type"],
            "campaign":               vehicle_row["campaign_name"] or "",
            "hull_integrity":         vehicle_row["hull_integrity"],
            "max_hull_integrity":     vehicle_row["max_hull_integrity"],
            "subsystem_name":         sr["subsystem_name"],
            "subsystem_type":         sr["subsystem_type"],
            "operational_status":     sr["operational_status"],
            "assigned_character":     sr["assigned_character_name"] or "",
            "updated_at":             vehicle_row["updated_at"].isoformat() if vehicle_row["updated_at"] else "",
            **{f"asset_{k}": v for k, v in asset_data.items()},
            **{f"sub_{k}": v for k, v in sub_data.items()},
        })
    return records


async def regenerate_vehicle_csv(pool: asyncpg.Pool, vehicle_id: str) -> None:
    records  = await fetch_vehicle_subsystems(pool, vehicle_id)
    csv_path = EXPORTS_DIR / f"asset_{vehicle_id}.csv"

    if not records:
        # Vehicle may have been deleted — remove stale CSV
        csv_path.unlink(missing_ok=True)
        return

    # Build fieldname order: identity cols first, then asset_*, then sub_*
    base_keys  = [
        "vehicle_id", "vehicle_name", "asset_type", "campaign",
        "hull_integrity", "max_hull_integrity",
        "subsystem_name", "subsystem_type", "operational_status",
        "assigned_character", "updated_at",
    ]
    asset_keys = sorted({k for r in records for k in r if k.startswith("asset_")})
    sub_keys   = sorted({k for r in records for k in r if k.startswith("sub_")})
    fieldnames = base_keys + asset_keys + sub_keys

    tmp_path = csv_path.with_suffix(".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    tmp_path.replace(csv_path)
    logger.info(
        "Written %d subsystem rows to %s", len(records), csv_path.name
    )


async def regenerate_all_vehicle_csvs(pool: asyncpg.Pool) -> None:
    """Regenerate CSVs for every vehicle in every campaign (used on startup)."""
    rows = await pool.fetch("SELECT id FROM vehicles")
    for row in rows:
        await regenerate_vehicle_csv(pool, str(row["id"]))


# ─────────────────────────────────────────────────────────────────────────────
# Main Event Loop
# ─────────────────────────────────────────────────────────────────────────────

async def run() -> None:
    logger.info("CSV Sync Worker starting…")

    pool = await asyncpg.create_pool(dsn=POSTGRES_DSN, min_size=1, max_size=3)
    redis = aioredis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        decode_responses=True,
    )

    # Generate all CSVs on startup
    await regenerate_character_csvs(pool)
    await regenerate_all_vehicle_csvs(pool)

    pubsub = redis.pubsub()
    await pubsub.subscribe(CHANNEL)
    logger.info("Subscribed to Redis channel '%s'. Listening…", CHANNEL)

    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            event      = json.loads(message["data"])
            event_type = event.get("event", "state_commit")

            if event_type == "state_commit":
                logger.info(
                    "state_commit: character=%s lethal=%s",
                    event.get("character_id"),
                    event.get("lethal"),
                )
                await regenerate_character_csvs(pool)

            elif event_type == "vehicle_commit":
                vehicle_id = event.get("vehicle_id", "")
                logger.info(
                    "vehicle_commit: vehicle=%s hull_delta=%s",
                    vehicle_id,
                    event.get("hull_delta"),
                )
                if vehicle_id:
                    await regenerate_vehicle_csv(pool, vehicle_id)

        except Exception as exc:
            logger.exception("CSV sync failed for event: %s", exc)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(run())
