"""
Ironclad GM – Faction / Reputation Service
===========================================
Tracks player reputation with named factions across a campaign.

Scores range from -100 (Enemy) to +100 (Allied):
  Allied   ≥ 75
  Friendly ≥ 40
  Neutral  ≥ 10  (also the initial default)
  Cautious ≥ -25
  Hostile  ≥ -60
  Enemy    <  -60

Scores are stored in the `factions` table as a JSONB column named
`disposition` with the schema: {"player_snowflake": score_int}.

The GM Director calls ai_adjust_from_narrative() fire-and-forget after each
narrative synthesis to update faction scores based on player actions.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_FACTION_ADJUSTMENT_PROMPT = """\
You are a faction reputation adjudicator for a tabletop RPG.
Given the player's action and the resulting narrative, determine which factions
(if any) would change their opinion of the player, and by how much.

Campaign factions: {faction_names}
Player action type: {action_type}
Narrative summary: {narrative_excerpt}

Respond with a JSON array ONLY — no other text:
[
  {{"faction": "<exact faction name>", "delta": <int -15 to +15>, "reason": "<one short sentence>"}},
  ...
]

Rules:
- Only include factions where the action meaningfully changed their opinion.
- delta must be between -15 and +15.
- If no faction is affected, return an empty array: []
"""

_SCORE_LABELS: list[tuple[int, str]] = [
    (75,   "Allied"),
    (40,   "Friendly"),
    (10,   "Neutral"),
    (-25,  "Cautious"),
    (-60,  "Hostile"),
    (-100, "Enemy"),
]


def _score_label(score: int) -> str:
    for threshold, label in _SCORE_LABELS:
        if score >= threshold:
            return label
    return "Enemy"


class FactionService:
    """
    Manages faction/reputation data for a campaign.
    """

    def __init__(self, db, gemini_client) -> None:
        self._db     = db
        self._gemini = gemini_client

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    async def get_all(self, campaign_id: str) -> list[dict[str, Any]]:
        """Return all factions for a campaign."""
        rows = await self._db.fetch(
            "SELECT id, name, description, disposition FROM factions WHERE campaign_id = $1 "
            "ORDER BY name",
            campaign_id,
        )
        return [dict(r) for r in rows]

    async def get_standings(
        self,
        player_id:   str,
        campaign_id: str,
    ) -> list[dict[str, Any]]:
        """
        Return faction standing data for a specific player.

        Returns list of:
          {name, score, label, description}
        """
        rows = await self._db.fetch(
            "SELECT name, description, disposition FROM factions WHERE campaign_id = $1",
            campaign_id,
        )
        standings = []
        for row in rows:
            disposition = row["disposition"] or {}
            score = int(disposition.get(player_id, 0))
            standings.append({
                "name":        row["name"],
                "description": row["description"],
                "score":       score,
                "label":       _score_label(score),
            })
        return sorted(standings, key=lambda x: x["score"], reverse=True)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    async def upsert_faction(
        self,
        campaign_id: str,
        name:        str,
        description: str = "",
    ) -> str:
        """
        Create or update a faction.  Returns the faction UUID.
        """
        row = await self._db.fetchrow(
            "SELECT id FROM factions WHERE campaign_id = $1 AND name = $2",
            campaign_id,
            name,
        )
        if row:
            await self._db.execute(
                "UPDATE factions SET description = $1 WHERE id = $2",
                description,
                str(row["id"]),
            )
            return str(row["id"])

        faction_id = await self._db.fetchval(
            """
            INSERT INTO factions (campaign_id, name, description)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            campaign_id,
            name,
            description,
        )
        return str(faction_id)

    async def adjust(
        self,
        campaign_id:  str,
        faction_name: str,
        player_id:    str,
        delta:        int,
    ) -> int:
        """
        Apply a reputation delta for a player with a specific faction.

        Score is clamped to [-100, 100].
        Returns the new score.
        """
        row = await self._db.fetchrow(
            "SELECT id, disposition FROM factions WHERE campaign_id = $1 AND name = $2",
            campaign_id,
            faction_name,
        )
        if not row:
            logger.warning("adjust called for unknown faction '%s' in campaign %s",
                           faction_name, campaign_id)
            return 0

        disposition: dict = dict(row["disposition"]) if row["disposition"] else {}
        current = int(disposition.get(player_id, 0))
        new_score = max(-100, min(100, current + delta))
        disposition[player_id] = new_score

        await self._db.execute(
            "UPDATE factions SET disposition = $1::jsonb WHERE id = $2",
            json.dumps(disposition),
            str(row["id"]),
        )
        logger.debug(
            "Faction '%s': player %s %+d → %d (%s)",
            faction_name, player_id, delta, new_score, _score_label(new_score),
        )
        return new_score

    # ------------------------------------------------------------------
    # AI-driven post-narrative adjustment
    # ------------------------------------------------------------------

    async def ai_adjust_from_narrative(
        self,
        campaign_id:      str,
        player_id:        str,
        narrative_excerpt: str,
        action_type:      str,
    ) -> None:
        """
        Ask Gemini to determine faction reputation changes from a narrative.
        Fires adjustments for each suggested change.

        This is designed to be called fire-and-forget via asyncio.create_task().
        """
        factions = await self.get_all(campaign_id)
        if not factions:
            return

        faction_names = ", ".join(f["name"] for f in factions)
        prompt = _FACTION_ADJUSTMENT_PROMPT.format(
            faction_names=faction_names,
            action_type=action_type,
            narrative_excerpt=narrative_excerpt[:800],
        )
        try:
            raw = await self._gemini.generate(
                system="You are a game master assistant. Output valid JSON only.",
                user=prompt,
                max_tokens=300,
                temperature=0.2,
            )
            # Strip code fences if present
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            adjustments: list[dict] = json.loads(raw)
            for adj in adjustments:
                fname  = adj.get("faction", "")
                delta  = int(adj.get("delta", 0))
                reason = adj.get("reason", "")
                if fname and delta:
                    new_score = await self.adjust(campaign_id, fname, player_id, delta)
                    logger.info(
                        "AI faction adj: '%s' %+d → %d (%s) | %s",
                        fname, delta, new_score, _score_label(new_score), reason,
                    )
        except Exception as exc:
            logger.warning("ai_adjust_from_narrative error: %s", exc)
