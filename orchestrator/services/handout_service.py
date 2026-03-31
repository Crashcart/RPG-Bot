"""
Ironclad GM – Handout Service
================================
Manages in-world documents that the GM creates and delivers to players.

Handouts can be:
- AI-authored (GM provides a brief, Gemini writes the in-character text)
- Manually authored (GM writes the text directly)
- Triggered automatically by the GM Director (handout_id in NarrativeResponsePayload)

Documents are stored in the `handouts` table; delivery tracking lives in
`handout_recipients`.  The Discord bot DMs the full content to the player
and shows a summary embed in the main channel.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# Prompt constant used by ai_write_handout
_IN_WORLD_DOCUMENT_PROMPT = """You are writing an in-world document for a tabletop RPG.
The document must be written entirely in the voice of the fictional world — no fourth-wall breaks,
no meta-game language, no modern references unless the setting warrants it.

Document type: {handout_type}
Title: {title}
Context brief (GM notes, not shown to players): {brief}
Tone: {tone}

Write the complete document text now. It should feel authentic to the genre and setting.
Length: 100-300 words. Use appropriate formatting for the document type
(e.g. a letter has a salutation and signature; a journal entry is personal and dated).
Output ONLY the document text — no preamble, no explanation."""


class HandoutService:
    """
    Creates, stores, and delivers in-world handout documents.
    """

    def __init__(self, db, gemini_client) -> None:
        """
        Args:
            db:            DatabaseService instance.
            gemini_client: GeminiClient for AI authoring.
        """
        self._db     = db
        self._gemini = gemini_client

    # ------------------------------------------------------------------
    # AI authoring
    # ------------------------------------------------------------------

    async def ai_write_handout(
        self,
        campaign_id:   str,
        title:         str,
        handout_type:  str = "general",
        brief:         str = "",
        tone:          str = "authentic to the campaign's setting",
    ) -> str:
        """
        Ask Gemini to write the handout content given a GM brief.

        Returns the generated content_text string.
        """
        prompt = _IN_WORLD_DOCUMENT_PROMPT.format(
            handout_type=handout_type,
            title=title,
            brief=brief or "No additional context provided.",
            tone=tone,
        )
        try:
            content = await self._gemini.generate(
                system=("You are a professional fantasy/sci-fi writer creating in-world documents "
                        "for an immersive tabletop RPG experience."),
                user=prompt,
                max_tokens=500,
                temperature=0.8,
            )
            return content.strip()
        except Exception as exc:
            logger.error("HandoutService.ai_write_handout failed: %s", exc)
            return f"[Document could not be generated: {exc}]"

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create_handout(
        self,
        campaign_id:   str,
        title:         str,
        content_text:  str,
        handout_type:  str = "general",
        image_url:     str = "",
        creator:       str = "gm",
        is_global:     bool = False,
    ) -> str:
        """
        Insert a handout into the DB.  Returns the new handout UUID.
        """
        handout_id = str(uuid.uuid4())
        await self._db.execute(
            """
            INSERT INTO handouts
                (id, campaign_id, title, content_text, image_url, handout_type, creator, is_global)
            VALUES ($1, $2, $3, $4, $5, $6::handout_type, $7, $8)
            """,
            handout_id,
            campaign_id,
            title,
            content_text,
            image_url,
            handout_type,
            creator,
            is_global,
        )
        return handout_id

    async def get_handout(self, handout_id: str) -> dict[str, Any] | None:
        """Fetch a single handout by UUID."""
        row = await self._db.fetchrow(
            "SELECT * FROM handouts WHERE id = $1", handout_id
        )
        return dict(row) if row else None

    async def list_campaign_handouts(
        self,
        campaign_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List all handouts for a campaign, newest first."""
        rows = await self._db.fetch(
            "SELECT * FROM handouts WHERE campaign_id = $1 ORDER BY created_at DESC LIMIT $2",
            campaign_id,
            limit,
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    async def deliver(self, handout_id: str, player_id: str) -> None:
        """
        Mark a handout as delivered to a player.
        Uses INSERT ... ON CONFLICT DO NOTHING so double-delivers are harmless.
        """
        await self._db.execute(
            """
            INSERT INTO handout_recipients (handout_id, player_id)
            VALUES ($1, $2)
            ON CONFLICT (handout_id, player_id) DO NOTHING
            """,
            handout_id,
            player_id,
        )

    async def get_player_handouts(
        self,
        player_id:   str,
        campaign_id: str,
    ) -> list[dict[str, Any]]:
        """Return all handouts delivered to a specific player."""
        rows = await self._db.fetch(
            """
            SELECT h.*, hr.delivered_at
            FROM handouts h
            JOIN handout_recipients hr ON h.id = hr.handout_id
            WHERE hr.player_id = $1 AND h.campaign_id = $2
            ORDER BY hr.delivered_at DESC
            """,
            player_id,
            campaign_id,
        )
        return [dict(r) for r in rows]

    async def get_pending_for_player(self, player_id: str) -> list[dict[str, Any]]:
        """
        Return global handouts that haven't been delivered to this player yet.
        Used by the bot's /handout list command to surface any missed documents.
        """
        rows = await self._db.fetch(
            """
            SELECT h.*
            FROM handouts h
            WHERE h.is_global = TRUE
              AND NOT EXISTS (
                  SELECT 1 FROM handout_recipients hr
                  WHERE hr.handout_id = h.id AND hr.player_id = $1
              )
            ORDER BY h.created_at DESC
            LIMIT 20
            """,
            player_id,
        )
        return [dict(r) for r in rows]

    async def get_delivery_status(
        self, handout_id: str
    ) -> list[dict[str, Any]]:
        """Return the list of players who have received this handout."""
        rows = await self._db.fetch(
            "SELECT player_id, delivered_at FROM handout_recipients WHERE handout_id = $1",
            handout_id,
        )
        return [dict(r) for r in rows]
