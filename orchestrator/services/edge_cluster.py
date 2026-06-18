"""
Ironclad GM — Edge Cluster Manager
Manages distributed edge node lifecycle:
  - Wake-on-LAN: wake ARM worker nodes before a session starts
  - SSH graceful shutdown
  - Node registration in node_registry
  - Thermal status checking
"""
from __future__ import annotations
import asyncio
import logging
import socket
from dataclasses import dataclass, field
from typing import Optional
import httpx

logger = logging.getLogger(__name__)
_WOL_PORT = 9
_WOL_BROADCAST = "255.255.255.255"
_SSH_TIMEOUT = 30


@dataclass
class EdgeNode:
    name: str
    host: str          # e.g. http://192.168.1.20:11434
    arch: str          # amd64 | arm64
    mac_address: str = ""
    ssh_host: str = ""
    ssh_user: str = "root"
    hardware_tier: str = "standard"  # standard | high | low


class EdgeClusterManager:
    def __init__(self, db=None, settings=None):
        self._db = db
        self._settings = settings

    @staticmethod
    def build_wol_packet(mac_address: str) -> bytes:
        mac_clean = mac_address.upper().replace(":", "").replace("-", "")
        if len(mac_clean) != 12 or not all(c in "0123456789ABCDEF" for c in mac_clean):
            raise ValueError(f"Invalid MAC address: {mac_address!r}")
        mac_bytes = bytes.fromhex(mac_clean)
        return b"\xff" * 6 + mac_bytes * 16

    @staticmethod
    async def send_wol_packet(mac_address: str, broadcast_ip: str = _WOL_BROADCAST) -> None:
        packet = EdgeClusterManager.build_wol_packet(mac_address)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _send_udp_broadcast, packet, broadcast_ip)
        logger.info("WoL magic packet sent to %s", mac_address)

    async def wake_worker_nodes(self, nodes: list[EdgeNode], wait_seconds: int = 60) -> None:
        wol_tasks = [self.send_wol_packet(n.mac_address) for n in nodes if n.mac_address]
        if wol_tasks:
            await asyncio.gather(*wol_tasks, return_exceptions=True)
            poll_tasks = [_poll_until_online(n.host, timeout=wait_seconds) for n in nodes if n.mac_address]
            await asyncio.gather(*poll_tasks, return_exceptions=True)

    async def register_edge_node(self, node: EdgeNode) -> None:
        if self._db is None:
            logger.warning("No DB — skipping registration for %s", node.name)
            return
        roles = ["adjudication"]
        if node.arch == "amd64" and node.hardware_tier == "high":
            roles.append("narrative")
        await self._db.upsert_node_registry(
            node_name=node.name, host=node.host, arch=node.arch, roles=roles, enabled=True
        )

    async def deregister_edge_node(self, node_name: str) -> None:
        if self._db is None:
            return
        await self._db.disable_node(node_name)

    async def graceful_shutdown_workers(self, nodes: list[EdgeNode]) -> None:
        tasks = [self._ssh_shutdown(n) for n in nodes if n.ssh_host]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _ssh_shutdown(self, node: EdgeNode) -> None:
        try:
            import asyncssh
            async with asyncssh.connect(
                node.ssh_host,
                username=node.ssh_user,
                known_hosts=None,
                connect_timeout=_SSH_TIMEOUT,
            ) as conn:
                await conn.run(
                    "docker service scale aetheris-worker_brain=0 2>/dev/null "
                    "|| sudo systemctl stop ollama",
                    timeout=_SSH_TIMEOUT,
                )
        except ImportError:
            logger.info("asyncssh not installed — SSH shutdown disabled")
        except Exception as exc:
            logger.warning("SSH shutdown failed for %s: %s", node.name, exc)

    async def get_thermal_status(self, nodes: list[EdgeNode]) -> dict[str, dict]:
        results = {}
        for node in nodes:
            try:
                status, ttft_ms = await _check_node_health(node.host)
                results[node.name] = {"status": status, "ttft_ms": ttft_ms}
            except Exception as exc:
                results[node.name] = {"status": "offline", "ttft_ms": None, "error": str(exc)}
        return results


def _send_udp_broadcast(packet: bytes, broadcast_ip: str) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast_ip, _WOL_PORT))


async def _poll_until_online(host: str, timeout: int = 60, interval: int = 5) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.get(f"{host}/api/tags")
                if r.status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(interval)
    return False


async def _check_node_health(host: str) -> tuple[str, int | None]:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{host}/api/tags")
            return ("online", None) if r.status_code == 200 else ("degraded", None)
    except Exception:
        return "offline", None
