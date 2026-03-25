"""
Ironclad GM – Admin Backchannel Service
==========================================
Manages the White Portal's private "God Mode" interface:
  • Stores OOC admin directives submitted through the web panel
  • Surfaces pending directives to the GM Director each player turn
  • Archives consumed directives for the audit trail
  • Enforces the Fair Play Sandbox — verifies no admin bypass is active
    in the pipeline

Separation of Powers
--------------------
  White Portal → Admin can ONLY influence the world through this service.
  Discord       → Admin account is a standard player.  NO elevated access.

The directive injection flow:
  1. Admin types a directive in the Backchannel UI, POST /api/backchannel/directive
  2. AdminBackchannelService.submit_directive() persists it as status='pending'
  3. On the next /action call in the same campaign, main.py calls
     get_pending_directives() BEFORE narration starts
  4. The list of directives is passed through to GMDirector.narrate()
  5. GMDirector injects the directive text as a [WORLD ARCHITECT DIRECTIVE]
     block at the TOP of the GM_SYSTEM_PROMPT — highest priority input
  6. After successful synthesis main.py calls consume_directives(ids)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from orchestrator.schemas.payloads import GMDirective, GMDirectiveRequest, DirectiveType

logger = logging.getLogger(__name__)

_MAX_DIRECTIVES_PER_TURN = 3   # hard cap; overridable via system_settings


class AdminBackchannelService:
    def __init__(self, pool) -> None:
        self._pool = pool

    # ── Submit ────────────────────────────────────────────────────────────────

    async def submit_directive(self, req: GMDirectiveRequest) -> GMDirective:
        """Persist a new admin directive and return the stored record."""
        row = await self._pool.fetchrow(
            """
            INSERT INTO gm_directives
                (campaign_id, admin_id, directive_type, directive_text, priority)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, status, submitted_at
            """,
            UUID(req.campaign_id),
            req.admin_id,
            req.directive_type.value,
            req.directive_text,
            req.priority,
        )
        logger.info(
            "Backchannel directive submitted: campaign=%s type=%s priority=%d",
            req.campaign_id, req.directive_type.value, req.priority,
        )
        return GMDirective(
            directive_id=str(row["id"]),
            campaign_id=req.campaign_id,
            admin_id=req.admin_id,
            directive_type=req.directive_type,
            directive_text=req.directive_text,
            priority=req.priority,
            status=row["status"],
            submitted_at=row["submitted_at"],
        )

    # ── Fetch for injection ────────────────────────────────────────────────────

    async def get_pending_directives(
        self,
        campaign_id: str,
        limit: int = _MAX_DIRECTIVES_PER_TURN,
    ) -> list[GMDirective]:
        """
        Return the highest-priority pending directives for a campaign, up to `limit`.
        Ordered: priority DESC, submitted_at ASC (oldest high-priority first).
        """
        rows = await self._pool.fetch(
            """
            SELECT id, admin_id, directive_type, directive_text,
                   priority, status, submitted_at
            FROM gm_directives
            WHERE campaign_id = $1 AND status = 'pending'
            ORDER BY priority DESC, submitted_at ASC
            LIMIT $2
            """,
            UUID(campaign_id),
            limit,
        )
        return [
            GMDirective(
                directive_id=str(r["id"]),
                campaign_id=campaign_id,
                admin_id=r["admin_id"],
                directive_type=DirectiveType(r["directive_type"]),
                directive_text=r["directive_text"],
                priority=r["priority"],
                status=r["status"],
                submitted_at=r["submitted_at"],
            )
            for r in rows
        ]

    # ── Consume ────────────────────────────────────────────────────────────────

    async def consume_directives(
        self,
        directive_ids: list[str],
        intent_id: str,
    ) -> None:
        """
        Mark a batch of directives as consumed after the GM has synthesised
        them into a narrative.  Called by main.py after narration completes.
        """
        if not directive_ids:
            return
        uuids = [UUID(d) for d in directive_ids]
        await self._pool.execute(
            """
            UPDATE gm_directives
            SET status             = 'consumed',
                consumed_at        = NOW(),
                consumed_intent_id = $1
            WHERE id = ANY($2::uuid[])
            """,
            UUID(intent_id),
            uuids,
        )
        logger.info(
            "Consumed %d directive(s) via intent %s", len(directive_ids), intent_id
        )

    async def cancel_directive(self, directive_id: str) -> None:
        """Cancel a pending directive (admin retracts before it fires)."""
        await self._pool.execute(
            """
            UPDATE gm_directives
            SET status = 'cancelled'
            WHERE id = $1 AND status = 'pending'
            """,
            UUID(directive_id),
        )

    # ── History queries ────────────────────────────────────────────────────────

    async def get_recent_directives(
        self,
        campaign_id: str,
        limit: int = 30,
    ) -> list[dict]:
        """Return recent directives (all statuses) for the Backchannel UI."""
        rows = await self._pool.fetch(
            """
            SELECT id, admin_id, directive_type, directive_text,
                   priority, status, submitted_at, consumed_at
            FROM gm_directives
            WHERE campaign_id = $1
            ORDER BY submitted_at DESC
            LIMIT $2
            """,
            UUID(campaign_id),
            limit,
        )
        return [
            {
                "directive_id":   str(r["id"]),
                "admin_id":       r["admin_id"],
                "directive_type": r["directive_type"],
                "directive_text": r["directive_text"],
                "priority":       r["priority"],
                "status":         r["status"],
                "submitted_at":   r["submitted_at"].strftime("%Y-%m-%d %H:%M:%S UTC"),
                "consumed_at":    r["consumed_at"].strftime("%H:%M:%S UTC") if r["consumed_at"] else None,
            }
            for r in rows
        ]
