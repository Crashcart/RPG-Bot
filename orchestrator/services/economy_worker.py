"""Ironclad GM – Async Market Maker & Deep Supply-Chain Simulation.

Runs a background asyncio loop that processes economy ticks for all active
campaigns.  Each tick:

1. For every enabled market_node, apply production_rate and demand_rate
   to the supply column in market_inventory.
2. Recalculate current_price using the inverse-supply formula:
       price = base_price * (max_supply / 2) / max(supply, max_supply * 0.05)
   – at 50% supply: price == base_price
   – at 10% supply: price ≈ 5 × base_price
   – capped at 20 × base_price, floored at 0.2 × base_price
3. Run Ghost Freighters: if any commodity at a node is priced >3× a peer
   node in the same campaign, simulate a haul that transfers units and
   records a 'ghost_haul' transaction, preventing permanent market collapse.

LLM is never invoked during a tick — all math is deterministic Python.

Public API (for Phase 2 adjudication interception):
    get_live_price(node_id, commodity_id) → Decimal
    execute_transaction(node_id, commodity_id, quantity, action, character_id) → TransactionResult
    get_market_context(campaign_id, limit) → list[MarketSummaryEntry]
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional
from uuid import UUID

import httpx

from orchestrator.config import Settings

logger = logging.getLogger(__name__)

# Price cap / floor multipliers relative to base_price
_PRICE_CAP   = Decimal("20.0")
_PRICE_FLOOR = Decimal("0.20")
# Ghost freighter fires when price at destination is >3× price at source
_GHOST_HAUL_RATIO = Decimal("3.0")
# Fraction of max_supply transferred in a single ghost haul
_GHOST_HAUL_FRACTION = Decimal("0.10")
# Minimum supply fraction used as denominator to avoid division-by-zero
_MIN_SUPPLY_FRAC = Decimal("0.05")


@dataclass(frozen=True)
class TransactionResult:
    transaction_id: str
    node_id: str
    commodity_id: str
    action: str
    quantity: Decimal
    unit_price: Decimal
    total_value: Decimal
    new_supply: Decimal


@dataclass(frozen=True)
class MarketSummaryEntry:
    node_name: str
    commodity_name: str
    supply: Decimal
    max_supply: Decimal
    current_price: Decimal
    base_price: Decimal
    price_ratio: Decimal   # current_price / base_price — for narrative flavour


class EconomyWorker:
    """Background economy tick worker.  Instantiate once and call start()."""

    def __init__(self, settings: Settings, pool) -> None:
        self._pool     = pool
        self._interval = settings.economy_tick_interval_seconds
        self._webhook  = settings.economy_discord_webhook_url.strip()
        self._task: Optional[asyncio.Task] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Kick off the background loop.  Call from app lifespan startup."""
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop(), name="economy-worker")
        logger.info("EconomyWorker started — tick interval %ds", self._interval)

    async def stop(self) -> None:
        """Cancel the loop gracefully.  Call from app lifespan shutdown."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("EconomyWorker stopped")

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_live_price(
        self, node_id: str, commodity_id: str
    ) -> Decimal:
        """Return the current price for (node, commodity).  Used by Phase 2."""
        row = await self._pool.fetchrow(
            """
            SELECT mi.current_price
            FROM   market_inventory mi
            WHERE  mi.node_id      = $1
              AND  mi.commodity_id = $2
            """,
            UUID(node_id), UUID(commodity_id),
        )
        if row is None:
            raise ValueError(
                f"No inventory record for node={node_id} commodity={commodity_id}"
            )
        return Decimal(str(row["current_price"]))

    async def execute_transaction(
        self,
        node_id: str,
        commodity_id: str,
        quantity: Decimal,
        action: str,
        character_id: Optional[str] = None,
    ) -> TransactionResult:
        """Execute a player buy/sell and atomically update supply pool."""
        if action not in ("buy", "sell"):
            raise ValueError(f"Invalid action '{action}' — must be 'buy' or 'sell'")
        if quantity <= 0:
            raise ValueError("quantity must be positive")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT mi.supply, mi.max_supply, mi.current_price,
                           c.base_price, c.is_legal, c.campaign_id
                    FROM   market_inventory mi
                    JOIN   commodities c ON c.id = mi.commodity_id
                    WHERE  mi.node_id      = $1
                      AND  mi.commodity_id = $2
                    FOR UPDATE
                    """,
                    UUID(node_id), UUID(commodity_id),
                )
                if row is None:
                    raise ValueError(
                        f"No inventory for node={node_id} commodity={commodity_id}"
                    )

                supply        = Decimal(str(row["supply"]))
                max_supply    = Decimal(str(row["max_supply"]))
                current_price = Decimal(str(row["current_price"]))
                base_price    = Decimal(str(row["base_price"]))
                is_legal      = row["is_legal"]
                campaign_id   = row["campaign_id"]

                if action == "buy":
                    if supply < quantity:
                        raise ValueError(
                            f"Insufficient supply: requested {quantity}, available {supply}"
                        )
                    new_supply = supply - quantity
                else:  # sell
                    new_supply = min(supply + quantity, max_supply)

                new_price = self._calculate_price(base_price, new_supply, max_supply)

                await conn.execute(
                    """
                    UPDATE market_inventory
                    SET    supply = $1, current_price = $2, updated_at = NOW()
                    WHERE  node_id = $3 AND commodity_id = $4
                    """,
                    float(new_supply), float(new_price),
                    UUID(node_id), UUID(commodity_id),
                )

                total_value = current_price * quantity
                tx_row = await conn.fetchrow(
                    """
                    INSERT INTO market_transactions
                        (campaign_id, node_id, commodity_id, character_id,
                         action, quantity, unit_price, total_value, is_contraband)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    RETURNING id
                    """,
                    campaign_id,
                    UUID(node_id), UUID(commodity_id),
                    UUID(character_id) if character_id else None,
                    action,
                    float(quantity), float(current_price), float(total_value),
                    not is_legal,
                )

        return TransactionResult(
            transaction_id=str(tx_row["id"]),
            node_id=node_id,
            commodity_id=commodity_id,
            action=action,
            quantity=quantity,
            unit_price=current_price,
            total_value=total_value,
            new_supply=new_supply,
        )

    async def get_market_context(
        self, campaign_id: str, limit: int = 10
    ) -> list[MarketSummaryEntry]:
        """Return a narrative-ready summary of the most volatile market entries.

        Sorted by price_ratio descending so the GM Director naturally focuses
        on the most economically interesting commodities.
        """
        rows = await self._pool.fetch(
            """
            SELECT mn.name AS node_name,
                   co.name AS commodity_name,
                   mi.supply, mi.max_supply, mi.current_price, co.base_price
            FROM   market_inventory mi
            JOIN   market_nodes  mn ON mn.id = mi.node_id
            JOIN   commodities   co ON co.id = mi.commodity_id
            WHERE  mn.campaign_id = $1
              AND  mn.is_enabled  = TRUE
            ORDER BY (mi.current_price / co.base_price) DESC
            LIMIT  $2
            """,
            UUID(campaign_id), limit,
        )
        entries = []
        for r in rows:
            base  = Decimal(str(r["base_price"]))
            price = Decimal(str(r["current_price"]))
            entries.append(MarketSummaryEntry(
                node_name=r["node_name"],
                commodity_name=r["commodity_name"],
                supply=Decimal(str(r["supply"])),
                max_supply=Decimal(str(r["max_supply"])),
                current_price=price,
                base_price=base,
                price_ratio=price / base if base > 0 else Decimal("1"),
            ))
        return entries

    async def upsert_market_node(
        self,
        campaign_id: str,
        name: str,
        node_type: str = "settlement",
        location_label: Optional[str] = None,
        security_rating: int = 5,
        metadata: Optional[dict] = None,
    ) -> str:
        """Create or update a market node and return its UUID."""
        import json as _json
        row = await self._pool.fetchrow(
            """
            INSERT INTO market_nodes
                (campaign_id, name, node_type, location_label,
                 security_rating, metadata)
            VALUES ($1, $2, $3::market_node_type, $4, $5, $6)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            UUID(campaign_id), name, node_type, location_label,
            security_rating, _json.dumps(metadata or {}),
        )
        if row:
            return str(row["id"])
        existing = await self._pool.fetchrow(
            "SELECT id FROM market_nodes WHERE campaign_id=$1 AND name=$2",
            UUID(campaign_id), name,
        )
        return str(existing["id"])

    async def upsert_commodity(
        self,
        campaign_id: str,
        name: str,
        base_price: Decimal = Decimal("10.00"),
        is_legal: bool = True,
        category: str = "general",
    ) -> str:
        """Create or update a commodity and return its UUID."""
        row = await self._pool.fetchrow(
            """
            INSERT INTO commodities (campaign_id, name, base_price, is_legal, category)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (campaign_id, name) DO UPDATE
                SET base_price = EXCLUDED.base_price,
                    is_legal   = EXCLUDED.is_legal,
                    category   = EXCLUDED.category
            RETURNING id
            """,
            UUID(campaign_id), name, float(base_price), is_legal, category,
        )
        return str(row["id"])

    async def set_inventory(
        self,
        node_id: str,
        commodity_id: str,
        supply: Decimal,
        production_rate: Decimal = Decimal("0"),
        demand_rate: Decimal = Decimal("0"),
        max_supply: Decimal = Decimal("1000"),
    ) -> None:
        """Insert or replace an inventory row.  Sets initial price from formula."""
        base_row = await self._pool.fetchrow(
            "SELECT base_price FROM commodities WHERE id = $1",
            UUID(commodity_id),
        )
        base_price = Decimal(str(base_row["base_price"])) if base_row else Decimal("10")
        initial_price = self._calculate_price(base_price, supply, max_supply)

        await self._pool.execute(
            """
            INSERT INTO market_inventory
                (node_id, commodity_id, supply, production_rate, demand_rate,
                 max_supply, current_price)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (node_id, commodity_id) DO UPDATE
                SET supply          = EXCLUDED.supply,
                    production_rate = EXCLUDED.production_rate,
                    demand_rate     = EXCLUDED.demand_rate,
                    max_supply      = EXCLUDED.max_supply,
                    current_price   = EXCLUDED.current_price,
                    updated_at      = NOW()
            """,
            UUID(node_id), UUID(commodity_id),
            float(supply), float(production_rate), float(demand_rate),
            float(max_supply), float(initial_price),
        )

    # ── Background Loop ───────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self._tick_all_campaigns()
            except Exception as exc:
                logger.error("EconomyWorker tick error: %s", exc, exc_info=True)

    async def _tick_all_campaigns(self) -> None:
        """Process one economy tick for every campaign that has market nodes."""
        campaign_ids = await self._pool.fetch(
            """
            SELECT DISTINCT campaign_id
            FROM   market_nodes
            WHERE  is_enabled = TRUE
            """
        )
        for row in campaign_ids:
            cid = row["campaign_id"]
            try:
                await self._tick_campaign(cid)
            except Exception as exc:
                logger.error("Economy tick failed for campaign %s: %s", cid, exc)

    async def _tick_campaign(self, campaign_id: UUID) -> None:
        start_ms = int(time.monotonic() * 1000)

        nodes = await self._pool.fetch(
            """
            SELECT id FROM market_nodes
            WHERE campaign_id = $1 AND is_enabled = TRUE
            """,
            campaign_id,
        )

        nodes_ticked = 0
        for node_row in nodes:
            await self._tick_node(node_row["id"])
            nodes_ticked += 1

        ghost_hauls = await self._run_ghost_freighters(campaign_id)

        await self._notify_price_spikes(campaign_id)

        duration_ms = int(time.monotonic() * 1000) - start_ms
        await self._pool.execute(
            """
            INSERT INTO economy_tick_log
                (campaign_id, nodes_ticked, ghost_hauls, duration_ms)
            VALUES ($1, $2, $3, $4)
            """,
            campaign_id, nodes_ticked, ghost_hauls, duration_ms,
        )
        logger.info(
            "Economy tick: campaign=%s nodes=%d ghost_hauls=%d ms=%d",
            campaign_id, nodes_ticked, ghost_hauls, duration_ms,
        )

    async def _tick_node(self, node_id: UUID) -> None:
        """Apply production/demand to all commodities at one node and reprice."""
        rows = await self._pool.fetch(
            """
            SELECT mi.id, mi.supply, mi.production_rate, mi.demand_rate,
                   mi.max_supply, c.base_price
            FROM   market_inventory mi
            JOIN   commodities c ON c.id = mi.commodity_id
            WHERE  mi.node_id = $1
            """,
            node_id,
        )
        for row in rows:
            supply     = Decimal(str(row["supply"]))
            prod_rate  = Decimal(str(row["production_rate"]))
            demand_rate = Decimal(str(row["demand_rate"]))
            max_supply = Decimal(str(row["max_supply"]))
            base_price = Decimal(str(row["base_price"]))

            new_supply = min(supply + prod_rate - demand_rate, max_supply)
            new_supply = max(new_supply, Decimal("0"))
            new_price  = self._calculate_price(base_price, new_supply, max_supply)

            await self._pool.execute(
                """
                UPDATE market_inventory
                SET    supply = $1, current_price = $2, updated_at = NOW()
                WHERE  id = $3
                """,
                float(new_supply), float(new_price), row["id"],
            )

    async def _run_ghost_freighters(self, campaign_id: UUID) -> int:
        """Equalize extreme price disparities across nodes in the same campaign.

        A Ghost Freighter haul fires when:
          price_at_destination / price_at_source > _GHOST_HAUL_RATIO

        It transfers _GHOST_HAUL_FRACTION of max_supply from source to
        destination and records a 'ghost_haul' transaction to ensure the
        audit trail stays complete.
        """
        rows = await self._pool.fetch(
            """
            SELECT
                cheap.node_id  AS src_node_id,
                exp.node_id    AS dst_node_id,
                cheap.commodity_id,
                cheap.supply          AS src_supply,
                cheap.max_supply      AS src_max,
                cheap.current_price   AS src_price,
                exp.supply            AS dst_supply,
                exp.max_supply        AS dst_max,
                exp.current_price     AS dst_price,
                co.base_price, co.campaign_id
            FROM   market_inventory cheap
            JOIN   market_nodes     mn_s ON mn_s.id = cheap.node_id
            JOIN   market_inventory exp  ON  exp.commodity_id = cheap.commodity_id
                                        AND exp.node_id != cheap.node_id
            JOIN   market_nodes     mn_d ON mn_d.id = exp.node_id
            JOIN   commodities      co   ON co.id   = cheap.commodity_id
            WHERE  mn_s.campaign_id = $1
              AND  mn_d.campaign_id = $1
              AND  mn_s.is_enabled  = TRUE
              AND  mn_d.is_enabled  = TRUE
              AND  cheap.supply > 0
              AND  exp.current_price > cheap.current_price * $2
            LIMIT 20
            """,
            campaign_id, float(_GHOST_HAUL_RATIO),
        )

        hauls_done = 0
        for row in rows:
            src_supply  = Decimal(str(row["src_supply"]))
            src_max     = Decimal(str(row["src_max"]))
            dst_supply  = Decimal(str(row["dst_supply"]))
            dst_max     = Decimal(str(row["dst_max"]))
            base_price  = Decimal(str(row["base_price"]))
            src_price   = Decimal(str(row["src_price"]))

            haul_qty = min(
                src_max * _GHOST_HAUL_FRACTION,
                src_supply,
                dst_max - dst_supply,
            )
            if haul_qty <= 0:
                continue

            new_src_supply = src_supply - haul_qty
            new_dst_supply = dst_supply + haul_qty
            new_src_price  = self._calculate_price(base_price, new_src_supply, src_max)
            new_dst_price  = self._calculate_price(base_price, new_dst_supply, dst_max)

            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE market_inventory
                        SET    supply = $1, current_price = $2, updated_at = NOW()
                        WHERE  node_id = $3 AND commodity_id = $4
                        """,
                        float(new_src_supply), float(new_src_price),
                        row["src_node_id"], row["commodity_id"],
                    )
                    await conn.execute(
                        """
                        UPDATE market_inventory
                        SET    supply = $1, current_price = $2, updated_at = NOW()
                        WHERE  node_id = $3 AND commodity_id = $4
                        """,
                        float(new_dst_supply), float(new_dst_price),
                        row["dst_node_id"], row["commodity_id"],
                    )
                    await conn.execute(
                        """
                        INSERT INTO market_transactions
                            (campaign_id, node_id, commodity_id, action,
                             quantity, unit_price, total_value)
                        VALUES ($1, $2, $3, 'ghost_haul', $4, $5, $6)
                        """,
                        row["campaign_id"],
                        row["src_node_id"], row["commodity_id"],
                        float(haul_qty), float(src_price),
                        float(haul_qty * src_price),
                    )
            hauls_done += 1

        return hauls_done

    async def _notify_price_spikes(
        self, campaign_id: UUID, spike_threshold: Decimal = Decimal("10")
    ) -> None:
        """Fire a Discord webhook embed on extreme price spikes (>10× base).
        Fails silently — never blocks the tick loop.
        """
        if not self._webhook:
            return
        try:
            rows = await self._pool.fetch(
                """
                SELECT mn.name AS node, co.name AS commodity,
                       mi.current_price, co.base_price
                FROM   market_inventory mi
                JOIN   market_nodes  mn ON mn.id = mi.node_id
                JOIN   commodities   co ON co.id = mi.commodity_id
                WHERE  mn.campaign_id = $1
                  AND  mi.current_price > co.base_price * $2
                LIMIT 5
                """,
                campaign_id, float(spike_threshold),
            )
            if not rows:
                return
            lines = [
                f"**{r['node']}** — {r['commodity']}: "
                f"{r['current_price']:.0f} cr (base {r['base_price']:.0f} cr)"
                for r in rows
            ]
            payload = {
                "embeds": [{
                    "title": "📈 Economy Alert — Price Spike Detected",
                    "description": "\n".join(lines),
                    "color": 0xFF4500,
                }]
            }
            async with httpx.AsyncClient(timeout=8) as client:
                await client.post(self._webhook, json=payload)
        except Exception as exc:
            logger.debug("Economy webhook failed (non-fatal): %s", exc)

    # ── Price Formula ─────────────────────────────────────────────────────────

    @staticmethod
    def _calculate_price(
        base_price: Decimal, supply: Decimal, max_supply: Decimal
    ) -> Decimal:
        """Inverse-supply pricing:

        price = base_price * (max_supply / 2) / max(supply, max_supply * 0.05)

        At 50% supply → base_price  (reference point)
        At 10% supply → 5 × base_price
        At  0% supply → 10 × base_price (capped at 20×)
        At 100% supply → 0.5 × base_price (floored at 0.2×)
        """
        if max_supply <= 0:
            return base_price
        min_denom = max_supply * _MIN_SUPPLY_FRAC
        effective_supply = max(supply, min_denom)
        raw = base_price * (max_supply / Decimal("2")) / effective_supply
        return max(_PRICE_FLOOR * base_price, min(_PRICE_CAP * base_price, raw))
