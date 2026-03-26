"""
Ironclad GM – Chronicle Recap Service
========================================
Generates a concise "Previously on…" catch-up summary for a player who
was offline while the rest of the party played.

Algorithm
---------
1. Find the player's last action timestamp in action_log.
2. Pull every other action in the same campaign after that timestamp
   (raw_input + narrative_summary pairs).
3. Pull the most recently updated story_context facts for the campaign
   (NPCs, locations, events) to give the summariser world-grounding.
4. Send everything to Gemini with a strict recap prompt.
5. Return the bulleted summary as a RecapResponse.

Design constraints
------------------
- If the player has never acted in this campaign, the recap covers the last
  24 hours of activity so a brand-new joiner still gets useful context.
- If there are no events since the last action, returns a short "all quiet"
  message without calling Gemini.
- Gemini token budget: input capped at 12,000 chars so large sessions don't
  blow through quota.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx

from orchestrator.config import Settings
from orchestrator.schemas.payloads import RecapRequest, RecapResponse

logger = logging.getLogger(__name__)

_RECAP_PROMPT = """\
You are the Game Master's Chronicle Keeper for a tabletop RPG campaign.

A player has been offline and is catching up.  Below are the actions and \
narrative summaries that happened while they were away, plus relevant world \
facts.  Produce a concise "Previously on…" summary for them.

Rules:
- Use 4-8 bullet points maximum.
- Each bullet covers one meaningful plot beat, scene, or consequence.
- Mention characters by name where possible.
- Include any inventory changes or stat changes that matter to the story.
- Use present-tense immersive language ("The party discovers…", "Rho falls…").
- Do NOT include mechanical numbers (HP totals, dice rolls).
- End with one sentence about where the story currently stands.
- Total length: 150–300 words.

RECENT WORLD FACTS:
{world_facts}

EVENTS SINCE YOU WERE LAST ONLINE:
{events}

Write the recap now.
"""

_QUIET_RECAP = (
    "📖 **Chronicle Recap**\n\n"
    "The world held its breath while you were away — no significant events "
    "have unfolded since your last action. The story awaits your return."
)


class ChronicleService:
    def __init__(self, settings: Settings, pool) -> None:
        self._pool = pool
        self._gemini_api_key = settings.gemini_api_key
        self._gemini_model   = settings.gemini_model

    async def generate_recap(self, request: RecapRequest) -> RecapResponse:
        """Build and return the recap for a player."""
        since_ts = await self._last_player_action_ts(
            request.player_id, request.campaign_id
        )

        # For a player who has never acted, recap the last 24 hours
        if since_ts is None:
            since_ts = datetime.now(timezone.utc) - timedelta(hours=24)

        events = await self._fetch_events_since(request.campaign_id, since_ts)

        if not events:
            return RecapResponse(
                player_id=request.player_id,
                campaign_id=request.campaign_id,
                recap_text=_QUIET_RECAP,
                events_covered=0,
                since_timestamp=since_ts,
            )

        world_facts = await self._fetch_world_facts(request.campaign_id)
        recap_text  = await self._call_gemini(events, world_facts)

        return RecapResponse(
            player_id=request.player_id,
            campaign_id=request.campaign_id,
            recap_text=recap_text,
            events_covered=len(events),
            since_timestamp=since_ts,
        )

    # ── Private Helpers ────────────────────────────────────────────────────────

    async def _last_player_action_ts(
        self, player_id: str, campaign_id: str
    ) -> datetime | None:
        row = await self._pool.fetchrow(
            """
            SELECT MAX(resolved_at) AS last_at
            FROM action_log
            WHERE player_id  = $1
              AND campaign_id = $2
              AND retconned   = FALSE
            """,
            player_id,
            UUID(campaign_id),
        )
        return row["last_at"] if row else None

    async def _fetch_events_since(
        self, campaign_id: str, since: datetime
    ) -> list[dict]:
        rows = await self._pool.fetch(
            """
            SELECT player_id, raw_input, narrative_summary, resolved_at
            FROM action_log
            WHERE campaign_id = $1
              AND resolved_at > $2
              AND retconned   = FALSE
            ORDER BY resolved_at ASC
            LIMIT 60
            """,
            UUID(campaign_id),
            since,
        )
        return [
            {
                "player_id":        r["player_id"],
                "raw_input":        r["raw_input"],
                "narrative_summary": r["narrative_summary"] or "",
                "resolved_at":      r["resolved_at"].strftime("%H:%M") if r["resolved_at"] else "",
            }
            for r in rows
        ]

    async def _fetch_world_facts(self, campaign_id: str) -> list[str]:
        rows = await self._pool.fetch(
            """
            SELECT entity_name, summary
            FROM story_context
            WHERE campaign_id = $1
            ORDER BY last_updated_at DESC
            LIMIT 15
            """,
            UUID(campaign_id),
        )
        return [f"{r['entity_name']}: {r['summary']}" for r in rows]

    async def _call_gemini(
        self, events: list[dict], world_facts: list[str]
    ) -> str:
        events_text = "\n".join(
            f"[{e['resolved_at']}] {e['raw_input']} → {e['narrative_summary'][:200]}"
            for e in events
        )
        facts_text = "\n".join(world_facts) if world_facts else "No established world facts yet."

        # Cap input to avoid excessive token usage
        events_text = events_text[:10000]

        prompt = _RECAP_PROMPT.format(
            world_facts=facts_text,
            events=events_text,
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature":    0.6,
                "maxOutputTokens": 512,
            },
        }
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._gemini_model}:generateContent?key={self._gemini_api_key}"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            return f"📖 **Chronicle Recap**\n\n{text.strip()}"
        except Exception as exc:
            logger.warning("Chronicle Gemini call failed: %s", exc)
            # Fallback: plain bullet list
            lines = [
                f"• [{e['resolved_at']}] {e['raw_input'][:80]}"
                for e in events[:10]
            ]
            return "📖 **Chronicle Recap** *(summary unavailable — raw log)*\n\n" + "\n".join(lines)
