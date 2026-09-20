# Issue #19 — Whisper Protocol: Hidden Psychological State & Perception Triggers

## Summary

Adds an asymmetric information layer to the GM pipeline. When a player's character witnesses
horror, loses sanity, or carries psychological flags (`cursed`, `paranoid`, `possessed`), the
GM Director forks its output: the public channel receives the canonical scene, while the
affected player receives a private ephemeral DM (the "whisper") describing their character's
distorted inner perception.

The player never sees raw sanity scores or flag state. The GM's whisper generation pass sees
a hidden context block; the player sees only its narrative output.

---

## Context

Issue #19 specified a four-component Whisper Protocol:

1. **`hidden_state` JSONB column** on `characters` — stores sanity (0–100), fear, and status flags.
2. **`whisper_log` audit table** — immutable record of every whisper delivered.
3. **`WhisperService`** — decides when to trigger, rate-limits via Redis, mutates DB state atomically.
4. **GMDirector integration** — wires `WhisperService` into Phase 4 narration; passes a
   GM-eyes-only context block to whisper generation.

The `main` branch already had a basic `whisper: str | None` field and `_generate_whisper()`
stub in `GMDirector`, but lacked the hidden state infrastructure entirely.

---

## Approach

### Database (`db/migrations/014_whisper_protocol.sql`)

```sql
ALTER TABLE characters
    ADD COLUMN IF NOT EXISTS hidden_state JSONB NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_characters_hidden_state
    ON characters USING GIN (hidden_state);

CREATE TABLE IF NOT EXISTS whisper_log ( ... );
```

The GIN index enables fast containment queries (e.g. `hidden_state @> '{"paranoid": true}'`).
`whisper_log` rows are never deleted; they form an immutable audit trail for session replay.

### Schema (`orchestrator/schemas/payloads.py`)

Added `HiddenStateDelta` — a Pydantic model Phase 2 (Ollama adjudication) emits when a
horror/sanity action resolves:

```python
class HiddenStateDelta(BaseModel):
    sanity_delta:   int             = 0
    flags_set:      dict[str, bool] = {}
    flags_cleared:  list[str]       = []
    trigger_reason: str             = ""
```

Added `hidden_state_delta: HiddenStateDelta | None = None` to `OllamaResolutionPayload`
so Phase 2 can signal psychological mutations without violating the "Ollama never narrates"
rule.

### Service (`orchestrator/services/whisper_service.py`)

`WhisperService` is responsible for all hidden-state I/O. The LLM is never called from here.

Key constants:
| Constant | Value | Meaning |
|---|---|---|
| `SANITY_WARNING` | 40 | Creeping dread begins |
| `SANITY_PARANOID` | 20 | Contradictory hallucinations |
| `SANITY_BREAKDOWN` | 5 | Full delusions appropriate |

**`should_trigger_whisper()`** returns `True` on any of:
- `action_type` in `_HORROR_ACTION_TYPES` (10 types)
- `reasoning` or `outcome` contains a horror keyword (12 keywords)
- `hidden_state` carries `paranoid`, `cursed`, or `possessed` flag
- `sanity` is at or below `SANITY_WARNING`

**`check_rate_limit()`** uses Redis INCR/EXPIRE (3 checks / 60 s per character). Fails
open — a Redis outage never blocks the narration pipeline.

**`apply_delta()`** is atomic: `SELECT … FOR UPDATE` → Python mutation → `UPDATE` →
`INSERT INTO whisper_log`. All queries are fully parameterised (no f-string SQL).

**`build_hidden_context()`** formats the psychological state into a GM-eyes-only block
prepended to the whisper generation system prompt.

### GMDirector (`orchestrator/services/gm_director.py`)

`__init__` now accepts `whisper_svc: WhisperService | None = None`.

`narrate()` integration flow (when `whisper_svc` is wired):
1. Fetch `hidden_state` from DB.
2. Call `should_trigger_whisper()` — fires on horror triggers independently of NPC task presence.
3. Call `check_rate_limit()` — enforces 3/60s cap.
4. Build `hidden_context` from current hidden state.
5. Fire `asyncio.create_task(apply_delta(...))` for Phase 2's `hidden_state_delta`.
6. Call `_generate_whisper(hidden_context=...)` concurrently with synthesis.
7. After whisper is generated, fire second `apply_delta` to record `whisper_text` in audit log.

`_generate_whisper(hidden_context: str = "")`:
- When `hidden_context` is non-empty, prepends it to `WHISPER_SYSTEM_PROMPT` before calling
  the storyteller. The base prompt remains unchanged when no hidden context is provided.

---

## Testing

`orchestrator/tests/test_whisper_protocol.py` — 35+ test cases across 7 classes:

| Class | What it covers |
|---|---|
| `TestShouldTriggerWhisper` | All 4 trigger paths + normal-action no-trigger |
| `TestRateLimit` | Allow, block, fail-open on Redis error, no-Redis always-allow |
| `TestBuildHiddenContext` | Empty state, sanity display, LOW/PARANOID/BREAKDOWN labels, active flags |
| `TestApplyDelta` | Drain, clamp to zero, flags_set, flags_cleared, paranoid/breakdown threshold, audit log |
| `TestHiddenStateDeltaSchema` | Default noop, negative drain, positive restore |
| `TestResolutionPayloadHiddenStateDelta` | None by default, accepts delta |
| `TestGenerateWhisperHiddenContext` | hidden_context prepended vs. default system prompt |

Run with:
```bash
pytest orchestrator/tests/test_whisper_protocol.py -v
```

---

## Assumptions

- `whisper_svc` is `None` by default — existing deployments without the migration applied
  continue to work. Phase 4 whisper generation degrades gracefully to the old stub path.
- `hidden_state_delta` from Phase 2 is optional. Adjudication nodes that don't emit it
  produce no state mutation; the whisper still fires based on `should_trigger_whisper()`.
- Redis rate-limiting is advisory. The fail-open pattern means a Redis restart never causes
  a narration failure, at the cost of potentially exceeding the rate cap during the outage.
- UUIDs in `whisper_log.intent_id` are a soft reference to `action_log` (no FK constraint)
  to avoid migration ordering complexity.
- The `low_sanity` flag is always recomputed from the sanity value; it is excluded from
  `build_hidden_context()`'s active-flags list to avoid redundancy with the severity label.
