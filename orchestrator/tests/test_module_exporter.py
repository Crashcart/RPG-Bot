"""
Tests for orchestrator.services.module_exporter (Issue #25).

All PostgreSQL and filesystem calls are mocked — no live DB required.
"""
from __future__ import annotations

import asyncio
import json
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.schemas.exporter_schemas import (
    ExportJobStatus,
    ExportManifest,
    ModuleExportRequest,
)
from orchestrator.services.module_exporter import ModuleExporter, _HARDWARE_TIERS


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_db():
    """Minimal DatabaseService mock with an asyncpg pool."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock()
    conn.fetch    = AsyncMock(return_value=[])
    conn.execute  = AsyncMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__  = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire.return_value = conn

    db = MagicMock()
    db.pool = pool
    return db, conn


@pytest.fixture
def exporter(tmp_path, mock_db):
    db, _ = mock_db
    return ModuleExporter(db=db, export_dir=tmp_path / "exports")


VALID_UUID   = "12345678-1234-4123-8123-123456789abc"
INVALID_UUID = "not-a-uuid"


# ── TestValidateCampaignId ────────────────────────────────────────────────────

class TestValidateCampaignId:
    def test_valid_uuid4(self):
        assert ModuleExporter.validate_campaign_id(VALID_UUID) is True

    def test_invalid_string(self):
        assert ModuleExporter.validate_campaign_id(INVALID_UUID) is False

    def test_empty_string(self):
        assert ModuleExporter.validate_campaign_id("") is False

    def test_not_a_string(self):
        assert ModuleExporter.validate_campaign_id(None) is False  # type: ignore

    def test_uuid_wrong_version(self):
        # v1 UUID — should fail
        assert ModuleExporter.validate_campaign_id("12345678-1234-1123-a123-123456789abc") is False

    def test_path_traversal_attempt(self):
        assert ModuleExporter.validate_campaign_id("../../../etc/passwd") is False


# ── TestStartExport ───────────────────────────────────────────────────────────

class TestStartExport:
    @pytest.mark.asyncio
    async def test_invalid_campaign_id_raises(self, exporter):
        req = ModuleExportRequest(campaign_id=INVALID_UUID)
        with pytest.raises(ValueError, match="Invalid campaign_id"):
            await exporter.start_export(req)

    @pytest.mark.asyncio
    async def test_returns_queued_status(self, exporter, mock_db):
        _, conn = mock_db
        conn.fetchrow.return_value = {"id": "job-uuid-1234"}
        req = ModuleExportRequest(campaign_id=VALID_UUID)
        with patch.object(asyncio, "create_task", return_value=MagicMock()) as mock_task:
            result = await exporter.start_export(req)
        assert result.status == ExportJobStatus.queued
        assert result.job_id == "job-uuid-1234"

    @pytest.mark.asyncio
    async def test_background_task_created(self, exporter, mock_db):
        _, conn = mock_db
        conn.fetchrow.return_value = {"id": "job-abc"}
        req = ModuleExportRequest(campaign_id=VALID_UUID)
        with patch.object(asyncio, "create_task") as mock_ct:
            mock_ct.return_value = MagicMock(add_done_callback=MagicMock())
            await exporter.start_export(req)
        mock_ct.assert_called_once()


# ── TestGetExportStatus ───────────────────────────────────────────────────────

class TestGetExportStatus:
    @pytest.mark.asyncio
    async def test_not_found_raises(self, exporter, mock_db):
        _, conn = mock_db
        conn.fetchrow.return_value = None
        with pytest.raises(KeyError, match="Export job not found"):
            await exporter.get_export_status("missing-job")

    @pytest.mark.asyncio
    async def test_returns_running_status(self, exporter, mock_db):
        from datetime import datetime, timezone
        _, conn = mock_db
        now = datetime.now(timezone.utc)
        conn.fetchrow.return_value = {
            "id": "job-1",
            "status": "running",
            "archive_path": None,
            "error_detail": None,
            "manifest": None,
            "created_at": now,
            "completed_at": None,
        }
        result = await exporter.get_export_status("job-1")
        assert result.status == ExportJobStatus.running
        assert result.archive_path is None

    @pytest.mark.asyncio
    async def test_parses_manifest_json(self, exporter, mock_db):
        from datetime import datetime, timezone
        _, conn = mock_db
        now = datetime.now(timezone.utc)
        manifest = ExportManifest(
            campaign_id=VALID_UUID,
            world_name="mothership",
            exported_at=now.isoformat(),
            sanitized=True,
            media_included=False,
        )
        conn.fetchrow.return_value = {
            "id": "job-2",
            "status": "complete",
            "archive_path": "/tmp/export.tar.gz",
            "error_detail": None,
            "manifest": manifest.model_dump_json(),
            "created_at": now,
            "completed_at": now,
        }
        result = await exporter.get_export_status("job-2")
        assert result.status == ExportJobStatus.complete
        assert result.manifest is not None
        assert result.manifest.world_name == "mothership"


# ── TestSanitizeRecord ────────────────────────────────────────────────────────

class TestSanitizeRecord:
    def test_nulls_discord_id_key(self, exporter):
        record = {"player_discord_id": "123456789012345678", "name": "Aragorn"}
        result = exporter.sanitize_record(record)
        assert result["player_discord_id"] is None
        assert result["name"] == "Aragorn"

    def test_nulls_user_id_key(self, exporter):
        record = {"user_id": "987654321098765432", "action": "attack"}
        result = exporter.sanitize_record(record)
        assert result["user_id"] is None

    def test_redacts_snowflake_in_text(self, exporter):
        record = {"narration": "Player 123456789012345678 attacked the goblin."}
        result = exporter.sanitize_record(record)
        assert "[REDACTED]" in result["narration"]
        assert "123456789012345678" not in result["narration"]

    def test_preserves_non_pii_fields(self, exporter):
        record = {"outcome": "hit", "damage": 12, "campaign_id": VALID_UUID}
        result = exporter.sanitize_record(record)
        assert result["outcome"] == "hit"
        assert result["damage"] == 12
        assert result["campaign_id"] == VALID_UUID

    def test_campaign_id_not_nulled(self, exporter):
        # campaign_id contains 'id' but should NOT be nulled
        record = {"campaign_id": VALID_UUID}
        result = exporter.sanitize_record(record)
        assert result["campaign_id"] == VALID_UUID


# ── TestGenerateManifest ──────────────────────────────────────────────────────

class TestGenerateManifest:
    def test_manifest_has_three_hardware_tiers(self, exporter):
        m = exporter._generate_manifest(
            campaign_id=VALID_UUID,
            world_name="shadowrun",
            sanitized=True,
            include_media=False,
            table_counts={"story_facts": 10, "characters": 5},
            media_file_count=0,
        )
        assert len(m.hardware_tiers) == 3
        tier_names = [t.tier_name for t in m.hardware_tiers]
        assert "budget" in tier_names
        assert "performance" in tier_names

    def test_manifest_world_name_set(self, exporter):
        m = exporter._generate_manifest(
            campaign_id=VALID_UUID,
            world_name="pirate_borg",
            sanitized=True,
            include_media=True,
            table_counts={},
            media_file_count=7,
        )
        assert m.world_name == "pirate_borg"
        assert m.media_file_count == 7
        assert m.sanitized is True

    def test_manifest_serialises_to_json(self, exporter):
        m = exporter._generate_manifest(
            campaign_id=VALID_UUID,
            world_name="mothership",
            sanitized=False,
            include_media=True,
            table_counts={"story_facts": 3},
            media_file_count=0,
        )
        raw = m.model_dump_json()
        parsed = json.loads(raw)
        assert parsed["schema_version"] == "1.0"
        assert parsed["campaign_id"] == VALID_UUID


# ── TestArchiveCreation ───────────────────────────────────────────────────────

class TestArchiveCreation:
    def test_write_json_creates_file(self, tmp_path, exporter):
        data = [{"id": "1", "name": "Grix"}]
        out = tmp_path / "test.json"
        ModuleExporter._write_json(out, data)
        assert out.exists()
        loaded = json.loads(out.read_text())
        assert loaded[0]["name"] == "Grix"

    def test_write_json_handles_datetime(self, tmp_path, exporter):
        from datetime import datetime, timezone
        data = [{"ts": datetime.now(timezone.utc)}]
        out = tmp_path / "ts.json"
        ModuleExporter._write_json(out, data)
        parsed = json.loads(out.read_text())
        assert isinstance(parsed[0]["ts"], str)

    def test_tar_gz_roundtrip(self, tmp_path):
        src = tmp_path / "payload"
        src.mkdir()
        (src / "manifest.json").write_text('{"hello": true}')
        archive = tmp_path / "out.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(src, arcname="payload")
        assert archive.exists()
        assert archive.stat().st_size > 0
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
        assert any("manifest.json" in n for n in names)
