# Issue #15 — Dynamic NPC & GM Voice Synthesis (Local TTS Pipeline)

## Summary

Implements the full TDR spec: a local Piper TTS pipeline that gives the GM and
every NPC a distinct, persistent voice without any cloud API costs.

## Architecture

```
GMDirector (Phase 4 narrative)
    │
    ▼
PiperTTSService.build_tts_cues(narrative_text, campaign_id)
    │
    ├─ parse_segments()         — split [Speaker]: text blocks
    │
    ├─ get_npc_voice(npc, campaign)  — Redis cache → PostgreSQL → narrator default
    │
    └─ returns list[TTSCue dict]     — one entry per speaker segment

Discord bot (voice_manager.py)
    │
    ├─ synthesise(text, voice_id)    — POST /api/tts?voice=... → WAV bytes
    │                                   (Redis-cached 24 h)
    └─ streams WAV to voice channel via Lavalink
```

## New files

| File | Description |
|------|-------------|
| `orchestrator/services/piper_tts_service.py` | Core TTS service |
| `orchestrator/tests/test_piper_tts_service.py` | 22 pytest-asyncio unit tests |
| `db/migrations/017_npc_voices.sql` | `npc_voice_assignments` table |

## Modified files

| File | Change |
|------|--------|
| `docker-compose.yml` | Added `piper-tts` service (linuxserver/piper on :10200) + `piper-models` volume |
| `.env.example` | Added `PIPER_URL`, `PIPER_NARRATOR_MODEL` |
| `orchestrator/services/__init__.py` | Exported `PiperTTSService` |

## TDR compliance

| TDR requirement | Implementation |
|-----------------|----------------|
| rhasspy/piper Docker container | `lscr.io/linuxserver/piper:latest` on `:10200` |
| Speaker diarization | `parse_segments()` — regex extracts `[Speaker]: text` blocks |
| Voice model routing per NPC | `get_npc_voice()` → `npc_voice_assignments` table |
| Chunked sentence streaming | `chunk_sentences()` — splits on `.!?` for low-latency playback |
| Voice persistence | `npc_voice_assignments` table; `set_npc_voice()` upsert |
| Non-fatal failure | `synthesise()` returns `None` on any error — pipeline never crashes |
| Cache-hit avoidance | SHA-256(voice+text) keyed in Redis, TTL 86400 s |

## Piper HTTP API

The `linuxserver/piper` container exposes:

```
POST /api/tts?voice=en_US-lessac-medium
Content-Type: text/plain
Body: text to synthesise
→ audio/wav bytes

GET /api/voices
→ [{"name": "en_US-lessac-medium", ...}, ...]
```

## Wiring into GMDirector

After merge, add the following to `orchestrator/main.py` lifespan and inject
`piper_tts` into `GMDirector`:

```python
# main.py — in the lifespan startup block:
from orchestrator.services import PiperTTSService

piper_tts = PiperTTSService(
    piper_url=settings.piper_url,          # add to orchestrator/config.py
    redis=cache,
    db=db_service,
    narrator_model=settings.piper_narrator_model,
)
```

In `gm_director.py` `narrate()`, after the narrative text is finalised
(Step 4d, before `ParadoxEngine`):

```python
if self._piper_tts:
    tts_cues = await self._piper_tts.build_tts_cues(
        response.narrative, context.campaign_id
    )
    from orchestrator.schemas.payloads import TTSCue
    response.tts_cues = [TTSCue(**c) for c in tts_cues]
```

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest orchestrator/tests/test_piper_tts_service.py -v
```

All 22 tests run with mocked httpx, Redis, and DB — no live Piper container needed.

## Migration note

Run `db/migrations/017_npc_voices.sql` after `013_inference_settings.sql` on main,
or after whichever migration is highest in the target environment.
Migrations 014-016 are in open PRs (#48-#50) and have not yet landed on `main`.
