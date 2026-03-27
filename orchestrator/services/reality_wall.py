"""
Reality Wall — World-State Persistence and Path Isolation
==========================================================
SQLite-backed (WAL mode) store that tracks which world/genre is active
per campaign and enforces strict path isolation so that handouts, echo
vault files, and media assets from one genre can never bleed into another.

Path contract
-------------
All world-scoped assets are rooted at:
    <data_dir>/handouts/<world_name>/
    <data_dir>/echo_vault/<world_name>/

RealityWall.resolve_handout_path() and resolve_vault_path() validate that
any requested path stays inside its genre directory, raising ValueError on
directory-traversal attempts.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS world_state (
    world_name          TEXT    PRIMARY KEY,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    metadata            TEXT    NOT NULL DEFAULT '{}',
    driftnet_channel_id TEXT    DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS campaign_world (
    campaign_id  TEXT    PRIMARY KEY,
    world_name   TEXT    NOT NULL REFERENCES world_state(world_name),
    set_at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS paradox_level (
    campaign_id   TEXT    PRIMARY KEY,
    level         INTEGER NOT NULL DEFAULT 1 CHECK(level BETWEEN 1 AND 10),
    updated_at    TEXT    NOT NULL
);
"""


class RealityWall:
    """
    SQLite-backed world-state registry.

    Thread-safe via asyncio executor; SQLite connections are created per-call
    so the service is safe to use from async context without a dedicated pool.
    """

    def __init__(self, data_dir: str = "/app/data", vault_dir: str | None = None) -> None:
        self._data_dir = Path(data_dir)
        # TDR §2: SQLite at /app/data/vault/scribe_core.db
        _vault = Path(vault_dir) if vault_dir else (self._data_dir / "vault")
        self._db_path  = _vault / "scribe_core.db"
        self._lock     = asyncio.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def init(self) -> None:
        """Create TDR-compliant directory tree and initialise the SQLite schema."""
        # TDR §3: /app/data/[asset_type]/[genre_name]/
        for asset_type in ("fonts", "templates", "handouts", "echo_vault", "vault"):
            (self._data_dir / asset_type).mkdir(parents=True, exist_ok=True)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        await self._run(_ddl_init, self._db_path)
        logger.info("RealityWall initialised — vault: %s", self._db_path)

    # ── World Registration ────────────────────────────────────────────────────

    async def register_world(self, world_name: str, metadata: dict | None = None) -> None:
        """Create a world entry and its isolated directory subtree."""
        import json
        now = _now()
        meta_str = json.dumps(metadata or {})
        await self._run(_upsert_world, self._db_path, world_name, now, meta_str)
        # Create isolated directories
        (self._data_dir / "handouts" / world_name).mkdir(parents=True, exist_ok=True)
        (self._data_dir / "echo_vault" / world_name).mkdir(parents=True, exist_ok=True)
        logger.debug("RealityWall: registered world '%s'", world_name)

    async def list_worlds(self) -> list[str]:
        rows = await self._run(_list_worlds, self._db_path)
        return [r[0] for r in rows]

    # ── Campaign ↔ World Binding ──────────────────────────────────────────────

    async def set_current_world(self, campaign_id: str, world_name: str) -> None:
        """Bind a campaign to a world, auto-registering the world if needed."""
        worlds = await self.list_worlds()
        if world_name not in worlds:
            await self.register_world(world_name)
        await self._run(_set_campaign_world, self._db_path, campaign_id, world_name, _now())
        logger.info("RealityWall: campaign %s → world '%s'", campaign_id, world_name)

    async def get_current_world(self, campaign_id: str) -> str | None:
        row = await self._run(_get_campaign_world, self._db_path, campaign_id)
        return row[0] if row else None

    # ── Driftnet Channel Binding ──────────────────────────────────────────────

    async def set_driftnet_channel(self, world_name: str, channel_id: str) -> None:
        """Bind a Discord channel ID to a world's driftnet."""
        await self._run(_set_driftnet, self._db_path, world_name, channel_id, _now())

    async def get_driftnet_channel(self, world_name: str) -> str | None:
        """Return the driftnet Discord channel ID for this world, or None."""
        row = await self._run(_get_driftnet, self._db_path, world_name)
        return row[0] if row else None

    # ── Paradox Level ─────────────────────────────────────────────────────────

    async def get_paradox_level(self, campaign_id: str) -> int:
        row = await self._run(_get_paradox, self._db_path, campaign_id)
        return int(row[0]) if row else 1

    async def set_paradox_level(self, campaign_id: str, level: int) -> None:
        level = max(1, min(10, level))
        await self._run(_set_paradox, self._db_path, campaign_id, level, _now())
        logger.info("RealityWall: campaign %s paradox_level → %d", campaign_id, level)

    # ── Path Resolution (isolation gate) ─────────────────────────────────────

    def resolve_handout_path(self, world_name: str, filename: str) -> Path:
        """
        Return the absolute path for a handout file in the given world.

        Raises ValueError if the resolved path escapes the world directory
        (directory-traversal guard).
        """
        return self._safe_path("handouts", world_name, filename)

    def resolve_vault_path(self, world_name: str, filename: str) -> Path:
        """Return the absolute path for an echo vault file, with traversal guard."""
        return self._safe_path("echo_vault", world_name, filename)

    def _safe_path(self, bucket: str, world_name: str, filename: str) -> Path:
        root     = (self._data_dir / bucket / world_name).resolve()
        resolved = (root / filename).resolve()
        if not str(resolved).startswith(str(root)):
            raise ValueError(
                f"Path traversal attempt rejected: '{filename}' escapes world '{world_name}'"
            )
        return resolved

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _run(self, fn, *args):
        loop = asyncio.get_running_loop()
        async with self._lock:
            return await loop.run_in_executor(None, fn, *args)


# ── SQLite helper functions (run in executor) ─────────────────────────────────

def _ddl_init(db_path: Path):
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_DDL)


def _upsert_world(db_path: Path, world_name: str, now: str, meta: str):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO world_state(world_name, created_at, updated_at, metadata) "
            "VALUES(?,?,?,?) ON CONFLICT(world_name) DO UPDATE SET updated_at=?, metadata=?",
            (world_name, now, now, meta, now, meta),
        )


def _list_worlds(db_path: Path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT world_name FROM world_state ORDER BY world_name").fetchall()


def _set_campaign_world(db_path: Path, campaign_id: str, world_name: str, now: str):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO campaign_world(campaign_id, world_name, set_at) VALUES(?,?,?) "
            "ON CONFLICT(campaign_id) DO UPDATE SET world_name=?, set_at=?",
            (campaign_id, world_name, now, world_name, now),
        )


def _get_campaign_world(db_path: Path, campaign_id: str):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT world_name FROM campaign_world WHERE campaign_id=?", (campaign_id,)
        ).fetchone()


def _get_paradox(db_path: Path, campaign_id: str):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT level FROM paradox_level WHERE campaign_id=?", (campaign_id,)
        ).fetchone()


def _set_driftnet(db_path: Path, world_name: str, channel_id: str, now: str):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO world_state(world_name, created_at, updated_at, driftnet_channel_id) "
            "VALUES(?,?,?,?) ON CONFLICT(world_name) DO UPDATE SET "
            "driftnet_channel_id=?, updated_at=?",
            (world_name, now, now, channel_id, channel_id, now),
        )


def _get_driftnet(db_path: Path, world_name: str):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT driftnet_channel_id FROM world_state WHERE world_name=?", (world_name,)
        ).fetchone()


def _set_paradox(db_path: Path, campaign_id: str, level: int, now: str):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO paradox_level(campaign_id, level, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(campaign_id) DO UPDATE SET level=?, updated_at=?",
            (campaign_id, level, now, level, now),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
