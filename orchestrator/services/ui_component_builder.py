"""
UIComponentBuilder — translates character state into Discord interactive components.

Issue #16 TDR: Native Interactive UI Translation Layer (Discord Component Mapping)
https://github.com/Crashcart/RPG-Bot/issues/16

Design rules (per TDR §3):
- Custom IDs are ≤100 chars in the format ``action:value|key1:val1|key2:val2``.
- Inventory menus are ephemeral (visible only to the acting player).
- Race-condition protection: the Discord bot is responsible for a Redis
  mutex on the player's inventory before executing any ``use_item`` action.
- This class is stateless; it never performs DB or Redis calls directly.
"""
from __future__ import annotations

import hashlib
from typing import Any

from orchestrator.schemas.payloads import (
    ActionRow,
    ButtonStyle,
    ComponentInteraction,
    DiscordButton,
    DiscordSelectMenu,
    SelectOption,
    UIComponentSet,
)

_MAX_OPTIONS_PER_MENU = 24   # Discord hard limit is 25; we keep 1 slot for safety
_CUSTOM_ID_MAX_LEN    = 100  # Discord hard limit


class UIComponentBuilder:
    """
    Builds Discord Action Rows from character/inventory/scene data.

    Usage (in GMDirector or narration.py after mechanical resolution)::

        builder = UIComponentBuilder()
        payload.ui_components = builder.build_from_action_context(
            action_type=resolution.action_type,
            character_stats=character.stats,
            inventory=inventory_snapshot,
        )
    """

    # ──────────────────────────────────────────────────────────────────────────
    # Custom ID encoding / decoding
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def encode_action_id(action: str, **params: str) -> str:
        """
        Encode an action and its parameters into a ≤100-char custom_id string.

        Format: ``action:value|key1:val1|key2:val2``

        When the full string would exceed 100 chars (Discord limit), the params
        are collapsed to a 16-char SHA-1 digest so the action key is preserved.
        """
        parts = [f"action:{action}"]
        for k, v in params.items():
            parts.append(f"{k}:{v}")
        raw = "|".join(parts)
        if len(raw) <= _CUSTOM_ID_MAX_LEN:
            return raw
        digest = hashlib.sha1(raw.encode()).hexdigest()[:16]
        return f"action:{action}|h:{digest}"

    @staticmethod
    def decode_action_id(custom_id: str) -> dict[str, str]:
        """
        Parse ``action:cast|spell_id:44|target:goblin_2``
        →  ``{'action': 'cast', 'spell_id': '44', 'target': 'goblin_2'}``.
        """
        result: dict[str, str] = {}
        for segment in custom_id.split("|"):
            if ":" not in segment:
                continue
            key, _, val = segment.partition(":")
            result[key.strip()] = val.strip()
        return result

    # ──────────────────────────────────────────────────────────────────────────
    # Combat components
    # ──────────────────────────────────────────────────────────────────────────

    def build_combat_components(
        self,
        character_stats: dict[str, Any],
        equipped_weapons: list[dict[str, Any]],
        available_spells: list[dict[str, Any]],
    ) -> UIComponentSet:
        """
        Build action rows for a combat turn.

        Row 0 — Attack buttons (one per equipped weapon, max 5).
        Row 1 — Defend / Flee / Use Item utility buttons.
        Row 2 — Spell select menu (up to 24 options; only added when spells exist).
        """
        rows: list[ActionRow] = []

        # Row 0: attack buttons (fallback to "Unarmed Strike" when nothing equipped)
        weapons = equipped_weapons if equipped_weapons else [
            {"name": "Unarmed Strike", "item_id": "unarmed"}
        ]
        attack_buttons = [
            DiscordButton(
                label=f"⚔ {w.get('name', 'Attack')[:20]}",
                custom_id=self.encode_action_id(
                    "attack", weapon_id=str(w.get("item_id", "unarmed"))
                ),
                style=ButtonStyle.DANGER,
            )
            for w in weapons[:5]
        ]
        rows.append(ActionRow(components=attack_buttons))

        # Row 1: utility buttons
        rows.append(ActionRow(components=[
            DiscordButton(
                label="🛡 Defend",
                custom_id=self.encode_action_id("defend"),
                style=ButtonStyle.PRIMARY,
            ),
            DiscordButton(
                label="💨 Flee",
                custom_id=self.encode_action_id("flee"),
                style=ButtonStyle.SECONDARY,
            ),
            DiscordButton(
                label="🎒 Use Item",
                custom_id=self.encode_action_id("inventory", page="0"),
                style=ButtonStyle.SECONDARY,
            ),
        ]))

        # Row 2: spell select menu (skipped when the character has no spells)
        if available_spells:
            options = [
                SelectOption(
                    label=s.get("name", "Spell")[:100],
                    value=self.encode_action_id(
                        "cast", spell_id=str(s.get("spell_id", "unknown"))
                    ),
                    description=str(s.get("description", ""))[:100],
                    emoji=s.get("emoji"),
                )
                for s in available_spells[:_MAX_OPTIONS_PER_MENU]
            ]
            rows.append(ActionRow(components=[
                DiscordSelectMenu(
                    custom_id=self.encode_action_id("spell_menu"),
                    placeholder="✨ Cast a spell…",
                    options=options,
                )
            ]))

        return UIComponentSet(action_rows=rows[:5])

    # ──────────────────────────────────────────────────────────────────────────
    # Exploration components
    # ──────────────────────────────────────────────────────────────────────────

    def build_exploration_components(
        self,
        interactive_objects: list[dict[str, Any]] | None = None,
    ) -> UIComponentSet:
        """
        Build action rows for an exploration turn.

        Row 0 — Context-sensitive interaction buttons for objects in the scene
                (only when ``interactive_objects`` is non-empty).
        Row 1 — Universal exploration buttons: Examine / Search / Wait / Inventory.
        """
        rows: list[ActionRow] = []

        # Row 0: per-object interaction buttons
        if interactive_objects:
            obj_buttons = [
                DiscordButton(
                    label=f"🖐 {obj.get('name', 'Object')[:20]}",
                    custom_id=self.encode_action_id(
                        "interact", object_id=str(obj.get("object_id", ""))
                    ),
                    style=ButtonStyle.PRIMARY,
                )
                for obj in interactive_objects[:5]
            ]
            rows.append(ActionRow(components=obj_buttons))

        # Row 1: universal exploration buttons
        rows.append(ActionRow(components=[
            DiscordButton(
                label="🔍 Examine",
                custom_id=self.encode_action_id("examine"),
                style=ButtonStyle.SECONDARY,
            ),
            DiscordButton(
                label="🔎 Search",
                custom_id=self.encode_action_id("search"),
                style=ButtonStyle.SECONDARY,
            ),
            DiscordButton(
                label="⏳ Wait",
                custom_id=self.encode_action_id("wait"),
                style=ButtonStyle.SECONDARY,
            ),
            DiscordButton(
                label="🎒 Inventory",
                custom_id=self.encode_action_id("inventory", page="0"),
                style=ButtonStyle.SECONDARY,
            ),
        ]))

        return UIComponentSet(action_rows=rows[:5])

    # ──────────────────────────────────────────────────────────────────────────
    # Inventory page (ephemeral, paginated)
    # ──────────────────────────────────────────────────────────────────────────

    def build_inventory_page(
        self,
        inventory: list[dict[str, Any]],
        page: int = 0,
    ) -> UIComponentSet:
        """
        Build an ephemeral inventory select menu with cursor pagination.

        Row 0 — Select menu of up to 24 items on the current page.
        Row 1 — Previous / Next navigation buttons (only when multiple pages exist).

        The result is always ``ephemeral=True``: the Discord bot must send it as
        an interaction response visible only to the acting player (prevents chat
        clutter and avoids exposing another player's inventory).
        """
        rows: list[ActionRow] = []
        total_pages = max(1, (len(inventory) + _MAX_OPTIONS_PER_MENU - 1)
                          // _MAX_OPTIONS_PER_MENU)
        page = max(0, min(page, total_pages - 1))
        start = page * _MAX_OPTIONS_PER_MENU
        page_items = inventory[start : start + _MAX_OPTIONS_PER_MENU]

        if page_items:
            options = [
                SelectOption(
                    label=f"{item.get('name', 'Item')[:80]} ×{item.get('quantity', 1)}",
                    value=self.encode_action_id(
                        "use_item", item_id=str(item.get("item_id", ""))
                    ),
                    description=str(item.get("description", ""))[:100],
                )
                for item in page_items
            ]
            rows.append(ActionRow(components=[
                DiscordSelectMenu(
                    custom_id=self.encode_action_id("item_select", page=str(page)),
                    placeholder=f"🎒 Inventory (page {page + 1}/{total_pages})",
                    options=options,
                )
            ]))

        # Navigation row: only added when there are multiple pages
        if total_pages > 1:
            nav_buttons: list[DiscordButton] = []  # type: ignore[type-arg]
            if page > 0:
                nav_buttons.append(DiscordButton(
                    label="◀ Prev",
                    custom_id=self.encode_action_id("inventory", page=str(page - 1)),
                    style=ButtonStyle.SECONDARY,
                ))
            if page < total_pages - 1:
                nav_buttons.append(DiscordButton(
                    label="Next ▶",
                    custom_id=self.encode_action_id("inventory", page=str(page + 1)),
                    style=ButtonStyle.SECONDARY,
                ))
            if nav_buttons:
                rows.append(ActionRow(components=nav_buttons))

        return UIComponentSet(action_rows=rows, ephemeral=True)

    # ──────────────────────────────────────────────────────────────────────────
    # Main entry point
    # ──────────────────────────────────────────────────────────────────────────

    #: Action types that produce combat component rows
    COMBAT_ACTION_TYPES: frozenset[str] = frozenset({
        "melee_attack", "ranged_attack", "spell_cast", "grapple",
        "combat_maneuver", "saving_throw",
    })

    #: Action types that produce exploration component rows
    EXPLORATION_ACTION_TYPES: frozenset[str] = frozenset({
        "examine", "search", "skill_check", "social", "stealth",
        "interact", "perception",
    })

    def build_from_action_context(
        self,
        action_type: str,
        character_stats: dict[str, Any],
        inventory: list[dict[str, Any]] | None = None,
        available_spells: list[dict[str, Any]] | None = None,
        interactive_objects: list[dict[str, Any]] | None = None,
    ) -> UIComponentSet | None:
        """
        Top-level factory: return the appropriate component set for the given
        ``action_type`` (from ``OllamaResolutionPayload.action_type``).

        Returns ``None`` for turns that produce no interactive choices —
        pure narration, downtime, OOC messages, or unrecognised action types.
        """
        equipped = [i for i in (inventory or []) if i.get("equipped")]
        spells   = available_spells or []

        if action_type in self.COMBAT_ACTION_TYPES:
            return self.build_combat_components(
                character_stats=character_stats,
                equipped_weapons=equipped,
                available_spells=spells,
            )
        if action_type in self.EXPLORATION_ACTION_TYPES:
            return self.build_exploration_components(
                interactive_objects=interactive_objects,
            )
        return None

    # ──────────────────────────────────────────────────────────────────────────
    # Interaction callback decoder
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def decode_interaction(raw: dict[str, Any]) -> ComponentInteraction:
        """
        Decode a Discord component interaction POST body into a
        ``ComponentInteraction``.

        ``raw`` is the full JSON dict that Discord sends to the interaction
        endpoint when a player clicks a button or selects a menu option.
        """
        data   = raw.get("data", {})
        cid    = data.get("custom_id", "")

        # Select menus nest the chosen value one level deeper
        values = data.get("values", [])
        if values:
            cid = values[0]

        params = UIComponentBuilder.decode_action_id(cid)
        action = params.pop("action", "unknown")

        # Member vs DM interaction: player_id lives in different places
        member  = raw.get("member") or {}
        user    = member.get("user") or raw.get("user") or {}
        player_id = str(user.get("id", ""))

        return ComponentInteraction(
            interaction_id=str(raw.get("id", "")),
            player_id=player_id,
            guild_id=str(raw.get("guild_id", "")),
            channel_id=str(raw.get("channel_id", "")),
            message_id=str((raw.get("message") or {}).get("id", "")),
            custom_id=cid,
            action=action,
            params=params,
        )
