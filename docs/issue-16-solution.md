# Issue #16 — Native Interactive UI Translation Layer (Discord Component Mapping)

## Summary

Implements stateless `UIComponentBuilder` that translates character state and
mechanical resolution output into Discord interactive components (buttons and
select menus). Players can execute RPG actions with a single click rather than
typing raw text commands.

## Architecture

```
OllamaResolutionPayload
        │
        ▼
UIComponentBuilder.build_from_action_context()
        │
        ├─► build_combat_components()     → UIComponentSet (attack/defend/spell rows)
        ├─► build_exploration_components() → UIComponentSet (examine/search/interact rows)
        └─► build_inventory_page()        → UIComponentSet (ephemeral paginated menu)
        │
        ▼
NarrativeResponsePayload.ui_components: UIComponentSet | None
        │
        ▼
Discord bot → attaches Action Rows to narrative embed message
        │
        ▼  (player clicks)
Discord interaction callback → UIComponentBuilder.decode_interaction()
        │
        ▼
ComponentInteraction → re-enter pipeline as new IntentPayload
```

## Files Changed

| File | Change |
|------|--------|
| `orchestrator/schemas/payloads.py` | Added `ButtonStyle`, `DiscordButton`, `SelectOption`, `DiscordSelectMenu`, `ActionRow`, `UIComponentSet`, `ComponentInteraction`; added `ui_components` field to `NarrativeResponsePayload` |
| `orchestrator/services/ui_component_builder.py` | New — `UIComponentBuilder` service |
| `orchestrator/services/__init__.py` | Added `UIComponentBuilder` export |
| `orchestrator/tests/test_ui_component_builder.py` | New — 34 pytest tests |

## Custom ID Encoding

Discord limits `custom_id` to 100 characters. All component IDs use the
compact pipe-separated format from TDR §3:

```
action:cast|spell_id:44|target:goblin_2
```

When the encoded string would exceed 100 chars, `encode_action_id()` falls back
to a SHA-1 digest of the params while preserving the action key:

```
action:attack|h:a1b2c3d4e5f60708
```

Decoded via `decode_action_id()` → `{'action': 'attack', 'h': 'a1b2c3...'}`

## Wiring into narration.py

```python
from orchestrator.services.ui_component_builder import UIComponentBuilder

_ui_builder = UIComponentBuilder()

# Inside narration.py after mechanical resolution and state commit:
payload.ui_components = _ui_builder.build_from_action_context(
    action_type=resolution.action_type,
    character_stats=character.stats,
    inventory=inventory_snapshot,
    available_spells=character.stats.get("known_spells", []),
    interactive_objects=scene_objects,   # from scene context, optional
)
```

## Wiring into Discord bot

### Attaching components to messages

```python
from orchestrator.schemas.payloads import NarrativeResponsePayload

async def deliver_narrative(payload: NarrativeResponsePayload, interaction):
    components = []
    if payload.ui_components:
        for row in payload.ui_components.action_rows:
            components.append(build_discord_action_row(row))  # map to discord.py View
    await interaction.followup.send(payload.narrative, components=components)
```

### Handling component interactions

```python
from orchestrator.services.ui_component_builder import UIComponentBuilder
from orchestrator.schemas.payloads import IntentPayload, CommandType

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return
    comp_interaction = UIComponentBuilder.decode_interaction(interaction.data_raw)
    # Convert to pipeline intent
    intent = IntentPayload(
        player_id=comp_interaction.player_id,
        guild_id=comp_interaction.guild_id,
        channel_id=comp_interaction.channel_id,
        session_token=comp_interaction.session_token,
        raw_input=_format_component_action(comp_interaction),
        command_type=CommandType.SLASH_COMMAND,
    )
    await dispatch_to_pipeline(intent)
```

### Inventory (ephemeral)

When `ui_components.ephemeral is True`, the Discord bot sends the components
via `interaction.response.send_message(..., ephemeral=True)` so only the
acting player sees the inventory. This prevents chat clutter and avoids
exposing another player's inventory state.

### Race-condition protection

Before executing a `use_item` action from a component callback, the Discord
bot must acquire a Redis lock on the player's inventory:

```python
lock_key = f"ironclad:lock:inventory:{player_id}"
async with redis.lock(lock_key, timeout=5):
    await dispatch_use_item(item_id, player_id)
```

This prevents duplicate item consumption if a player clicks the same button
multiple times before the first response arrives.

## TDR Compliance

| Requirement | Implementation |
|-------------|----------------|
| Contextual State Fetch (§2.B.1) | `build_from_action_context()` receives inventory + spell list |
| Component Generation (§2.B.2) | Attack buttons, utility row, spell select menu |
| Custom ID Serialization (§3) | `action:cast\|spell_id:44\|target:goblin_2` ≤ 100 chars |
| Callback Interception (§2.B.4) | `decode_interaction()` maps raw Discord body → `ComponentInteraction` |
| Ephemeral Fog of War (Option 1) | `UIComponentSet.ephemeral = True` for inventory pages |
| Paginated Inventories (Option 3) | `build_inventory_page()` with cursor prev/next navigation |
| Security mutex (§3) | Redis lock pattern documented in wiring guide above |
