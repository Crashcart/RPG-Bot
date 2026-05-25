# Issue #14 — Speculative Narrative Pre-Computation (Zero-Latency Engine)

## Summary

Upgrades `PropheticBuffer` from a single-branch prefetch worker to a full
three-branch branching orchestrator with load-aware scaling and keyword-based
semantic cache resolution, eliminating LLM narration latency on common
follow-up actions.

## Problem

Self-hosted Ollama on consumer hardware introduces 5–15 s narration latency
per turn. While the player deliberates, GPU/CPU sits idle — wasted time that
can be spent pre-generating the most likely GM responses.

## Architecture

### Before (main branch)
- Single follow-up branch predicted per turn via `_FOLLOW_UP_MAP` (outcome → `follow_ups[0]`)
- No semantic resolution — only legacy `get_prefetched_text(intent_id)` lookup
- No load awareness

### After (this PR)

```
 PipelineResult ──► enqueue(result)
                         │
                   ┌─────▼─────────────────────────────────────────┐
                   │  Background Worker (_prefetch)                  │
                   │                                                 │
                   │  1. Predict top-N follow-up labels              │
                   │     (from _FOLLOW_UP_MAP, outcome-based)        │
                   │                                                 │
                   │  2. _effective_branch_count()                   │
                   │     → psutil CPU/RAM check                      │
                   │     → 3 (low) / 2 (moderate) / 1 (heavy)       │
                   │                                                 │
                   │  3. asyncio.gather(*[_generate_branch(lbl)])    │
                   │     → storyteller.generate() per branch         │
                   │     → 20 s timeout per branch                   │
                   │                                                 │
                   │  4. cache.set(ironclad:speculative:{guild_id})  │
                   │     JSON array of BranchEntry, TTL 300 s        │
                   └─────────────────────────────────────────────────┘

 Player submits input
       │
       ▼
 get_speculative_response(guild_id, player_input)
       │
       ├─ cache.get(ironclad:speculative:{guild_id})
       │       │
       │   branches JSON
       │       │
       │  _best_match(player_input, branches)
       │       │
       │  ┌────▼──────────────────────────────────┐
       │  │  For each branch:                      │
       │  │    _tokenise(player_input)             │
       │  │    _score_branch(tokens, label)        │
       │  │    = |tokens ∩ keywords| / |tokens|   │
       │  └───────────────────────────────────────┘
       │       │
       │  best_score ≥ threshold (default 0.30)
       │       │
       ├── HIT: return BranchEntry (narrative_text + ambient_audio_key)
       └── MISS: return None → pipeline falls back to Phase 4
```

## Changed Files

| File | Change |
|------|--------|
| `orchestrator/services/prophetic_buffer.py` | Complete rewrite — 3-branch prediction, semantic resolution, load scaling |
| `orchestrator/config.py` | 8 new `speculative_*` settings (all with safe defaults) |
| `orchestrator/tests/test_prophetic_buffer.py` | 45 pytest-asyncio tests |
| `requirements-dev.txt` | `pytest`, `pytest-asyncio`, `psutil` |

## New Config Settings

All settings have safe defaults. No `.env` changes are required to enable the feature.

| Env Var | Default | Description |
|---------|---------|-------------|
| `SPECULATIVE_ENGINE_ENABLED` | `true` | Master on/off switch |
| `SPECULATIVE_BRANCHES` | `3` | Max branches to pre-generate per turn |
| `SPECULATIVE_TTL_SECONDS` | `300` | Redis TTL for cached branches (5 min) |
| `SPECULATIVE_SIMILARITY_THRESHOLD` | `0.30` | Min keyword-overlap score for a cache hit |
| `SPECULATIVE_CPU_DISABLE` | `85` | CPU % at which to reduce to 1 branch |
| `SPECULATIVE_CPU_SCALE_DOWN` | `70` | CPU % at which to reduce to 2 branches |
| `SPECULATIVE_RAM_DISABLE` | `90` | RAM % at which to reduce to 1 branch |
| `SPECULATIVE_RAM_SCALE_DOWN` | `80` | RAM % at which to reduce to 2 branches |

## Wiring Instructions

### 1. Pass `settings` to PropheticBuffer in `orchestrator/main.py`

```python
from orchestrator.config import get_settings
from orchestrator.services.prophetic_buffer import PropheticBuffer

prophetic_buffer = PropheticBuffer(
    cache=cache_service,
    storyteller=gemini_client,   # or claude_client
    settings=get_settings(),
)
```

### 2. Try speculative cache before Phase 4 in the action pipeline

```python
# After Phase 3 state commit, before Phase 4 narration:
speculative = await prophetic_buffer.get_speculative_response(
    guild_id=intent.guild_id,
    player_input=intent.raw_input,
)
if speculative:
    # Cache hit: use pre-generated prose — Phase 4 LLM call skipped
    return NarrativeResponsePayload(
        ...
        narrative=speculative["narrative_text"],
        ambient_audio_key=speculative["ambient_audio_key"],
    )
# Cache miss: fall through to normal Phase 4 generation
```

### 3. Enqueue after every completed PipelineResult

```python
# At the end of the action pipeline:
await prophetic_buffer.enqueue(pipeline_result)   # fire-and-forget
```

## Running Tests

```bash
pip install -r requirements-dev.txt
pytest orchestrator/tests/test_prophetic_buffer.py -v
```

Expected: 45 tests, 0 failures.

## TDR Compliance

| Requirement | Status |
|-------------|--------|
| Top-3 speculative branches pre-generated | ✅ |
| Load-aware branch count (psutil CPU + RAM) | ✅ |
| Semantic similarity cache resolution | ✅ (precision keyword-overlap) |
| Redis TTL 5 min | ✅ (`speculative_ttl_seconds=300`) |
| Near-zero latency on cache hit | ✅ (storyteller.generate() bypassed) |
| Backward-compatible legacy API | ✅ (`get_prefetched_text`, `get_prefetched_audio` retained) |
| Configurable thresholds and load limits | ✅ (all via `Settings`) |
| No new heavy infrastructure required | ✅ (Redis + existing storyteller only) |
