# Issue #12 — Immersion Enforcement & Dynamic UI Middleware

## Summary

Implements the `ImmersionFilter` service that post-processes every LLM narrative
before it reaches the player, enforcing strict in-world immersion.

## What changed

| File | Change |
|------|--------|
| `orchestrator/services/immersion_filter.py` | New service — 3 passes + state-hash UI gate |
| `orchestrator/tests/test_immersion_filter.py` | 18 unit tests |
| `db/migrations/015_immersion_filter.sql` | Per-campaign settings table |
| `orchestrator/schemas/payloads.py` | Added `render_character_sheet: bool` to `NarrativeResponsePayload` |
| `orchestrator/services/gm_director.py` | Wired in as Step 4e; surfaces `render_character_sheet` in response |

## Approach

### Three ordered passes

1. **Censorship Reversion** — 24 known asterisk-censored words expanded to full
   uncensored form; unknown intra-word asterisks stripped as fallback.
2. **Markdown List/Table Flattening** — bullet lists, numbered lists, and tables
   in narrative prose rewritten as flowing semicolon-joined sentences.
3. **Brand Name Nullification** — 80+ seed brands blocked with lore-friendly
   substitutions; per-campaign extra blocklist via DB.

### Character-Sheet UI Gate

`ImmersionFilter.should_render_character_sheet(pre_state, post_state)` computes
SHA-256 hashes of the before/after state dicts and returns `True` only when at
least one value changed. The result is surfaced as `render_character_sheet: bool`
in `NarrativeResponsePayload` so the Discord bot can suppress the embed on
no-change turns.

### Integration

```python
# main.py
from orchestrator.services.immersion_filter import ImmersionFilter
immersion_filter = ImmersionFilter()
gm_director = GMDirector(..., immersion_filter=immersion_filter)
```

Applied as Step 4e in `gm_director.narrate()` — after structural text filtering
(Step 4d) and before the Paradox Engine (Step 4f). Gracefully absent when
`immersion_filter=None` (e.g. unit tests).

## Assumptions

- Extra per-campaign blocklist rows loaded lazily (not per-request) — cache TTL
  of 5 minutes is sufficient for the White Portal admin use-case.
- `render_character_sheet=False` when the filter is absent but state delta is
  empty; `True` when state delta is non-empty (existing fallback logic preserved).

## Testing

`pytest orchestrator/tests/test_immersion_filter.py -v`
