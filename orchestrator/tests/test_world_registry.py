"""
Unit tests for WorldRegistry — Dynamic Genre Orchestration.

Tests cover:
- scan() with empty dirs, fonts-only, templates-only, and merged discovery
- Metadata priority: identity.json overrides world.json (TDR §3)
- Malformed JSON is handled gracefully (fallback to minimal schema)
- list_worlds() returns sorted display names
- get_schema() cache hit and miss
- reload() forces a re-read from disk
- manifest() creates new world on disk + returns (schema, True)
- manifest() on existing world returns (schema, False)
- switch_campaign_world() delegates to RealityWall.set_current_world
- get_campaign_schema() retrieves the world schema for an active campaign
- _slugify() helper converts folder names correctly
- gm_tone_block property returns correct injection block
- WorldSchema.embed_color converts hex to int
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Load world_registry and world_schema directly from source files to avoid
# triggering the heavy services/__init__.py.
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent.parent.parent

def _load(dotted_name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(dotted_name, ROOT / rel_path)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules[dotted_name] = mod  # register so cross-module imports resolve
    spec.loader.exec_module(mod)
    return mod

_world_schema_mod   = _load("orchestrator.schemas.world_schema",  "orchestrator/schemas/world_schema.py")
_world_registry_mod = _load("orchestrator.services.world_registry", "orchestrator/services/world_registry.py")

WorldSchema   = _world_schema_mod.WorldSchema
WorldRegistry = _world_registry_mod.WorldRegistry
_slugify      = _world_registry_mod._slugify


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_registry(tmp_path: Path) -> tuple[WorldRegistry, MagicMock]:
    """Return (WorldRegistry, mock_reality_wall) wired to tmp_path."""
    rw = MagicMock()
    rw.register_world = AsyncMock()
    rw.set_current_world = AsyncMock()
    rw.get_current_world = AsyncMock(return_value=None)
    registry = WorldRegistry(data_dir=str(tmp_path), reality_wall=rw)
    return registry, rw


def _write_world_json(fonts_dir: Path, world_name: str, data: dict) -> None:
    world_dir = fonts_dir / world_name
    world_dir.mkdir(parents=True, exist_ok=True)
    (world_dir / "world.json").write_text(json.dumps(data), encoding="utf-8")


def _write_identity_json(templates_dir: Path, world_name: str, data: dict) -> None:
    world_dir = templates_dir / world_name
    world_dir.mkdir(parents=True, exist_ok=True)
    (world_dir / "identity.json").write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# scan()
# ---------------------------------------------------------------------------

class TestScan:
    def test_scan_empty_dirs_returns_empty_list(self, tmp_path):
        registry, _ = _make_registry(tmp_path)
        result = asyncio.run(registry.scan())
        assert result == []

    def test_scan_discovers_world_from_fonts(self, tmp_path):
        _write_world_json(tmp_path / "fonts", "mothership", {"display_name": "Mothership"})
        registry, _ = _make_registry(tmp_path)
        result = asyncio.run(registry.scan())
        assert "mothership" in result

    def test_scan_discovers_world_from_templates(self, tmp_path):
        _write_identity_json(tmp_path / "templates", "shadowrun", {"display_name": "Shadowrun"})
        registry, _ = _make_registry(tmp_path)
        result = asyncio.run(registry.scan())
        assert "shadowrun" in result

    def test_scan_merges_both_dirs(self, tmp_path):
        _write_world_json(tmp_path / "fonts", "mothership", {"display_name": "Mothership"})
        _write_identity_json(tmp_path / "templates", "vtm", {"display_name": "VtM"})
        registry, _ = _make_registry(tmp_path)
        result = asyncio.run(registry.scan())
        assert set(result) == {"mothership", "vtm"}

    def test_scan_deduplicates_world_in_both_tiers(self, tmp_path):
        _write_world_json(tmp_path / "fonts", "mothership", {"display_name": "Mothership"})
        _write_identity_json(tmp_path / "templates", "mothership", {"display_name": "Mothership"})
        registry, _ = _make_registry(tmp_path)
        result = asyncio.run(registry.scan())
        assert result.count("mothership") == 1

    def test_scan_populates_cache(self, tmp_path):
        _write_world_json(tmp_path / "fonts", "mothership", {"display_name": "Mothership"})
        registry, _ = _make_registry(tmp_path)
        asyncio.run(registry.scan())
        assert registry.get_schema("mothership") is not None

    def test_scan_returns_sorted_list(self, tmp_path):
        for name in ("zardoz", "mothership", "amber"):
            _write_world_json(tmp_path / "fonts", name, {"display_name": name.title()})
        registry, _ = _make_registry(tmp_path)
        result = asyncio.run(registry.scan())
        assert result == sorted(result)


# ---------------------------------------------------------------------------
# Metadata priority (identity.json overrides world.json)
# ---------------------------------------------------------------------------

class TestMetadataPriority:
    def test_loads_display_name_from_world_json(self, tmp_path):
        _write_world_json(tmp_path / "fonts", "mothership", {"display_name": "Mothership"})
        registry, _ = _make_registry(tmp_path)
        schema = registry._load_from_disk("mothership")
        assert schema.display_name == "Mothership"

    def test_loads_narrative_tone_from_identity_json(self, tmp_path):
        _write_identity_json(
            tmp_path / "templates", "mothership",
            {"display_name": "Mothership", "narrative_tone": "grimdark sci-fi horror"},
        )
        registry, _ = _make_registry(tmp_path)
        schema = registry._load_from_disk("mothership")
        assert schema.narrative_tone == "grimdark sci-fi horror"

    def test_identity_json_overrides_world_json_tone(self, tmp_path):
        _write_world_json(
            tmp_path / "fonts", "mothership",
            {"display_name": "Mothership", "narrative_tone": "generic sci-fi"},
        )
        _write_identity_json(
            tmp_path / "templates", "mothership",
            {"display_name": "Mothership Override", "narrative_tone": "grimdark sci-fi horror"},
        )
        registry, _ = _make_registry(tmp_path)
        schema = registry._load_from_disk("mothership")
        assert schema.narrative_tone == "grimdark sci-fi horror"
        assert schema.display_name == "Mothership Override"

    def test_identity_json_does_not_override_with_empty_values(self, tmp_path):
        """Empty string in identity.json must NOT overwrite a non-empty world.json value."""
        _write_world_json(
            tmp_path / "fonts", "mothership",
            {"display_name": "Mothership", "narrative_tone": "cosmic horror"},
        )
        _write_identity_json(
            tmp_path / "templates", "mothership",
            {"display_name": "Mothership", "narrative_tone": ""},
        )
        registry, _ = _make_registry(tmp_path)
        schema = registry._load_from_disk("mothership")
        assert schema.narrative_tone == "cosmic horror"

    def test_minimal_schema_returned_when_no_json(self, tmp_path):
        (tmp_path / "fonts" / "new_world").mkdir(parents=True, exist_ok=True)
        registry, _ = _make_registry(tmp_path)
        schema = registry._load_from_disk("new_world")
        assert isinstance(schema, WorldSchema)
        assert schema.system == "new_world"

    def test_malformed_world_json_falls_back_to_minimal(self, tmp_path):
        world_dir = tmp_path / "fonts" / "broken"
        world_dir.mkdir(parents=True, exist_ok=True)
        (world_dir / "world.json").write_text("{not valid json}", encoding="utf-8")
        registry, _ = _make_registry(tmp_path)
        schema = registry._load_from_disk("broken")
        assert isinstance(schema, WorldSchema)

    def test_malformed_identity_json_falls_back_gracefully(self, tmp_path):
        _write_world_json(
            tmp_path / "fonts", "mothership",
            {"display_name": "Mothership"},
        )
        id_dir = tmp_path / "templates" / "mothership"
        id_dir.mkdir(parents=True, exist_ok=True)
        (id_dir / "identity.json").write_text("{bad json", encoding="utf-8")
        registry, _ = _make_registry(tmp_path)
        schema = registry._load_from_disk("mothership")
        assert schema.display_name == "Mothership"

    def test_system_defaults_to_folder_name_when_blank(self, tmp_path):
        _write_world_json(
            tmp_path / "fonts", "pirate_borg",
            {"display_name": "Pirate Borg", "system": ""},
        )
        registry, _ = _make_registry(tmp_path)
        schema = registry._load_from_disk("pirate_borg")
        assert schema.system == "pirate_borg"

    def test_loads_full_world_json(self, tmp_path):
        data = {
            "display_name": "Pirate Borg",
            "primary_color": "#FFD700",
            "narrative_tone": "grimdark pirate chaos",
            "system": "pirate_borg",
            "dice_notation": "d6",
            "tags": ["pirate", "grimdark", "horror"],
        }
        _write_world_json(tmp_path / "fonts", "pirate_borg", data)
        registry, _ = _make_registry(tmp_path)
        schema = registry._load_from_disk("pirate_borg")
        assert schema.primary_color == "#FFD700"
        assert schema.dice_notation == "d6"
        assert "pirate" in schema.tags


# ---------------------------------------------------------------------------
# list_worlds / get_schema / reload
# ---------------------------------------------------------------------------

class TestCacheMethods:
    def test_list_worlds_empty_before_scan(self, tmp_path):
        registry, _ = _make_registry(tmp_path)
        assert registry.list_worlds() == []

    def test_list_worlds_after_scan_sorted_by_display_name(self, tmp_path):
        worlds = {
            "zzz_world": "Zzz World",
            "aaa_world": "Aaa World",
            "mmm_world": "Mmm World",
        }
        for folder, display in worlds.items():
            _write_world_json(tmp_path / "fonts", folder, {"display_name": display})
        registry, _ = _make_registry(tmp_path)
        asyncio.run(registry.scan())
        names = [s.display_name for s in registry.list_worlds()]
        assert names == sorted(names, key=str.lower)

    def test_get_schema_returns_cached_schema(self, tmp_path):
        _write_world_json(tmp_path / "fonts", "mothership", {"display_name": "Mothership"})
        registry, _ = _make_registry(tmp_path)
        asyncio.run(registry.scan())
        schema = registry.get_schema("mothership")
        assert schema is not None
        assert schema.display_name == "Mothership"

    def test_get_schema_returns_none_for_unknown_world(self, tmp_path):
        registry, _ = _make_registry(tmp_path)
        assert registry.get_schema("does_not_exist") is None

    def test_reload_refreshes_cache(self, tmp_path):
        _write_world_json(tmp_path / "fonts", "mothership", {"display_name": "Old Name"})
        registry, _ = _make_registry(tmp_path)
        asyncio.run(registry.scan())
        assert registry.get_schema("mothership").display_name == "Old Name"
        # Update the JSON on disk
        _write_world_json(tmp_path / "fonts", "mothership", {"display_name": "New Name"})
        schema = registry.reload("mothership")
        assert schema.display_name == "New Name"
        assert registry.get_schema("mothership").display_name == "New Name"


# ---------------------------------------------------------------------------
# manifest()
# ---------------------------------------------------------------------------

class TestManifest:
    def test_manifest_creates_new_world_dir_and_world_json(self, tmp_path):
        registry, _ = _make_registry(tmp_path)
        schema, manifested = asyncio.run(registry.manifest("pirate_borg"))
        assert manifested is True
        assert (tmp_path / "fonts" / "pirate_borg" / "world.json").exists()

    def test_manifest_returns_schema(self, tmp_path):
        registry, _ = _make_registry(tmp_path)
        schema, manifested = asyncio.run(registry.manifest("pirate_borg"))
        assert isinstance(schema, WorldSchema)

    def test_manifest_existing_world_returns_false(self, tmp_path):
        _write_world_json(tmp_path / "fonts", "mothership", {"display_name": "Mothership"})
        registry, _ = _make_registry(tmp_path)
        asyncio.run(registry.scan())
        _, manifested = asyncio.run(registry.manifest("mothership"))
        assert manifested is False

    def test_manifest_new_world_registers_with_reality_wall(self, tmp_path):
        registry, rw = _make_registry(tmp_path)
        asyncio.run(registry.manifest("new_world"))
        rw.register_world.assert_called()

    def test_manifest_twice_is_idempotent(self, tmp_path):
        registry, _ = _make_registry(tmp_path)
        _, first = asyncio.run(registry.manifest("pirate_borg"))
        _, second = asyncio.run(registry.manifest("pirate_borg"))
        assert first is True
        assert second is False

    def test_manifest_world_json_contains_minimal_fields(self, tmp_path):
        registry, _ = _make_registry(tmp_path)
        asyncio.run(registry.manifest("test_world"))
        raw = json.loads((tmp_path / "fonts" / "test_world" / "world.json").read_text())
        assert "display_name" in raw
        assert "system" in raw

    def test_manifest_adds_world_to_cache(self, tmp_path):
        registry, _ = _make_registry(tmp_path)
        asyncio.run(registry.manifest("new_game"))
        assert registry.get_schema("new_game") is not None


# ---------------------------------------------------------------------------
# switch_campaign_world / get_campaign_schema
# ---------------------------------------------------------------------------

class TestCampaignHelpers:
    def test_switch_campaign_world_calls_reality_wall(self, tmp_path):
        _write_world_json(tmp_path / "fonts", "mothership", {"display_name": "Mothership"})
        registry, rw = _make_registry(tmp_path)
        asyncio.run(registry.scan())
        asyncio.run(registry.switch_campaign_world("campaign-001", "mothership"))
        rw.set_current_world.assert_called_once_with("campaign-001", "mothership")

    def test_switch_campaign_world_manifests_if_needed(self, tmp_path):
        registry, _ = _make_registry(tmp_path)
        _, manifested = asyncio.run(registry.switch_campaign_world("campaign-002", "new_world"))
        assert manifested is True

    def test_get_campaign_schema_returns_none_when_no_world_set(self, tmp_path):
        registry, rw = _make_registry(tmp_path)
        rw.get_current_world = AsyncMock(return_value=None)
        result = asyncio.run(registry.get_campaign_schema("campaign-000"))
        assert result is None

    def test_get_campaign_schema_returns_schema(self, tmp_path):
        _write_world_json(tmp_path / "fonts", "mothership", {"display_name": "Mothership"})
        registry, rw = _make_registry(tmp_path)
        asyncio.run(registry.scan())
        rw.get_current_world = AsyncMock(return_value="mothership")
        schema = asyncio.run(registry.get_campaign_schema("campaign-001"))
        assert schema is not None
        assert schema.display_name == "Mothership"


# ---------------------------------------------------------------------------
# _slugify helper
# ---------------------------------------------------------------------------

class TestSlugify:
    @pytest.mark.parametrize("input_name,expected", [
        ("mothership",               "Mothership"),
        ("pirate_borg",              "Pirate Borg"),
        ("vampire_the_masquerade",   "Vampire The Masquerade"),
        # capitalize() lowercases the rest of each word, so "6e" → "6e" not "6E"
        ("shadowrun_6e",             "Shadowrun 6e"),
        ("single",                   "Single"),
        ("with-dashes",              "With Dashes"),
        ("UPPER_CASE",               "Upper Case"),
    ])
    def test_slugify(self, input_name, expected):
        assert _slugify(input_name) == expected


# ---------------------------------------------------------------------------
# WorldSchema properties
# ---------------------------------------------------------------------------

class TestWorldSchemaProperties:
    def test_embed_color_parses_hex(self):
        schema = WorldSchema(display_name="Test", primary_color="#FF4500")
        assert schema.embed_color == 0xFF4500

    def test_embed_color_default_is_white(self):
        # primary_color defaults to "#FFFFFF" when not supplied
        schema = WorldSchema(display_name="Test")
        assert schema.embed_color == 0xFFFFFF

    def test_gm_tone_block_both_fields(self):
        schema = WorldSchema(
            display_name="Test",
            narrative_tone="grimdark",
            description="A dark world.",
        )
        block = schema.gm_tone_block
        assert "NARRATIVE TONE: grimdark" in block
        assert "WORLD CONTEXT: A dark world." in block

    def test_gm_tone_block_tone_only(self):
        schema = WorldSchema(display_name="Test", narrative_tone="space opera")
        block = schema.gm_tone_block
        assert "NARRATIVE TONE: space opera" in block
        assert "WORLD CONTEXT" not in block

    def test_gm_tone_block_empty_when_no_tone_or_description(self):
        schema = WorldSchema(display_name="Test")
        assert schema.gm_tone_block == ""

    def test_driftnet_channel_id_defaults_empty(self):
        schema = WorldSchema(display_name="Test")
        assert schema.driftnet_channel_id == ""
