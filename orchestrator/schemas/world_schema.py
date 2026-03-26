"""
Aetheris Dynamic Schema — World / RPG System Metadata
======================================================
A `world.json` file placed inside `data/fonts/<world_name>/` defines the full
look, feel, and AI personality for that RPG system.

File location contract
----------------------
    data/
      fonts/
        <world_name>/
          world.json          ← this schema
          *.ttf               ← fonts for Discord embed rendering (optional)

Minimal world.json (only display_name required)
-----------------------------------------------
    {"display_name": "My Homebrew"}

Full world.json
---------------
    {
      "display_name":    "Mothership",
      "primary_color":   "#FF4500",
      "default_font":    "mothership.ttf",
      "narrative_tone":  "grimdark sci-fi horror",
      "description":     "You are adrift in a dying universe...",
      "system":          "mothership",
      "dice_notation":   "percentile",
      "tags":            ["sci-fi", "horror", "survival"]
    }
"""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


class WorldSchema(BaseModel):
    """
    Metadata loaded from `data/fonts/<world_name>/world.json`.

    All fields except `display_name` are optional so that a minimal
    one-line JSON file is sufficient to manifest a new world.
    """

    display_name:   str  = Field(..., description="Human-readable name shown in Discord")
    primary_color:  str  = Field(
        default="#FFFFFF",
        description="Hex colour code for Discord embed accent and image text rendering",
        pattern=r"^#[0-9A-Fa-f]{6}$",
    )
    default_font:   str  = Field(
        default="",
        description="Filename of the .ttf to use for this world (must exist in the same folder)",
    )
    narrative_tone: str  = Field(
        default="",
        description=(
            "Short vibe descriptor injected into GM prompts, e.g. "
            "'grimdark sci-fi horror' or 'pulp swashbuckling adventure'"
        ),
    )
    description:    str  = Field(
        default="",
        description=(
            "One-paragraph world brief the AI reads before every narration call. "
            "Use this to establish setting, era, themes, and vocabulary."
        ),
    )
    system:         str  = Field(
        default="",
        description=(
            "Canonical system identifier used by the mechanical engine "
            "(e.g. 'mothership', 'shadowrun_6e', 'vtm_v5'). "
            "Defaults to the folder name if blank."
        ),
    )
    dice_notation:  str  = Field(
        default="",
        description="Dominant dice notation for this system (e.g. 'd20', 'd10', 'percentile', '2d6')",
    )
    tags:           list[str] = Field(
        default_factory=list,
        description="Free-form tags for filtering and discovery (e.g. ['horror', 'sci-fi'])",
    )
    extra:          dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary extension fields for future use",
    )

    @property
    def embed_color(self) -> int:
        """Parse primary_color hex string to Discord integer colour."""
        try:
            return int(self.primary_color.lstrip("#"), 16)
        except ValueError:
            return 0xFFFFFF

    @property
    def gm_tone_block(self) -> str:
        """
        Format the tone + description into an injection block for GM prompts.
        Returns empty string when neither tone nor description is set.
        """
        parts: list[str] = []
        if self.narrative_tone:
            parts.append(f"NARRATIVE TONE: {self.narrative_tone}.")
        if self.description:
            parts.append(f"WORLD CONTEXT: {self.description}")
        return "\n".join(parts)


class WorldSwitchRequest(BaseModel):
    """POST /api/world/switch payload."""
    campaign_id: str = Field(..., description="Campaign UUID to rebind")
    world_name:  str = Field(..., description="Folder name under data/fonts/ (created if absent)")


class WorldSwitchResponse(BaseModel):
    """Confirmation returned after a world switch."""
    campaign_id:  str
    world_name:   str
    manifested:   bool = Field(description="True if the world folder was newly created")
    schema:       WorldSchema
