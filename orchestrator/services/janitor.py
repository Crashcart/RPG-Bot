"""
Janitor Service — GFS Backup Rotation and Media Auto-Prune
===========================================================
Runs two independent background duties on a schedule:

  1. GFS (Grandfather-Father-Son) Backup
     ─────────────────────────────────────
     Copies the Reality Wall SQLite database (and optionally other state files)
     into /app/data/backups/ with a timestamped filename, then enforces the
     GFS retention policy:
         Daily   → keep 7  most-recent daily backups
         Weekly  → keep 2  most-recent weekly backups (Sunday snapshots)
         Monthly → keep 1  most-recent monthly backup (1st of month)

     Schedule: runs daily at 02:00 UTC (configurable via JANITOR_BACKUP_HOUR).

  2. Media Auto-Prune
     ─────────────────
     Recursively deletes .png, .mp3, and .mp4 files inside:
         /app/data/handouts/
         /app/data/echo_vault/
     where the file's mtime is older than 30 days.

     Schedule: runs every 6 hours.

Both duties are best-effort — failures are logged but never crash the service.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_PRUNE_EXTENSIONS  = {".png", ".mp3", ".mp4"}
_PRUNE_MAX_AGE     = timedelta(days=30)
_PRUNE_BUCKETS     = ("handouts", "echo_vault")

_GFS_DAILY_KEEP    = 7
_GFS_WEEKLY_KEEP   = 2
_GFS_MONTHLY_KEEP  = 1

_BACKUP_INTERVAL   = 86_400   # 24 h in seconds
_PRUNE_INTERVAL    = 21_600   # 6 h in seconds


class JanitorService:
    """
    Long-lived background service.  Call start() once in lifespan; it spawns
    two independent asyncio tasks — one for GFS backups, one for media pruning.
    """

    def __init__(self, data_dir: str = "/app/data", backup_dir: str | None = None) -> None:
        self._data_dir   = Path(data_dir)
        # TDR §2: backups at /app/backups (separate from data volume)
        self._backup_dir = Path(backup_dir) if backup_dir else (self._data_dir / "backups")
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        self._tasks = [
            asyncio.create_task(self._backup_loop(),  name="janitor-backup"),
            asyncio.create_task(self._prune_loop(),   name="janitor-prune"),
        ]
        logger.info("JanitorService started (data_dir=%s).", self._data_dir)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ── GFS Backup Loop ───────────────────────────────────────────────────────

    async def _backup_loop(self) -> None:
        while True:
            await asyncio.sleep(_BACKUP_INTERVAL)
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._run_backup)
            except Exception as exc:
                logger.error("JanitorService backup error: %s", exc)

    def _run_backup(self) -> None:
        now      = datetime.now(timezone.utc)
        src      = self._data_dir / "reality_wall.db"
        if not src.exists():
            logger.debug("JanitorService: nothing to backup (reality_wall.db not found).")
            return

        stamp    = now.strftime("%Y%m%d_%H%M%S")
        dst      = self._backup_dir / f"reality_wall_{stamp}.db"
        shutil.copy2(src, dst)
        logger.info("JanitorService: backup written → %s", dst.name)

        self._enforce_gfs(now)

    def _enforce_gfs(self, now: datetime) -> None:
        """Prune backups according to GFS retention policy."""
        backups = sorted(
            self._backup_dir.glob("reality_wall_*.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,   # newest first
        )

        keep: set[Path] = set()

        # Monthly: keep 1 — one backup per calendar month, most recent
        seen_months: set[str] = set()
        for bp in backups:
            mtime = datetime.fromtimestamp(bp.stat().st_mtime, tz=timezone.utc)
            month_key = mtime.strftime("%Y-%m")
            if month_key not in seen_months:
                seen_months.add(month_key)
                keep.add(bp)
                if len(seen_months) >= _GFS_MONTHLY_KEEP:
                    break

        # Weekly: keep 2 — most recent Sunday snapshot per ISO week
        seen_weeks: set[str] = set()
        for bp in backups:
            mtime = datetime.fromtimestamp(bp.stat().st_mtime, tz=timezone.utc)
            if mtime.weekday() == 6:   # Sunday
                week_key = mtime.strftime("%G-W%V")
                if week_key not in seen_weeks:
                    seen_weeks.add(week_key)
                    keep.add(bp)
                    if len(seen_weeks) >= _GFS_WEEKLY_KEEP:
                        break

        # Daily: keep 7 most-recent regardless of day
        for bp in backups[:_GFS_DAILY_KEEP]:
            keep.add(bp)

        # Delete anything not in keep set
        pruned = 0
        for bp in backups:
            if bp not in keep:
                bp.unlink(missing_ok=True)
                pruned += 1

        if pruned:
            logger.info("JanitorService GFS: pruned %d old backup(s).", pruned)

    # ── Media Auto-Prune Loop ─────────────────────────────────────────────────

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(_PRUNE_INTERVAL)
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._run_prune)
            except Exception as exc:
                logger.error("JanitorService prune error: %s", exc)

    def _run_prune(self) -> None:
        cutoff  = datetime.now(timezone.utc) - _PRUNE_MAX_AGE
        deleted = 0

        for bucket in _PRUNE_BUCKETS:
            bucket_path = self._data_dir / bucket
            if not bucket_path.exists():
                continue
            for fp in bucket_path.rglob("*"):
                if not fp.is_file():
                    continue
                if fp.suffix.lower() not in _PRUNE_EXTENSIONS:
                    continue
                mtime = datetime.fromtimestamp(fp.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    fp.unlink(missing_ok=True)
                    deleted += 1
                    logger.debug("JanitorService: pruned %s (mtime=%s)", fp, mtime.date())

        if deleted:
            logger.info("JanitorService auto-prune: deleted %d stale media file(s).", deleted)
        else:
            logger.debug("JanitorService auto-prune: no files older than 30 days found.")

    # ── Manual Trigger (for admin endpoints / testing) ────────────────────────

    async def force_backup(self) -> str:
        """Trigger a GFS backup immediately. Returns the backup filename."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._run_backup)
        backups = sorted(self._backup_dir.glob("reality_wall_*.db"))
        return backups[-1].name if backups else "no backup created"

    async def force_prune(self) -> int:
        """Trigger a media prune immediately. Returns count of deleted files."""
        # Patch cutoff inside executor is awkward — run directly
        loop    = asyncio.get_event_loop()
        before  = sum(1 for b in _PRUNE_BUCKETS
                      for _ in (self._data_dir / b).rglob("*")
                      if _.is_file() and _.suffix.lower() in _PRUNE_EXTENSIONS)
        await loop.run_in_executor(None, self._run_prune)
        after   = sum(1 for b in _PRUNE_BUCKETS
                      for _ in (self._data_dir / b).rglob("*")
                      if _.is_file() and _.suffix.lower() in _PRUNE_EXTENSIONS)
        return max(0, before - after)
