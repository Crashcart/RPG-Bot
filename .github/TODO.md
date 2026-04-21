# Ironclad GM — Task Tracker

One agent, one in-progress task at a time. Mark completed immediately after finishing.

Statuses: `[x]` completed · `[-]` in-progress · `[ ]` not-started

---

## Active Session: `claude/api-payload-schemas-0jkYr`

### Multimedia Expansion
- [x] Create `db/migrations/012_multimedia.sql` — handouts, factions, npc_portraits, music_feedback
- [x] Extend `orchestrator/schemas/payloads.py` — SFXCue, MusicCue, new NarrativeResponsePayload fields
- [x] Add new env vars to `orchestrator/config.py` — elevenlabs, comfyui, stability_ai, groq, openrouter, together, openai, sillytavern
- [x] Create `orchestrator/services/openai_compat_client.py` — Groq/OpenRouter/Together/SillyTavern
- [x] Create `orchestrator/services/elevenlabs_client.py` — SFX + TTS REST client
- [x] Create `orchestrator/services/image_gen.py` — ComfyUI / Stability AI / DALL-E 3 backend
- [x] Create `orchestrator/services/handout_service.py` — AI-authored in-world documents
- [x] Create `orchestrator/services/faction_service.py` — reputation scores + AI auto-adjust
- [x] Update `orchestrator/services/gemini_client.py` — generate_music() via Lyria 3
- [x] Update `orchestrator/services/gm_director.py` — orchestrator context, sound/scene sub-agents, multimedia payload
- [x] Update `orchestrator/services/node_router.py` — SillyTavern URL-busting cache, cloud adj routing
- [x] Update `orchestrator/prompts/gm_prompts.py` — new sub-agent prompts, GM_DIRECTOR_ORCHESTRATOR_CONTEXT
- [x] Update `orchestrator/services/sub_agent_dispatcher.py` — sound_director + scene_describer types
- [x] Update `discord-bot/voice_manager.py` — Lyria loop, SFX, TTS provider switching, idle watchdog
- [x] Update `discord-bot/bot.py` — on_voice_state_update, multimedia delivery, /handout /map /reputation /music commands
- [x] Add multimedia API endpoints to `orchestrator/main.py`
- [x] Create `db/migrations/013_inference_settings.sql` — system_settings seeds
- [x] Add SillyTavern to `orchestrator/services/openai_compat_client.py`, `config.py`, `web_ui.py`, `settings.html`

### Governance / .github
- [-] Create `.github/` governance files (REPO_SETUP_GUIDE, copilot-instructions, REPO_CONFIG, TODO, PLANNING, BRANCH_AWARE_FILES, ci.yml, pull_request_template)

### Pending
- [ ] White Portal — create `handouts.html`, `factions.html`, `gm_advisor.html` templates
- [ ] White Portal — update `web_ui.py` routes for handouts, factions, GM advisor pages
- [ ] White Portal — update `base.html` nav links for new pages
- [ ] Add ComfyUI service to `docker-compose.yml`
- [ ] Expand `PropheticBuffer.run_idle_prefetch()` — music/portraits/scene images/recaps during idle
- [ ] Brand filtering — update sub-agent post-processor to allow PDF-sourced names
- [ ] Create `compose.alpha.yml`, `compose.beta.yml`, `compose.prod.yml` tier overrides

---

## Backlog

- [ ] `discord-bot/requirements.txt` — pin versions for all dependencies
- [ ] Add `pytest` test suite scaffold under `orchestrator/tests/`
- [ ] Telemetry WebSocket — add reconnect logic on client disconnect
- [ ] Rolling Vault — add configurable window size to White Portal settings
- [ ] PropheticBuffer — expose cache hit rate via `/api/prophetic/stats` endpoint
