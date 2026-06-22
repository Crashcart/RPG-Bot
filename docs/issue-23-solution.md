# Issue #23 — Synchronized Voice & Music Multiplexing (Lavalink Integration)

## Summary

Promotes the Lavalink audio engine from a fallback stub to the **primary audio driver** for all Discord voice output. When Lavalink is connected, the JVM maintains the UDP connection to Discord’s voice servers and streams audio directly from the internal `media-proxy` HTTP source, completely freeing the Python event loop from audio I/O.

## Context

Before this change:
- `discord.py` / FFmpeg handled the Discord UDP connection in the Python process
- Lyria music URLs were downloaded locally before playback
- Lavalink (`wavelink`) was wired in `requirements.txt` and `docker-compose.yml` but used only as a last-resort fallback when `audio_url` was empty
- The `ironclad-discord` container had no access to the `media-assets` Docker volume, so TTS and SFX files generated at runtime could not be served via `media-proxy`

## Approach

### New module: `discord-bot/lavalink_manager.py`
- `is_ready()` — reads `wavelink.Pool.nodes` to detect live connection (no separate setup call needed)
- `get_player(channel)` — creates or reuses a `wavelink.Player` for the guild; disconnects any plain `discord.VoiceClient` first
- `play_url(player, url, volume_pct, loop)` — resolves via Lavalink HTTP source and starts playback
- `play_and_wait(player, url, volume_pct)` — one-shot playback for TTS with poll-based completion detection
- `on_track_end(payload)` — wavelink event handler for ambient track continuity (register in `bot.py` as `on_wavelink_track_end`)

### Updated `discord-bot/voice_manager.py`
- Module-level `_ASSETS_DIR` / `_assets_http_url()` — converts local paths to media-proxy HTTP URLs
- `_vc_*` polymorphic helpers — `_vc_is_playing`, `_vc_is_connected`, `_vc_stop`, `_vc_disconnect` work for both `wavelink.Player` and `discord.VoiceClient`
- `_get_or_join()` — creates `wavelink.Player` when Lavalink is ready; falls back to `VoiceClient`
- `play_music()` — Lavalink path streams directly from HTTP URL (no local download); FFmpeg fallback preserved
- `_play_ambient()` — Lavalink path serves `.mp3` from `/app/assets/audio/` via media-proxy HTTP; FFmpeg fallback preserved
- `_play_tts_queue()` — TTS files written to `/app/assets/tts/` (shared volume); Lavalink plays via HTTP URL; ambient paused/restored around each clip; FFmpeg fallback preserved
- `play_sfx()` — SFX from `/app/assets/sfx/` served via media-proxy HTTP to Lavalink; FFmpeg fallback preserved

### `docker-compose.yml`
- `ironclad-discord` now mounts `media-assets:/app/assets` (shared with `media-proxy`) so TTS/SFX/music written at runtime are immediately accessible to Lavalink via HTTP
- `LAVALINK_HOST=lavalink-audio` set explicitly (fixes service name mismatch with old default `"lavalink"`)
- `ASSETS_DIR`, `TTS_CACHE_DIR`, `AUDIO_DIR`, `SFX_CACHE_DIR`, `MUSIC_CACHE_DIR` wired to `/app/assets/*`
- `lavalink-audio` gains `_JAVA_OPTIONS=-Xmx256m -Xms64m` to cap JVM heap and prevent starvation of the Ollama inference nodes
- `ironclad-discord` `depends_on: lavalink-audio: condition: service_healthy`

### `lavalink/application.yml`
- Added comment documenting that `http: true` is the primary audio source for Ironclad GM
- `local: false` preserved (local filesystem not exposed to Lavalink; HTTP source used instead)

## Testing

- [ ] Set `LAVALINK_PASSWORD` in `.env`; confirm `lavalink-audio` healthcheck passes
- [ ] Join a voice channel; confirm `wavelink.Player` appears in guild (bot log: `LavaMgr: Player connected`)
- [ ] Trigger a Lyria music cue; confirm audio plays without any local download log message
- [ ] Trigger a TTS narration; confirm file appears in `/app/assets/tts/` and plays via Lavalink
- [ ] Trigger ambient audio; confirm Lavalink log shows HTTP source request to `media-proxy`
- [ ] Unset `LAVALINK_PASSWORD`; confirm FFmpeg fallback activates and all audio still works

## Assumptions

- `wavelink>=3.4.0` (already in `requirements.txt`)
- `media-proxy` serves `/app/assets/` as static files at `http://media-proxy:8001/assets/`
- Ambient `.mp3` files must be placed in `./data/audio/` on the host (mapped to `/app/assets/audio/` inside the container)
- Existing `bot.py` `setup_hook` already calls `wavelink.Pool.connect()` — no changes required to `bot.py`
