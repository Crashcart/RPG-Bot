"""
Pydantic schemas for the Make & Take campaign module exporter (Issue #25).
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ExportJobStatus(str, Enum):
    queued   = "queued"
    running  = "running"
    complete = "complete"
    failed   = "failed"


class HardwareTier(BaseModel):
    """Hardware scaling tier embedded in manifest.json."""
    tier_name:          str
    min_ram_gb:         int
    recommended_model:  str
    quantization:       str
    description:        str = ""


class ExportManifest(BaseModel):
    """manifest.json written at the root of the exported archive."""
    schema_version:  str                = "1.0"
    campaign_id:     str
    world_name:      str
    exported_at:     str
    sanitized:       bool
    media_included:  bool
    hardware_tiers:  list[HardwareTier] = Field(default_factory=list)
    required_models: list[str]          = Field(default_factory=list)
    table_counts:    dict[str, int]     = Field(default_factory=dict)
    media_file_count: int               = 0
    archive_size_bytes: int             = 0


class ModuleExportRequest(BaseModel):
    campaign_id:          str
    sanitize_player_data: bool = True
    include_media:        bool = True


class ExportJobResponse(BaseModel):
    job_id:       str
    status:       ExportJobStatus
    archive_path: str | None           = None
    manifest:     ExportManifest | None = None
    error_detail: str | None           = None
    created_at:   str
    completed_at: str | None           = None
