"""
Tests for EdgeClusterManager — WoL packet construction, node registration,
thermal health checks, and wake/sleep lifecycle.
"""
from __future__ import annotations
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from orchestrator.services.edge_cluster import (
    EdgeClusterManager,
    EdgeNode,
    _send_udp_broadcast,
)


# ── TestBuildWolPacket ────────────────────────────────────────────────────────

class TestBuildWolPacket:
    def test_colon_separator(self):
        pkt = EdgeClusterManager.build_wol_packet("AA:BB:CC:DD:EE:FF")
        assert len(pkt) == 102
        assert pkt[:6] == b"\xff" * 6
        assert pkt[6:12] == bytes.fromhex("AABBCCDDEEFF")
        assert pkt[6:] == bytes.fromhex("AABBCCDDEEFF") * 16

    def test_dash_separator(self):
        pkt = EdgeClusterManager.build_wol_packet("AA-BB-CC-DD-EE-FF")
        assert len(pkt) == 102

    def test_lowercase(self):
        pkt = EdgeClusterManager.build_wol_packet("aa:bb:cc:dd:ee:ff")
        assert len(pkt) == 102
        assert pkt[6:12] == bytes.fromhex("aabbccddeeff")

    def test_invalid_short(self):
        with pytest.raises(ValueError):
            EdgeClusterManager.build_wol_packet("AA:BB:CC")

    def test_invalid_chars(self):
        with pytest.raises(ValueError):
            EdgeClusterManager.build_wol_packet("GG:HH:II:JJ:KK:LL")

    def test_zero_mac(self):
        pkt = EdgeClusterManager.build_wol_packet("00:00:00:00:00:00")
        assert pkt == b"\xff" * 6 + b"\x00" * 6 * 16

    def test_broadcast_mac(self):
        pkt = EdgeClusterManager.build_wol_packet("FF:FF:FF:FF:FF:FF")
        assert pkt == b"\xff" * 102


# ── TestEdgeNodeRegistration ──────────────────────────────────────────────────

class TestEdgeNodeRegistration:
    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.upsert_node_registry = AsyncMock()
        db.disable_node = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_arm64_gets_adjudication_only(self, mock_db):
        mgr = EdgeClusterManager(db=mock_db)
        node = EdgeNode(
            name="worker-1", host="http://192.168.1.20:11434",
            arch="arm64", hardware_tier="standard",
        )
        await mgr.register_edge_node(node)
        mock_db.upsert_node_registry.assert_called_once()
        roles = mock_db.upsert_node_registry.call_args[1]["roles"]
        assert "adjudication" in roles
        assert "narrative" not in roles

    @pytest.mark.asyncio
    async def test_amd64_high_tier_gets_narrative(self, mock_db):
        mgr = EdgeClusterManager(db=mock_db)
        node = EdgeNode(
            name="worker-2", host="http://192.168.1.21:11434",
            arch="amd64", hardware_tier="high",
        )
        await mgr.register_edge_node(node)
        roles = mock_db.upsert_node_registry.call_args[1]["roles"]
        assert "adjudication" in roles
        assert "narrative" in roles

    @pytest.mark.asyncio
    async def test_no_db_logs_warning(self, caplog):
        import logging
        mgr = EdgeClusterManager(db=None)
        node = EdgeNode(name="worker-3", host="http://192.168.1.22:11434", arch="arm64")
        with caplog.at_level(logging.WARNING):
            await mgr.register_edge_node(node)
        assert "No DB" in caplog.text

    @pytest.mark.asyncio
    async def test_deregister_calls_disable(self, mock_db):
        mgr = EdgeClusterManager(db=mock_db)
        await mgr.deregister_edge_node("worker-1")
        mock_db.disable_node.assert_called_once_with("worker-1")


# ── TestSendWolPacket ─────────────────────────────────────────────────────────

class TestSendWolPacket:
    @pytest.mark.asyncio
    async def test_send_calls_udp_broadcast(self):
        with patch("orchestrator.services.edge_cluster._send_udp_broadcast") as mock_send:
            await EdgeClusterManager.send_wol_packet("AA:BB:CC:DD:EE:FF")
        mock_send.assert_called_once()
        pkt, addr = mock_send.call_args[0]
        assert len(pkt) == 102
        assert addr == "255.255.255.255"

    def test_packet_length_is_102_bytes(self):
        pkt = EdgeClusterManager.build_wol_packet("11:22:33:44:55:66")
        assert len(pkt) == 102


# ── TestThermalStatus ─────────────────────────────────────────────────────────

class TestThermalStatus:
    @pytest.mark.asyncio
    async def test_online_node(self):
        mgr = EdgeClusterManager()
        node = EdgeNode(name="w1", host="http://192.168.1.20:11434", arch="amd64")
        with patch(
            "orchestrator.services.edge_cluster._check_node_health",
            new_callable=AsyncMock,
            return_value=("online", None),
        ):
            result = await mgr.get_thermal_status([node])
        assert result["w1"]["status"] == "online"

    @pytest.mark.asyncio
    async def test_offline_node(self):
        mgr = EdgeClusterManager()
        node = EdgeNode(name="w2", host="http://192.168.1.21:11434", arch="arm64")
        with patch(
            "orchestrator.services.edge_cluster._check_node_health",
            new_callable=AsyncMock,
            return_value=("offline", None),
        ):
            result = await mgr.get_thermal_status([node])
        assert result["w2"]["status"] == "offline"

    @pytest.mark.asyncio
    async def test_exception_returns_offline(self):
        mgr = EdgeClusterManager()
        node = EdgeNode(name="w3", host="http://192.168.1.22:11434", arch="arm64")
        with patch(
            "orchestrator.services.edge_cluster._check_node_health",
            new_callable=AsyncMock,
            side_effect=Exception("connection timeout"),
        ):
            result = await mgr.get_thermal_status([node])
        assert result["w3"]["status"] == "offline"
        assert "error" in result["w3"]


# ── TestWakeWorkerNodes ───────────────────────────────────────────────────────

class TestWakeWorkerNodes:
    @pytest.mark.asyncio
    async def test_no_mac_skips_wol(self):
        mgr = EdgeClusterManager()
        node = EdgeNode(
            name="w1", host="http://192.168.1.20:11434",
            arch="arm64", mac_address="",
        )
        with patch.object(mgr, "send_wol_packet", new_callable=AsyncMock) as mock_wol:
            await mgr.wake_worker_nodes([node], wait_seconds=0)
        mock_wol.assert_not_called()

    @pytest.mark.asyncio
    async def test_with_mac_sends_wol(self):
        mgr = EdgeClusterManager()
        node = EdgeNode(
            name="w2", host="http://192.168.1.21:11434",
            arch="arm64", mac_address="AA:BB:CC:DD:EE:FF",
        )
        with patch.object(mgr, "send_wol_packet", new_callable=AsyncMock) as mock_wol, \
             patch(
                 "orchestrator.services.edge_cluster._poll_until_online",
                 new_callable=AsyncMock,
                 return_value=True,
             ):
            await mgr.wake_worker_nodes([node], wait_seconds=1)
        mock_wol.assert_called_once_with("AA:BB:CC:DD:EE:FF")
