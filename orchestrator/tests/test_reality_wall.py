"""
Unit tests for RealityWall — SQLite-backed world-state registry.

Tests cover:
- Directory and schema initialisation
- World registration and listing
- Campaign ↔ world binding (set/get, auto-register)
- Driftnet channel binding
- Paradox level CRUD (default, clamp, roundtrip)
- Path isolation (resolve_handout_path / resolve_vault_path, traversal guard)
- SQLite WAL mode is active after init
"""

import asyncio
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load reality_wall directly from its source file to avoid triggering the
# heavy services/__init__.py (which requires asyncpg, httpx, chromadb, etc.)
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent.parent.parent
_MODULE_PATH = ROOT / "orchestrator" / "services" / "reality_wall.py"
_spec = importlib.util.spec_from_file_location("orchestrator.services.reality_wall", _MODULE_PATH)
_mod  = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
RealityWall = _mod.RealityWall


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_wall(tmp_path: Path) -> RealityWall:
    """Return an initialised RealityWall pointing at a temp directory."""
    return RealityWall(data_dir=str(tmp_path), vault_dir=str(tmp_path / "vault"))


async def _init(rw: RealityWall) -> RealityWall:
    await rw.init()
    return rw


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

class TestInit:
    def test_creates_directory_tree(self, tmp_path):
        rw = _make_wall(tmp_path)
        asyncio.run(rw.init())
        for asset_type in ("fonts", "templates", "handouts", "echo_vault", "vault"):
            assert (tmp_path / asset_type).is_dir(), f"{asset_type}/ not created"

    def test_creates_sqlite_database(self, tmp_path):
        rw = _make_wall(tmp_path)
        asyncio.run(rw.init())
        assert (tmp_path / "vault" / "scribe_core.db").exists()

    def test_wal_mode_active(self, tmp_path):
        rw = _make_wall(tmp_path)
        asyncio.run(rw.init())
        db_path = tmp_path / "vault" / "scribe_core.db"
        with sqlite3.connect(db_path) as conn:
            row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal"

    def test_init_idempotent(self, tmp_path):
        """Calling init() twice must not raise."""
        rw = _make_wall(tmp_path)
        asyncio.run(rw.init())
        asyncio.run(rw.init())
        assert (tmp_path / "vault" / "scribe_core.db").exists()


# ---------------------------------------------------------------------------
# World Registration
# ---------------------------------------------------------------------------

class TestWorldRegistration:
    def setup_method(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        self.tmp = Path(self._tmpdir)
        self.rw = _make_wall(self.tmp)
        asyncio.run(self.rw.init())

    def test_register_creates_world_entry(self):
        asyncio.run(self.rw.register_world("mothership"))
        worlds = asyncio.run(self.rw.list_worlds())
        assert "mothership" in worlds

    def test_register_creates_handouts_dir(self):
        asyncio.run(self.rw.register_world("shadowrun"))
        assert (self.tmp / "handouts" / "shadowrun").is_dir()

    def test_register_creates_echo_vault_dir(self):
        asyncio.run(self.rw.register_world("shadowrun"))
        assert (self.tmp / "echo_vault" / "shadowrun").is_dir()

    def test_register_multiple_worlds(self):
        asyncio.run(self.rw.register_world("mothership"))
        asyncio.run(self.rw.register_world("shadowrun"))
        asyncio.run(self.rw.register_world("pirate_borg"))
        worlds = asyncio.run(self.rw.list_worlds())
        assert set(worlds) == {"mothership", "shadowrun", "pirate_borg"}

    def test_register_idempotent(self):
        """Re-registering the same world must not raise or duplicate."""
        asyncio.run(self.rw.register_world("mothership"))
        asyncio.run(self.rw.register_world("mothership"))
        worlds = asyncio.run(self.rw.list_worlds())
        assert worlds.count("mothership") == 1

    def test_register_with_metadata(self):
        asyncio.run(self.rw.register_world("vtm", {"edition": "v5"}))
        worlds = asyncio.run(self.rw.list_worlds())
        assert "vtm" in worlds

    def test_list_worlds_empty_initially(self):
        worlds = asyncio.run(self.rw.list_worlds())
        assert worlds == []


# ---------------------------------------------------------------------------
# Campaign ↔ World Binding
# ---------------------------------------------------------------------------

class TestCampaignWorldBinding:
    def setup_method(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        self.tmp = Path(self._tmpdir)
        self.rw = _make_wall(self.tmp)
        asyncio.run(self.rw.init())

    def test_set_and_get_current_world(self):
        asyncio.run(self.rw.register_world("mothership"))
        asyncio.run(self.rw.set_current_world("campaign-001", "mothership"))
        result = asyncio.run(self.rw.get_current_world("campaign-001"))
        assert result == "mothership"

    def test_get_current_world_unknown_campaign_returns_none(self):
        result = asyncio.run(self.rw.get_current_world("nonexistent-campaign"))
        assert result is None

    def test_set_current_world_auto_registers_world(self):
        """set_current_world should auto-register a world that doesn't exist yet."""
        asyncio.run(self.rw.set_current_world("campaign-002", "new_world"))
        worlds = asyncio.run(self.rw.list_worlds())
        assert "new_world" in worlds

    def test_campaign_world_can_be_changed(self):
        asyncio.run(self.rw.register_world("mothership"))
        asyncio.run(self.rw.register_world("shadowrun"))
        asyncio.run(self.rw.set_current_world("campaign-003", "mothership"))
        asyncio.run(self.rw.set_current_world("campaign-003", "shadowrun"))
        result = asyncio.run(self.rw.get_current_world("campaign-003"))
        assert result == "shadowrun"

    def test_multiple_campaigns_independent(self):
        asyncio.run(self.rw.register_world("mothership"))
        asyncio.run(self.rw.register_world("vtm"))
        asyncio.run(self.rw.set_current_world("campaign-a", "mothership"))
        asyncio.run(self.rw.set_current_world("campaign-b", "vtm"))
        assert asyncio.run(self.rw.get_current_world("campaign-a")) == "mothership"
        assert asyncio.run(self.rw.get_current_world("campaign-b")) == "vtm"


# ---------------------------------------------------------------------------
# Driftnet Channel Binding
# ---------------------------------------------------------------------------

class TestDriftnetChannel:
    def setup_method(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        self.tmp = Path(self._tmpdir)
        self.rw = _make_wall(self.tmp)
        asyncio.run(self.rw.init())

    def test_set_and_get_driftnet_channel(self):
        asyncio.run(self.rw.register_world("mothership"))
        asyncio.run(self.rw.set_driftnet_channel("mothership", "123456789012345678"))
        result = asyncio.run(self.rw.get_driftnet_channel("mothership"))
        assert result == "123456789012345678"

    def test_get_driftnet_channel_unset_returns_none(self):
        asyncio.run(self.rw.register_world("shadowrun"))
        result = asyncio.run(self.rw.get_driftnet_channel("shadowrun"))
        assert result is None

    def test_get_driftnet_channel_unknown_world_returns_none(self):
        result = asyncio.run(self.rw.get_driftnet_channel("does_not_exist"))
        assert result is None

    def test_driftnet_channel_can_be_updated(self):
        asyncio.run(self.rw.register_world("vtm"))
        asyncio.run(self.rw.set_driftnet_channel("vtm", "111"))
        asyncio.run(self.rw.set_driftnet_channel("vtm", "999"))
        result = asyncio.run(self.rw.get_driftnet_channel("vtm"))
        assert result == "999"

    def test_driftnet_auto_creates_world_on_set(self):
        """set_driftnet_channel upserts world_state so it must succeed even for a new world."""
        asyncio.run(self.rw.set_driftnet_channel("pirate_borg", "42"))
        result = asyncio.run(self.rw.get_driftnet_channel("pirate_borg"))
        assert result == "42"


# ---------------------------------------------------------------------------
# Paradox Level
# ---------------------------------------------------------------------------

class TestParadoxLevel:
    def setup_method(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        self.tmp = Path(self._tmpdir)
        self.rw = _make_wall(self.tmp)
        asyncio.run(self.rw.init())

    def test_default_paradox_level_is_one(self):
        level = asyncio.run(self.rw.get_paradox_level("campaign-xyz"))
        assert level == 1

    def test_set_and_get_paradox_level(self):
        asyncio.run(self.rw.set_paradox_level("campaign-001", 7))
        level = asyncio.run(self.rw.get_paradox_level("campaign-001"))
        assert level == 7

    def test_paradox_level_clamps_below_one(self):
        asyncio.run(self.rw.set_paradox_level("campaign-002", 0))
        assert asyncio.run(self.rw.get_paradox_level("campaign-002")) == 1

    def test_paradox_level_clamps_above_ten(self):
        asyncio.run(self.rw.set_paradox_level("campaign-003", 11))
        assert asyncio.run(self.rw.get_paradox_level("campaign-003")) == 10

    def test_paradox_level_accepts_boundary_one(self):
        asyncio.run(self.rw.set_paradox_level("campaign-004", 1))
        assert asyncio.run(self.rw.get_paradox_level("campaign-004")) == 1

    def test_paradox_level_accepts_boundary_ten(self):
        asyncio.run(self.rw.set_paradox_level("campaign-005", 10))
        assert asyncio.run(self.rw.get_paradox_level("campaign-005")) == 10

    def test_paradox_level_can_be_updated(self):
        asyncio.run(self.rw.set_paradox_level("campaign-006", 3))
        asyncio.run(self.rw.set_paradox_level("campaign-006", 8))
        assert asyncio.run(self.rw.get_paradox_level("campaign-006")) == 8

    def test_paradox_levels_are_campaign_isolated(self):
        asyncio.run(self.rw.set_paradox_level("campaign-a", 2))
        asyncio.run(self.rw.set_paradox_level("campaign-b", 9))
        assert asyncio.run(self.rw.get_paradox_level("campaign-a")) == 2
        assert asyncio.run(self.rw.get_paradox_level("campaign-b")) == 9


# ---------------------------------------------------------------------------
# Path Isolation (Traversal Guard)
# ---------------------------------------------------------------------------

class TestPathIsolation:
    def setup_method(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        self.tmp = Path(self._tmpdir)
        self.rw = _make_wall(self.tmp)
        asyncio.run(self.rw.init())

    def test_resolve_handout_path_returns_correct_path(self):
        asyncio.run(self.rw.register_world("mothership"))
        path = self.rw.resolve_handout_path("mothership", "map.png")
        expected_root = (self.tmp / "handouts" / "mothership").resolve()
        assert str(path).startswith(str(expected_root))
        assert path.name == "map.png"

    def test_resolve_vault_path_returns_correct_path(self):
        asyncio.run(self.rw.register_world("mothership"))
        path = self.rw.resolve_vault_path("mothership", "ambient.mp3")
        expected_root = (self.tmp / "echo_vault" / "mothership").resolve()
        assert str(path).startswith(str(expected_root))
        assert path.name == "ambient.mp3"

    def test_resolve_handout_path_rejects_traversal(self):
        asyncio.run(self.rw.register_world("mothership"))
        with pytest.raises(ValueError, match="traversal"):
            self.rw.resolve_handout_path("mothership", "../../secret.txt")

    def test_resolve_vault_path_rejects_traversal(self):
        asyncio.run(self.rw.register_world("mothership"))
        with pytest.raises(ValueError, match="traversal"):
            self.rw.resolve_vault_path("mothership", "../../../etc/passwd")

    def test_resolve_handout_path_rejects_absolute_escape(self):
        asyncio.run(self.rw.register_world("mothership"))
        with pytest.raises(ValueError, match="traversal"):
            self.rw.resolve_handout_path("mothership", "/etc/passwd")

    def test_resolve_handout_path_allows_nested_subdir(self):
        """Paths that stay inside the world dir are valid."""
        asyncio.run(self.rw.register_world("mothership"))
        path = self.rw.resolve_handout_path("mothership", "maps/level1.png")
        expected_root = (self.tmp / "handouts" / "mothership").resolve()
        assert str(path).startswith(str(expected_root))
