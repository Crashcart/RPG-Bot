"""
Tests for UIComponentBuilder — Issue #16.

Run with: pytest orchestrator/tests/test_ui_component_builder.py -v
"""
from __future__ import annotations

import pytest

from orchestrator.schemas.payloads import (
    ButtonStyle,
    DiscordButton,
    DiscordSelectMenu,
    UIComponentSet,
)
from orchestrator.services.ui_component_builder import (
    _CUSTOM_ID_MAX_LEN,
    _MAX_OPTIONS_PER_MENU,
    UIComponentBuilder,
)


@pytest.fixture
def builder() -> UIComponentBuilder:
    return UIComponentBuilder()


@pytest.fixture
def sample_inventory() -> list[dict]:
    return [
        {"item_id": f"item_{i}", "name": f"Potion {i}", "quantity": 1,
         "description": f"A healing potion #{i}", "equipped": False}
        for i in range(30)
    ]


@pytest.fixture
def sample_weapons() -> list[dict]:
    return [
        {"item_id": "sword_1", "name": "Longsword", "equipped": True},
        {"item_id": "dagger_2", "name": "Dagger", "equipped": True},
    ]


@pytest.fixture
def sample_spells() -> list[dict]:
    return [
        {"spell_id": "fireball", "name": "Fireball", "description": "Deals fire damage"},
        {"spell_id": "heal", "name": "Heal", "description": "Restores HP"},
    ]


# ─────────────────────────────────────────────────────────────────────────────
# TestEncodeDecodeRoundTrip
# ─────────────────────────────────────────────────────────────────────────────

class TestEncodeDecodeRoundTrip:
    def test_simple_action_roundtrip(self, builder: UIComponentBuilder) -> None:
        cid = builder.encode_action_id("attack", weapon_id="sword_1")
        decoded = builder.decode_action_id(cid)
        assert decoded["action"] == "attack"
        assert decoded["weapon_id"] == "sword_1"

    def test_multiple_params_roundtrip(self, builder: UIComponentBuilder) -> None:
        cid = builder.encode_action_id("cast", spell_id="fireball", target="goblin_2")
        decoded = builder.decode_action_id(cid)
        assert decoded["action"] == "cast"
        assert decoded["spell_id"] == "fireball"
        assert decoded["target"] == "goblin_2"

    def test_custom_id_format(self, builder: UIComponentBuilder) -> None:
        cid = builder.encode_action_id("cast", spell_id="44", target="goblin_2")
        assert cid == "action:cast|spell_id:44|target:goblin_2"

    def test_encoded_id_within_discord_limit(self, builder: UIComponentBuilder) -> None:
        cid = builder.encode_action_id("attack", weapon_id="a" * 50, target="b" * 50)
        assert len(cid) <= _CUSTOM_ID_MAX_LEN

    def test_long_params_fall_back_to_hash(self, builder: UIComponentBuilder) -> None:
        long_val = "x" * 200
        cid = builder.encode_action_id("attack", weapon_id=long_val)
        assert len(cid) <= _CUSTOM_ID_MAX_LEN
        decoded = builder.decode_action_id(cid)
        assert decoded["action"] == "attack"
        assert "h" in decoded  # hash digest present

    def test_no_params(self, builder: UIComponentBuilder) -> None:
        cid = builder.encode_action_id("defend")
        decoded = builder.decode_action_id(cid)
        assert decoded["action"] == "defend"

    def test_decode_malformed_segment_skipped(self, builder: UIComponentBuilder) -> None:
        decoded = builder.decode_action_id("action:flee|INVALID|key:val")
        assert decoded["action"] == "flee"
        assert decoded["key"] == "val"
        assert "INVALID" not in decoded


# ─────────────────────────────────────────────────────────────────────────────
# TestCombatComponents
# ─────────────────────────────────────────────────────────────────────────────

class TestCombatComponents:
    def test_returns_ui_component_set(
        self, builder: UIComponentBuilder, sample_weapons: list[dict]
    ) -> None:
        result = builder.build_combat_components(
            character_stats={}, equipped_weapons=sample_weapons, available_spells=[]
        )
        assert isinstance(result, UIComponentSet)

    def test_two_rows_without_spells(
        self, builder: UIComponentBuilder, sample_weapons: list[dict]
    ) -> None:
        result = builder.build_combat_components(
            character_stats={}, equipped_weapons=sample_weapons, available_spells=[]
        )
        assert len(result.action_rows) == 2

    def test_three_rows_with_spells(
        self,
        builder: UIComponentBuilder,
        sample_weapons: list[dict],
        sample_spells: list[dict],
    ) -> None:
        result = builder.build_combat_components(
            character_stats={}, equipped_weapons=sample_weapons,
            available_spells=sample_spells
        )
        assert len(result.action_rows) == 3

    def test_attack_buttons_use_danger_style(
        self, builder: UIComponentBuilder, sample_weapons: list[dict]
    ) -> None:
        result = builder.build_combat_components(
            character_stats={}, equipped_weapons=sample_weapons, available_spells=[]
        )
        attack_row = result.action_rows[0]
        for btn in attack_row.components:
            assert isinstance(btn, DiscordButton)
            assert btn.style == ButtonStyle.DANGER

    def test_unarmed_fallback_when_no_weapons(
        self, builder: UIComponentBuilder
    ) -> None:
        result = builder.build_combat_components(
            character_stats={}, equipped_weapons=[], available_spells=[]
        )
        attack_row = result.action_rows[0]
        assert len(attack_row.components) == 1
        btn = attack_row.components[0]
        assert isinstance(btn, DiscordButton)
        assert "Unarmed" in btn.label

    def test_max_five_attack_buttons(
        self, builder: UIComponentBuilder
    ) -> None:
        many_weapons = [{"item_id": f"w{i}", "name": f"Weapon {i}", "equipped": True}
                        for i in range(10)]
        result = builder.build_combat_components(
            character_stats={}, equipped_weapons=many_weapons, available_spells=[]
        )
        assert len(result.action_rows[0].components) <= 5

    def test_spell_row_is_select_menu(
        self,
        builder: UIComponentBuilder,
        sample_weapons: list[dict],
        sample_spells: list[dict],
    ) -> None:
        result = builder.build_combat_components(
            character_stats={}, equipped_weapons=sample_weapons,
            available_spells=sample_spells
        )
        spell_row = result.action_rows[2]
        assert len(spell_row.components) == 1
        assert isinstance(spell_row.components[0], DiscordSelectMenu)

    def test_total_rows_never_exceed_five(
        self, builder: UIComponentBuilder
    ) -> None:
        spells = [{"spell_id": f"s{i}", "name": f"Spell {i}"} for i in range(30)]
        weapons = [{"item_id": f"w{i}", "name": f"Sword {i}", "equipped": True}
                   for i in range(10)]
        result = builder.build_combat_components(
            character_stats={}, equipped_weapons=weapons, available_spells=spells
        )
        assert len(result.action_rows) <= 5


# ─────────────────────────────────────────────────────────────────────────────
# TestExplorationComponents
# ─────────────────────────────────────────────────────────────────────────────

class TestExplorationComponents:
    def test_returns_ui_component_set(self, builder: UIComponentBuilder) -> None:
        result = builder.build_exploration_components()
        assert isinstance(result, UIComponentSet)

    def test_one_row_without_objects(self, builder: UIComponentBuilder) -> None:
        result = builder.build_exploration_components(interactive_objects=None)
        assert len(result.action_rows) == 1

    def test_two_rows_with_objects(self, builder: UIComponentBuilder) -> None:
        objects = [{"object_id": "door_1", "name": "Iron Door"}]
        result = builder.build_exploration_components(interactive_objects=objects)
        assert len(result.action_rows) == 2

    def test_universal_row_has_four_buttons(self, builder: UIComponentBuilder) -> None:
        result = builder.build_exploration_components()
        universal_row = result.action_rows[-1]
        assert len(universal_row.components) == 4

    def test_not_ephemeral(self, builder: UIComponentBuilder) -> None:
        result = builder.build_exploration_components()
        assert result.ephemeral is False

    def test_max_five_object_buttons(self, builder: UIComponentBuilder) -> None:
        objects = [{"object_id": f"obj_{i}", "name": f"Object {i}"} for i in range(10)]
        result = builder.build_exploration_components(interactive_objects=objects)
        assert len(result.action_rows[0].components) <= 5


# ─────────────────────────────────────────────────────────────────────────────
# TestInventoryPage
# ─────────────────────────────────────────────────────────────────────────────

class TestInventoryPage:
    def test_always_ephemeral(
        self, builder: UIComponentBuilder, sample_inventory: list[dict]
    ) -> None:
        result = builder.build_inventory_page(sample_inventory)
        assert result.ephemeral is True

    def test_single_page_no_nav_row(self, builder: UIComponentBuilder) -> None:
        small_inv = [{"item_id": f"i{i}", "name": f"Item {i}", "quantity": 1}
                     for i in range(5)]
        result = builder.build_inventory_page(small_inv)
        assert len(result.action_rows) == 1  # only select menu, no nav

    def test_multi_page_has_nav_row(
        self, builder: UIComponentBuilder, sample_inventory: list[dict]
    ) -> None:
        result = builder.build_inventory_page(sample_inventory, page=0)
        assert len(result.action_rows) == 2  # select menu + nav

    def test_page_clipped_to_last_valid_page(
        self, builder: UIComponentBuilder, sample_inventory: list[dict]
    ) -> None:
        result = builder.build_inventory_page(sample_inventory, page=999)
        assert len(result.action_rows) >= 1

    def test_page_clipped_to_zero(
        self, builder: UIComponentBuilder, sample_inventory: list[dict]
    ) -> None:
        result = builder.build_inventory_page(sample_inventory, page=-5)
        assert len(result.action_rows) >= 1

    def test_options_per_page_within_limit(
        self, builder: UIComponentBuilder, sample_inventory: list[dict]
    ) -> None:
        result = builder.build_inventory_page(sample_inventory, page=0)
        menu = result.action_rows[0].components[0]
        assert isinstance(menu, DiscordSelectMenu)
        assert len(menu.options) <= _MAX_OPTIONS_PER_MENU

    def test_prev_button_absent_on_first_page(
        self, builder: UIComponentBuilder, sample_inventory: list[dict]
    ) -> None:
        result = builder.build_inventory_page(sample_inventory, page=0)
        nav_buttons = result.action_rows[1].components
        labels = [btn.label for btn in nav_buttons if isinstance(btn, DiscordButton)]
        assert not any("Prev" in lbl for lbl in labels)

    def test_next_button_absent_on_last_page(
        self, builder: UIComponentBuilder, sample_inventory: list[dict]
    ) -> None:
        result = builder.build_inventory_page(sample_inventory, page=1)
        nav_buttons = result.action_rows[1].components
        labels = [btn.label for btn in nav_buttons if isinstance(btn, DiscordButton)]
        assert not any("Next" in lbl for lbl in labels)

    def test_empty_inventory_returns_empty_rows(
        self, builder: UIComponentBuilder
    ) -> None:
        result = builder.build_inventory_page([], page=0)
        assert result.action_rows == []


# ─────────────────────────────────────────────────────────────────────────────
# TestBuildFromActionContext
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildFromActionContext:
    def test_melee_attack_returns_combat(
        self, builder: UIComponentBuilder, sample_weapons: list[dict]
    ) -> None:
        result = builder.build_from_action_context(
            action_type="melee_attack",
            character_stats={},
            inventory=sample_weapons,
        )
        assert result is not None
        assert not result.ephemeral

    def test_spell_cast_returns_combat(
        self,
        builder: UIComponentBuilder,
        sample_weapons: list[dict],
        sample_spells: list[dict],
    ) -> None:
        result = builder.build_from_action_context(
            action_type="spell_cast",
            character_stats={},
            inventory=sample_weapons,
            available_spells=sample_spells,
        )
        assert result is not None

    def test_examine_returns_exploration(self, builder: UIComponentBuilder) -> None:
        result = builder.build_from_action_context(
            action_type="examine", character_stats={}
        )
        assert result is not None
        assert not result.ephemeral

    def test_skill_check_returns_exploration(self, builder: UIComponentBuilder) -> None:
        result = builder.build_from_action_context(
            action_type="skill_check", character_stats={}
        )
        assert result is not None

    def test_unknown_action_returns_none(self, builder: UIComponentBuilder) -> None:
        result = builder.build_from_action_context(
            action_type="ooc_message", character_stats={}
        )
        assert result is None

    def test_only_equipped_items_used_as_weapons(
        self, builder: UIComponentBuilder
    ) -> None:
        inventory = [
            {"item_id": "sword", "name": "Sword", "equipped": True},
            {"item_id": "potion", "name": "Potion", "equipped": False},
        ]
        result = builder.build_from_action_context(
            action_type="melee_attack", character_stats={}, inventory=inventory
        )
        assert result is not None
        attack_labels = [
            btn.label
            for btn in result.action_rows[0].components
            if isinstance(btn, DiscordButton)
        ]
        assert any("Sword" in lbl for lbl in attack_labels)
        assert not any("Potion" in lbl for lbl in attack_labels)


# ─────────────────────────────────────────────────────────────────────────────
# TestDecodeInteraction
# ─────────────────────────────────────────────────────────────────────────────

class TestDecodeInteraction:
    def _make_button_raw(self, custom_id: str, player_id: str = "123") -> dict:
        return {
            "id": "inter_001",
            "guild_id": "guild_999",
            "channel_id": "chan_888",
            "member": {"user": {"id": player_id}},
            "message": {"id": "msg_777"},
            "data": {"custom_id": custom_id, "component_type": 2},
        }

    def _make_select_raw(self, value: str, player_id: str = "123") -> dict:
        return {
            "id": "inter_002",
            "guild_id": "guild_999",
            "channel_id": "chan_888",
            "member": {"user": {"id": player_id}},
            "message": {"id": "msg_777"},
            "data": {
                "custom_id": "item_select",
                "component_type": 3,
                "values": [value],
            },
        }

    def test_button_interaction_decoded(self) -> None:
        raw = self._make_button_raw("action:attack|weapon_id:sword_1")
        result = UIComponentBuilder.decode_interaction(raw)
        assert result.action == "attack"
        assert result.params["weapon_id"] == "sword_1"
        assert result.player_id == "123"

    def test_select_menu_value_overrides_custom_id(self) -> None:
        raw = self._make_select_raw("action:cast|spell_id:fireball")
        result = UIComponentBuilder.decode_interaction(raw)
        assert result.action == "cast"
        assert result.params["spell_id"] == "fireball"

    def test_interaction_fields_populated(self) -> None:
        raw = self._make_button_raw("action:defend", player_id="456")
        result = UIComponentBuilder.decode_interaction(raw)
        assert result.interaction_id == "inter_001"
        assert result.guild_id == "guild_999"
        assert result.channel_id == "chan_888"
        assert result.message_id == "msg_777"
        assert result.player_id == "456"

    def test_dm_interaction_no_member(self) -> None:
        raw = {
            "id": "inter_dm",
            "guild_id": "",
            "channel_id": "dm_chan",
            "user": {"id": "789"},
            "message": {"id": "dm_msg"},
            "data": {"custom_id": "action:flee"},
        }
        result = UIComponentBuilder.decode_interaction(raw)
        assert result.player_id == "789"
        assert result.action == "flee"

    def test_empty_body_returns_unknown_action(self) -> None:
        result = UIComponentBuilder.decode_interaction({})
        assert result.action == "unknown"
