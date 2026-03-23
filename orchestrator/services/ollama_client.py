"""Client for the local Ollama mechanical adjudication engine."""

from __future__ import annotations

import json
import logging
import random
import re

import httpx

from orchestrator.config import Settings
from orchestrator.prompts.guardrails import MECHANICAL_SYSTEM_PROMPT
from orchestrator.schemas.payloads import (
    ActionOutcome,
    CharacterSnapshot,
    ContextAssemblyPayload,
    DiceRequest,
    OllamaResolutionPayload,
    OperationalStatus,
    StateDelta,
    StatDelta,
    SubsystemDelta,
    VehicleDelta,
)

logger = logging.getLogger(__name__)


def _roll_dice(notation: str, modifier: int = 0) -> int:
    """
    True-RNG dice roller.  Supports standard notation: NdM[+/-X].
    Examples: '1d20', '2d6+3', '1d8-1'
    """
    notation = notation.strip().lower()
    match = re.match(r"^(\d+)d(\d+)([+-]\d+)?$", notation)
    if not match:
        raise ValueError(f"Invalid dice notation: {notation!r}")
    count = int(match.group(1))
    sides = int(match.group(2))
    inline_mod = int(match.group(3) or 0)
    total = sum(random.randint(1, sides) for _ in range(count))
    return total + inline_mod + modifier


class OllamaClient:
    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.ollama_host
        self._model    = settings.ollama_model
        self._timeout  = settings.ollama_timeout_seconds

    @classmethod
    def from_node(cls, node: dict, settings: Settings) -> "OllamaClient":
        """Construct a client pointed at a specific node_registry entry."""
        obj = cls.__new__(cls)
        obj._base_url = node["host"].rstrip("/")
        obj._model    = node["model"] or settings.ollama_model
        obj._timeout  = settings.ollama_timeout_seconds
        return obj

    async def resolve_action(
        self,
        context: ContextAssemblyPayload,
    ) -> OllamaResolutionPayload:
        """
        Send the assembled context to Ollama for mechanical adjudication.
        Injects a true-RNG dice result before returning to the caller.
        """
        user_prompt = self._build_user_prompt(context)

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/api/chat",
                json={
                    "model": self._model,
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": MECHANICAL_SYSTEM_PROMPT},
                        {"role": "user",   "content": user_prompt},
                    ],
                    "format": "json",
                },
            )
            response.raise_for_status()

        raw = response.json()
        content = raw["message"]["content"]

        try:
            payload_dict = json.loads(content)
        except json.JSONDecodeError as exc:
            logger.error("Ollama returned non-JSON content: %s", content[:500])
            raise ValueError(f"Ollama output is not valid JSON: {exc}") from exc

        return self._build_resolution(context.intent_id, payload_dict)

    # Fields stripped from item_data before it is shown to Ollama.
    # Flavor text in these fields can confuse the mechanical engine.
    _ITEM_FLAVOR_KEYS = frozenset({
        "description", "lore", "flavor", "flavor_text",
        "history", "quote", "notes", "appearance",
    })

    def _sanitise_inventory(self, inventory: list[dict]) -> list[dict]:
        """Return inventory with narrative/flavor fields removed.

        Only name, quantity, weight, and mechanical_properties are sent to
        Ollama — everything else is flavor the adjudication engine must not
        read.  The full JSONB payload is preserved unchanged in PostgreSQL.
        """
        clean = []
        for item in inventory:
            mechanical = {
                k: v for k, v in item.items()
                if k not in self._ITEM_FLAVOR_KEYS
            }
            # Keep mechanical_properties sub-dict intact but strip flavor within it
            if isinstance(mechanical.get("mechanical_properties"), dict):
                mechanical["mechanical_properties"] = {
                    k: v for k, v in mechanical["mechanical_properties"].items()
                    if k not in self._ITEM_FLAVOR_KEYS
                }
            clean.append(mechanical)
        return clean

    def _build_user_prompt(self, ctx: ContextAssemblyPayload) -> str:
        rule_text = "\n".join(
            f"[{c.source}] {c.content}" for c in ctx.rule_chunks
        ) or "No specific rule chunks retrieved."

        char = ctx.character
        inventory_text = json.dumps(self._sanitise_inventory(ctx.inventory_snapshot), indent=2)

        vehicle_block = ""
        if ctx.vehicle_context:
            vehicle_lines = []
            for v in ctx.vehicle_context:
                line = (
                    f"  Vehicle: {v['name']} (type={v['asset_type']}, "
                    f"hull={v['hull_integrity']}/{v['max_hull_integrity']})"
                )
                vehicle_lines.append(line)
                for sub in v.get("subsystems", []):
                    assigned = sub.get("assigned_character_id") or "uncrewed"
                    vehicle_lines.append(
                        f"    Subsystem [{sub['subsystem_type']}] {sub['subsystem_name']}: "
                        f"status={sub['operational_status']}  crew={assigned}  "
                        f"stats={json.dumps(sub.get('subsystem_data', {}))}"
                    )
            vehicle_block = "\nVEHICLE / ASSET CONTEXT:\n" + "\n".join(vehicle_lines) + "\n"

        return (
            f"ACTIVE SYSTEM: {char.system}\n\n"
            f"CHARACTER STATE:\n{json.dumps(char.stats, indent=2)}\n\n"
            f"INVENTORY (mechanical fields only):\n{inventory_text}\n"
            f"{vehicle_block}\n"
            f"RULEBOOK CONTEXT:\n{rule_text}\n\n"
            f"PLAYER ACTION: {ctx.raw_input}\n\n"
            "Resolve the action. Output only the JSON payload."
        )

    def _build_resolution(
        self, intent_id: str, d: dict
    ) -> OllamaResolutionPayload:
        """Parse the LLM JSON output and inject the true dice roll."""
        dice_req = DiceRequest(
            notation=d.get("dice_request", {}).get("notation", "1d20"),
            modifier=d.get("dice_request", {}).get("modifier", 0),
            purpose=d.get("dice_request", {}).get("purpose", ""),
        )

        # True-RNG injection – the backend owns the dice
        roll_result = _roll_dice(dice_req.notation, dice_req.modifier)

        raw_delta = d.get("state_delta", {})
        stat_deltas = [
            StatDelta(
                stat_key=sd["stat_key"],
                old_value=sd["old_value"],
                new_value=sd["new_value"],
            )
            for sd in raw_delta.get("stat_deltas", [])
        ]

        # Vehicle deltas — parse subsystem status changes and hull damage
        vehicle_deltas: list[VehicleDelta] = []
        for vd in raw_delta.get("vehicle_deltas", []):
            subsystem_deltas = [
                SubsystemDelta(
                    subsystem_name=sd["subsystem_name"],
                    new_status=OperationalStatus(sd["new_status"]) if sd.get("new_status") else None,
                    assigned_character_id=sd.get("assigned_character_id", "__no_change__"),
                )
                for sd in vd.get("subsystems", [])
            ]
            vehicle_deltas.append(VehicleDelta(
                vehicle_id=vd.get("vehicle_id", ""),
                hull_delta=int(vd.get("hull_delta", 0)),
                subsystems=subsystem_deltas,
            ))

        delta = StateDelta(
            character_id=raw_delta.get("character_id", ""),
            stat_deltas=stat_deltas,
            status_change=raw_delta.get("status_change"),
            inventory_delta=raw_delta.get("inventory_delta", []),
            vehicle_deltas=vehicle_deltas,
        )

        return OllamaResolutionPayload(
            intent_id=intent_id,
            action_type=d.get("action_type", "unknown"),
            difficulty=int(d.get("difficulty", 10)),
            dice_request=dice_req,
            roll_result=roll_result,
            outcome=ActionOutcome(d.get("outcome", ActionOutcome.FAILURE)),
            state_delta=delta,
            rulebook_citations=d.get("rulebook_citations", []),
            reasoning=d.get("reasoning", ""),
        )
