"""
Unit tests for DiskAgentService and RollingVault.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.disk_agent import DiskAgentService
from orchestrator.services.rolling_vault import RollingVault, WINDOW_SIZE


# ─────────────────────────────────────────────────────────────────────────────
# DiskAgentService tests
# ─────────────────────────────────────────────────────────────────────────────


CAMPAIGN_ID = "camp001"


class TestDiskAgentSafePath:
    """_safe_path() security boundary enforcement."""

    def test_valid_path_resolves_within_sandbox(self, tmp_path):
        svc = DiskAgentService(str(tmp_path))
        result = svc._safe_path(CAMPAIGN_ID, "maps/world.svg")
        assert str(result).startswith(str(tmp_path / CAMPAIGN_ID))

    def test_traversal_with_double_dot_raises(self, tmp_path):
        svc = DiskAgentService(str(tmp_path))
        with pytest.raises(ValueError, match="traversal"):
            svc._safe_path(CAMPAIGN_ID, "../other_campaign/secret.txt")

    def test_absolute_path_escaping_raises(self, tmp_path):
        svc = DiskAgentService(str(tmp_path))
        with pytest.raises(ValueError):
            svc._safe_path(CAMPAIGN_ID, "/etc/passwd")

    def test_blocked_characters_raise(self, tmp_path):
        svc = DiskAgentService(str(tmp_path))
        for ch in (";", "&", "$", "|", "`"):
            with pytest.raises(ValueError, match="Disallowed"):
                svc._safe_path(CAMPAIGN_ID, f"maps/map{ch}rm -rf.svg")

    def test_invalid_campaign_id_with_slash_raises(self, tmp_path):
        svc = DiskAgentService(str(tmp_path))
        with pytest.raises(ValueError, match="Invalid campaign_id"):
            svc._safe_path("../../evil", "ok.txt")

    def test_empty_rel_path_raises(self, tmp_path):
        svc = DiskAgentService(str(tmp_path))
        with pytest.raises(ValueError, match="must not be empty"):
            svc._safe_path(CAMPAIGN_ID, "")


class TestDiskAgentWrite:
    """write() creates files in the sandbox."""

    @pytest.mark.asyncio
    async def test_write_creates_file(self, tmp_path):
        svc = DiskAgentService(str(tmp_path))
        result = await svc.write(CAMPAIGN_ID, "lore_notes/chapter1.txt", "Once upon a time…")
        expected = tmp_path / CAMPAIGN_ID / "lore_notes" / "chapter1.txt"
        assert expected.exists()
        assert expected.read_text("utf-8") == "Once upon a time…"
        assert result["bytes_written"] == len("Once upon a time…".encode("utf-8"))

    @pytest.mark.asyncio
    async def test_write_creates_intermediate_dirs(self, tmp_path):
        svc = DiskAgentService(str(tmp_path))
        await svc.write(CAMPAIGN_ID, "deep/nested/dir/note.txt", "hello")
        assert (tmp_path / CAMPAIGN_ID / "deep" / "nested" / "dir" / "note.txt").exists()

    @pytest.mark.asyncio
    async def test_write_overwrites_existing_file(self, tmp_path):
        svc = DiskAgentService(str(tmp_path))
        await svc.write(CAMPAIGN_ID, "doc.txt", "v1")
        await svc.write(CAMPAIGN_ID, "doc.txt", "v2")
        assert (tmp_path / CAMPAIGN_ID / "doc.txt").read_text("utf-8") == "v2"

    @pytest.mark.asyncio
    async def test_write_returns_relative_path(self, tmp_path):
        svc = DiskAgentService(str(tmp_path))
        result = await svc.write(CAMPAIGN_ID, "map.svg", "<svg/>")
        assert result["path"] == f"{CAMPAIGN_ID}/map.svg"


class TestDiskAgentRead:
    """read() retrieves file content from the sandbox."""

    @pytest.mark.asyncio
    async def test_read_existing_file(self, tmp_path):
        svc = DiskAgentService(str(tmp_path))
        await svc.write(CAMPAIGN_ID, "note.txt", "Secret lore")
        content = await svc.read(CAMPAIGN_ID, "note.txt")
        assert content == "Secret lore"

    @pytest.mark.asyncio
    async def test_read_missing_file_raises(self, tmp_path):
        svc = DiskAgentService(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            await svc.read(CAMPAIGN_ID, "nonexistent.txt")


class TestDiskAgentList:
    """list_files() returns files in the sandbox."""

    @pytest.mark.asyncio
    async def test_list_empty_campaign(self, tmp_path):
        svc = DiskAgentService(str(tmp_path))
        result = await svc.list_files(CAMPAIGN_ID)
        assert result == []

    @pytest.mark.asyncio
    async def test_list_finds_written_files(self, tmp_path):
        svc = DiskAgentService(str(tmp_path))
        await svc.write(CAMPAIGN_ID, "maps/a.svg", "a")
        await svc.write(CAMPAIGN_ID, "lore/b.txt", "b")
        result = await svc.list_files(CAMPAIGN_ID)
        paths = [r["path"] for r in result]
        assert "maps/a.svg" in paths
        assert "lore/b.txt" in paths
        assert all("size_bytes" in r and "modified" in r for r in result)

    @pytest.mark.asyncio
    async def test_list_subdir_filters_correctly(self, tmp_path):
        svc = DiskAgentService(str(tmp_path))
        await svc.write(CAMPAIGN_ID, "maps/x.svg", "x")
        await svc.write(CAMPAIGN_ID, "lore/y.txt", "y")
        result = await svc.list_files(CAMPAIGN_ID, subdir="maps")
        paths = [r["path"] for r in result]
        assert "maps/x.svg" in paths
        assert all("lore" not in p for p in paths)


class TestDiskAgentDelete:
    """delete() removes files from the sandbox."""

    @pytest.mark.asyncio
    async def test_delete_existing_file(self, tmp_path):
        svc = DiskAgentService(str(tmp_path))
        await svc.write(CAMPAIGN_ID, "temp.txt", "bye")
        deleted = await svc.delete(CAMPAIGN_ID, "temp.txt")
        assert deleted is True
        assert not (tmp_path / CAMPAIGN_ID / "temp.txt").exists()

    @pytest.mark.asyncio
    async def test_delete_missing_file_returns_false(self, tmp_path):
        svc = DiskAgentService(str(tmp_path))
        result = await svc.delete(CAMPAIGN_ID, "ghost.txt")
        assert result is False


# ─────────────────────────────────────────────────────────────────────────────
# RollingVault tests
# ─────────────────────────────────────────────────────────────────────────────


def _make_conn():
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.executemany = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetch = AsyncMock(return_value=[])
    conn.transaction = MagicMock(return_value=_AsyncCM())
    return conn


class _AsyncCM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


def _make_pool_with_conn(conn):
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_PoolAcquireCM(conn))
    return pool


class _PoolAcquireCM:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_):
        pass


def _make_node_router(nodes=None):
    router = MagicMock()
    router.get_nodes_for_role_by_latency = AsyncMock(return_value=nodes or [])
    return router


CAID = "a0000000-0000-0000-0000-000000000001"


class TestRollingVaultAppend:
    """append() inserts turns and may trigger compression."""

    @pytest.mark.asyncio
    async def test_append_without_bound_pool_is_noop(self):
        vault = RollingVault(_make_node_router())
        # pool not bound → should log and return silently
        await vault.append(CAID, "attack", "hit!")

    @pytest.mark.asyncio
    async def test_append_inserts_player_and_gm_rows(self):
        conn = _make_conn()
        conn.fetchval.side_effect = [1, 5]  # next_seq=1, then count=5 (below WINDOW_SIZE)
        vault = RollingVault(_make_node_router())
        vault.bind(_make_pool_with_conn(conn))

        await vault.append(CAID, "I attack", "You hit for 8 damage.")
        conn.executemany.assert_called_once()
        rows = conn.executemany.call_args.args[1]
        roles = [r[2] for r in rows]
        assert "player" in roles
        assert "gm" in roles

    @pytest.mark.asyncio
    async def test_append_triggers_compression_when_full(self):
        conn = _make_conn()
        conn.fetchval.side_effect = [1, WINDOW_SIZE + 1]  # next_seq, then count
        vault = RollingVault(_make_node_router())
        vault.bind(_make_pool_with_conn(conn))

        with patch.object(vault, "_compress_oldest", AsyncMock()) as mock_comp:
            await vault.append(CAID, "p", "g")

        mock_comp.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_append_truncates_long_content(self):
        conn = _make_conn()
        conn.fetchval.side_effect = [1, 0]
        vault = RollingVault(_make_node_router())
        vault.bind(_make_pool_with_conn(conn))

        long_text = "x" * 5000
        await vault.append(CAID, long_text, long_text)
        rows = conn.executemany.call_args.args[1]
        for row in rows:
            assert len(row[3]) <= 2000


class TestRollingVaultGetContextBlock:
    """get_context_block() formats history for injection."""

    @pytest.mark.asyncio
    async def test_empty_history_returns_empty_string(self):
        conn = _make_conn()
        conn.fetch.return_value = []
        vault = RollingVault(_make_node_router())
        vault.bind(_make_pool_with_conn(conn))
        block = await vault.get_context_block(CAID)
        assert block == ""

    @pytest.mark.asyncio
    async def test_formats_recent_events_section(self):
        conn = _make_conn()
        conn.fetch.return_value = [
            {"role": "player", "content": "I search the room.", "is_summary": False},
            {"role": "gm",     "content": "You find a hidden door.", "is_summary": False},
        ]
        vault = RollingVault(_make_node_router())
        vault.bind(_make_pool_with_conn(conn))
        block = await vault.get_context_block(CAID)
        assert "[RECENT EVENTS]" in block
        assert "Player: I search the room." in block
        assert "GM: You find a hidden door." in block

    @pytest.mark.asyncio
    async def test_formats_prior_summary_section(self):
        conn = _make_conn()
        conn.fetch.return_value = [
            {"role": "summary", "content": "The party boarded the ship.", "is_summary": True},
            {"role": "player",  "content": "I go below deck.", "is_summary": False},
        ]
        vault = RollingVault(_make_node_router())
        vault.bind(_make_pool_with_conn(conn))
        block = await vault.get_context_block(CAID)
        assert "[PRIOR SESSION SUMMARY]" in block
        assert "The party boarded the ship." in block
        assert "[RECENT EVENTS]" in block

    @pytest.mark.asyncio
    async def test_returns_empty_string_when_pool_unbound(self):
        vault = RollingVault(_make_node_router())
        block = await vault.get_context_block(CAID)
        assert block == ""


class TestRollingVaultClear:
    """clear() removes all entries for a campaign."""

    @pytest.mark.asyncio
    async def test_clear_executes_delete(self):
        conn = _make_conn()
        vault = RollingVault(_make_node_router())
        vault.bind(_make_pool_with_conn(conn))
        await vault.clear(CAID)
        conn.execute.assert_called_once()
        sql = conn.execute.call_args.args[0]
        assert "DELETE" in sql

    @pytest.mark.asyncio
    async def test_clear_without_pool_is_noop(self):
        vault = RollingVault(_make_node_router())
        await vault.clear(CAID)  # must not raise


class TestRollingVaultCompressor:
    """_call_compressor() uses NodeRouter to select a node."""

    @pytest.mark.asyncio
    async def test_no_nodes_returns_empty_string(self):
        vault = RollingVault(_make_node_router(nodes=[]))
        result = await vault._call_compressor("events text")
        assert result == ""

    @pytest.mark.asyncio
    async def test_compressor_calls_ollama_chat(self):
        node = {"url": "http://brain:11434", "model": "mistral:7b-instruct"}
        router = _make_node_router(nodes=[node])
        vault = RollingVault(router)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"message": {"content": "Summary here."}}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            mock_client_instance = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client_instance.post = AsyncMock(return_value=mock_response)

            result = await vault._call_compressor("Player attacked. GM responded.")

        assert result == "Summary here."

    @pytest.mark.asyncio
    async def test_compressor_returns_empty_on_exception(self):
        node = {"url": "http://brain:11434", "model": "mistral"}
        router = _make_node_router(nodes=[node])
        vault = RollingVault(router)

        with patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value.__aenter__ = AsyncMock(side_effect=RuntimeError("connection refused"))
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await vault._call_compressor("events")

        assert result == ""
