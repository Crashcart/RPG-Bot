"""
Ironclad GM — World Object Tracker Schemas
==========================================
Pydantic data contracts for the persistent world-object system (Issue #7).
Kept in a companion file so payloads.py stays focused on pipeline phases.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class WorldObjectStatus(str, Enum):
    ACTIVE    = "active"
    LOCKED    = "locked"
    CONSUMED  = "consumed"
    DESTROYED = "destroyed"


class WorldObjectRecord(BaseModel):
    """
    Full representation of one world object row.
    Returned by ObjectTracker methods and from API endpoints.
    """
    entity_id:        str
    campaign_id:      str
    base_description: str
    image_url:        str                  = ""
    current_state:    dict[str, Any]       = Field(default_factory=dict)
    parent_entity_id: str | None           = None
    object_status:    WorldObjectStatus    = WorldObjectStatus.ACTIVE
    created_at:       datetime
    updated_at:       datetime


class RegisterObjectRequest(BaseModel):
    """Request body for POST /api/objects."""
    campaign_id:      str
    base_description: str
    image_url:        str            = ""
    parent_entity_id: str | None     = None
    initial_state:    dict[str, Any] = Field(default_factory=dict)


class MutateObjectRequest(BaseModel):
    """Request body for PATCH /api/objects/{entity_id}."""
    state_patch:   dict[str, Any] = Field(default_factory=dict)
    new_image_url: str | None     = None
    new_status:    WorldObjectStatus | None = None


class ObjectContextSummary(BaseModel):
    """Token-efficient LLM-injectable single-line description of a world object."""
    entity_id:   str
    summary_str: str
    image_url:   str              = ""
    status:      WorldObjectStatus = WorldObjectStatus.ACTIVE
