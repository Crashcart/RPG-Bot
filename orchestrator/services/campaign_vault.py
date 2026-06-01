"""
Campaign Vault — Per-Campaign SQLite Isolation & Hibernation
=============================================================
Each active campaign gets its own SQLite file (WAL mode) at:
    <data_dir>/vault/campaign_<campaign_id>.db

The vault serves two purposes:
  1. Isolated per-campaign key-value store for lightweight in-game state
     that doesn't warrant a full PostgreSQL round-trip.
  2. Hibernation snapshot: when a campaign is idle the caller can flush
     active session state here and spin down the worker. On the next
     player action, rehydrate() restores the snapshot.

Security
--------
- campaign_id must match UUID4 format before any filesystem operation.
- All resolved paths are checked against the vault root, preventing
  directory-traversal via crafted campaign IDs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_CAMPAIGN_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS kv_cache (
    key         TEXT    PRIMARY KEY,
    value       TEXT    NOT NULL,
    stored_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS session_snapshot (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    snapshot        TEXT    NOT NULL,
    snapshotted_at  TEXT    NOT NULL
);
"""


class VaultError(Exception):
    """Raised when an operation violates campaign vault security constraints."""


class CampaignVault:
    """
    Per-campaign SQLite isolation and hibernation manager.

    Instantiate once at application startup and pass to any service that needs
    per-campaign isolated storage or hibernation support.

        vault = CampaignVault(data_dir="/app/data")
        await vault.init()

    All public methods are async-safe. SQLite I/O is offloaded to a
    thread-pool executor; per-campaign asyncio locks prevent concurrent
    writes to the same file.
    """

    HIBERNATE_IDLE_MINUTES: int = 15

    def __init__(self, data_dir: str = "/app/data") -> None:
        self._vault_root = Path(data_dir) / "vault"
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_meta = asyncio.Lock()

    # ── Lifecycle ──────────────────────────────────────────────────────────────────────

    async def init(self) -> None:
        """Ensure the vault root directory exists."""
        self._vault_root.mkdir(parents=True, exist_ok=True)
        logger.info("CampaignVault initialised — root: %s", self._vault_root)

    # ── Provisioning ───────────────────────────────────────────────────────────────────

    async def provision(self, campaign_id: str) -> Path:
        """
        Create the per-campaign SQLite database if it doesn't already exist.

        Returns the Path to the database file.
        Idempotent — safe to call multiple times for the same campaign_id.
        """
        db_path = self._safe_db_path(campaign_id)
        lock = await self._get_lock(campaign_id)
        async with lock:
            await _arun(_init_campaign_db, db_path)
        logger.debug("CampaignVault: provisioned vault for campaign %s", campaign_id)
        return db_path

    async def destroy(self, campaign_id: str) -> bool:
        """
        Delete the per-campaign vault file.

        Returns True if a file was removed, False if it didn't exist.
        """
        db_path = self._safe_db_path(campaign_id)
        lock = await self._get_lock(campaign_id)
        async with lock:
            if db_path.exists():
                db_path.unlink()
                logger.info("CampaignVault: destroyed vault for campaign %s", campaign_id)
                return True
        return False

    async def list_vaults(self) -> list[str]:
        """Return campaign IDs for all vault files currently on disk."""
        loop = asyncio.get_running_loop()
        entries: list[Path] = await loop.run_in_executor(
            None, lambda: list(self._vault_root.glob("campaign_*.db"))
        )
        ids = []
        for p in entries:
            candidate = p.stem[len("campaign_"):]  # strip "campaign_" prefix
            if _UUID_RE.match(candidate):
                ids.append(candidate.lower())
        return sorted(ids)

    # ── Key-Value Store ──────────────────────────────────────────────────────────────────

    async def kv_set(self, campaign_id: str, key: str, value: object) -> None:
        """Persist a JSON-serialisable value under *key* in the campaign vault."""
        db_path = self._safe_db_path(campaign_id)
        serialised = json.dumps(value)
        lock = await self._get_lock(campaign_id)
        async with lock:
            await _arun(_kv_set, db_path, key, serialised, _now())

    async def kv_get(self, campaign_id: str, key: str) -> object:
        """
        Retrieve a value from the campaign vault by key.

        Returns None if the key does not exist or the vault has not been
        provisioned yet.
        """
        db_path = self._safe_db_path(campaign_id)
        if not db_path.exists():
            return None
        row = await _arun(_kv_get, db_path, key)
        return json.loads(row[0]) if row else None

    async def kv_delete(self, campaign_id: str, key: str) -> None:
        """Remove a key from the campaign vault (no-op if absent)."""
        db_path = self._safe_db_path(campaign_id)
        if not db_path.exists():
            return
        lock = await self._get_lock(campaign_id)
        async with lock:
            await _arun(_kv_delete, db_path, key)

    # ── Hibernation ─────────────────────────────────────────────────────────────────────

    async def hibernate(self, campaign_id: str, snapshot: dict) -> None:
        """
        Flush in-memory session state to the vault.

        *snapshot* should contain the full context dict (e.g. active Redis
        session keys) that must survive a worker restart. The vault file is
        created if it does not already exist.
        """
        db_path = self._safe_db_path(campaign_id)
        serialised = json.dumps(snapshot)
        lock = await self._get_lock(campaign_id)
        async with lock:
            await _arun(_init_campaign_db, db_path)
            await _arun(_write_snapshot, db_path, serialised, _now())
        logger.info("CampaignVault: hibernated campaign %s", campaign_id)

    async def rehydrate(self, campaign_id: str) -> dict:
        """
        Load the hibernation snapshot from the vault.

        Returns an empty dict for a cold-start (no vault or no snapshot) or
        if the snapshot data is corrupt. Never raises on a missing file.
        """
        db_path = self._safe_db_path(campaign_id)
        if not db_path.exists():
            return {}
        row = await _arun(_read_snapshot, db_path)
        if row is None:
            return {}
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "CampaignVault: corrupt snapshot for campaign %s — returning empty dict",
                campaign_id,
            )
            return {}

    async def clear_snapshot(self, campaign_id: str) -> None:
        """Delete the hibernation snapshot row (post-rehydration cleanup)."""
        db_path = self._safe_db_path(campaign_id)
        if not db_path.exists():
            return
        lock = await self._get_lock(campaign_id)
        async with lock:
            await _arun(_clear_snapshot, db_path)

    # ── Security helpers ──────────────────────────────────────────────────────────────────

    @staticmethod
    def validate_campaign_id(campaign_id: str) -> str:
        """
        Validate *campaign_id* is a properly-formatted UUID4.

        Returns the lower-cased campaign_id on success.
        Raises VaultError on invalid input.
        """
        if not isinstance(campaign_id, str):
            raise VaultError("campaign_id must be a string")
        cid = campaign_id.strip().lower()
        if not _UUID_RE.match(cid):
            raise VaultError(
                f"Invalid campaign_id '{campaign_id}': must be a UUID "
                "(xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)"
            )
        return cid

    def _safe_db_path(self, campaign_id: str) -> Path:
        """
        Return the absolute SQLite path for *campaign_id*.

        Raises VaultError if campaign_id fails UUID validation or if the
        resolved path would escape the vault root directory.
        """
        cid = self.validate_campaign_id(campaign_id)
        candidate = self._vault_root / f"campaign_{cid}.db"
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self._vault_root.resolve())
        except ValueError:
            raise VaultError(
                f"Path traversal attempt rejected for campaign_id '{campaign_id}'"
            )
        return resolved

    async def _get_lock(self, campaign_id: str) -> asyncio.Lock:
        async with self._locks_meta:
            if campaign_id not in self._locks:
                self._locks[campaign_id] = asyncio.Lock()
            return self._locks[campaign_id]


# ── Module-level async runner ────────────────────────────────────────────────────────────────

async def _arun(fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn, *args)


# ── SQLite helper functions (run in executor) ─────────────────────────────────────────────

def _init_campaign_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_CAMPAIGN_DDL)


def _kv_set(db_path: Path, key: str, value: str, now: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO kv_cache(key, value, stored_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, stored_at=excluded.stored_at",
            (key, value, now),
        )


def _kv_get(db_path: Path, key: str):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT value FROM kv_cache WHERE key=?", (key,)
        ).fetchone()


def _kv_delete(db_path: Path, key: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM kv_cache WHERE key=?", (key,))


def _write_snapshot(db_path: Path, snapshot: str, now: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO session_snapshot(id, snapshot, snapshotted_at) VALUES(1,?,?) "
            "ON CONFLICT(id) DO UPDATE SET snapshot=excluded.snapshot, "
            "snapshotted_at=excluded.snapshotted_at",
            (snapshot, now),
        )


def _read_snapshot(db_path: Path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT snapshot FROM session_snapshot WHERE id=1"
        ).fetchone()


def _clear_snapshot(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM session_snapshot WHERE id=1")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
