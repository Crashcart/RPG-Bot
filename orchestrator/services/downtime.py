"""
Ironclad GM – Downtime Task Service
======================================
Handles isolated, personal progression tasks that players submit before
logging off.  The GM runs dice checks in the background and delivers a
mini-story DM when the player returns.

Flow
----
1. Player: /downtime I want to spend 8 hours researching the artifact.
2. Discord bot: POST /api/downtime  → DowntimeService.submit_task()
3. Orchestrator background loop runs every 60 s:
     DowntimeService.resolve_pending() — picks up tasks whose resolves_at
     is in the past, calls Ollama (or Gemini), writes result_narrative.
4. Discord bot polls GET /api/downtime/notifications/{player_id} every 30 s.
5. Bot DMs the player with the result, PATCH /api/downtime/{task_id}/notified.

Isolation guarantee
-------------------
Downtime tasks operate on a personal sub-timeline.  They DO NOT modify the
main character stats table or trigger any state_commit event — the narrative
result is purely descriptive.  Any mechanical benefits the player wants to
formalise must be applied by the admin via the White Portal.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx

from orchestrator.config import Settings
from orchestrator.schemas.payloads import (
    DowntimePendingNotification,
    DowntimeSubmitRequest,
    DowntimeTaskStatus,
)

logger = logging.getLogger(__name__)

_DOWNTIME_PROMPT = """\
You are the Game Master for a tabletop RPG.

A player character is spending {duration_hours} hours doing the following:

"{description}"

Character name: {character_name}
Campaign system: {campaign_system}

Write a short personal narrative (150-250 words) describing what they discover, \
accomplish, or experience during this downtime period.  Make it immersive and \
specific — mention sensory details, NPCs they interact with, objects they examine.

If the task has a plausible skill check outcome, describe a single meaningful \
result (success, partial success, or complication) without revealing dice numbers.

End with one line: "**What you learned:** <one concrete takeaway sentence>."
"""


class DowntimeService:
    def __init__(self, settings: Settings, pool) -> None:
        self._pool           = pool
        self._gemini_api_key = settings.gemini_api_key
        self._gemini_model   = settings.gemini_model
        self._ollama_host    = settings.ollama_host
        self._ollama_model   = settings.ollama_model

    # ── Public API ────────────────────────────────────────────────────────────

    async def submit_task(self, req: DowntimeSubmitRequest) -> DowntimeTaskStatus:
        """Persist a new downtime task and return its status."""
        resolves_at = datetime.now(timezone.utc) + timedelta(hours=req.duration_hours)

        # Resolve character_id from player + campaign if not provided
        char_row = await self._pool.fetchrow(
            """
            SELECT id FROM characters
            WHERE player_id = $1 AND campaign_id = $2 AND status = 'ALIVE'
            ORDER BY created_at DESC LIMIT 1
            """,
            req.player_id,
            UUID(req.campaign_id),
        )
        character_id = char_row["id"] if char_row else None

        row = await self._pool.fetchrow(
            """
            INSERT INTO downtime_tasks
                (campaign_id, player_id, character_id, description,
                 duration_hours, resolves_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id, status, submitted_at, resolves_at
            """,
            UUID(req.campaign_id),
            req.player_id,
            character_id,
            req.description,
            req.duration_hours,
            resolves_at,
        )

        logger.info(
            "Downtime task submitted: player=%s task=%s resolves_at=%s",
            req.player_id, str(row["id"]), resolves_at.isoformat(),
        )

        return DowntimeTaskStatus(
            task_id=str(row["id"]),
            description=req.description,
            status=row["status"],
            duration_hours=req.duration_hours,
            submitted_at=row["submitted_at"],
            resolves_at=row["resolves_at"],
        )

    async def resolve_pending(self) -> int:
        """
        Called by the background loop.  Picks up every overdue task, generates
        a narrative, and marks it complete.  Returns the number resolved.
        """
        rows = await self._pool.fetch(
            """
            SELECT dt.id, dt.player_id, dt.character_id, dt.description,
                   dt.duration_hours, dt.campaign_id,
                   c.name AS character_name,
                   ca.system AS campaign_system
            FROM downtime_tasks dt
            LEFT JOIN characters c  ON c.id  = dt.character_id
            LEFT JOIN campaigns  ca ON ca.id = dt.campaign_id
            WHERE dt.status = 'pending'
              AND dt.resolves_at <= NOW()
            LIMIT 20
            """,
        )
        if not rows:
            return 0

        resolved = 0
        for row in rows:
            task_id = row["id"]

            # Mark as resolving to prevent double-processing
            await self._pool.execute(
                "UPDATE downtime_tasks SET status = 'resolving' WHERE id = $1",
                task_id,
            )
            try:
                narrative = await self._generate_narrative(
                    description=row["description"],
                    duration_hours=row["duration_hours"],
                    character_name=row["character_name"] or "your character",
                    campaign_system=row["campaign_system"] or "a fantasy RPG",
                )
                await self._pool.execute(
                    """
                    UPDATE downtime_tasks
                    SET status           = 'complete',
                        result_narrative = $1,
                        resolved_at      = NOW()
                    WHERE id = $2
                    """,
                    narrative, task_id,
                )
                resolved += 1
            except Exception as exc:
                logger.error("Downtime task %s failed: %s", task_id, exc)
                await self._pool.execute(
                    """
                    UPDATE downtime_tasks
                    SET status = 'failed', resolved_at = NOW()
                    WHERE id = $1
                    """,
                    task_id,
                )

        if resolved:
            logger.info("Downtime resolver: %d tasks resolved", resolved)
        return resolved

    async def get_pending_notifications(
        self, player_id: str
    ) -> list[DowntimePendingNotification]:
        """
        Return completed tasks that haven't been DM'd to the player yet.
        The Discord bot calls this on a poll loop.
        """
        rows = await self._pool.fetch(
            """
            SELECT dt.id, dt.result_narrative,
                   c.name AS character_name
            FROM downtime_tasks dt
            LEFT JOIN characters c ON c.id = dt.character_id
            WHERE dt.player_id = $1
              AND dt.status    = 'complete'
              AND dt.notified  = FALSE
            ORDER BY dt.resolved_at
            """,
            player_id,
        )
        return [
            DowntimePendingNotification(
                task_id=str(r["id"]),
                player_id=player_id,
                result_narrative=r["result_narrative"] or "",
                character_name=r["character_name"] or "",
            )
            for r in rows
        ]

    async def mark_notified(self, task_id: str) -> None:
        await self._pool.execute(
            "UPDATE downtime_tasks SET notified = TRUE WHERE id = $1",
            UUID(task_id),
        )

    # ── Narrative Generation ──────────────────────────────────────────────────

    async def _generate_narrative(
        self,
        description: str,
        duration_hours: int,
        character_name: str,
        campaign_system: str,
    ) -> str:
        prompt = _DOWNTIME_PROMPT.format(
            description=description[:800],
            duration_hours=duration_hours,
            character_name=character_name,
            campaign_system=campaign_system,
        )

        # Try Gemini first, fall back to Ollama
        narrative = await self._try_gemini(prompt)
        if not narrative:
            narrative = await self._try_ollama(prompt)
        if not narrative:
            narrative = (
                f"*{character_name} spent {duration_hours} hours on their task. "
                f"The details are lost to the passage of time…*"
            )
        return narrative

    async def _try_gemini(self, prompt: str) -> str | None:
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.8, "maxOutputTokens": 512},
        }
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._gemini_model}:generateContent?key={self._gemini_api_key}"
        )
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as exc:
            logger.debug("Gemini downtime call failed: %s", exc)
            return None

    async def _try_ollama(self, prompt: str) -> str | None:
        payload = {
            "model":  self._ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": 400, "temperature": 0.8},
        }
        try:
            async with httpx.AsyncClient(
                base_url=self._ollama_host, timeout=60
            ) as client:
                resp = await client.post("/api/generate", json=payload)
                resp.raise_for_status()
            return resp.json().get("response", "").strip()
        except Exception as exc:
            logger.debug("Ollama downtime call failed: %s", exc)
            return None
