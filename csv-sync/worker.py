"""
Ironclad GM – CSV Sync Worker
================================
Subscribes to Redis pub/sub for state_commit events.
On each event, regenerates characters_living.csv and characters_dead.csv
from the PostgreSQL characters table.

Design:
  - Full-table regeneration on each event is safe at home-lab scale and
    avoids partial-write corruption.
  - Dead characters are atomically moved to characters_dead.csv.
  - The CSV files are volume-mounted and accessible to players directly.
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

# CSV column order (stats are flattened as individual columns)
LIVING_CSV = EXPORTS_DIR / "characters_living.csv"
DEAD_CSV   = EXPORTS_DIR / "characters_dead.csv"


# ─────────────────────────────────────────────────────────────────────────────
# DB Helpers
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
            "id":            str(row["id"]),
            "player_id":     row["player_id"],
            "name":          row["name"],
            "system":        row["system"],
            "status":        row["status"],
            "campaign":      row["campaign_name"] or "",
            "updated_at":    row["updated_at"].isoformat() if row["updated_at"] else "",
            **{f"stat_{k}": v for k, v in stats.items()},
        })
    return result


def _write_csv(path: Path, records: list[dict]) -> None:
    if not records:
        path.write_text("")
        return

    # Collect all keys in order (id, player_id, name, ... then stat_*)
    base_keys  = ["id", "player_id", "name", "system", "status", "campaign", "updated_at"]
    stat_keys  = sorted({k for r in records for k in r if k.startswith("stat_")})
    fieldnames = base_keys + stat_keys

    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)

    # Atomic rename
    tmp_path.replace(path)
    logger.info("Written %d records to %s", len(records), path.name)


# ─────────────────────────────────────────────────────────────────────────────
# Main Sync Logic
# ─────────────────────────────────────────────────────────────────────────────

async def regenerate_csvs(pool: asyncpg.Pool) -> None:
    living = await fetch_characters(pool, "ALIVE")
    dead   = await fetch_characters(pool, "DEAD")
    _write_csv(LIVING_CSV, living)
    _write_csv(DEAD_CSV, dead)


async def run() -> None:
    logger.info("CSV Sync Worker starting…")

    pool = await asyncpg.create_pool(dsn=POSTGRES_DSN, min_size=1, max_size=3)
    redis = aioredis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        decode_responses=True,
    )

    # Generate initial CSVs on startup
    await regenerate_csvs(pool)

    # Subscribe to commit events
    pubsub = redis.pubsub()
    await pubsub.subscribe(CHANNEL)
    logger.info("Subscribed to Redis channel '%s'. Listening…", CHANNEL)

    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            event = json.loads(message["data"])
            logger.info(
                "Received commit event: character=%s lethal=%s",
                event.get("character_id"),
                event.get("lethal"),
            )
            await regenerate_csvs(pool)
        except Exception as exc:
            logger.exception("CSV sync failed for event: %s", exc)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(run())
