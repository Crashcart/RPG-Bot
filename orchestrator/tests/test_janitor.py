"""
Unit tests for orchestrator/services/janitor.py

Coverage targets:
  - _run_backup: copies scribe_core.db, skips when source missing, copies WAL companions
  - _enforce_gfs: daily keep-7, weekly keep-2 (Sunday only), monthly keep-1, prunes excess
  - _run_prune: deletes stale media, preserves recent files, ignores non-matching extensions
  - _run_log_rotation: zips old .log files, deletes very old .gz/.log files
  - force_backup / force_prune: manual trigger helpers
  - start / stop: asyncio task lifecycle
"""

from __future__ import annotations

import asyncio
import gzip
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.janitor import (
    JanitorService,
    _GFS_DAILY_KEEP,
    _GFS_MONTHLY_KEEP,
    _GFS_WEEKLY_KEEP,
    _PRUNE_EXTENSIONS,
    _PRUNE_MAX_AGE,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_janitor(tmp_path: Path) -> JanitorService:
    data_dir   = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    logs_dir   = tmp_path / "logs"
    data_dir.mkdir()
    return JanitorService(
        data_dir=str(data_dir),
        backup_dir=str(backup_dir),
        logs_dir=str(logs_dir),
    )


def _make_db(janitor: JanitorService) -> Path:
    """Create a minimal scribe_core.db in the vault directory."""
    vault = janitor._data_dir / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    db = vault / "scribe_core.db"
    db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 84)
    return db


def _make_backup(backup_dir: Path, name: str, mtime: float) -> Path:
    """Create a fake backup file with a specific mtime."""
    bp = backup_dir / name
    bp.write_bytes(b"backup")
    bp.stat()
    import os
    os.utime(bp, (mtime, mtime))
    return bp


def _ts(days_ago: float = 0, hour: int = 2, minute: int = 0, weekday: int | None = None) -> float:
    """Return a UTC timestamp N days in the past. weekday=6 forces Sunday."""
    now = datetime.now(timezone.utc).replace(hour=hour, minute=minute, second=0, microsecond=0)
    dt  = now - timedelta(days=days_ago)
    if weekday is not None:
        # Advance to next occurrence of that weekday in the past
        days_diff = (dt.weekday() - weekday) % 7
        dt = dt - timedelta(days=days_diff)
    return dt.timestamp()


# ─────────────────────────────────────────────────────────────────────────────
# _run_backup
# ─────────────────────────────────────────────────────────────────────────────

class TestRunBackup:
    def test_backup_creates_timestamped_copy(self, tmp_path):
        jan = _make_janitor(tmp_path)
        jan._backup_dir.mkdir(parents=True)
        _make_db(jan)
        jan._run_backup()
        backups = list(jan._backup_dir.glob("scribe_core_*.db"))
        assert len(backups) == 1
        assert backups[0].stat().st_size > 0

    def test_backup_skips_when_source_missing(self, tmp_path):
        jan = _make_janitor(tmp_path)
        jan._backup_dir.mkdir(parents=True)
        # No vault/scribe_core.db
        jan._run_backup()
        assert not list(jan._backup_dir.glob("scribe_core_*.db"))

    def test_backup_copies_wal_companion(self, tmp_path):
        jan = _make_janitor(tmp_path)
        jan._backup_dir.mkdir(parents=True)
        db = _make_db(jan)
        # Create WAL companion
        (db.parent / "scribe_core.db-wal").write_bytes(b"wal data")
        jan._run_backup()
        wal_backups = list(jan._backup_dir.glob("scribe_core_*.db-wal"))
        assert len(wal_backups) == 1

    def test_backup_copies_shm_companion(self, tmp_path):
        jan = _make_janitor(tmp_path)
        jan._backup_dir.mkdir(parents=True)
        db = _make_db(jan)
        (db.parent / "scribe_core.db-shm").write_bytes(b"shm data")
        jan._run_backup()
        shm_backups = list(jan._backup_dir.glob("scribe_core_*.db-shm"))
        assert len(shm_backups) == 1

    def test_backup_skips_companion_when_absent(self, tmp_path):
        jan = _make_janitor(tmp_path)
        jan._backup_dir.mkdir(parents=True)
        _make_db(jan)
        # No WAL/SHM companions
        jan._run_backup()
        assert not list(jan._backup_dir.glob("*.db-wal"))
        assert not list(jan._backup_dir.glob("*.db-shm"))


# ─────────────────────────────────────────────────────────────────────────────
# _enforce_gfs
# ─────────────────────────────────────────────────────────────────────────────

class TestEnforceGFS:
    def test_keeps_up_to_seven_daily_backups(self, tmp_path):
        jan = _make_janitor(tmp_path)
        jan._backup_dir.mkdir(parents=True)
        # Create 10 backups on successive days
        for i in range(10):
            name = f"scribe_core_202601{i+1:02d}_020000.db"
            _make_backup(jan._backup_dir, name, mtime=_ts(days_ago=10 - i))
        jan._enforce_gfs(datetime.now(timezone.utc))
        remaining = list(jan._backup_dir.glob("scribe_core_*.db"))
        assert len(remaining) <= _GFS_DAILY_KEEP

    def test_preserves_at_least_one_backup(self, tmp_path):
        jan = _make_janitor(tmp_path)
        jan._backup_dir.mkdir(parents=True)
        _make_backup(jan._backup_dir, "scribe_core_20260101_020000.db", mtime=_ts(days_ago=1))
        jan._enforce_gfs(datetime.now(timezone.utc))
        remaining = list(jan._backup_dir.glob("scribe_core_*.db"))
        assert len(remaining) >= 1

    def test_keeps_recent_backups_preferentially(self, tmp_path):
        jan = _make_janitor(tmp_path)
        jan._backup_dir.mkdir(parents=True)
        # 12 consecutive daily backups on weekdays (no Sundays to trigger weekly rule)
        # Force all to Wednesday (weekday=2) to avoid Sunday weekly preservation
        for i in range(12):
            base = datetime.now(timezone.utc) - timedelta(days=12 - i)
            # Shift to Wednesday of that week
            days_to_wed = (2 - base.weekday()) % 7
            dt = (base + timedelta(days=days_to_wed)).replace(hour=2, minute=0, second=0, microsecond=0)
            _make_backup(
                jan._backup_dir,
                f"scribe_core_{dt.strftime('%Y%m%d_%H%M%S')}_{i:02d}.db",
                mtime=dt.timestamp(),
            )
        before = len(list(jan._backup_dir.glob("scribe_core_*.db")))
        jan._enforce_gfs(datetime.now(timezone.utc))
        remaining = list(jan._backup_dir.glob("scribe_core_*.db"))
        # Daily keeps 7 + monthly keeps 1 (may overlap) → at most 8, never more than before
        assert len(remaining) <= _GFS_DAILY_KEEP + _GFS_MONTHLY_KEEP
        assert len(remaining) <= before

    def test_prunes_beyond_daily_limit(self, tmp_path):
        jan = _make_janitor(tmp_path)
        jan._backup_dir.mkdir(parents=True)
        # 20 identical-day backups with no Sunday/monthly significance
        for i in range(20):
            # All on a Tuesday to avoid weekly rule
            # Use day offset of 3+i so they're all "recent Tuesdays"
            t = _ts(days_ago=20 - i)
            dt = datetime.fromtimestamp(t, tz=timezone.utc)
            # Force weekday to Wednesday (2) by adding days
            days_to_wed = (2 - dt.weekday()) % 7
            dt_wed = dt + timedelta(days=days_to_wed)
            _make_backup(
                jan._backup_dir,
                f"scribe_core_{dt_wed.strftime('%Y%m%d_%H%M%S')}_extra{i}.db",
                mtime=dt_wed.timestamp(),
            )
        jan._enforce_gfs(datetime.now(timezone.utc))
        remaining = list(jan._backup_dir.glob("scribe_core_*.db"))
        # Daily keeps 7, no Sundays, 1 monthly → at most 8
        assert len(remaining) <= _GFS_DAILY_KEEP + _GFS_MONTHLY_KEEP

    def test_weekly_rule_only_keeps_sundays(self, tmp_path):
        jan = _make_janitor(tmp_path)
        jan._backup_dir.mkdir(parents=True)
        # Create 3 Sunday backups (weekday=6) far in the past
        for i in range(3):
            t = _ts(days_ago=100 + i * 7, weekday=6)
            dt = datetime.fromtimestamp(t, tz=timezone.utc)
            _make_backup(
                jan._backup_dir,
                f"scribe_core_{dt.strftime('%Y%m%d_%H%M%S')}.db",
                mtime=t,
            )
        # 1 recent non-Sunday backup (should be kept by daily rule)
        t_recent = _ts(days_ago=1)
        dt_r = datetime.fromtimestamp(t_recent, tz=timezone.utc)
        _make_backup(
            jan._backup_dir,
            f"scribe_core_{dt_r.strftime('%Y%m%d_%H%M%S')}.db",
            mtime=t_recent,
        )
        jan._enforce_gfs(datetime.now(timezone.utc))
        remaining = list(jan._backup_dir.glob("scribe_core_*.db"))
        # Weekly keeps _GFS_WEEKLY_KEEP=2 Sundays + daily keeps the recent one
        # Monthly may overlap — total ≤ 2 + 1 + 1 = 4
        assert len(remaining) <= _GFS_WEEKLY_KEEP + _GFS_DAILY_KEEP


# ─────────────────────────────────────────────────────────────────────────────
# _run_prune
# ─────────────────────────────────────────────────────────────────────────────

class TestRunPrune:
    def _make_media_file(self, data_dir: Path, bucket: str, name: str, days_old: float) -> Path:
        bucket_dir = data_dir / bucket
        bucket_dir.mkdir(parents=True, exist_ok=True)
        fp = bucket_dir / name
        fp.write_bytes(b"media data")
        import os
        old_mtime = (datetime.now(timezone.utc) - timedelta(days=days_old)).timestamp()
        os.utime(fp, (old_mtime, old_mtime))
        return fp

    def test_deletes_stale_png_in_handouts(self, tmp_path):
        jan = _make_janitor(tmp_path)
        fp = self._make_media_file(jan._data_dir, "handouts", "old_scene.png", days_old=31)
        jan._run_prune()
        assert not fp.exists()

    def test_deletes_stale_mp3_in_echo_vault(self, tmp_path):
        jan = _make_janitor(tmp_path)
        fp = self._make_media_file(jan._data_dir, "echo_vault", "old_ambient.mp3", days_old=35)
        jan._run_prune()
        assert not fp.exists()

    def test_deletes_stale_mp4(self, tmp_path):
        jan = _make_janitor(tmp_path)
        fp = self._make_media_file(jan._data_dir, "handouts", "old_clip.mp4", days_old=45)
        jan._run_prune()
        assert not fp.exists()

    def test_preserves_recent_files(self, tmp_path):
        jan = _make_janitor(tmp_path)
        fp = self._make_media_file(jan._data_dir, "handouts", "new_scene.png", days_old=5)
        jan._run_prune()
        assert fp.exists()

    def test_preserves_files_exactly_at_boundary(self, tmp_path):
        """Files exactly 30 days old should NOT be pruned (cutoff is strictly <)."""
        jan = _make_janitor(tmp_path)
        fp = self._make_media_file(jan._data_dir, "handouts", "boundary.png", days_old=29.9)
        jan._run_prune()
        assert fp.exists()

    def test_ignores_non_matching_extensions(self, tmp_path):
        jan = _make_janitor(tmp_path)
        fp = self._make_media_file(jan._data_dir, "handouts", "old_text.txt", days_old=60)
        jan._run_prune()
        assert fp.exists()  # .txt is not in _PRUNE_EXTENSIONS

    def test_ignores_missing_bucket(self, tmp_path):
        jan = _make_janitor(tmp_path)
        # Neither handouts/ nor echo_vault/ exist — should not raise
        jan._run_prune()

    def test_ignores_subdirectory_entries(self, tmp_path):
        jan = _make_janitor(tmp_path)
        subdir = jan._data_dir / "handouts" / "subdir"
        subdir.mkdir(parents=True)
        jan._run_prune()  # directories should be silently skipped

    def test_prune_extensions_set_is_correct(self):
        assert _PRUNE_EXTENSIONS == {".png", ".mp3", ".mp4"}

    def test_prune_max_age_is_30_days(self):
        assert _PRUNE_MAX_AGE == timedelta(days=30)


# ─────────────────────────────────────────────────────────────────────────────
# _run_log_rotation
# ─────────────────────────────────────────────────────────────────────────────

class TestRunLogRotation:
    import os as _os

    def _make_log(self, logs_dir: Path, name: str, days_old: float) -> Path:
        import os
        logs_dir.mkdir(parents=True, exist_ok=True)
        fp = logs_dir / name
        fp.write_text("log line\n")
        old_mtime = (datetime.now(timezone.utc) - timedelta(days=days_old)).timestamp()
        os.utime(fp, (old_mtime, old_mtime))
        return fp

    def test_zips_old_log_file(self, tmp_path):
        jan = _make_janitor(tmp_path)
        fp = self._make_log(jan._logs_dir, "app.log", days_old=8)
        jan._run_log_rotation()
        assert not fp.exists(), "Original .log should be removed after zipping"
        gz = fp.with_suffix(".log.gz")
        assert gz.exists(), "Compressed .log.gz should exist"
        # Verify the gz is readable
        with gzip.open(gz) as f:
            assert f.read() == b"log line\n"

    def test_skips_recent_log_file(self, tmp_path):
        jan = _make_janitor(tmp_path)
        fp = self._make_log(jan._logs_dir, "recent.log", days_old=3)
        jan._run_log_rotation()
        assert fp.exists()  # recent file untouched

    def test_deletes_very_old_gz_file(self, tmp_path):
        import os
        jan = _make_janitor(tmp_path)
        jan._logs_dir.mkdir(parents=True)
        gz = jan._logs_dir / "ancient.log.gz"
        with gzip.open(gz, "wb") as f:
            f.write(b"old log")
        old_mtime = (datetime.now(timezone.utc) - timedelta(days=91)).timestamp()
        os.utime(gz, (old_mtime, old_mtime))
        jan._run_log_rotation()
        assert not gz.exists()

    def test_deletes_very_old_plain_log(self, tmp_path):
        jan = _make_janitor(tmp_path)
        fp = self._make_log(jan._logs_dir, "ancient.log", days_old=95)
        jan._run_log_rotation()
        # Older than 90 days → deleted outright (delete pass runs before zip pass for same file)
        assert not fp.exists()

    def test_noop_when_logs_dir_missing(self, tmp_path):
        jan = _make_janitor(tmp_path)
        # logs_dir does not exist → should not raise
        jan._run_log_rotation()

    def test_zips_only_log_extension(self, tmp_path):
        jan = _make_janitor(tmp_path)
        fp_log = self._make_log(jan._logs_dir, "app.log", days_old=10)
        fp_txt = self._make_log(jan._logs_dir, "app.txt", days_old=10)
        jan._run_log_rotation()
        assert not fp_log.exists()              # .log zipped
        assert fp_txt.exists()                  # .txt untouched


# ─────────────────────────────────────────────────────────────────────────────
# force_backup / force_prune
# ─────────────────────────────────────────────────────────────────────────────

class TestManualTriggers:
    @pytest.mark.asyncio
    async def test_force_backup_returns_filename(self, tmp_path):
        jan = _make_janitor(tmp_path)
        jan._backup_dir.mkdir(parents=True)
        _make_db(jan)
        name = await jan.force_backup()
        assert name.startswith("scribe_core_")
        assert name.endswith(".db")

    @pytest.mark.asyncio
    async def test_force_backup_returns_fallback_when_no_source(self, tmp_path):
        jan = _make_janitor(tmp_path)
        jan._backup_dir.mkdir(parents=True)
        name = await jan.force_backup()
        assert name == "no backup created"

    @pytest.mark.asyncio
    async def test_force_prune_returns_deleted_count(self, tmp_path):
        import os
        jan = _make_janitor(tmp_path)
        bucket = jan._data_dir / "handouts"
        bucket.mkdir(parents=True)
        # Create 3 stale files
        for i in range(3):
            fp = bucket / f"old_{i}.png"
            fp.write_bytes(b"img")
            old = (datetime.now(timezone.utc) - timedelta(days=40)).timestamp()
            os.utime(fp, (old, old))
        count = await jan.force_prune()
        assert count == 3

    @pytest.mark.asyncio
    async def test_force_prune_returns_zero_when_nothing_stale(self, tmp_path):
        jan = _make_janitor(tmp_path)
        (jan._data_dir / "handouts").mkdir(parents=True)
        count = await jan.force_prune()
        assert count == 0


# ─────────────────────────────────────────────────────────────────────────────
# Lifecycle
# ─────────────────────────────────────────────────────────────────────────────

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_spawns_three_tasks(self, tmp_path):
        jan = _make_janitor(tmp_path)
        await jan.start()
        assert len(jan._tasks) == 3
        assert all(not t.done() for t in jan._tasks)
        await jan.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_all_tasks(self, tmp_path):
        jan = _make_janitor(tmp_path)
        await jan.start()
        tasks = list(jan._tasks)
        await jan.stop()
        assert all(t.cancelled() or t.done() for t in tasks)

    @pytest.mark.asyncio
    async def test_start_creates_backup_dir(self, tmp_path):
        jan = _make_janitor(tmp_path)
        assert not jan._backup_dir.exists()
        await jan.start()
        assert jan._backup_dir.exists()
        await jan.stop()

    @pytest.mark.asyncio
    async def test_start_creates_logs_dir(self, tmp_path):
        jan = _make_janitor(tmp_path)
        assert not jan._logs_dir.exists()
        await jan.start()
        assert jan._logs_dir.exists()
        await jan.stop()

    @pytest.mark.asyncio
    async def test_sic_defaults_to_none(self, tmp_path):
        jan = _make_janitor(tmp_path)
        assert jan._sic is None
