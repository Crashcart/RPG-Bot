# Issue #19 — Whisper Protocol: Asynchronous Hidden State & Sanity Management

## Summary

Implements the "Whisper Protocol" TDR: a private GM → player channel that delivers
secret psychological events (sanity drain, paranoia, hallucinations) via Discord DMs
or ephemeral messages, bypassing the public narrative channel entirely.

## Context

The existing pipeline already has the `whisper` field on `NarrativeResponsePayload`
and a `_generate_whisper()` method in `GMDirector`, but those only fire when NPC
dialogue sub-tasks are present. The TDR requires hidden-state-driven whispers
(sanity loss, cursed object contact, paranoia flag) that are independent of NPC
presence. The Discord bot already reads `response.whisper` and DMs the player.

## Approach

### 1. Database Migration (`db/migrations/014_whisper_protocol.sql`)
- Add `hidden_state JSONB NOT NULL DEFAULT '{}'` column to `characters`
- Create `whisper_log` immutable audit table (character_id, intent_id, trigger, delta, whisper_text)
- GIN index on `hidden_state` for efficient flag/sanity queries

### 2. Schema Addition (`orchestrator/schemas/payloads.py`)
- Add `HiddenStateDelta` Pydantic model (sanity_drain, flags_add, flags_remove, trigger_whisper)
- Add `hidden_state_delta: HiddenStateDelta | None` field to `OllamaResolutionPayload`
  so the mechanical adjudicator can signal hidden consequences alongside visible ones

### 3. New Service (`orchestrator/services/whisper_service.py`)
- `WhisperService` manages reads/writes to `characters.hidden_state`
- `should_trigger_whisper()` — evaluates action_type, reasoning keywords, existing flags,
  and sanity thresholds; Redis rate-limits perception checks (3/60 s per character)
- `apply_delta()` — atomically merges sanity drain + flag changes into JSONB
- `build_hidden_context()` — formats a compact hidden-state string for the whisper prompt
- `log_whisper_delivery()` — writes the audit row to `whisper_log`

### 4. Database Service additions (`orchestrator/services/database.py`)
- `get_hidden_state(character_id)` — SELECT hidden_state FROM characters
- `apply_hidden_state_delta(conn, character_id, delta)` — JSONB merge within transaction
- `log_whisper(...)` — INSERT into whisper_log

### 5. GM Director extension (`orchestrator/services/gm_director.py`)
- Accept optional `whisper_svc: WhisperService` in `__init__`
- In `narrate()`: after sub-agent dispatch, call `whisper_svc.should_trigger_whisper()`;
  if triggered, set `should_whisper = True` and inject hidden_context into the prompt
- Extend `_generate_whisper()` to accept `hidden_context: str` and prepend it to the
  whisper prompt so the storyteller knows the player's actual psychological state
- Fire-and-forget `log_whisper_delivery()` after whisper generation

## Testing

`orchestrator/tests/test_whisper_protocol.py` covers:
- `should_trigger_whisper` fires on horror action_types
- Rate-limiting blocks a 4th check within the window
- `paranoid` flag unconditionally triggers whisper (ignores rate limit)
- Sanity below threshold triggers unconditionally
- `apply_delta` drains sanity and merges flags correctly
- `build_hidden_context` formats the right description at each sanity band
- `HiddenStateDelta` schema validates correctly

## Assumptions

- Discord bot already reads `NarrativeResponsePayload.whisper` and DMs the player;
  no bot-side changes are needed for the basic ephemeral delivery.
- `hidden_state` starts as `{}` (sanity defaults to 100 in `WhisperService`, not stored
  until first drain — avoids unnecessary DB writes for healthy characters).
- The Ollama adjudicator is not yet trained to emit `hidden_state_delta`; the
  `WhisperService.should_trigger_whisper()` heuristic covers the gap until a
  prompt-engineering pass teaches the adjudicator to signal hidden consequences.
- Rate limiting uses Redis with fail-open semantics: if Redis is unavailable the
  whisper is allowed through so players don't lose horror events on infra hiccups.
