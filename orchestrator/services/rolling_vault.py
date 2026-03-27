"""
Rolling Vault — Sliding Context Window Manager
===============================================
Prevents local LLM context-window overflow during long RPG sessions by
maintaining a bounded, structured history for each campaign.

How it works
------------
1. After every pipeline turn, the orchestrator calls append() with the
   player's raw input and the GM's narrative response.  Both are stored
   verbatim in the rolling_vault PostgreSQL table.

2. When the raw turn count for a campaign exceeds WINDOW_SIZE, the oldest
   COMPRESS_BATCH turns are sent to the local Brain (Ollama) for compression
   into a single 2-3 sentence summary.  The original rows are deleted and the
   summary is inserted in their place.

3. The ingestion phase calls get_context_block() to obtain a formatted
   history string, which is prepended to every Ollama adjudication prompt:

       [PRIOR SESSION SUMMARY]
       The party boarded the Meridian and discovered a locked cargo hold…

       [RECENT EVENTS]
       Player: I shoot at the guard.
       GM: Your plasma round clips his shoulder…

Token budget
------------
WINDOW_SIZE  = 20 raw turns  → ~4 000 tokens verbatim (200 t/turn avg)
Summaries    → ~150 tokens each; the vault may accumulate several
Total budget → target ≤ 2 500 tokens for context injection

The NodeRouter is used for compression so it benefits from the same
node-health and TTFT benchmarking as all other Ollama calls.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg
    from orchestrator.services.node_router import NodeRouter

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

WINDOW_SIZE     = 20   # keep at most this many verbatim turns per campaign
COMPRESS_BATCH  = 10   # when overflow: compress the oldest N turns

_COMPRESS_ROLE  = "narrative"   # NodeRouter role for compression calls
_COMPRESS_MAX_T = 200           # token budget for the summary output

_COMPRESS_SYSTEM = (
    "You are a concise game record keeper for a tabletop RPG. "
    "Your only job is to summarise recent events accurately and briefly. "
    "Output exactly one paragraph of 2-3 sentences. "
    "Focus on outcomes, character changes, and plot developments. "
    "Do NOT add commentary, introductions, or headings."
)

_COMPRESS_USER_TMPL = (
    "Summarise the following recent RPG events into one short paragraph:\n\n{events}"
)


# ── Service ───────────────────────────────────────────────────────────────────

class RollingVault:
    """
    Sliding context window with Ollama-driven compression.

    Designed as a long-lived singleton injected with the asyncpg pool and
    NodeRouter after the DB connection is established.
    """

    def __init__(self, node_router: "NodeRouter") -> None:
        self._node_router = node_router
        self._pool: "asyncpg.Pool | None" = None

    def bind(self, pool: "asyncpg.Pool") -> None:
        """Bind the DB connection pool (called from lifespan after db.connect())."""
        self._pool = pool

    # ── Public API ────────────────────────────────────────────────────────────

    async def append(
        self,
        campaign_id: str,
        player_input: str,
        gm_response:  str,
    ) -> None:
        """
        Record a completed turn (player action + GM response) and trigger
        compression if the window is full.

        Safe to await fire-and-forget — compression errors are logged but
        never propagate to the caller.
        """
        if not self._pool:
            logger.warning("RollingVault: pool not bound — skipping append.")
            return
        try:
            await self._insert_turn(campaign_id, player_input, gm_response)
            await self._maybe_compress(campaign_id)
        except Exception as exc:
            logger.error("RollingVault.append failed (non-fatal): %s", exc)

    async def get_context_block(self, campaign_id: str) -> str:
        """
        Return a formatted history string ready for injection into prompts.

        Format:
            [PRIOR SESSION SUMMARY]
            <summary text>

            [RECENT EVENTS]
            Player: <text>
            GM: <text>
            ...

        Returns an empty string if no history exists yet (first turn).
        """
        if not self._pool:
            return ""
        try:
            return await self._build_context_block(campaign_id)
        except Exception as exc:
            logger.error("RollingVault.get_context_block failed (non-fatal): %s", exc)
            return ""

    async def clear(self, campaign_id: str) -> None:
        """Delete all rolling vault entries for a campaign (admin / retcon use)."""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM rolling_vault WHERE campaign_id = $1::uuid", campaign_id
            )
        logger.info("RollingVault: cleared history for campaign %s", campaign_id)

    # ── Insert ────────────────────────────────────────────────────────────────

    async def _insert_turn(
        self, campaign_id: str, player_input: str, gm_response: str
    ) -> None:
        async with self._pool.acquire() as conn:
            # Atomic: get next seq and insert both rows
            async with conn.transaction():
                next_seq: int = await conn.fetchval(
                    """
                    SELECT COALESCE(MAX(turn_seq), 0) + 1
                    FROM rolling_vault
                    WHERE campaign_id = $1::uuid
                    """,
                    campaign_id,
                )
                await conn.executemany(
                    """
                    INSERT INTO rolling_vault (campaign_id, turn_seq, role, content)
                    VALUES ($1::uuid, $2, $3, $4)
                    """,
                    [
                        (campaign_id, next_seq,     "player", player_input[:2000]),
                        (campaign_id, next_seq + 1, "gm",     gm_response[:2000]),
                    ],
                )

    # ── Compression ───────────────────────────────────────────────────────────

    async def _maybe_compress(self, campaign_id: str) -> None:
        """Compress oldest COMPRESS_BATCH turns if raw turn count > WINDOW_SIZE."""
        async with self._pool.acquire() as conn:
            raw_count: int = await conn.fetchval(
                """
                SELECT COUNT(*) FROM rolling_vault
                WHERE campaign_id = $1::uuid AND is_summary = FALSE
                """,
                campaign_id,
            )

        if raw_count <= WINDOW_SIZE:
            return

        logger.info(
            "RollingVault: %d raw turns for campaign %s — compressing oldest %d.",
            raw_count, campaign_id, COMPRESS_BATCH,
        )
        await self._compress_oldest(campaign_id)

    async def _compress_oldest(self, campaign_id: str) -> None:
        """
        Fetch the oldest COMPRESS_BATCH raw turns, summarise them via Ollama,
        insert the summary, then delete the original rows — all in one transaction.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, role, content
                FROM rolling_vault
                WHERE campaign_id = $1::uuid AND is_summary = FALSE
                ORDER BY turn_seq ASC
                LIMIT $2
                """,
                campaign_id, COMPRESS_BATCH,
            )

        if not rows:
            return

        # Build the text block to be compressed
        event_lines = []
        for row in rows:
            label = "Player" if row["role"] == "player" else "GM"
            event_lines.append(f"{label}: {row['content']}")
        events_text = "\n".join(event_lines)

        # Call Ollama for compression
        summary_text = await self._call_compressor(events_text)
        if not summary_text:
            logger.warning("RollingVault: compression returned empty — skipping.")
            return

        # Atomically: insert summary, delete originals
        row_ids = [r["id"] for r in rows]
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                min_seq: int = await conn.fetchval(
                    "SELECT MIN(turn_seq) FROM rolling_vault WHERE id = ANY($1::bigint[])",
                    row_ids,
                )
                await conn.execute(
                    """
                    INSERT INTO rolling_vault
                        (campaign_id, turn_seq, role, content, is_summary)
                    VALUES ($1::uuid, $2, 'summary', $3, TRUE)
                    """,
                    campaign_id, min_seq, summary_text,
                )
                await conn.execute(
                    "DELETE FROM rolling_vault WHERE id = ANY($1::bigint[])", row_ids
                )

        logger.info(
            "RollingVault: compressed %d turns → 1 summary for campaign %s.",
            len(rows), campaign_id,
        )

    async def _call_compressor(self, events_text: str) -> str:
        """
        Route a compression request through NodeRouter to the local Brain.
        Returns the summary string, or empty string on failure.
        """
        try:
            nodes = await self._node_router.get_nodes_for_role_by_latency(
                _COMPRESS_ROLE
            )
            if not nodes:
                logger.warning(
                    "RollingVault: no narrative nodes available for compression."
                )
                return ""

            node     = nodes[0]
            host     = node["url"]
            model    = node.get("model", "mistral:7b-instruct")
            user_msg = _COMPRESS_USER_TMPL.format(events=events_text)

            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{host}/api/chat",
                    json={
                        "model":  model,
                        "stream": False,
                        "messages": [
                            {"role": "system",  "content": _COMPRESS_SYSTEM},
                            {"role": "user",    "content": user_msg},
                        ],
                        "options": {"num_predict": _COMPRESS_MAX_T},
                    },
                )
                resp.raise_for_status()
                return resp.json()["message"]["content"].strip()

        except Exception as exc:
            logger.error("RollingVault compression call failed: %s", exc)
            return ""

    # ── Context Assembly ──────────────────────────────────────────────────────

    async def _build_context_block(self, campaign_id: str) -> str:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT role, content, is_summary
                FROM rolling_vault
                WHERE campaign_id = $1::uuid
                ORDER BY turn_seq ASC
                """,
                campaign_id,
            )

        if not rows:
            return ""

        summaries   = [r for r in rows if r["is_summary"]]
        recent_raw  = [r for r in rows if not r["is_summary"]]

        parts: list[str] = []

        if summaries:
            parts.append("[PRIOR SESSION SUMMARY]")
            for s in summaries:
                parts.append(s["content"])
            parts.append("")  # blank line separator

        if recent_raw:
            parts.append("[RECENT EVENTS]")
            for r in recent_raw:
                label = "Player" if r["role"] == "player" else "GM"
                parts.append(f"{label}: {r['content']}")

        return "\n".join(parts)
