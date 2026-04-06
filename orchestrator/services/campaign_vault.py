"""
Campaign Vault — Multi-Tenant Isolated Campaign Storage
=======================================================
Implements the Database-per-Tenant architecture described in TDR §2-3.

Each campaign is provisioned with:
  - An isolated SQLite database: <vault_dir>/campaigns/campaign_<uuid>.db
  - A unique Redis key namespace:  ironclad:campaign:<uuid>:*
  - Scoped filesystem paths:       <data_dir>/handouts/<campaign_id>/
                                   <data_dir>/echo_vault/<campaign_id>/

Security
--------
campaign_id values are validated as strict RFC 4122 UUIDs before any
filesystem operation, preventing path traversal attacks such as:
    ../campaign_admin.db
    ../../etc/passwd

Logic Flow (TDR §2B)
--------------------
1. Instance Initialization  — provision() creates the DB and initialises schema.
2. Contextual Routing       — get_db_path() returns the validated, safe path.
3. Isolated Execution       — the caller mounts only this file; no other .db
                              is accessible through this API.

Per-Campaign SQLite Schema
--------------------------
  campaign_meta   — campaign name, world, creation timestamp
  npc_memories    — NPC knowledge log, isolated per campaign
  volatile_state  — arbitrary KV store for transient in-game state
  session_log     — lightweight local copy of session events

These tables supplement the shared PostgreSQL data and are designed to be
exported, duplicated, or hibernated independently (Option 1 & 2 from TDR §4).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Strict RFC 4122 UUID v4 pattern — only this is a valid campaign_id.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_CAMPAIGN_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS campaign_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS npc_memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    npc_name    TEXT NOT NULL,
    fact        TEXT NOT NULL,
    context     TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_npc_memories_name ON npc_memories(npc_name);

CREATE TABLE IF NOT EXISTS volatile_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    expires_at  TEXT DEFAULT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_session_log_type ON session_log(event_type);
"""

# ─────────────────────────────────────────────────────────────────────────────


class CampaignVault:
    """
    Multi-tenant campaign database manager.

    Each campaign receives a fully isolated SQLite file at:
        <vault_dir>/campaigns/campaign_<uuid>.db

    All public methods accept a ``campaign_id`` that must be a valid UUID v4.
    Any non-UUID value is rejected immediately, which prevents path traversal.
    """

    def __init__(
        self,
        vault_dir: str = "/app/data/vault",
        data_dir: str = "/app/data",
    ) -> None:
        self._vault_dir = Path(vault_dir)
        self._data_dir  = Path(data_dir)
        self._campaigns_dir = self._vault_dir / "campaigns"
        self._lock = asyncio.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def init(self) -> None:
        """Create the campaigns directory tree on first boot."""
        self._campaigns_dir.mkdir(parents=True, exist_ok=True)
        logger.info("CampaignVault initialised — campaigns dir: %s", self._campaigns_dir)

    # ── Provisioning ──────────────────────────────────────────────────────────

    async def provision(
        self,
        campaign_id: str,
        name: str = "",
        world: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Path:
        """
        Provision an isolated SQLite database for *campaign_id*.

        Creates the file and initialises the schema on first call.
        Subsequent calls are idempotent — the existing database is returned.

        Args:
            campaign_id: A valid UUID v4 string.
            name:        Human-readable campaign name (stored in campaign_meta).
            world:       Active world/genre at provisioning time.
            metadata:    Arbitrary extra metadata (stored as JSON).

        Returns:
            Path to the campaign's SQLite database file.

        Raises:
            ValueError: If campaign_id is not a valid UUID v4.
        """
        db_path = self.get_db_path(campaign_id)  # validates UUID

        # Create isolated asset directories
        for bucket in ("handouts", "echo_vault"):
            (self._data_dir / bucket / campaign_id).mkdir(parents=True, exist_ok=True)

        await self._run(_init_campaign_db, db_path, campaign_id, name, world, metadata or {})
        logger.info("CampaignVault: provisioned campaign %s → %s", campaign_id, db_path)
        return db_path

    async def destroy(self, campaign_id: str) -> bool:
        """
        Permanently delete the isolated database for *campaign_id*.

        Returns True if the file existed and was removed, False otherwise.

        Raises:
            ValueError: If campaign_id is not a valid UUID v4.
        """
        db_path = self.get_db_path(campaign_id)
        if not db_path.exists():
            return False

        async with self._lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, db_path.unlink)

        logger.info("CampaignVault: destroyed campaign vault %s", campaign_id)
        return True

    # ── Path Resolution ───────────────────────────────────────────────────────

    def get_db_path(self, campaign_id: str) -> Path:
        """
        Return the absolute SQLite path for *campaign_id*.

        The path is constructed from a validated UUID, never from raw user
        input, so directory traversal is structurally impossible.

        Raises:
            ValueError: If campaign_id is not a valid UUID v4.
        """
        self._validate_campaign_id(campaign_id)
        return self._campaigns_dir / f"campaign_{campaign_id}.db"

    def is_provisioned(self, campaign_id: str) -> bool:
        """Return True if a SQLite database exists for *campaign_id*."""
        try:
            return self.get_db_path(campaign_id).exists()
        except ValueError:
            return False

    # ── Discovery ─────────────────────────────────────────────────────────────

    async def list_campaigns(self) -> list[dict]:
        """
        Return metadata for every provisioned campaign vault.

        Each entry contains:
          {
            "campaign_id":   str,
            "db_path":       str,
            "size_bytes":    int,
            "provisioned_at": str,   # ISO-8601 UTC
            "name":          str,
            "world":         str,
          }
        """
        self._campaigns_dir.mkdir(parents=True, exist_ok=True)
        results: list[dict] = []

        for db_file in sorted(self._campaigns_dir.glob("campaign_*.db")):
            stem = db_file.stem                         # "campaign_<uuid>"
            campaign_id = stem[len("campaign_"):]       # strip prefix

            if not _UUID_RE.match(campaign_id):
                continue                                # skip malformed files

            stat = db_file.stat()
            try:
                meta = await self._run(_read_meta, db_file)
            except Exception:
                meta = {}

            results.append({
                "campaign_id":    campaign_id,
                "db_path":        str(db_file),
                "size_bytes":     stat.st_size,
                "provisioned_at": meta.get("provisioned_at", ""),
                "name":           meta.get("name", ""),
                "world":          meta.get("world", ""),
            })

        return results

    # ── Per-Campaign State Operations ─────────────────────────────────────────

    async def set_volatile(
        self,
        campaign_id: str,
        key: str,
        value: Any,
        expires_at: str | None = None,
    ) -> None:
        """Write a volatile KV entry to the campaign's local database."""
        db_path = self.get_db_path(campaign_id)
        await self._run(_set_volatile, db_path, key, json.dumps(value), expires_at, _now())

    async def get_volatile(self, campaign_id: str, key: str) -> Any | None:
        """Read a volatile KV entry, returning None if absent or expired."""
        db_path = self.get_db_path(campaign_id)
        row = await self._run(_get_volatile, db_path, key, _now())
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return row[0]

    async def record_npc_memory(
        self,
        campaign_id: str,
        npc_name: str,
        fact: str,
        context: str = "",
    ) -> None:
        """Append an NPC memory fact to the campaign's isolated database."""
        db_path = self.get_db_path(campaign_id)
        await self._run(_insert_npc_memory, db_path, npc_name, fact, context, _now())

    async def get_npc_memories(
        self,
        campaign_id: str,
        npc_name: str,
        limit: int = 20,
    ) -> list[dict]:
        """Return the most recent *limit* memory facts for *npc_name*."""
        db_path = self.get_db_path(campaign_id)
        rows = await self._run(_fetch_npc_memories, db_path, npc_name, limit)
        return [
            {"npc_name": r[0], "fact": r[1], "context": r[2], "recorded_at": r[3]}
            for r in (rows or [])
        ]

    async def log_session_event(
        self,
        campaign_id: str,
        event_type: str,
        payload: dict | None = None,
    ) -> None:
        """Append a lightweight session event to the campaign's local log."""
        db_path = self.get_db_path(campaign_id)
        await self._run(
            _insert_session_event, db_path, event_type, json.dumps(payload or {}), _now()
        )

    # ── Export (Multiverse Export Protocol — TDR §4 Option 2) ─────────────────

    async def export_snapshot(self, campaign_id: str) -> dict:
        """
        Serialise the entire per-campaign SQLite database to a portable dict.

        The snapshot includes all npc_memories, volatile_state, session_log,
        and campaign_meta rows.  The caller may cryptographically sign and
        inject it into another campaign via import_snapshot().

        Raises:
            ValueError:       If campaign_id is not a valid UUID v4.
            FileNotFoundError: If no vault exists for this campaign.
        """
        db_path = self.get_db_path(campaign_id)
        if not db_path.exists():
            raise FileNotFoundError(f"No vault provisioned for campaign {campaign_id}")

        data = await self._run(_export_all, db_path)
        return {
            "campaign_id":   campaign_id,
            "exported_at":   _now(),
            "schema_version": 1,
            "data":          data,
        }

    async def import_snapshot(
        self,
        target_campaign_id: str,
        snapshot: dict,
        merge: bool = False,
    ) -> None:
        """
        Import a portable snapshot into *target_campaign_id*.

        If *merge* is False (default) volatile_state and npc_memories are
        replaced.  If True, incoming rows are appended, keeping existing data.

        Raises:
            ValueError: If target_campaign_id is not a valid UUID v4, or if
                        the snapshot schema version is unsupported.
        """
        if snapshot.get("schema_version", 0) != 1:
            raise ValueError(f"Unsupported snapshot schema version: {snapshot.get('schema_version')}")

        db_path = self.get_db_path(target_campaign_id)
        if not db_path.exists():
            await self.provision(target_campaign_id)

        await self._run(_import_all, db_path, snapshot["data"], merge)
        logger.info(
            "CampaignVault: imported snapshot from %s → %s (merge=%s)",
            snapshot.get("campaign_id"),
            target_campaign_id,
            merge,
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_campaign_id(campaign_id: str) -> None:
        """
        Raise ValueError if *campaign_id* is not a valid UUID v4.

        This is the single chokepoint that prevents path traversal.
        A UUID v4 can contain only hex digits and hyphens in a fixed pattern,
        making '../', '/', '\\', '%2F', and all other traversal payloads
        structurally impossible.
        """
        if not campaign_id or not _UUID_RE.match(campaign_id):
            raise ValueError(
                f"Invalid campaign_id {campaign_id!r}: must be a UUID v4 "
                "(e.g. 550e8400-e29b-41d4-a716-446655440000)."
            )

    async def _run(self, fn, *args):
        loop = asyncio.get_running_loop()
        async with self._lock:
            return await loop.run_in_executor(None, fn, *args)


# ── SQLite helper functions (run in executor thread) ──────────────────────────

def _init_campaign_db(
    db_path: Path,
    campaign_id: str,
    name: str,
    world: str,
    metadata: dict,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_CAMPAIGN_DDL)
        now = _now()
        for key, value in {
            "campaign_id":    campaign_id,
            "name":           name,
            "world":          world,
            "metadata":       json.dumps(metadata),
            "provisioned_at": now,
        }.items():
            conn.execute(
                "INSERT INTO campaign_meta(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, now),
            )


def _read_meta(db_path: Path) -> dict:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT key, value FROM campaign_meta").fetchall()
        return {r[0]: r[1] for r in rows}


def _set_volatile(
    db_path: Path, key: str, value: str, expires_at: str | None, now: str
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO volatile_state(key, value, expires_at, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, expires_at=excluded.expires_at, "
            "updated_at=excluded.updated_at",
            (key, value, expires_at, now),
        )


def _get_volatile(db_path: Path, key: str, now: str) -> tuple | None:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT value FROM volatile_state WHERE key=? "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (key, now),
        ).fetchone()


def _insert_npc_memory(
    db_path: Path, npc_name: str, fact: str, context: str, now: str
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO npc_memories(npc_name, fact, context, recorded_at) VALUES(?,?,?,?)",
            (npc_name, fact, context, now),
        )


def _fetch_npc_memories(
    db_path: Path, npc_name: str, limit: int
) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT npc_name, fact, context, recorded_at FROM npc_memories "
            "WHERE npc_name=? ORDER BY recorded_at DESC LIMIT ?",
            (npc_name, limit),
        ).fetchall()


def _insert_session_event(
    db_path: Path, event_type: str, payload: str, now: str
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO session_log(event_type, payload, recorded_at) VALUES(?,?,?)",
            (event_type, payload, now),
        )


def _export_all(db_path: Path) -> dict:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        def _table(name: str) -> list[dict]:
            return [dict(r) for r in conn.execute(f"SELECT * FROM {name}").fetchall()]

        return {
            "campaign_meta":  _table("campaign_meta"),
            "npc_memories":   _table("npc_memories"),
            "volatile_state": _table("volatile_state"),
            "session_log":    _table("session_log"),
        }


def _import_all(db_path: Path, data: dict, merge: bool) -> None:
    with sqlite3.connect(db_path) as conn:
        if not merge:
            conn.execute("DELETE FROM npc_memories")
            conn.execute("DELETE FROM volatile_state")

        for row in data.get("npc_memories", []):
            conn.execute(
                "INSERT OR IGNORE INTO npc_memories(npc_name, fact, context, recorded_at) "
                "VALUES(:npc_name, :fact, :context, :recorded_at)",
                row,
            )

        for row in data.get("volatile_state", []):
            conn.execute(
                "INSERT INTO volatile_state(key, value, expires_at, updated_at) VALUES "
                "(:key, :value, :expires_at, :updated_at) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "expires_at=excluded.expires_at, updated_at=excluded.updated_at",
                row,
            )

        for row in data.get("session_log", []):
            conn.execute(
                "INSERT OR IGNORE INTO session_log(event_type, payload, recorded_at) "
                "VALUES(:event_type, :payload, :recorded_at)",
                row,
            )

        for row in data.get("campaign_meta", []):
            # Only import identity fields; skip provisioned_at / campaign_id overwrite
            if row["key"] not in ("campaign_id", "provisioned_at"):
                conn.execute(
                    "INSERT INTO campaign_meta(key, value, updated_at) VALUES(:key, :value, :updated_at) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    row,
                )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
