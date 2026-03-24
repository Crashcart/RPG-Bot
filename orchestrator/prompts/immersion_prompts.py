"""
Ironclad GM – Living Discord Immersion Prompts
===============================================
Prompts and rule-tables for the Task 4 "Living Discord" features.

Whisper System
--------------
A short, private synthesis run in parallel with the main narrative synthesis.
Only fires when NPC interactions are present in the scene plan.
Output goes to the player's DM — never to the main channel.

Ambient Audio
-------------
Rule-based mapping from environment type (inferred from action_type) to an
audio key that the Discord bot maps to a local audio file.

Combat Thread Detection
-----------------------
Rule-based inference from action_type keywords to determine whether this
turn is part of a combat encounter (generates thread_event + thread_content).

Channel Directive Detection
----------------------------
Rule-based scan of the mechanical reasoning field for capture/escape keywords
that trigger a channel_directive (move player to dungeon/prison/hospital).
"""

from __future__ import annotations

import re

# ─────────────────────────────────────────────────────────────────────────────
# Whisper System — Paranoia / Private Perception
# ─────────────────────────────────────────────────────────────────────────────

WHISPER_SYSTEM_PROMPT = """\
You are the GM's private channel to the player's inner awareness.
You feed the player secret perceptions that their character — deeply skeptical of people \
and attuned to deception — would notice, but that no one else at the table would catch.

RULES:
1. Output exactly 2-3 sentences. No more.
2. Second person, present tense ("You notice…", "Something about his tone…").
3. Never echo what the main narrative already said — this must be private, hidden intel.
4. Never use game-mechanical terms (roll, check, DC, modifier, skill).
5. Focus on micro-expressions, body language, vocal tells, environmental details, \
   inconsistencies — the kind of thing a paranoid, perceptive character would catch.
6. No preamble. No "Here is the whisper:". Begin immediately with the private observation.\
"""

WHISPER_PROMPT = """\
SCENE CONTEXT (public narrative summary):
{narrative_summary}

NPCs ACTIVE THIS SCENE:
{npc_list}

MECHANICAL OUTCOME (internal — do not echo):
{mechanical_outcome}

Generate the secret DM whisper — 2-3 sentences of what the character's paranoid, \
perceptive eye catches that no one else would notice.\
"""

# ─────────────────────────────────────────────────────────────────────────────
# Ambient Audio Key Mapping
# ─────────────────────────────────────────────────────────────────────────────
# Maps environment/scene type → audio key understood by the Discord bot.
# The bot maps these keys to actual audio file paths (configured via env vars).
# None means no ambient audio change is triggered.

AMBIENT_AUDIO_MAP: dict[str, str | None] = {
    "combat encounter":    "combat_tension",
    "social interaction":  "tavern_chatter",
    "exploration/stealth": "dungeon_ambience",
    "crafting/downtime":   "workshop_sounds",
    "general scene":       None,
}

# ─────────────────────────────────────────────────────────────────────────────
# Combat Thread Detection (action_type → thread event inference)
# ─────────────────────────────────────────────────────────────────────────────

# action_type substrings that indicate an active combat turn
_COMBAT_ACTION_KEYWORDS: frozenset[str] = frozenset({
    "attack", "melee", "ranged", "shoot", "stab", "slash", "strike",
    "dodge", "parry", "defend", "grapple", "charge", "ambush", "combat",
    "throw", "draw_weapon", "reload", "suppress", "flank",
})

# action_type substrings or reasoning keywords that indicate combat has ENDED
_COMBAT_END_KEYWORDS: frozenset[str] = frozenset({
    "flee", "escape", "surrender", "retreat", "yield", "combat_end",
    "disengage", "fall_unconscious", "stabilize", "death_save",
})

# reasoning field keywords that suggest the player was captured/imprisoned
_CAPTURE_KEYWORDS: tuple[str, ...] = (
    "captured", "arrested", "imprisoned", "thrown in", "locked up",
    "shackled", "put in chains", "dragged to", "taken prisoner",
    "thrown into", "confined", "incarcerated", "detained",
)

# reasoning field keywords that suggest the player escaped confinement
_ESCAPE_KEYWORDS: tuple[str, ...] = (
    "escaped", "freed", "breaks free", "breaks out", "rescued",
    "released", "liberated", "unlocked", "fled the cell",
)


def is_combat_action(action_type: str) -> bool:
    """Return True if the action_type signals an active combat move."""
    lower = action_type.lower()
    return any(kw in lower for kw in _COMBAT_ACTION_KEYWORDS)


def is_combat_end(action_type: str, reasoning: str, status_change) -> bool:
    """Return True if this turn signals the END of a combat encounter."""
    from orchestrator.schemas.payloads import CharacterStatus  # avoid circular at module level
    if status_change == CharacterStatus.DEAD:
        return True
    lower_action = action_type.lower()
    if any(kw in lower_action for kw in _COMBAT_END_KEYWORDS):
        return True
    lower_reason = (reasoning or "").lower()
    return any(kw in lower_reason for kw in _COMBAT_END_KEYWORDS)


def detect_channel_directive(
    action_type: str,
    reasoning:   str,
    outcome_value: str,
) -> tuple[str | None, str | None]:
    """
    Scan the mechanical reasoning for capture or escape keywords.

    Returns (action, channel_key) or (None, None) if no directive is warranted.
    action is "move_to" or "restore".
    channel_key is "dungeon", "prison", or "main".
    """
    lower = (reasoning or "").lower()

    # Escape / freedom (must check before capture to avoid false match)
    if any(kw in lower for kw in _ESCAPE_KEYWORDS):
        return "restore", "main"

    # Capture / imprisonment
    if any(kw in lower for kw in _CAPTURE_KEYWORDS):
        # Distinguish dungeon vs prison by keyword
        if any(k in lower for k in ("dungeon", "cell", "pit", "underground", "chained")):
            return "move_to", "dungeon"
        return "move_to", "prison"

    return None, None


def build_thread_content(resolution, character_name: str) -> str:
    """
    Build the mechanical details block for the combat thread.
    No LLM call — derived directly from the OllamaResolutionPayload.
    """
    outcome_emoji = {
        "critical_success": "🌟",
        "success":          "✅",
        "partial_success":  "⚡",
        "failure":          "❌",
        "critical_failure": "💀",
    }.get(resolution.outcome.value, "🎲")

    lines = [
        f"**{outcome_emoji} {resolution.action_type.replace('_', ' ').title()}**",
        f"Roll: `{resolution.dice_request.notation}` → **{resolution.roll_result}** "
        f"(DC {resolution.difficulty}) → {resolution.outcome.value.replace('_', ' ').upper()}",
    ]

    if resolution.state_delta.stat_deltas:
        lines.append("\n**Stat Changes:**")
        for sd in resolution.state_delta.stat_deltas:
            delta = sd.new_value - sd.old_value if isinstance(sd.new_value, (int, float)) and isinstance(sd.old_value, (int, float)) else "?"
            sign  = "+" if isinstance(delta, (int, float)) and delta > 0 else ""
            lines.append(f"  • `{sd.stat_key}`: {sd.old_value} → **{sd.new_value}** ({sign}{delta})")

    if resolution.state_delta.inventory_delta:
        lines.append("\n**Inventory Changes:**")
        for item in resolution.state_delta.inventory_delta[:6]:
            name = item.get("name", item.get("item_name", "unknown"))
            qty  = item.get("quantity", item.get("qty", "?"))
            lines.append(f"  • {name} (qty: {qty})")

    if resolution.state_delta.status_change:
        lines.append(f"\n⚠️ **Status:** {resolution.state_delta.status_change.value}")

    if resolution.rulebook_citations:
        lines.append("\n**Rulebook:**")
        for cite in resolution.rulebook_citations[:3]:
            lines.append(f"  • *{cite}*")

    if resolution.reasoning:
        lines.append(f"\n> {resolution.reasoning[:280]}")

    lines.append(f"\n*— {character_name}*")
    return "\n".join(lines)
