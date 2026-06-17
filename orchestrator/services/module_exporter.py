"""
Campaign Module Exporter — Issue #25 ("Make & Take" Module Exporter)

Packages an active or completed campaign into a portable .tar.gz archive
suitable for redistribution and re-deployment on any compatible home-lab stack.

Pipeline
--------
  1. Freeze    — Creates export_jobs record in PostgreSQL
  2. Extract   — Reads campaign tables as sanitised JSON snapshots
  3. Sanitize  — Strips Discord User IDs, active player chars, session data
  4. Bundle    — Copies world media assets (handouts + echo_vault) if requested
  5. Manifest  — Generates manifest.json with hardware-tier scaling hints
  6. Compress  — tar.gz via stdlib tarfile (zero extra dependencies)
  7. Commit    — Updates export_jobs record with archive_path and final status
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..schemas.exporter_schemas import (
    ExportJobResponse,
    ExportJobStatus,
    ExportManifest,
    HardwareTier,
    ModuleExportRequest,
)

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Discord user-ID pattern (17-20 digit snowflake)
_DISCORD_ID_RE = re.compile(r"\b[0-9]{17,20}\b")

# Hardware scaling tiers bundled into manifest.json
_HARDWARE_TIERS: list[HardwareTier] = [
    HardwareTier(
        tier_name="budget",
        min_ram_gb=4,
        recommended_model="phi3:3b-mini-4k-instruct-q4_K_M",
        quantization="Q4_K_M",
        description="ARM edge cluster / Raspberry Pi 4 / GeeekPi rack",
    ),
    HardwareTier(
        tier_name="standard",
        min_ram_gb=8,
        recommended_model="mistral:7b-instruct-q4_K_M",
        quantization="Q4_K_M",
        description="Mid-range home server, Intel NUC, entry-level NAS",
    ),
    HardwareTier(
        tier_name="performance",
        min_ram_gb=16,
        recommended_model="mistral:7b-instruct",
        quantization="none",
        description="Full-precision 7B+ on Synology DS918+ or dedicated GPU",
    ),
]

# World-state tables exported in full
_LORE_TABLES = ("story_facts", "story_entities")

# Tables exported with player PII scrubbed
_SANITIZED_TABLES = ("action_log",)

# Tables exported only for rows where is_npc=TRUE / no linked discord user
_NPC_ONLY_TABLES = ("characters", "inventories")

# Fully excluded — ephemeral or privileged
_EXCLUDED_TABLES = (
    "sessions",
    "admin_accounts",
    "gm_directives",
    "player_presence",
    "retcon_log",
)


class ModuleExporter:
    """
    Asynchronous campaign export service.

    Inject DatabaseService and configure the export output directory via
    the `export_dir` parameter.  Call `start_export()` to enqueue a
    background job and return immediately; poll `get_export_status()` for
    completion.
    """

    def __init__(self, db: Any, export_dir: Path | str = "/app/exports/modules") -> None:
        self._db = db
        self._export_dir = Path(export_dir)
        self._export_dir.mkdir(parents=True, exist_ok=True)
        self._running: dict[str, asyncio.Task] = {}

    # ── Public API ───────────────────────────────────────────────────────────

    async def start_export(self, request: ModuleExportRequest) -> ExportJobResponse:
        """Enqueue a background export and return the job record immediately."""
        if not self.validate_campaign_id(request.campaign_id):
            raise ValueError(f"Invalid campaign_id: {request.campaign_id!r}")

        job_id = await self._create_job(request)
        task = asyncio.create_task(
            self._run_export(
                job_id,
                request.campaign_id,
                request.sanitize_player_data,
                request.include_media,
            ),
            name=f"export-{job_id[:8]}",
        )
        self._running[job_id] = task
        task.add_done_callback(lambda _: self._running.pop(job_id, None))

        return ExportJobResponse(
            job_id=job_id,
            status=ExportJobStatus.queued,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    async def get_export_status(self, job_id: str) -> ExportJobResponse:
        """Return the current state of an export job."""
        async with self._db.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, status, archive_path, error_detail, manifest,
                       created_at, completed_at
                FROM   export_jobs
                WHERE  id = $1
                """,
                job_id,
            )
        if row is None:
            raise KeyError(f"Export job not found: {job_id}")

        manifest = None
        if row["manifest"]:
            manifest = ExportManifest(**json.loads(row["manifest"]))

        return ExportJobResponse(
            job_id=str(row["id"]),
            status=ExportJobStatus(row["status"]),
            archive_path=row["archive_path"],
            manifest=manifest,
            error_detail=row["error_detail"],
            created_at=row["created_at"].isoformat(),
            completed_at=row["completed_at"].isoformat() if row["completed_at"] else None,
        )

    # ── Background export pipeline ───────────────────────────────────────────

    async def _run_export((
        self,
        job_id: str,
        campaign_id: str,
        sanitize: bool,
        include_media: bool,
    ) -> None:
        await self._set_status(job_id, ExportJobStatus.running)
        tmp_dir: Path | None = None
        try:
            tmp_dir = Path(tempfile.mkdtemp(prefix=f"export_{job_id[:8]}_"))
            campaign_dir = tmp_dir / f"campaign_{campaign_id}"
            campaign_dir.mkdir()

            # Step 1: extract lore tables
            table_counts: dict[str, int] = {}
            for table in _LORE_TABLES:
                count = await self._extract_table(conn=None, table=table,
                                                   campaign_id=campaign_id,
                                                   out_dir=campaign_dir)
                table_counts[table] = count

            # Step 2: extract action log with sanitization
            for table in _SANITIZED_TABLES:
                count = await self._extract_sanitized_table(
                    table=table, campaign_id=campaign_id,
                    out_dir=campaign_dir, sanitize=sanitize,
                )
                table_counts[table] = count

            # Step 3: extract NPC-only rows
            for table in _NPC_ONLY_TABLES:
                count = await self._extract_npc_only_table(
                    table=table, campaign_id=campaign_id, out_dir=campaign_dir
                )
                table_counts[table] = count

            # Step 4: world name
            world_name = await self._get_world_name(campaign_id)

            # Step 5: bundle media
            media_file_count = 0
            if include_media:
                media_file_count = self._bundle_media(campaign_id, campaign_dir)

            # Step 6: generate manifest
            manifest = self._generate_manifest(
                campaign_id=campaign_id,
                world_name=world_name,
                sanitized=sanitize,
                include_media=include_media,
                table_counts=table_counts,
                media_file_count=media_file_count,
            )
            manifest_path = campaign_dir / "manifest.json"
            manifest_path.write_text(manifest.model_dump_json(indent=2))

            # Step 7: compress
            archive_path = self._export_dir / f"campaign_{campaign_id}_{job_id[:8]}.tar.gz"
            with tarfile.open(archive_path, "w:gz") as tar:
                tar.add(campaign_dir, arcname=f"campaign_{campaign_id}")

            manifest.archive_size_bytes = archive_path.stat().st_size

            await self._complete_job(job_id, str(archive_path), manifest)
            logger.info("Export complete: job=%s archive=%s", job_id[:8], archive_path)

        except Exception as exc:
            logger.error("Export failed: job=%s error=%s", job_id[:8], exc, exc_info=True)
            await self._fail_job(job_id, str(exc))
        finally:
            if tmp_dir and tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── Data extraction helpers ──────────────────────────────────────────────

    async def _extract_table(
        self,
        conn: Any,
        table: str,
        campaign_id: str,
        out_dir: Path,
    ) -> int:
        """Extract all rows for a campaign from a lore table."""
        async with self._db.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {table} WHERE campaign_id = $1 ORDER BY id",
                campaign_id,
            )
        data = [dict(r) for r in rows]
        self._write_json(out_dir / f"{table}.json", data)
        return len(data)

    async def _extract_sanitized_table(
        self,
        table: str,
        campaign_id: str,
        out_dir: Path,
        sanitize: bool,
    ) -> int:
        """Extract action_log, nulling player_discord_id when sanitize=True."""
        async with self._db.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {table} WHERE campaign_id = $1 ORDER BY id",
                campaign_id,
            )
        data = [dict(r) for r in rows]
        if sanitize:
            data = [self._sanitize_record(rec) for rec in data]
        self._write_json(out_dir / f"{table}.json", data)
        return len(data)

    async def _extract_npc_only_table(
        self,
        table: str,
        campaign_id: str,
        out_dir: Path,
    ) -> int:
        """Extract only NPC rows (no linked Discord user)."""
        async with self._db.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT * FROM {table}
                WHERE  campaign_id = $1
                  AND  (is_npc = TRUE OR player_discord_id IS NULL)
                ORDER  BY id
                """,
                campaign_id,
            )
        data = [dict(r) for r in rows]
        self._write_json(out_dir / f"{table}.json", data)
        return len(data)

    # ── Sanitization ─────────────────────────────────────────────────────────

    def sanitize_record(self, record: dict) -> dict:
        """Public entry point for single-record sanitization (used in tests)."""
        return self._sanitize_record(record)

    def _sanitize_record(self, record: dict) -> dict:
        """
        Scrub a single database row dict:
          - Null out any key named *discord_id* or *user_id*
          - Replace any 17-20 digit snowflake string in text values with [REDACTED]
        """
        out: dict = {}
        for key, value in record.items():
            lower = key.lower()
            if "discord_id" in lower or ("user_id" in lower and "campaign" not in lower):
                out[key] = None
            elif isinstance(value, str):
                out[key] = _DISCORD_ID_RE.sub("[REDACTED]", value)
            else:
                out[key] = value
        return out

    # ── Media bundling ───────────────────────────────────────────────────────

    def _bundle_media(
        self,
        campaign_id: str,
        out_dir: Path,
    ) -> int:
        """
        Copy handouts/{world}/ and echo_vault/{world}/ for the campaign.
        Returns the number of media files copied.
        """
        from ..services.reality_wall import RealityWall
        media_dir = out_dir / "media"
        media_dir.mkdir(exist_ok=True)
        count = 0

        base = Path("/app/data")
        for silo in ("handouts", "echo_vault"):
            silo_path = base / silo
            if not silo_path.exists():
                continue
            for world_dir in silo_path.iterdir():
                if not world_dir.is_dir():
                    continue
                dst = media_dir / silo / world_dir.name
                dst.mkdir(parents=True, exist_ok=True)
                for f in world_dir.iterdir():
                    if f.suffix.lower() in (".png", ".mp3", ".mp4", ".ogg", ".wav"):
                        shutil.copy2(f, dst / f.name)
                        count += 1
        return count

    # ── Manifest generation ──────────────────────────────────────────────────

    def _generate_manifest(
        self,
        campaign_id: str,
        world_name: str,
        sanitized: bool,
        include_media: bool,
        table_counts: dict[str, int],
        media_file_count: int,
    ) -> ExportManifest:
        return ExportManifest(
            campaign_id=campaign_id,
            world_name=world_name,
            exported_at=datetime.now(timezone.utc).isoformat(),
            sanitized=sanitized,
            media_included=include_media,
            hardware_tiers=_HARDWARE_TIERS,
            required_models=["mistral:7b-instruct-q4_K_M"],
            table_counts=table_counts,
            media_file_count=media_file_count,
        )

    # ── DB helpers ───────────────────────────────────────────────────────────

    async def _create_job(self, request: ModuleExportRequest) -> str:
        async with self._db.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO export_jobs (campaign_id, sanitized, include_media)
                VALUES ($1, $2, $3)
                RETURNING id::TEXT
                """,
                request.campaign_id,
                request.sanitize_player_data,
                request.include_media,
            )
        return row["id"]

    async def _set_status(self, job_id: str, status: ExportJobStatus) -> None:
        col = "started_at" if status == ExportJobStatus.running else "completed_at"
        async with self._db.pool.acquire() as conn:
            await conn.execute(
                f"""
                UPDATE export_jobs
                SET    status = $1, {col} = NOW()
                WHERE  id = $2
                """,
                status.value,
                job_id,
            )

    async def _complete_job(
        self, job_id: str, archive_path: str, manifest: ExportManifest
    ) -> None:
        async with self._db.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE export_jobs
                SET    status = 'complete',
                       archive_path = $1,
                       manifest = $2,
                       completed_at = NOW()
                WHERE  id = $3
                """,
                archive_path,
                manifest.model_dump_json(),
                job_id,
            )

    async def _fail_job(self, job_id: str, error_detail: str) -> None:
        async with self._db.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE export_jobs
                SET    status = 'failed',
                       error_detail = $1,
                       completed_at = NOW()
                WHERE  id = $2
                """,
                error_detail[:2000],
                job_id,
            )

    async def _get_world_name(self, campaign_id: str) -> str:
        """Read the active world name from campaigns table."""
        try:
            async with self._db.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT world_name FROM campaigns WHERE id = $1", campaign_id
                )
            return row["world_name"] if row and row["world_name"] else "unknown"
        except Exception:
            return "unknown"

    # ── Utilities ────────────────────────────────────────────────────────────

    @staticmethod
    def validate_campaign_id(campaign_id: str) -> bool:
        """Reject non-UUID4 strings before any filesystem operations."""
        if not isinstance(campaign_id, str):
            return False
        return bool(_UUID4_RE.match(campaign_id))

    @staticmethod
    def _write_json(path: Path, data: list[dict]) -> None:
        """Write a list of dicts to a JSON file, handling non-serialisable types."""
        def _default(obj: Any) -> Any:
            if hasattr(obj, "isoformat"):
                return obj.isoformat()
            return str(obj)

        path.write_text(json.dumps(data, indent=2, default=_default))
