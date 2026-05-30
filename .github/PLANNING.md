# Ironclad GM — Planning & Coordination

Centralised record of initiatives, architectural decisions, and handoff notes.
Update this file whenever a significant decision is made or a session hands off to a new agent.

---

## Current Initiative: Multimedia Expansion

**Branch:** `claude/api-payload-schemas-0jkYr`
**Status:** In progress — Phase 5–9 remaining

### What was built
- **Lyria 3 music**: Gemini `generate_music()` calls Lyria 3, saves 30-second clips, Discord bot
  loops via FFmpeg `-stream_loop -1`. Lavalink is fallback only.
- **ElevenLabs SFX**: `ElevenLabsClient.generate_sfx()` → short clips cached by SHA-256.
- **Image generation**: Pluggable `ImageGenService` — ComfyUI (local), Stability AI, DALL-E 3,
  or disabled. Backend selected at runtime via `image_gen_backend` system_setting.
- **Cloud adjudication**: `OpenAICompatClient` covers Groq, OpenRouter, Together, and SillyTavern.
  `NodeRouter` reads `adjudication_provider` setting; SillyTavern URL is re-read live (cache-busted).
- **SillyTavern**: External-only (not installed). URL + optional API key configurable via
  White Portal → Settings. Usable as adjudication engine or cloud storyteller.
- **Handouts**: `HandoutService` — AI-authored in-world documents, player delivery tracking.
- **Factions**: `FactionService` — per-player reputation -100..100, AI auto-adjust post-narrative.
- **Idle detection**: `VoiceManager._idle_watchdog()` (timeout) + `on_voice_state_update` (immediate).
- **Music feedback**: `/music approve`, `/music skip`, `/music change <note>` Discord commands.
- **GM Director self-awareness**: `GM_DIRECTOR_ORCHESTRATOR_CONTEXT` prepended to system prompt.
- **New slash commands**: `/handout list|view`, `/map generate|show`, `/reputation`, `/music *`.
- **New API endpoints**: `/api/sfx/generate`, `/api/maps/*`, `/api/handouts/*`, `/api/factions/*`,
  `/api/music/*`, `/api/settings/value`.

### What remains
- ComfyUI in `docker-compose.yml`
- PropheticBuffer idle prefetch expansion
- Brand filtering integration (PDF-sourced names allowed; real-world brands blocked)
- Wire `handout_svc`, `faction_svc`, `gemini`, `world_registry` onto `app.state` in `main.py` lifespan

---

## Architectural Decisions

### 2026-04 — Lyria 3 replaces Lavalink as primary music source
Gemini Lyria 3 generates actual audio; Lavalink searches YouTube/SoundCloud as fallback.
30-second clips loop via FFmpeg. Lavalink remains in `docker-compose.yml` for the fallback path.

### 2026-04 — SillyTavern as external-only provider
SillyTavern is never added to `docker-compose.yml`. Users bring their own instance.
The `sillytavern_url` system_setting is re-read on each adjudication call so URL changes
take effect without restart. The model field is optional — ST uses its own active model.

### 2026-04 — OpenAI-compat single client for cloud adjudication
Groq, OpenRouter, Together, and SillyTavern all share `OpenAICompatClient` because they expose
the same `/v1/chat/completions` format. Provider-specific behaviour (JSON mode, headers) is
handled inside the client rather than at call sites.

### 2026-04 — Brand filtering strategy
Names from ingested PDF rulebooks (retrieved via RAG from ChromaDB) are allowed in generated
narrative. Real-world brand names are blocked by the existing post-processor unless they also
appear in an ingested rulebook. This preserves licensed RPG content (e.g. a product that
canonically names real brands) while blocking random real-world references.

### 2025 — Pydantic payloads as the pipeline contract
`orchestrator/schemas/payloads.py` is the single source of truth. All pipeline phases communicate
exclusively through these models. This decision is frozen — it cannot be relaxed without a full
architecture review.

---

## Handoff Notes

### White Portal pages (completed 2026-05-30)
- `handouts.html`, `factions.html`, `gm_advisor.html` — all three created.
- Routes added to `web_ui.py`: `/handouts`, `/handouts/create`, `/handouts/deliver`,
  `/factions`, `/factions/upsert`, `/factions/adjust`, `/gm-advisor`, `/gm-advisor/ask`.
- Nav updated in `base.html`.
- Helper accessors `_handout_svc`, `_faction_svc`, `_gemini`, `_world_registry` added.
- **Remaining**: expose these four on `app.state` in `main.py` lifespan so the accessors work.

### For the next agent expanding PropheticBuffer
- `PropheticBuffer` is in `orchestrator/services/prophetic_buffer.py`.
- Add `run_idle_prefetch()` that generates: music clip, NPC portrait, scene image, recap text,
  TTS clip, and calls `node_router.warmup_all_nodes()`.
- The idle prefetch loop should be triggered from `main.py` lifespan, gated on no active
  VoiceClient connections (check via a Redis key set by the bot, or a simple time-since-last-action check).

### For the agent implementing brand filtering
- The post-processor is in `orchestrator/services/sub_agent_dispatcher.py` — look for
  `brand_violation` handling.
- RAG context is assembled in `orchestrator/pipeline/ingestion.py` — the PDF chunks that are
  retrieved for the current turn contain the allowed brand vocabulary.
- Strategy: extract proper nouns from the RAG chunks (simple NER or regex over capitalised terms),
  build a per-turn allow-list, pass it into the post-processor alongside the blocked list.
