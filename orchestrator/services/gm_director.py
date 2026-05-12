"""
GM Director — Tier 1 Central Storyteller
==========================================
The autonomous Game Master that coordinates sub-agent delegation and
synthesizes the final immersive narrative delivered to the player.

Two-Tier Architecture
---------------------
Tier 1 (this service):
    The GM Director acts as the sole Game Master.  It silently controls the
    plot, decides what needs to be delegated, and synthesizes all content
    into a single coherent passage.  The player never sees the internal
    planning or sub-agent outputs — only the finished narrative.

Tier 2 (SubAgentDispatcher):
    Local Ollama nodes tagged actor or scribe receive highly specific task
    briefs from the GM and return raw, uncensored content.  They have no
    awareness of the broader scene; they are focused generators.

Execution Flow (per player turn)
---------------------------------
  Step 4a — Planning Pass  (hidden)
      The GM analyses the scene (player action + mechanical outcome + NPC
      list) and produces a JSON plan identifying which elements should be
      delegated to sub-agents and which the GM will narrate directly.
      A failed or invalid planning response gracefully falls back to a
      zero-delegation plan (GM synthesizes everything directly).

  Step 4b — Sub-Agent Dispatch  (concurrent, hidden)
      All sub-tasks fire in parallel.  Each sub-agent receives an uncensored
      system prompt and a tightly scoped task brief.  The Originality Lock
      (brand filter) runs on every sub-agent output.

  Step 4c — Synthesis Pass  (produces player-visible output)
      The GM receives the assembled sub-agent content alongside the full
      mechanical context and story memory, and writes the final prose.

      Character Sheet Gate: the stat/inventory change block is injected into
      the synthesis prompt ONLY when Phase 3 committed actual changes this
      turn.  The GM never dumps a stat block unprompted.

  Step 4d — Structural Text Filter  (post-process)
      The final output is scanned for accidental structural formatting
      (chapter headings, numbered lists, dividers).  Any detected patterns
      are stripped and a warning is logged.

Storyteller Selection
----------------------
  Cloud Storyteller ON  → GeminiClient.generate() runs Steps 4a and 4c.
  Cloud Storyteller OFF → The auto-promoted local Ollama node (fastest TTFT)
                          runs Steps 4a and 4c.
                          Sub-agents (Step 4b) are ALWAYS local Ollama nodes.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchestrator.services.claude_client         import ClaudeClient
    from orchestrator.services.elevenlabs_client     import ElevenLabsClient
    from orchestrator.services.faction_service       import FactionService
    from orchestrator.services.gemini_client         import GeminiClient
    from orchestrator.services.handout_service       import HandoutService
    from orchestrator.services.image_gen             import ImageGenService
    from orchestrator.services.node_router           import NodeRouter
    from orchestrator.services.paradox_engine        import ParadoxEngine
    from orchestrator.services.reality_wall          import RealityWall
    from orchestrator.services.story_memory          import StoryMemoryService
    from orchestrator.services.sub_agent_dispatcher  import SubAgentDispatcher
    from orchestrator.services.telemetry             import TelemetryService
    from orchestrator.services.whisper_service       import WhisperService
    from orchestrator.services.world_registry        import WorldRegistry

import asyncio

from orchestrator.prompts.gm_prompts import (
    GM_DIRECTIVE_BLOCK,
    GM_DIRECTOR_ORCHESTRATOR_CONTEXT,
    GM_PLANNING_PROMPT,
    GM_PLANNING_SYSTEM_PROMPT,
    GM_STAT_CHANGE_BLOCK,
    GM_SYNTHESIS_PROMPT,
    GM_SYSTEM_PROMPT,
    MUSIC_SCENE_PROMPTS,
    STRUCTURAL_PATTERNS,
    SUBAGENT_SCENE_DESCRIBER_PROMPT,
    SUBAGENT_SOUND_DIRECTOR_PROMPT,
)
from orchestrator.prompts.immersion_prompts import (
    AMBIENT_AUDIO_MAP,
    WHISPER_PROMPT,
    WHISPER_SYSTEM_PROMPT,
    build_thread_content,
    detect_channel_directive,
    is_combat_action,
    is_combat_end,
)
from orchestrator.schemas.payloads import (
    ActionOutcome,
    ChannelDirective,
    CharacterSnapshot,
    GMDirective,
    GMPlanResult,
    MusicCue,
    SFXCue,
    ThreadEvent,
    TTSCue,
    NarrativeResponsePayload,
    OllamaResolutionPayload,
    StateCommitPayload,
    SubAgentTask,
)

logger = logging.getLogger(__name__)

_PLANNING_MAX_TOKENS  = 1024
_SYNTHESIS_MAX_TOKENS = 900
_WHISPER_MAX_TOKENS   = 120   # 2-3 tight sentences
_MAX_STORY_FACTS      = 5


class GMDirector:
    """
    Central Game Master Director.

    Wired as a long-lived service in main.py.  Selects its storyteller
    (Gemini or best auto-promoted local Ollama) on every call via
    _select_storyteller(), so the Cloud Storyteller toggle takes effect
    immediately without a restart.
    """

    def __init__(
        self,
        gemini:         "GeminiClient",
        node_router:    "NodeRouter",
        dispatcher:     "SubAgentDispatcher",
        story_memory:   "StoryMemoryService",
        telemetry:      "TelemetryService | None" = None,
        claude:         "ClaudeClient | None" = None,
        cloud_provider: str = "gemini",
        reality_wall:   "RealityWall | None" = None,
        paradox_engine: "ParadoxEngine | None" = None,
        world_registry: "WorldRegistry | None" = None,
        # ── Multimedia services (optional) ────────────────────────────────
        image_gen:      "ImageGenService | None" = None,
        elevenlabs:     "ElevenLabsClient | None" = None,
        handout_svc:    "HandoutService | None" = None,
        faction_svc:    "FactionService | None" = None,
        whisper_svc:    "WhisperService | None" = None,
        db=None,
    ) -> None:
        self._gemini         = gemini
        self._claude         = claude
        self._cloud_provider = cloud_provider
        self._node_router    = node_router
        self._dispatcher     = dispatcher
        self._story_memory   = story_memory
        self._telemetry      = telemetry
        self._reality_wall   = reality_wall
        self._paradox_engine = paradox_engine
        self._world_registry = world_registry
        self._image_gen      = image_gen
        self._elevenlabs     = elevenlabs
        self._handout_svc    = handout_svc
        self._faction_svc    = faction_svc
        self._whisper_svc    = whisper_svc
        self._db             = db

    # ── Public Interface ────────────────────────────────────────────────────────

    async def narrate(
        self,
        resolution:       OllamaResolutionPayload,
        commit:           StateCommitPayload,
        character:        CharacterSnapshot,
        player_intent:    str,
        campaign_system:  str,
        campaign_id:      str,
        active_directives: list["GMDirective"] | None = None,
    ) -> NarrativeResponsePayload:
        """
        Full GM Director pipeline: plan → delegate → synthesize → filter.

        Returns a NarrativeResponsePayload ready for Discord delivery.
        """
        # ── Select Tier 1 Storyteller ──────────────────────────────────────────
        storyteller = await self._select_storyteller()
        storyteller_name = getattr(storyteller, "_node_name", "gemini-cloud")

        if self._telemetry:
            await self._telemetry.emit(
                "storyteller_selected",
                storyteller=storyteller_name,
                campaign_id=campaign_id,
            )

        # ── Story Memory Retrieval ─────────────────────────────────────────────
        story_context = await self._story_memory.retrieve_relevant_context(
            query=player_intent,
            campaign_id=campaign_id,
        )
        story_lines = [
            f"[{f.entity_type.value.upper()}] {f.entity_name}: {f.summary}"
            for f in story_context
        ] if story_context else []

        # ── Step 4a: Planning Pass ─────────────────────────────────────────────
        plan = await self._planning_pass(
            storyteller=storyteller,
            resolution=resolution,
            player_intent=player_intent,
        )
        logger.info(
            "GM Director [storyteller=%s]: %d sub-tasks planned.",
            storyteller_name, len(plan.sub_tasks),
        )

        if self._telemetry:
            await self._telemetry.emit(
                "planning_done",
                sub_tasks=len(plan.sub_tasks),
                direct_elements=len(plan.direct_elements),
                storyteller=storyteller_name,
            )

        # ── Inject multimedia sub-tasks into the plan ───────────────────────
        scene_brief = f"{resolution.action_type} — {player_intent[:120]}"
        tone = "gritty"

        # sound_director: always add unless it's a purely social turn
        if resolution.action_type not in ("social_talk", "ooc"):
            plan.sub_tasks.append(SubAgentTask(
                task_type="sound_director",
                entity_name="SFX",
                entity_role="sound effect curator",
                scene_context=scene_brief,
                player_action_context=player_intent[:200],
                tone=tone,
                max_words=80,
            ))

        # scene_describer: add on major scene transitions (new location or combat start)
        if plan.trigger_scene_image or is_combat_action(resolution.action_type):
            plan.sub_tasks.append(SubAgentTask(
                task_type="scene_describer",
                entity_name="Scene",
                entity_role="image generation prompt composer",
                scene_context=scene_brief,
                player_action_context=player_intent[:200],
                tone=tone,
                max_words=150,
            ))

        # ── Step 4b: Sub-Agent Dispatch ────────────────────────────────────────
        sub_results = await self._dispatcher.dispatch_all(plan.sub_tasks)

        if self._telemetry and plan.sub_tasks:
            actor_names = ", ".join(t.entity_name for t in plan.sub_tasks[:5])
            await self._telemetry.emit(
                "sub_agent_dispatch",
                count=len(plan.sub_tasks),
                actors=actor_names,
            )

        # Log any brand violations for operator review
        for r in sub_results:
            if r.brand_violation:
                logger.warning(
                    "Brand violation stripped in sub-agent result: task=%s node=%s",
                    r.task.task_id, r.node_name,
                )

        assembled_elements = _format_assembled_elements(sub_results)

        # ── Step 4c: Synthesis Pass + Whisper (concurrent) ──────────────────
        mech_context = _format_mechanical_context(resolution)
        stat_block   = _build_stat_change_block(resolution)
        story_block  = (
            "\n".join(f"  • {line}" for line in story_lines)
            if story_lines
            else "  (No prior facts established — this is the opening scene.)"
        )
        direct_block = (
            "\n".join(f"  • {e}" for e in plan.direct_elements)
            if plan.direct_elements
            else "  (None specified — all scene content came from sub-agents.)"
        )

        # Build directive block for World Architect injection
        directive_block = _build_directive_block(active_directives)

        synthesis_prompt = GM_SYNTHESIS_PROMPT.format(
            directive_block=directive_block,
            mechanical_context=mech_context,
            story_context=story_block,
            player_action=player_intent,
            assembled_elements=assembled_elements or "  (No sub-agent content was generated.)",
            direct_elements=direct_block,
            stat_change_block=stat_block,
        )

        # Fire synthesis and whisper concurrently — whisper adds zero latency
        if self._telemetry:
            await self._telemetry.emit("synthesis_start", storyteller=storyteller_name)

        # ── Inject dynamic world tone + capture driftnet channel ──────────────
        # Prepend orchestrator self-awareness context so the GM Director knows
        # it commands Lyria, ElevenLabs, ImageGen, and sub-agent AIs.
        synthesis_system  = GM_DIRECTOR_ORCHESTRATOR_CONTEXT + GM_SYSTEM_PROMPT
        driftnet_channel_id: str = ""
        if self._world_registry:
            try:
                world_schema = await self._world_registry.get_campaign_schema(campaign_id)
                if world_schema:
                    if world_schema.gm_tone_block:
                        synthesis_system = world_schema.gm_tone_block + "\n\n" + GM_SYSTEM_PROMPT
                        if self._telemetry:
                            await self._telemetry.emit(
                                "world_tone_injected",
                                world=world_schema.display_name,
                                campaign_id=campaign_id,
                            )
                    driftnet_channel_id = world_schema.driftnet_channel_id or ""
            except Exception as _wt_exc:
                logger.debug("World tone injection failed (non-fatal): %s", _wt_exc)

        # ── Whisper trigger evaluation ─────────────────────────────────────────
        # NPC dialogue tasks always warrant a whisper (original behaviour).
        # WhisperService extends the trigger to horror actions and low sanity
        # independently of whether any NPC tasks are present.
        has_npc_tasks  = any(r.task.task_type == "npc_dialogue" for r in sub_results)
        should_whisper = has_npc_tasks
        hidden_context = ""
        if self._whisper_svc:
            triggered, hidden_state = await self._whisper_svc.should_trigger_whisper(
                character_id=character.character_id,
                action_type=resolution.action_type,
                reasoning=resolution.reasoning,
                outcome=resolution.outcome.value,
            )
            if triggered:
                should_whisper = True
                hidden_context = self._whisper_svc.build_hidden_context(hidden_state)

        synthesis_coro = storyteller.generate(
            system_prompt=synthesis_system,
            user_prompt=synthesis_prompt,
            max_tokens=_SYNTHESIS_MAX_TOKENS,
        )
        whisper_coro = (
            self._generate_whisper(
                storyteller, resolution, plan, sub_results, player_intent, hidden_context
            )
            if should_whisper else asyncio.sleep(0, result=None)
        )

        raw_narrative, whisper_text = await asyncio.gather(synthesis_coro, whisper_coro)

        # ── Step 4d: Structural Text Filter ───────────────────────────────────
        final_narrative, stripped_count = _strip_structural_text(raw_narrative)

        if self._telemetry:
            await self._telemetry.emit(
                "synthesis_done",
                length=len(final_narrative),
                stripped=stripped_count,
                storyteller=storyteller_name,
            )

        if stripped_count:
            logger.warning(
                "GM synthesis output contained %d structural pattern(s); stripped. "
                "storyteller=%s",
                stripped_count, storyteller_name,
            )

        # ── Step 4e: Paradox Engine (unreliable narrator injection) ────────────
        if self._paradox_engine and self._reality_wall:
            try:
                paradox_level = await self._reality_wall.get_paradox_level(campaign_id)
                if paradox_level > 1:
                    final_narrative = self._paradox_engine.apply(final_narrative, paradox_level)
                    if self._telemetry:
                        await self._telemetry.emit(
                            "paradox_applied",
                            level=paradox_level,
                            campaign_id=campaign_id,
                        )
            except Exception as px_exc:
                logger.debug("Paradox Engine failed (non-fatal): %s", px_exc)

        # ── Persist New World Facts (best-effort) ─────────────────────────────
        try:
            await self._story_memory.extract_and_store(
                narrative=final_narrative,
                campaign_id=campaign_id,
                intent_id=resolution.intent_id,
            )
        except Exception as exc:
            logger.warning("GM Director: fact extraction failed (best-effort): %s", exc)

        # ── Task 4: Living Discord Immersion fields ───────────────────────────
        tts_cues      = _build_tts_cues(sub_results)
        thread_ev, thread_title, thread_body = _build_thread_event(resolution, character.name)
        ambient_key   = _infer_ambient_audio_key(resolution)
        ch_action, ch_key = detect_channel_directive(
            resolution.action_type,
            resolution.reasoning,
            resolution.outcome.value,
        )
        channel_directive = (
            ChannelDirective(action=ch_action, channel_key=ch_key,
                             reason=f"Narrative event: {resolution.action_type}")
            if ch_action else None
        )

        # ── Multimedia: SFX, Music, Scene Image ──────────────────────────────
        sfx_cues: list[SFXCue] = []
        scene_image_prompt: str | None = None

        # Extract sound_director and scene_describer results
        for r in sub_results:
            if r.task.task_type == "sound_director":
                sfx_cues = _parse_sfx_cues(r.raw_output)
            elif r.task.task_type == "scene_describer":
                scene_image_prompt = r.raw_output.strip()[:500] or None

        # Music cue: build from ambient key + scene type
        music_cue: MusicCue | None = None
        scene_type = _resolve_scene_type(resolution.action_type, ambient_key)
        if scene_type:
            music_prompt = MUSIC_SCENE_PROMPTS.get(
                scene_type,
                f"atmospheric {scene_type} music, fantasy RPG setting"
            )
            music_cue = MusicCue(
                scene_type=scene_type,
                music_prompt=music_prompt,
                lavalink_query=f"dark fantasy {scene_type} ambient music no copyright",
            )
            # Fire-and-forget: generate Lyria audio in background
            asyncio.create_task(
                _populate_music_cue_url(music_cue, self._gemini, self._db)
            )

        # Fire-and-forget faction adjustment
        if self._faction_svc:
            asyncio.create_task(
                self._faction_svc.ai_adjust_from_narrative(
                    campaign_id=campaign_id,
                    player_id=character.character_id,
                    narrative_excerpt=final_narrative[:800],
                    action_type=resolution.action_type,
                )
            )

        # ── Build response ─────────────────────────────────────────────────────
        outcome_label = resolution.outcome.value.replace("_", " ").title()
        embed_title   = f"{character.name}: {outcome_label}"
        first_sentence = final_narrative.split(".")[0].strip()
        if len(first_sentence) > 12:
            embed_title = first_sentence[:60] + ("…" if len(first_sentence) > 60 else "")

        logger.info(
            "GM Director complete: storyteller=%s sub_agents=%d narrative=%d chars "
            "whisper=%s tts_cues=%d thread=%s lethal=%s sfx=%d music=%s",
            storyteller_name,
            len(plan.sub_tasks),
            len(final_narrative),
            "yes" if whisper_text else "no",
            len(tts_cues),
            thread_ev.value if thread_ev else "none",
            commit.lethal,
            len(sfx_cues),
            scene_type or "none",
        )

        return NarrativeResponsePayload(
            prompt_id=resolution.intent_id,
            intent_id=resolution.intent_id,
            narrative=final_narrative,
            embed_title=embed_title,
            whisper=whisper_text or None,
            thread_event=thread_ev,
            thread_title=thread_title,
            thread_content=thread_body,
            ambient_audio_key=ambient_key,
            tts_cues=tts_cues,
            channel_directive=channel_directive,
            driftnet_channel_id=driftnet_channel_id,
            sfx_cues=sfx_cues,
            music_cue=music_cue,
            scene_image_prompt=scene_image_prompt,
            npc_portrait_name=plan.trigger_npc_portrait,
        )

    # ── Private: Whisper Generation ─────────────────────────────────────────────

    async def _generate_whisper(
        self,
        storyteller,
        resolution:     OllamaResolutionPayload,
        plan:           GMPlanResult,
        sub_results,
        player_intent:  str,
        hidden_context: str = "",
    ) -> str | None:
        """
        Generate the secret private-perception DM whisper in parallel with synthesis.

        Fires when NPC dialogue sub-tasks are present OR when WhisperService
        determines a horror/sanity trigger is active.  A failed whisper silently
        returns None — the main narrative is unaffected.
        """
        npc_names = ", ".join(
            r.task.entity_name for r in sub_results
            if r.task.task_type == "npc_dialogue"
        )
        outcome_str = f"{resolution.outcome.value}: {resolution.reasoning[:80] or ''}"

        whisper_prompt = WHISPER_PROMPT.format(
            narrative_summary=player_intent[:200],
            npc_list=npc_names or "unspecified NPC",
            mechanical_outcome=outcome_str,
        )
        if hidden_context:
            whisper_prompt = hidden_context + "\n\n" + whisper_prompt

        try:
            text = await storyteller.generate(
                system_prompt=WHISPER_SYSTEM_PROMPT,
                user_prompt=whisper_prompt,
                max_tokens=_WHISPER_MAX_TOKENS,
            )
            return text.strip() if text and len(text.strip()) > 10 else None
        except Exception as exc:
            logger.debug("Whisper generation failed (non-fatal): %s", exc)
            return None

    # ── Private: Storyteller Selection ─────────────────────────────────────────

    async def _select_storyteller(self):
        """
        Return the Tier 1 storyteller for this turn.

        Cloud ON + provider=claude → ClaudeClient (if configured)
        Cloud ON + provider=gemini → GeminiClient (default)
        Cloud OFF → Auto-promoted fastest Ollama narrative node (TTFT order)
        OFF + no node → cloud fallback (with a warning)
        """
        use_cloud = await self._node_router.is_storyteller_enabled()
        if use_cloud:
            if self._cloud_provider == "claude" and self._claude is not None:
                return self._claude
            return self._gemini

        local = await self._node_router.get_storyteller_client()
        if local is None:
            cloud_name = "Claude" if self._cloud_provider == "claude" and self._claude else "Gemini"
            logger.warning(
                "GM Director: Cloud Storyteller is OFF but no narrative-tagged node is available. "
                "Falling back to %s. Tag at least one Ollama node with role='narrative'.",
                cloud_name,
            )
            if self._cloud_provider == "claude" and self._claude is not None:
                return self._claude
            return self._gemini

        return local

    # ── Private: Planning Pass ───────────────────────────────────────────────

    async def _planning_pass(
        self,
        storyteller,
        resolution:    OllamaResolutionPayload,
        player_intent: str,
    ) -> GMPlanResult:
        """
        Ask the Tier 1 storyteller to identify which scene elements should
        be delegated to sub-agents and which it will handle directly.

        On any failure (JSON parse error, timeout, malformed response) the
        method returns an empty plan so the GM synthesizes everything itself.
        This ensures a planning failure never blocks narrative generation.
        """
        npc_list   = _extract_npc_list(resolution)
        env_type   = _extract_environment_type(resolution)
        outcome_str = (
            f"{resolution.outcome.value.replace('_', ' ')}: "
            f"{resolution.reasoning[:120] if resolution.reasoning else ''}"
        )

        planning_prompt = GM_PLANNING_PROMPT.format(
            player_action=player_intent,
            mechanical_outcome=outcome_str,
            npc_list=npc_list or "none visible",
            environment_type=env_type or "unspecified",
        )

        try:
            raw_plan = await storyteller.generate(
                system_prompt=GM_PLANNING_SYSTEM_PROMPT,
                user_prompt=planning_prompt,
                max_tokens=_PLANNING_MAX_TOKENS,
            )
            plan_data = _parse_json_safely(raw_plan)

            sub_tasks = []
            for t in plan_data.get("sub_tasks", []):
                try:
                    sub_tasks.append(SubAgentTask(**t))
                except Exception as task_exc:
                    logger.debug("Skipping malformed sub-task entry: %s (%s)", t, task_exc)

            return GMPlanResult(
                sub_tasks=sub_tasks,
                direct_elements=plan_data.get("direct_elements", []),
            )

        except Exception as exc:
            logger.warning(
                "GM Director planning pass failed (%s); proceeding with direct synthesis.", exc
            )
            return GMPlanResult(sub_tasks=[], direct_elements=["full scene"])


# ── Private Helpers ──────────────────────────────────────────────────────────────

def _extract_npc_list(resolution: OllamaResolutionPayload) -> str:
    """
    Best-effort NPC extraction from the mechanical resolution.

    Checks the reasoning field for capitalized proper nouns as a heuristic.
    The planning pass will refine this with actual scene context from the GM.
    """
    if not resolution.reasoning:
        return ""
    # Extract tokens that look like proper nouns (capitalized, 3+ chars, not all-caps)
    candidates = re.findall(r"\b([A-Z][a-z]{2,})\b", resolution.reasoning)
    # De-duplicate, exclude common mechanical words
    _exclude = {"The", "This", "That", "With", "From", "Into", "Upon", "When", "Roll"}
    nouns = [w for w in dict.fromkeys(candidates) if w not in _exclude]
    return ", ".join(nouns[:5]) if nouns else ""


def _extract_environment_type(resolution: OllamaResolutionPayload) -> str:
    """Infer a rough environment type from the action_type for planning context."""
    action = resolution.action_type.lower()
    if any(k in action for k in ("attack", "combat", "fight", "strike", "shoot", "slash")):
        return "combat encounter"
    if any(k in action for k in ("speak", "talk", "persuade", "intimidate", "negotiate", "ask")):
        return "social interaction"
    if any(k in action for k in ("sneak", "hide", "stealth", "search", "investigate")):
        return "exploration/stealth"
    if any(k in action for k in ("craft", "repair", "build", "create")):
        return "crafting/downtime"
    return "general scene"


def _format_assembled_elements(sub_results) -> str:
    """Format sub-agent results into a labelled block for the synthesis prompt."""
    if not sub_results:
        return ""
    parts = []
    for r in sub_results:
        if not r.raw_output:
            continue
        label = f"[{r.task.task_type.upper()} — {r.task.entity_name}]"
        parts.append(f"{label}\n{r.raw_output}")
    return "\n\n".join(parts)


def _format_mechanical_context(resolution: OllamaResolutionPayload) -> str:
    """Build a terse mechanical fact block for the GM's internal synthesis context."""
    stat_lines = []
    for sd in resolution.state_delta.stat_deltas:
        stat_lines.append(f"  {sd.stat_key}: {sd.old_value} → {sd.new_value}")
    stat_block = "\n".join(stat_lines) if stat_lines else "  (no stat changes)"

    inv_changes = resolution.state_delta.inventory_delta
    inv_block = (
        "\n".join(f"  {json.dumps(i)}" for i in inv_changes[:5])
        if inv_changes else "  (no inventory changes)"
    )

    status_line = (
        f"  Status change: {resolution.state_delta.status_change.value}"
        if resolution.state_delta.status_change else ""
    )

    return (
        f"Action type: {resolution.action_type}\n"
        f"Roll: {resolution.dice_request.notation} → {resolution.roll_result} "
        f"(DC {resolution.difficulty}) → {resolution.outcome.value}\n"
        f"Stat changes:\n{stat_block}\n"
        f"Inventory changes:\n{inv_block}\n"
        f"{status_line}"
    ).strip()


def _build_stat_change_block(resolution: OllamaResolutionPayload) -> str:
    """
    Character Sheet Gate.

    Returns a populated stat-change block for the synthesis prompt ONLY when
    at least one stat or inventory item physically changed this turn.
    Returns an empty string otherwise — the GM must not mention stats at all.
    """
    stat_deltas = resolution.state_delta.stat_deltas
    inv_delta   = resolution.state_delta.inventory_delta
    status_ch   = resolution.state_delta.status_change

    if not stat_deltas and not inv_delta and not status_ch:
        return ""   # ← gate: nothing changed, no char sheet mention

    lines: list[str] = []
    for sd in stat_deltas:
        lines.append(f"  {sd.stat_key}: {sd.old_value} → {sd.new_value}")
    for item in inv_delta[:6]:
        name = item.get("name", item.get("item_name", "unknown item"))
        qty  = item.get("quantity", item.get("qty", "?"))
        lines.append(f"  Inventory: {name} (qty change: {qty:+d})" if isinstance(qty, int)
                     else f"  Inventory: {name} ({qty})")
    if status_ch:
        lines.append(f"  STATUS → {status_ch.value}")

    changes_text = "\n".join(lines)
    return GM_STAT_CHANGE_BLOCK.format(changes=changes_text)


def _parse_json_safely(raw: str) -> dict[str, Any]:
    """
    Extract a JSON object from a raw LLM response, stripping any markdown
    code fences or prose that might surround it.
    """
    raw = raw.strip()
    # Strip ```json ... ``` fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    # Try direct parse first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Fall back: find the first { ... } block
    start = raw.find("{")
    end   = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse JSON from planning response: {raw[:200]!r}")


def _strip_structural_text(text: str) -> tuple[str, int]:
    """
    Remove structural formatting from the GM's synthesis output.

    Returns (cleaned_text, number_of_patterns_stripped).
    """
    stripped = 0
    for pattern in STRUCTURAL_PATTERNS:
        new_text, count = pattern.subn("", text)
        if count:
            stripped += count
            text = new_text
    # Collapse multiple blank lines left behind by stripping
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, stripped


# ── Task 4 Helper Functions ────────────────────────────────────────────────────────

def _build_tts_cues(sub_results) -> list[TTSCue]:
    """
    Convert npc_dialogue sub-agent results into ordered TTSCue objects.

    Each cue carries the voice_id of the Ollama node that generated the
    dialogue, giving each "Actor" node a persistent vocal persona.
    """
    cues: list[TTSCue] = []
    for r in sub_results:
        if r.task.task_type != "npc_dialogue" or not r.raw_output:
            continue
        cues.append(TTSCue(
            entity_name=r.task.entity_name,
            text=r.raw_output,
            voice_id=r.voice_id,
            node_name=r.node_name,
        ))
    return cues


def _build_thread_event(
    resolution: OllamaResolutionPayload,
    character_name: str,
) -> tuple[ThreadEvent | None, str, str | None]:
    """
    Determine whether a Discord combat thread should be opened, updated,
    or closed, and build the thread content block.

    Returns (thread_event, thread_title, thread_content).
    """
    if is_combat_end(
        resolution.action_type,
        resolution.reasoning,
        resolution.state_delta.status_change,
    ):
        content = build_thread_content(resolution, character_name)
        title   = f"Combat – {resolution.action_type.replace('_', ' ').title()}"
        return ThreadEvent.CLOSE, title, content

    if is_combat_action(resolution.action_type):
        content = build_thread_content(resolution, character_name)
        title   = f"Combat – {resolution.action_type.replace('_', ' ').title()}"
        return ThreadEvent.COMBAT, title, content

    return None, "Encounter Details", None


def _build_directive_block(directives: list | None) -> str:
    """
    Build the [WORLD ARCHITECT DIRECTIVE] injection block for the synthesis prompt.
    Returns an empty string when there are no active directives so the prompt
    format() call is always clean.
    """
    if not directives:
        return ""
    lines = []
    for d in directives:
        type_label = d.directive_type.value.replace("_", " ").upper()
        lines.append(f"[{type_label}] {d.directive_text}")
    return GM_DIRECTIVE_BLOCK.format(directives="\n".join(lines))


def _infer_ambient_audio_key(resolution: OllamaResolutionPayload) -> str | None:
    """
    Map the inferred environment type to an ambient audio key.

    Returns None if no audio change is warranted (e.g. a stationary social
    scene in a known location — the existing ambient loop continues).
    """
    action = resolution.action_type.lower()
    if any(k in action for k in ("attack", "combat", "fight", "strike", "shoot")):
        return AMBIENT_AUDIO_MAP.get("combat encounter")
    if any(k in action for k in ("speak", "talk", "persuade", "intimidate", "ask")):
        return AMBIENT_AUDIO_MAP.get("social interaction")
    if any(k in action for k in ("sneak", "hide", "stealth", "search", "investigate")):
        return AMBIENT_AUDIO_MAP.get("exploration/stealth")
    if any(k in action for k in ("craft", "repair", "build", "create")):
        return AMBIENT_AUDIO_MAP.get("crafting/downtime")
    return None


# ── Multimedia Helper Functions ──────────────────────────────────────────────────────

def _parse_sfx_cues(raw_output: str) -> list[SFXCue]:
    """
    Parse the JSON output of a sound_director sub-agent into SFXCue objects.
    Returns an empty list if parsing fails.
    """
    raw_output = raw_output.strip()
    # Strip code fences
    import re as _re
    raw_output = _re.sub(r"^```(?:json)?\s*", "", raw_output)
    raw_output = _re.sub(r"\s*```$", "", raw_output)
    # Find the JSON array
    start = raw_output.find("[")
    end   = raw_output.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        items = json.loads(raw_output[start:end + 1])
        cues = []
        for item in items:
            if isinstance(item, dict) and "description" in item:
                cues.append(SFXCue(
                    sfx_key=item["description"],
                    delay_ms=int(item.get("delay_ms", 0)),
                    source="elevenlabs",
                ))
        return cues[:3]  # max 3 SFX per turn
    except Exception:
        return []


def _resolve_scene_type(action_type: str, ambient_key: str | None) -> str | None:
    """Map action_type and ambient_key to a Lyria music scene type."""
    action = action_type.lower()
    if any(k in action for k in ("attack", "combat", "fight", "strike", "shoot", "explode")):
        return "combat"
    if any(k in action for k in ("speak", "talk", "persuade", "barter", "chat", "negotiate")):
        return "social"
    if any(k in action for k in ("sneak", "hide", "stealth", "search", "investigate", "explore")):
        return "exploration"
    if any(k in action for k in ("rest", "sleep", "camp", "craft", "repair", "downtime")):
        return "rest"
    if ambient_key and "tension" in ambient_key.lower():
        return "tension"
    return None


async def _populate_music_cue_url(music_cue: MusicCue, gemini, db) -> None:
    """
    Background task: generate the Lyria audio and populate music_cue.audio_url.
    The MusicCue object is mutated in place; the Discord bot reads audio_url
    after a short delay when playing back.
    """
    try:
        url = await gemini.generate_music(
            music_prompt=music_cue.music_prompt,
            scene_type=music_cue.scene_type,
            db=db,
        )
        if url:
            music_cue.audio_url = url
            logger.debug("Music cue URL populated: %s → %s", music_cue.scene_type, url[:60])
    except Exception as exc:
        logger.warning("_populate_music_cue_url failed: %s", exc)
