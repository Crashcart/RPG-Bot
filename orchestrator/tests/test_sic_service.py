"""
Unit tests for orchestrator/services/sic.py

Covers:
  SystemIntegrityCheck.run                  — status aggregation
  SystemIntegrityCheck._check_paths         — pillar 1
  SystemIntegrityCheck._check_database      — pillar 2
  SystemIntegrityCheck._check_gpu           — pillar 3
  SystemIntegrityCheck._check_permissions   — pillar 4
  SystemIntegrityCheck._persist             — Redis write
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from orchestrator.services.sic import (
    PillarResult,
    SICResult,
    SystemIntegrityCheck,
    _permission_probe,
    _sqlite_integrity_check,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_env(tmp_path):
    """Return a data_dir + backups_dir pair with the required subdirectory structure."""
    data_dir = tmp_path / "data"
    (data_dir / "vault").mkdir(parents=True)
    (data_dir / "fonts").mkdir()
    (data_dir / "templates").mkdir()
    (data_dir / "handouts").mkdir()
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    # Create a minimal valid SQLite DB
    db_path = data_dir / "vault" / "scribe_core.db"
    conn = sqlite3.connect(db_path)
    conn.close()
    return data_dir, backups_dir


def _sic(tmp_env, *, cache=None, ollama_host="http://brain:11434"):
    data_dir, backups_dir = tmp_env
    return SystemIntegrityCheck(
        data_dir=str(data_dir),
        backups_dir=str(backups_dir),
        ollama_host=ollama_host,
        cache=cache,
    )


# ---------------------------------------------------------------------------
# TestRun — status aggregation
# ---------------------------------------------------------------------------

class TestRun:
    @pytest.mark.asyncio
    async def test_all_pass_returns_healthy(self, tmp_env):
        sic = _sic(tmp_env)
        with patch.object(sic, "_check_gpu", AsyncMock(return_value=PillarResult(
            name="gpu_passthrough", passed=True, critical=False, message="OK"
        ))):
            result = await sic.run()
        assert result.status == "healthy"
        assert len(result.pillars) == 4

    @pytest.mark.asyncio
    async def test_critical_fail_returns_critical(self, tmp_env):
        sic = _sic(tmp_env)
        with patch.object(sic, "_check_paths", AsyncMock(return_value=PillarResult(
            name="path_validation", passed=False, critical=True, message="DB missing"
        ))), patch.object(sic, "_check_gpu", AsyncMock(return_value=PillarResult(
            name="gpu_passthrough", passed=True, critical=False, message="OK"
        ))):
            result = await sic.run()
        assert result.status == "critical"

    @pytest.mark.asyncio
    async def test_warning_only_returns_unstable(self, tmp_env):
        sic = _sic(tmp_env)
        # Override GPU as warning (non-critical fail)
        with patch.object(sic, "_check_gpu", AsyncMock(return_value=PillarResult(
            name="gpu_passthrough", passed=False, critical=False, message="CPU mode"
        ))):
            result = await sic.run()
        assert result.status == "unstable"

    @pytest.mark.asyncio
    async def test_pillar_exception_produces_critical_fail(self, tmp_env):
        sic = _sic(tmp_env)
        with patch.object(sic, "_check_paths", AsyncMock(side_effect=RuntimeError("boom"))):
            with patch.object(sic, "_check_gpu", AsyncMock(return_value=PillarResult(
                name="gpu_passthrough", passed=True, critical=False, message="OK"
            ))):
                result = await sic.run()
        assert result.status == "critical"

    @pytest.mark.asyncio
    async def test_run_calls_persist_when_cache_provided(self, tmp_env):
        cache = MagicMock()
        cache.set = AsyncMock()
        sic = _sic(tmp_env, cache=cache)
        with patch.object(sic, "_check_gpu", AsyncMock(return_value=PillarResult(
            name="gpu_passthrough", passed=True, critical=False, message="OK"
        ))):
            # _persist is fire-and-forget (asyncio.create_task); call it directly
            with patch("orchestrator.services.sic.asyncio.create_task") as mock_task:
                result = await sic.run()
                mock_task.assert_called_once()


# ---------------------------------------------------------------------------
# TestCheckPaths — pillar 1
# ---------------------------------------------------------------------------

class TestCheckPaths:
    @pytest.mark.asyncio
    async def test_all_paths_present_passes(self, tmp_env):
        sic = _sic(tmp_env)
        result = await sic._check_paths()
        assert result.passed is True
        assert result.critical is True

    @pytest.mark.asyncio
    async def test_db_missing_critical_fail(self, tmp_env):
        data_dir, backups_dir = tmp_env
        (data_dir / "vault" / "scribe_core.db").unlink()
        sic = _sic(tmp_env)
        result = await sic._check_paths()
        assert result.passed is False
        assert result.critical is True
        assert "scribe_core.db" in result.message.lower() or "reality anchor" in result.message.lower()

    @pytest.mark.asyncio
    async def test_asset_dirs_missing_non_critical(self, tmp_env):
        data_dir, backups_dir = tmp_env
        import shutil
        shutil.rmtree(data_dir / "fonts")
        shutil.rmtree(data_dir / "templates")
        sic = _sic(tmp_env)
        result = await sic._check_paths()
        assert result.passed is False
        assert result.critical is False


# ---------------------------------------------------------------------------
# TestCheckDatabase — pillar 2
# ---------------------------------------------------------------------------

class TestCheckDatabase:
    @pytest.mark.asyncio
    async def test_valid_db_passes(self, tmp_env):
        sic = _sic(tmp_env)
        result = await sic._check_database()
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_db_missing_fails(self, tmp_env):
        data_dir, backups_dir = tmp_env
        (data_dir / "vault" / "scribe_core.db").unlink()
        sic = _sic(tmp_env)
        result = await sic._check_database()
        assert result.passed is False
        assert result.critical is True

    @pytest.mark.asyncio
    async def test_corrupted_db_fails(self, tmp_env):
        data_dir, _ = tmp_env
        db_path = data_dir / "vault" / "scribe_core.db"
        with patch(
            "orchestrator.services.sic._sqlite_integrity_check",
            return_value="*** in database main\nPage 3: wrong \"1\" expected=5",
        ):
            sic = _sic(tmp_env)
            result = await sic._check_database()
        assert result.passed is False
        assert "corrupt" in result.message.lower() or "detected" in result.message.lower()

    @pytest.mark.asyncio
    async def test_executor_exception_fails(self, tmp_env):
        with patch(
            "orchestrator.services.sic._sqlite_integrity_check",
            side_effect=sqlite3.DatabaseError("unreadable"),
        ):
            sic = _sic(tmp_env)
            result = await sic._check_database()
        assert result.passed is False
        assert result.critical is True


# ---------------------------------------------------------------------------
# TestCheckGpu — pillar 3
# ---------------------------------------------------------------------------

class TestCheckGpu:
    @pytest.mark.asyncio
    async def test_vram_detected_passes(self, tmp_env):
        sic = _sic(tmp_env)
        mock_resp_ps = MagicMock()
        mock_resp_ps.status_code = 200
        mock_resp_ps.json.return_value = {"models": [{"size_vram": 1024 * 1024 * 4096}]}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp_ps)

        with patch("orchestrator.services.sic.httpx.AsyncClient", return_value=mock_client):
            result = await sic._check_gpu()

        assert result.passed is True
        assert result.critical is False

    @pytest.mark.asyncio
    async def test_brain_alive_no_vram_warning(self, tmp_env):
        sic = _sic(tmp_env)
        mock_resp_ps = MagicMock()
        mock_resp_ps.status_code = 200
        mock_resp_ps.json.return_value = {"models": [{"size_vram": 0}]}

        mock_resp_tags = MagicMock()
        mock_resp_tags.status_code = 200
        mock_resp_tags.json.return_value = {"models": ["llama3"]}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=[mock_resp_ps, mock_resp_tags])

        with patch("orchestrator.services.sic.httpx.AsyncClient", return_value=mock_client):
            result = await sic._check_gpu()

        assert result.passed is False
        assert result.critical is False
        assert "cpu" in result.message.lower() or "latency" in result.message.lower()

    @pytest.mark.asyncio
    async def test_brain_unreachable_warning(self, tmp_env):
        sic = _sic(tmp_env)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with patch("orchestrator.services.sic.httpx.AsyncClient", return_value=mock_client):
            result = await sic._check_gpu()

        assert result.passed is False
        assert result.critical is False
        assert "unreachable" in result.message.lower()

    @pytest.mark.asyncio
    async def test_unexpected_exception_warning(self, tmp_env):
        sic = _sic(tmp_env)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("random error"))

        with patch("orchestrator.services.sic.httpx.AsyncClient", return_value=mock_client):
            result = await sic._check_gpu()

        assert result.passed is False
        assert result.critical is False


# ---------------------------------------------------------------------------
# TestCheckPermissions — pillar 4
# ---------------------------------------------------------------------------

class TestCheckPermissions:
    @pytest.mark.asyncio
    async def test_write_succeeds_passes(self, tmp_env):
        sic = _sic(tmp_env)
        result = await sic._check_permissions()
        assert result.passed is True
        assert result.critical is True

    @pytest.mark.asyncio
    async def test_write_fails_critical(self, tmp_env):
        with patch(
            "orchestrator.services.sic._permission_probe",
            side_effect=PermissionError("read-only filesystem"),
        ):
            sic = _sic(tmp_env)
            result = await sic._check_permissions()
        assert result.passed is False
        assert result.critical is True
        assert "lockout" in result.message.lower() or "fail" in result.message.lower()


# ---------------------------------------------------------------------------
# TestPersist
# ---------------------------------------------------------------------------

class TestPersist:
    @pytest.mark.asyncio
    async def test_writes_json_and_status_to_redis(self, tmp_env):
        cache = MagicMock()
        cache.set = AsyncMock()
        sic = _sic(tmp_env, cache=cache)
        result = SICResult(status="healthy", pillars=[])
        await sic._persist(result)

        assert cache.set.call_count == 2
        keys = {c[0][0] for c in cache.set.call_args_list}
        assert "ironclad:sic:result" in keys
        assert "ironclad:sic:status" in keys

    @pytest.mark.asyncio
    async def test_redis_error_is_silent(self, tmp_env):
        cache = MagicMock()
        cache.set = AsyncMock(side_effect=Exception("Redis down"))
        sic = _sic(tmp_env, cache=cache)
        result = SICResult(status="healthy", pillars=[])
        # Must not raise
        await sic._persist(result)


# ---------------------------------------------------------------------------
# TestHelpers — thread-safe executor functions
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_sqlite_integrity_check_ok(self, tmp_path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(db)
        conn.close()
        assert _sqlite_integrity_check(db) == "ok"

    def test_permission_probe_creates_and_removes_sentinel(self, tmp_path):
        _permission_probe(tmp_path)
        assert not (tmp_path / ".sic_probe").exists()

    def test_permission_probe_creates_dir_if_missing(self, tmp_path):
        target = tmp_path / "new_subdir"
        _permission_probe(target)
        assert target.is_dir()
