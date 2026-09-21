# Issue #12 — Immersion Enforcement & Dynamic UI Middleware

## Summary

Centralised `ImmersionFilter` service that applies four sequential text transforms
to all LLM output before it reaches the player, plus a hash-based character-sheet
render gate.

## Context

Before this change, brand filtering lived in `sub_agent_dispatcher.py` and
structural text stripping lived in `gm_director.py`. Both were module-level
helper functions with no shared interface and no tests.  Neither implemented
asterisk-censorship reversion or markdown list flattening, and neither had
a mechanism to suppress redundant character-sheet renders.

## Approach

`orchestrator/services/immersion_filter.py` — `ImmersionFilter` class:

| Method | What it does |
|--------|-------------|
| `scrub_brands(text)` | Replace all real-world brand/IP names with `[???]` using a single compiled alternation regex — O(n) scan instead of 150 individual passes |
| `flatten_markdown_lists(text)` | Convert `- item` and `1. item` lines to inline prose (run before structural strip to preserve content) |
| `strip_structural(text)` | Remove markdown headings, dividers, numbered lists, bullet markers via `STRUCTURAL_PATTERNS` |
| `revert_censorship(text)` | Replace asterisk-masked words (`f***`, `s**t`) with elided form (`f…`, `s…t`) |
| `process(text)` → `(cleaned, report)` | Apply all four transforms in sequence; return telemetry report |
| `should_render_sheet(player_id, stats)` | Async; returns `True` iff stats changed since last call (MD5 gate) |
| `should_render_sheet_sync(...)` | Sync variant for non-async callers |
| `invalidate_sheet_cache(player_id)` | Force next render check to return `True` |

### Sheet-diff gate

An MD5 hash of the JSON-serialised stat dict (keys sorted for stability) is
stored per `player_id` in an in-process dict, falling back gracefully when
Redis is unavailable.  The Discord bot calls `should_render_sheet_sync` before
deciding whether to include the stat-block embed.

### Transform order

`flatten_markdown_lists` runs **before** `strip_structural` so bullet-item
content is converted to prose before the structural patterns strip the leading
`- ` marker (which would otherwise consume the first character of each item).

## Testing

`orchestrator/tests/test_immersion_filter.py` — 36 pytest-asyncio tests across
7 classes.  Run with:

```bash
pip install -r requirements-dev.txt
pytest orchestrator/tests/test_immersion_filter.py -v
```

All 36 tests pass ✅.

## Wiring

```python
# orchestrator/main.py  (app lifespan)
from orchestrator.services.immersion_filter import ImmersionFilter

immersion = ImmersionFilter(redis_client=cache.client)

# Pass to SubAgentDispatcher (replaces internal _strip_brand_violations)
dispatcher = SubAgentDispatcher(..., immersion_filter=immersion)

# Pass to GMDirector (replaces internal _strip_structural_text)
gm_director = GMDirector(..., immersion_filter=immersion)
```

Both services accept `immersion_filter=None` so existing deployments that
have not yet been updated continue to use their internal implementations.

## Assumptions

- `BRAND_BLOCKLIST` and `STRUCTURAL_PATTERNS` remain the single source of truth
  in `orchestrator/prompts/gm_prompts.py`; `ImmersionFilter` imports them.
- The sheet-diff gate is per-process by default; Redis persistence is opt-in.
- Asterisk reversion uses an elided-suffix strategy rather than full word
  recovery, which is impossible without the original token.
