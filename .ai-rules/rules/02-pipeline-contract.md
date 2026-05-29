## Pipeline Contract

- All inter-service data passes through Pydantic models in `orchestrator/schemas/payloads.py`.
  Never pass raw dicts between pipeline phases.
- **Ollama never narrates.** Phase 2 produces only `OllamaResolutionPayload`. Narrative prose is
  Phase 4 only (GMDirector → Gemini / Claude / SillyTavern).
- **Dice are backend-only.** The LLM requests a roll via `DiceRequest`; the backend generates the
  result. The model cannot influence dice outcomes.
- **NodeRouter for all Ollama calls.** Never hardcode an Ollama node URL. All calls go through
  `NodeRouter.get_ollama_client_for_role()`.
- `action_log` rows are immutable. Retcons set `retconned=TRUE`; rows are never deleted.
- Sub-agent output is post-processed for brand filtering before reaching `NarrativeResponsePayload`.
