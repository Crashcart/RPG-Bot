"""
Ironclad GM – Retcon Service
================================
Allows a privileged admin to roll back a specific action — typically used
when the AI hallucinated something inconsistent, offensive, or mechanically
wrong and the GM needs to erase it and rewrite the scene.

What retcon does
----------------
1. Looks up the action_log row by intent_id.
2. Reads the stored StateCommitPayload (state_delta column) to find the
   pre_state (character stats before the bad action) and character_id.
3. Restores the character stats to pre_state in a single atomic UPDATE.
4. Flags the action_log row retconned=TRUE, records who did it and why.
5. Writes an entry to retcon_log for full audit.
6. Returns RetconResponse so the Discord bot can confirm to the admin.

What retcon does NOT do
-----------------------
- It does NOT rewind inventory changes (to avoid losing item history).
  Inventory adjustments must be made manually via the White Portal.
- It does NOT delete story_context facts — the admin decides whether to
  delete lore entries separately via the Lore Archive UI.
- It does NOT reverse vehicle hull / subsystem changes (same rationale).
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

from orchestrator.schemas.payloads import RetconRequest, RetconResponse

logger = logging.getLogger(__name__)


class RetconService:
    def __init__(self, pool) -> None:
        self._pool = pool

    async def apply_retcon(self, req: RetconRequest) -> RetconResponse:
        """
        Reverse the character stat changes from a specific action.
        Raises ValueError if the action is not found or already retconned.
        """
        # ── 1. Fetch the action_log row ────────────────────────────────────────
        row = await self._pool.fetchrow(
            """
            SELECT id, character_id, state_delta, retconned
            FROM action_log
            WHERE intent_id = $1
            LIMIT 1
            """,
            UUID(req.intent_id),
        )
        if not row:
            raise ValueError(f"No action found for intent_id={req.intent_id}")
        if row["retconned"]:
            raise ValueError(f"Action {req.intent_id} has already been retconned.")

        character_id = row["character_id"]
        if not character_id:
            raise ValueError("Action log row has no character_id — cannot retcon.")

        # ── 2. Parse StateCommitPayload from state_delta column ────────────────
        raw_delta = row["state_delta"]
        if isinstance(raw_delta, str):
            commit_data = json.loads(raw_delta)
        elif isinstance(raw_delta, dict):
            commit_data = raw_delta
        else:
            # asyncpg returns JSONB as dict-like objects
            commit_data = dict(raw_delta)

        pre_state  = commit_data.get("pre_state", {})
        post_state = commit_data.get("post_state", {})

        if not pre_state:
            raise ValueError("No pre_state found in state_delta — cannot retcon.")

        # ── 3. Restore character stats to pre_state ───────────────────────────
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE characters
                    SET stats      = $1,
                        updated_at = NOW()
                    WHERE id = $2
                    """,
                    json.dumps(pre_state),
                    character_id,
                )

                # ── 4. Flag action_log row as retconned ───────────────────────
                await conn.execute(
                    """
                    UPDATE action_log
                    SET retconned     = TRUE,
                        retconned_at  = NOW(),
                        retconned_by  = $1
                    WHERE intent_id   = $2
                    """,
                    req.admin_id,
                    UUID(req.intent_id),
                )

                # ── 5. Write retcon_log audit entry ───────────────────────────
                await conn.execute(
                    """
                    INSERT INTO retcon_log
                        (intent_id, retconned_by, pre_state_snapshot,
                         post_state_snapshot, reason)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    UUID(req.intent_id),
                    req.admin_id,
                    json.dumps(pre_state),
                    json.dumps(post_state),
                    req.reason,
                )

        logger.info(
            "Retcon applied: intent=%s character=%s by=%s reason=%s",
            req.intent_id, str(character_id), req.admin_id, req.reason,
        )

        return RetconResponse(
            intent_id=req.intent_id,
            character_id=str(character_id),
            restored_stats=pre_state,
        )
