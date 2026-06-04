"""Unit tests for EconomyWorker (Issue #24 — Async Market Maker).

All database and HTTP calls are mocked — no live server required.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.economy_worker import (
    EconomyWorker,
    MarketSummaryEntry,
    TransactionResult,
    _PRICE_CAP,
    _PRICE_FLOOR,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _settings(**kwargs):
    s = MagicMock()
    s.economy_tick_interval_seconds = kwargs.get("interval", 3600)
    s.economy_discord_webhook_url   = kwargs.get("webhook", "")
    return s


def _pool():
    p = MagicMock()
    p.fetchrow  = AsyncMock()
    p.fetch     = AsyncMock(return_value=[])
    p.execute   = AsyncMock()
    p.acquire   = MagicMock()
    return p


# ── TestPriceFormula ──────────────────────────────────────────────────────────

class TestPriceFormula:
    """Verify the inverse-supply pricing function at key supply levels."""

    base = Decimal("100")
    max_s = Decimal("1000")

    def _p(self, supply):
        return EconomyWorker._calculate_price(self.base, Decimal(str(supply)), self.max_s)

    def test_at_half_supply_equals_base(self):
        price = self._p(500)
        # 100 * 500 / 500 = 100
        assert price == self.base

    def test_at_tenth_supply_is_five_times_base(self):
        price = self._p(100)
        # 100 * 500 / 100 = 500
        assert price == self.base * Decimal("5")

    def test_at_full_supply_is_half_base(self):
        price = self._p(1000)
        # 100 * 500 / 1000 = 50
        assert price == self.base / 2

    def test_at_zero_supply_hits_price_cap(self):
        price = self._p(0)
        # min_denom = 1000 * 0.05 = 50 → 100 * 500 / 50 = 1000 = base * 10
        # but capped at base * 20 → stays at 1000
        assert price == self.base * Decimal("10")

    def test_price_floor_applied(self):
        # Enormous supply — price should not go below PRICE_FLOOR * base
        price = EconomyWorker._calculate_price(
            self.base, Decimal("999999"), self.max_s
        )
        assert price == _PRICE_FLOOR * self.base

    def test_price_cap_applied(self):
        # Tiny supply, huge max_supply — price should not exceed CAP * base
        price = EconomyWorker._calculate_price(
            self.base, Decimal("1"), Decimal("100000")
        )
        assert price == _PRICE_CAP * self.base

    def test_zero_max_supply_returns_base(self):
        price = EconomyWorker._calculate_price(self.base, Decimal("100"), Decimal("0"))
        assert price == self.base


# ── TestGetLivePrice ──────────────────────────────────────────────────────────

class TestGetLivePrice:
    @pytest.mark.asyncio
    async def test_returns_price_from_db(self):
        pool = _pool()
        pool.fetchrow.return_value = {"current_price": 75.50}
        worker = EconomyWorker(_settings(), pool)
        price = await worker.get_live_price("node-uuid", "commodity-uuid")
        assert price == Decimal("75.50")

    @pytest.mark.asyncio
    async def test_raises_when_not_found(self):
        pool = _pool()
        pool.fetchrow.return_value = None
        worker = EconomyWorker(_settings(), pool)
        with pytest.raises(ValueError, match="No inventory record"):
            await worker.get_live_price("00000000-0000-0000-0000-000000000001",
                                         "00000000-0000-0000-0000-000000000002")


# ── TestExecuteTransaction ────────────────────────────────────────────────────

class TestExecuteTransaction:
    def _make_conn(self, supply=500.0, max_supply=1000.0, price=100.0,
                   base=100.0, legal=True):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value={
            "supply":        supply,
            "max_supply":    max_supply,
            "current_price": price,
            "base_price":    base,
            "is_legal":      legal,
            "campaign_id":   "00000000-0000-0000-0000-000000000099",
        })
        conn.execute = AsyncMock()
        conn.fetchrow_tx = AsyncMock(return_value={"id": "00000000-0000-0000-0000-000000000055"})
        # second fetchrow call is for INSERT RETURNING
        conn.fetchrow.side_effect = [
            {"supply": supply, "max_supply": max_supply, "current_price": price,
             "base_price": base, "is_legal": legal,
             "campaign_id": "00000000-0000-0000-0000-000000000099"},
            {"id": "00000000-0000-0000-0000-000000000055"},
        ]
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__  = AsyncMock(return_value=False)
        return conn

    def _make_pool(self, conn):
        pool = _pool()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__  = AsyncMock(return_value=False)
        pool.acquire.return_value = ctx
        return pool

    @pytest.mark.asyncio
    async def test_buy_reduces_supply(self):
        conn = self._make_conn(supply=500.0)
        pool = self._make_pool(conn)
        worker = EconomyWorker(_settings(), pool)
        result = await worker.execute_transaction(
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
            Decimal("50"), "buy",
        )
        assert result.action == "buy"
        assert result.quantity == Decimal("50")
        assert result.new_supply == Decimal("450")

    @pytest.mark.asyncio
    async def test_sell_increases_supply(self):
        conn = self._make_conn(supply=300.0)
        pool = self._make_pool(conn)
        worker = EconomyWorker(_settings(), pool)
        result = await worker.execute_transaction(
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
            Decimal("100"), "sell",
        )
        assert result.action == "sell"
        assert result.new_supply == Decimal("400")

    @pytest.mark.asyncio
    async def test_sell_capped_at_max_supply(self):
        conn = self._make_conn(supply=950.0, max_supply=1000.0)
        pool = self._make_pool(conn)
        worker = EconomyWorker(_settings(), pool)
        result = await worker.execute_transaction(
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
            Decimal("200"), "sell",
        )
        assert result.new_supply == Decimal("1000")  # capped

    @pytest.mark.asyncio
    async def test_buy_raises_on_insufficient_supply(self):
        conn = self._make_conn(supply=10.0)
        pool = self._make_pool(conn)
        worker = EconomyWorker(_settings(), pool)
        with pytest.raises(ValueError, match="Insufficient supply"):
            await worker.execute_transaction(
                "00000000-0000-0000-0000-000000000001",
                "00000000-0000-0000-0000-000000000002",
                Decimal("50"), "buy",
            )

    @pytest.mark.asyncio
    async def test_invalid_action_raises(self):
        worker = EconomyWorker(_settings(), _pool())
        with pytest.raises(ValueError, match="Invalid action"):
            await worker.execute_transaction(
                "00000000-0000-0000-0000-000000000001",
                "00000000-0000-0000-0000-000000000002",
                Decimal("50"), "loot",
            )

    @pytest.mark.asyncio
    async def test_zero_quantity_raises(self):
        worker = EconomyWorker(_settings(), _pool())
        with pytest.raises(ValueError, match="quantity must be positive"):
            await worker.execute_transaction(
                "00000000-0000-0000-0000-000000000001",
                "00000000-0000-0000-0000-000000000002",
                Decimal("0"), "buy",
            )


# ── TestTickNode ──────────────────────────────────────────────────────────────

class TestTickNode:
    @pytest.mark.asyncio
    async def test_production_increases_supply(self):
        pool = _pool()
        import uuid
        inv_id = uuid.uuid4()
        pool.fetch.return_value = [{
            "id": inv_id,
            "supply": 400.0,
            "production_rate": 50.0,
            "demand_rate": 10.0,
            "max_supply": 1000.0,
            "base_price": 100.0,
        }]
        worker = EconomyWorker(_settings(), pool)
        await worker._tick_node(uuid.uuid4())
        # Called once with UPDATE for new supply=440
        assert pool.execute.called
        call_args = pool.execute.call_args
        new_supply_arg = call_args[0][1]  # positional arg $1
        assert abs(new_supply_arg - 440.0) < 0.01

    @pytest.mark.asyncio
    async def test_demand_decreases_supply(self):
        pool = _pool()
        import uuid
        inv_id = uuid.uuid4()
        pool.fetch.return_value = [{
            "id": inv_id,
            "supply": 500.0,
            "production_rate": 0.0,
            "demand_rate": 100.0,
            "max_supply": 1000.0,
            "base_price": 100.0,
        }]
        worker = EconomyWorker(_settings(), pool)
        await worker._tick_node(uuid.uuid4())
        call_args = pool.execute.call_args
        new_supply_arg = call_args[0][1]
        assert abs(new_supply_arg - 400.0) < 0.01

    @pytest.mark.asyncio
    async def test_supply_floored_at_zero(self):
        pool = _pool()
        import uuid
        pool.fetch.return_value = [{
            "id": uuid.uuid4(),
            "supply": 5.0,
            "production_rate": 0.0,
            "demand_rate": 100.0,
            "max_supply": 1000.0,
            "base_price": 100.0,
        }]
        worker = EconomyWorker(_settings(), pool)
        await worker._tick_node(uuid.uuid4())
        call_args = pool.execute.call_args
        new_supply_arg = call_args[0][1]
        assert new_supply_arg == 0.0

    @pytest.mark.asyncio
    async def test_supply_capped_at_max(self):
        pool = _pool()
        import uuid
        pool.fetch.return_value = [{
            "id": uuid.uuid4(),
            "supply": 990.0,
            "production_rate": 100.0,
            "demand_rate": 0.0,
            "max_supply": 1000.0,
            "base_price": 100.0,
        }]
        worker = EconomyWorker(_settings(), pool)
        await worker._tick_node(uuid.uuid4())
        call_args = pool.execute.call_args
        new_supply_arg = call_args[0][1]
        assert new_supply_arg == 1000.0


# ── TestGhostFreighters ───────────────────────────────────────────────────────

class TestGhostFreighters:
    @pytest.mark.asyncio
    async def test_no_haul_when_no_disparity(self):
        pool = _pool()
        pool.fetch.return_value = []  # no nodes with sufficient price gap
        import uuid
        worker = EconomyWorker(_settings(), pool)
        hauls = await worker._run_ghost_freighters(uuid.uuid4())
        assert hauls == 0

    @pytest.mark.asyncio
    async def test_haul_fires_on_disparity(self):
        import uuid
        pool = _pool()
        src_node = uuid.uuid4()
        dst_node = uuid.uuid4()
        commodity = uuid.uuid4()
        campaign  = uuid.uuid4()
        pool.fetch.return_value = [{
            "src_node_id":  src_node,
            "dst_node_id":  dst_node,
            "commodity_id": commodity,
            "src_supply":  800.0,
            "src_max":     1000.0,
            "src_price":   50.0,
            "dst_supply":  50.0,
            "dst_max":     1000.0,
            "dst_price":   500.0,
            "base_price":  100.0,
            "campaign_id": campaign,
        }]
        conn = MagicMock()
        conn.execute = AsyncMock()
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__  = AsyncMock(return_value=False)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__  = AsyncMock(return_value=False)
        pool.acquire.return_value = ctx
        worker = EconomyWorker(_settings(), pool)
        hauls = await worker._run_ghost_freighters(campaign)
        assert hauls == 1
        # Three UPDATE/INSERT calls expected per haul
        assert conn.execute.call_count == 3

    @pytest.mark.asyncio
    async def test_haul_quantity_capped_by_dst_capacity(self):
        import uuid
        pool = _pool()
        campaign = uuid.uuid4()
        pool.fetch.return_value = [{
            "src_node_id":  uuid.uuid4(),
            "dst_node_id":  uuid.uuid4(),
            "commodity_id": uuid.uuid4(),
            "src_supply":  800.0,
            "src_max":     1000.0,
            "src_price":   50.0,
            "dst_supply":  995.0,   # nearly full — only 5 units capacity left
            "dst_max":     1000.0,
            "dst_price":   500.0,
            "base_price":  100.0,
            "campaign_id": campaign,
        }]
        conn = MagicMock()
        conn.execute = AsyncMock()
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__  = AsyncMock(return_value=False)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__  = AsyncMock(return_value=False)
        pool.acquire.return_value = ctx
        worker = EconomyWorker(_settings(), pool)
        hauls = await worker._run_ghost_freighters(campaign)
        # Still fires — 5 units transferred
        assert hauls == 1


# ── TestGetMarketContext ──────────────────────────────────────────────────────

class TestGetMarketContext:
    @pytest.mark.asyncio
    async def test_returns_sorted_entries(self):
        pool = _pool()
        pool.fetch.return_value = [
            {"node_name": "Docklands",  "commodity_name": "Fuel",
             "supply": 100.0, "max_supply": 1000.0,
             "current_price": 500.0, "base_price": 100.0},
            {"node_name": "Agri-Dome",  "commodity_name": "Food",
             "supply": 900.0, "max_supply": 1000.0,
             "current_price": 50.0,  "base_price": 100.0},
        ]
        worker = EconomyWorker(_settings(), pool)
        entries = await worker.get_market_context(
            "00000000-0000-0000-0000-000000000001"
        )
        assert len(entries) == 2
        assert entries[0].node_name == "Docklands"
        assert entries[0].price_ratio == Decimal("5")   # 500/100
        assert entries[1].price_ratio == Decimal("0.5") # 50/100

    @pytest.mark.asyncio
    async def test_empty_campaign_returns_empty_list(self):
        pool = _pool()
        pool.fetch.return_value = []
        worker = EconomyWorker(_settings(), pool)
        entries = await worker.get_market_context(
            "00000000-0000-0000-0000-000000000001"
        )
        assert entries == []


# ── TestBackgroundLoop ────────────────────────────────────────────────────────

class TestBackgroundLoop:
    @pytest.mark.asyncio
    async def test_start_creates_task(self):
        worker = EconomyWorker(_settings(interval=9999), _pool())
        await worker.start()
        assert worker._task is not None
        assert not worker._task.done()
        await worker.stop()

    @pytest.mark.asyncio
    async def test_start_idempotent(self):
        worker = EconomyWorker(_settings(interval=9999), _pool())
        await worker.start()
        task_before = worker._task
        await worker.start()  # second call should be no-op
        assert worker._task is task_before
        await worker.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self):
        worker = EconomyWorker(_settings(interval=9999), _pool())
        await worker.start()
        await worker.stop()
        assert worker._task.cancelled() or worker._task.done()


# ── TestNotifyPriceSpikes ─────────────────────────────────────────────────────

class TestNotifyPriceSpikes:
    @pytest.mark.asyncio
    async def test_no_webhook_configured_skips_silently(self):
        pool = _pool()
        import uuid
        worker = EconomyWorker(_settings(webhook=""), pool)
        await worker._notify_price_spikes(uuid.uuid4())  # should not raise
        pool.fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_webhook_fires_on_spike(self):
        pool = _pool()
        import uuid
        pool.fetch.return_value = [{
            "node": "War Zone", "commodity": "Meds",
            "current_price": 1200.0, "base_price": 100.0,
        }]
        worker = EconomyWorker(_settings(webhook="https://example.test/hook"), pool)
        with patch("orchestrator.services.economy_worker.httpx.AsyncClient") as mock_client:
            client_inst = AsyncMock()
            client_inst.post = AsyncMock()
            mock_client.return_value.__aenter__ = AsyncMock(return_value=client_inst)
            mock_client.return_value.__aexit__  = AsyncMock(return_value=False)
            await worker._notify_price_spikes(uuid.uuid4())
        client_inst.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_webhook_failure_does_not_raise(self):
        pool = _pool()
        import uuid
        pool.fetch.return_value = [{
            "node": "War Zone", "commodity": "Meds",
            "current_price": 1200.0, "base_price": 100.0,
        }]
        worker = EconomyWorker(_settings(webhook="https://example.test/hook"), pool)
        with patch("orchestrator.services.economy_worker.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(side_effect=Exception("network error"))
            mock_client.return_value.__aexit__  = AsyncMock(return_value=False)
            await worker._notify_price_spikes(uuid.uuid4())  # must not raise
