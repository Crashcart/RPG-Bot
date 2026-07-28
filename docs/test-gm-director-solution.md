# Test Suite: GMDirector & SubAgentDispatcher

## Summary

This PR adds comprehensive pytest-asyncio unit tests for the two most critical
unstested services in the Ironclad GM orchestration layer.

## Files Added

| File | Tests | Coverage |
|------|-------|----------|
| `orchestrator/tests/test_gm_director.py` | 52 | GMDirector class + 13 helper functions |
| `orchestrator/tests/test_sub_agent_dispatcher.py` | 20 | SubAgentDispatcher + brand-filter utilities |
| `orchestrator/tests/__init__.py` | — | Package marker |

## What's Tested

### Helper Functions (pure Python, no mocks needed)
- `_parse_json_safely` — plain JSON, fenced JSON, embedded JSON, bad input
- `_strip_structural_text` — markdown headers, numbered lists, bullet points, dividers
- `_build_stat_change_block` — Character Sheet Gate (empty when no changes)
- `_extract_npc_list` — proper noun extraction, word exclusion, 5-name cap
- `_extract_environment_type` — all 4 environment type mappings + general fallback
- `_format_assembled_elements` — single/multi result, empty output skipping
- `_format_mechanical_context` — stat deltas, inventory delta, status change
- `_build_tts_cues` — NPC dialogue → TTSCue, non-dialogue excluded, voice_id propagation
- `_build_thread_event` — combat open, combat close, non-combat (None)
- `_build_directive_block` — None/empty → empty string, single/multi directive inclusion
- `_infer_ambient_audio_key` — combat/social/stealth/unknown mappings
- `_parse_sfx_cues` — valid JSON, cap at 3, fenced JSON, malformed input, delay_ms
- `_resolve_scene_type` — all 5 scene types + tension ambient fallback

### GMDirector Methods
- `_select_storyteller` — cloud ON (gemini), cloud ON (claude), cloud OFF (local), cloud OFF (fallback)
- `_planning_pass` — valid plan, invalid JSON fallback, LLM exception fallback, malformed sub-task skip
- `_generate_whisper` — success, exception (returns None), too-short response (returns None)
- `narrate` — integration: basic success, story memory retrieval, non-fatal extract_and_store failure,
  paradox engine applied/skipped, world tone injection + driftnet channel, structural text stripping,
  active directives, NPC tasks → TTS cues

### SubAgentDispatcher
- `_detect_brand_violation` — found/not-found, case-insensitive, partial match
- `_strip_brand_violations` — replacement, case-insensitive, clean text unchanged, multiple brands
- `dispatch_all` — empty list, success, exception → empty result, multiple tasks, order preservation
- `_dispatch_one` — clean output, brand-violation retry, strip+flag after max retries, node fallback chain, ttft_ms, node_name

## Running

```bash
pytest orchestrator/tests/test_gm_director.py -v
pytest orchestrator/tests/test_sub_agent_dispatcher.py -v
# or all at once:
pytest orchestrator/tests/ -v
```

## TDR Compliance
- `narrate()` tests verify the Character Sheet Gate (stat block suppressed when nothing changed)
- `narrate()` tests verify paradox engine passthrough at level 1 (passthrough)
- Brand-filter retry and strip paths tested end-to-end
- All mock failures are non-fatal (non-fatal paths tested explicitly)
