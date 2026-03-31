"""
Ironclad GM – Voice Channel Manager
=====================================
Manages all audio output in Discord voice channels for the Living Discord
immersion layer (Task 4 — Voice Channel Puppeteering).

Features
--------
  Ambient audio     – Loop a pre-recorded environmental audio file (rain,
                      tavern chatter, dungeon hum) whenever the scene type
                      changes.  Plays at reduced volume as a background layer.

  Lyria music       – Stream AI-generated music from Gemini Lyria 3.
                      30-second clips are looped via FFmpeg `-stream_loop -1`
                      for continuous ambient playback until a new MusicCue
                      arrives.  Falls back to Lavalink search if audio_url
                      is empty and a lavalink_query is provided.

  SFX               – One-shot sound effects via ElevenLabs or local vault
                      files.  Pauses ambient briefly, plays the effect, then
                      resumes.

  TTS puppeteering  – Speak NPC dialogue aloud.  Three providers are
                      supported, selected by the `tts_provider` system_setting:
                        • edge_tts      (default, free, no key needed)
                        • elevenlabs    (ElevenLabs TTS REST API)
                        • openai_tts    (OpenAI /audio/speech endpoint)
                      Each Ollama Actor node has a unique voice identity that
                      persists across the campaign.  TTS files are cached by
                      SHA-256(voice_id, text) to avoid regenerating identical
                      lines.

  Idle detection    – Two-layer idle detection:
                        1. Immediate: on_voice_state_update in bot.py disconnects
                           when the channel becomes human-empty.
                        2. Timeout: _idle_watchdog() background task disconnects
                           after `voice_idle_timeout_s` seconds of inactivity
                           (default 300 s, configurable via system_setting).

  Voice client mgmt – One VoiceClient per guild.  The manager automatically
                      joins the player's voice channel if not connected, or
                      moves to the player's current channel if they moved.

Dependencies (install in discord-bot container):
  discord.py[voice]  — includes PyNaCl for audio encryption
  edge-tts           — async Microsoft Edge TTS, zero quota, high quality
  ffmpeg             — system package for audio transcoding
  httpx              — async HTTP client for media download + TTS APIs

Audio file layout (mounted volume or AUDIO_DIR env var):
  /app/audio/
    combat_tension.mp3
    tavern_chatter.mp3
    dungeon_ambience.mp3
    workshop_sounds.mp3
    <any_key>.mp3        ← add new ambient tracks freely

TTS cache layout (TTS_CACHE_DIR env var, default /tmp/ironclad_tts):
  /tmp/ironclad_tts/
    <voice_id_hash>_<text_hash>.mp3   ← auto-generated, persists for session

Music cache layout:
  /app/assets/music/
    <sha256>.mp3         ← Lyria-generated clips from the orchestrator

SFX cache layout:
  /app/assets/sfx/
    <sha256>.mp3         ← ElevenLabs or vault SFX clips
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path

import discord
import httpx

logger = logging.getLogger(__name__)

_AUDIO_DIR    = Path(os.environ.get("AUDIO_DIR",      "/app/audio"))
_TTS_CACHE    = Path(os.environ.get("TTS_CACHE_DIR",  "/tmp/ironclad_tts"))
_MUSIC_CACHE  = Path(os.environ.get("MUSIC_CACHE_DIR", "/app/assets/music"))
_SFX_CACHE    = Path(os.environ.get("SFX_CACHE_DIR",   "/app/assets/sfx"))
_AMBIENT_VOL  = float(os.environ.get("AMBIENT_VOLUME",  "0.25"))
_TTS_VOL      = float(os.environ.get("TTS_VOLUME",      "0.90"))
_DEFAULT_VOICE = "en-US-GuyNeural"
_ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://scribe:8000")
_ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
_OPENAI_API_KEY     = os.environ.get("OPENAI_API_KEY", "")
_LAVALINK_PASSWORD  = os.environ.get("LAVALINK_PASSWORD", "")
_LAVALINK_HOST      = os.environ.get("LAVALINK_HOST", "lavalink")

# Default idle timeout — overridden by system_setting 'voice_idle_timeout_s'
_DEFAULT_IDLE_TIMEOUT = int(os.environ.get("VOICE_IDLE_TIMEOUT_S", "300"))
_WATCHDOG_INTERVAL    = 30   # seconds between watchdog checks

# Maps audio keys to filenames under _AUDIO_DIR
_AUDIO_FILES: dict[str, str] = {
    "combat_tension":   "combat_tension.mp3",
    "tavern_chatter":   "tavern_chatter.mp3",
    "dungeon_ambience": "dungeon_ambience.mp3",
    "workshop_sounds":  "workshop_sounds.mp3",
}


class VoiceManager:
    """
    Singleton-style manager (one instance per bot) for Discord voice audio.

    All public methods are async-safe and can be called from concurrent tasks.
    """

    def __init__(self) -> None:
        for d in (_TTS_CACHE, _MUSIC_CACHE, _SFX_CACHE):
            d.mkdir(parents=True, exist_ok=True)

        # guild_id → active VoiceClient
        self._voice_clients: dict[int, discord.VoiceClient] = {}
        # guild_id → current ambient audio key (prevents redundant restarts)
        self._current_ambient: dict[int, str | None] = {}
        # guild_id → currently playing music URL (prevents re-playing same URL)
        self._current_music_url: dict[int, str] = {}
        # guild_id → monotonic timestamp of last player activity
        self._last_activity: dict[int, float] = {}
        # Background idle watchdog task
        self._idle_watchdog_task: asyncio.Task | None = None
        # Shared HTTP client set by bot.py after setup_hook
        self._http: httpx.AsyncClient | None = None

    def set_http_client(self, client: httpx.AsyncClient) -> None:
        """Called by bot.py after the HTTP client is initialised."""
        self._http = client

    async def start_idle_watchdog(self) -> None:
        """Start the background idle detection task (call from bot setup_hook)."""
        if self._idle_watchdog_task is None or self._idle_watchdog_task.done():
            self._idle_watchdog_task = asyncio.create_task(
                self._idle_watchdog(), name="voice-idle-watchdog"
            )
            logger.info("VoiceManager: idle watchdog started (interval=%ds).", _WATCHDOG_INTERVAL)

    def track_activity(self, guild_id: int) -> None:
        """Record that a player action occurred in this guild (resets idle clock)."""
        self._last_activity[guild_id] = time.monotonic()

    # ── Public API ─────────────────────────────────────────────────────────────

    async def handle_turn_audio(
        self,
        member:            discord.Member,
        ambient_audio_key: str | None,
        tts_cues:          list[dict],
    ) -> None:
        """
        Called after posting the main narrative embed.

        1. Joins or moves to the member's voice channel.
        2. Starts (or switches) ambient audio if the key changed.
        3. Queues TTS cues to play sequentially after ambient fades in.

        Silently returns if the member is not in a voice channel.
        """
        if not member.voice or not member.voice.channel:
            return

        voice_channel = member.voice.channel
        guild_id      = member.guild.id

        self.track_activity(guild_id)
        vc = await self._get_or_join(voice_channel)
        if vc is None:
            return

        try:
            await self._play_ambient(vc, guild_id, ambient_audio_key)
            if tts_cues:
                await asyncio.sleep(0.8)
                await self._play_tts_queue(vc, tts_cues)
        except Exception as exc:
            logger.error("VoiceManager audio error (guild=%d): %s", guild_id, exc)

    async def play_music(
        self,
        guild_id:       int,
        audio_url:      str,
        volume:         float = 0.45,
        crossfade_s:    float = 2.0,
        lavalink_query: str   = "",
        music_prompt:   str   = "",
    ) -> None:
        """
        Play AI-generated music from a media-proxy URL.

        Primary path: downloads audio from audio_url and plays it with
        FFmpeg's `-stream_loop -1` flag so 30-second Lyria clips loop
        continuously until a new MusicCue arrives.

        Fallback: if audio_url is empty and lavalink_query is non-empty,
        delegates to Lavalink via wavelink (requires LAVALINK_PASSWORD).

        Skips silently if the same URL is already playing.
        Does nothing if the bot is not connected in this guild.
        """
        # Skip if same URL already playing
        if audio_url and self._current_music_url.get(guild_id) == audio_url:
            return

        vc = self._voice_clients.get(guild_id)
        if vc is None or not vc.is_connected():
            logger.debug(
                "VoiceManager.play_music: no voice client for guild %d — skipping.", guild_id
            )
            return

        # Crossfade: stop current playback with a brief gap
        if vc.is_playing() or vc.is_paused():
            vc.stop()
            if crossfade_s > 0:
                await asyncio.sleep(min(crossfade_s, 2.5))

        # Primary path — Lyria audio URL
        if audio_url:
            local_path = await _download_and_cache_audio(audio_url, _MUSIC_CACHE)
            if local_path:
                self._current_music_url[guild_id] = audio_url
                ffmpeg_opts = {
                    "before_options": "-stream_loop -1",  # loop the 30-second clip
                    "options":        "-vn",
                }
                source = discord.FFmpegPCMAudio(str(local_path), **ffmpeg_opts)
                vc.play(
                    discord.PCMVolumeTransformer(source, volume=volume),
                    after=lambda e: logger.debug("Music playback ended: %s", e) if e else None,
                )
                logger.info(
                    "VoiceManager: Lyria music started guild=%d url=%s",
                    guild_id, audio_url,
                )
                return
            logger.warning(
                "VoiceManager: could not download Lyria audio from %s", audio_url
            )

        # Fallback — Lavalink search
        if lavalink_query and _LAVALINK_PASSWORD:
            await self._play_lavalink(vc, guild_id, lavalink_query, volume)

    async def stop_music(self, guild_id: int) -> None:
        """Stop the current music for a guild (for /music skip or regeneration)."""
        vc = self._voice_clients.get(guild_id)
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        self._current_music_url.pop(guild_id, None)
        logger.info("VoiceManager: music stopped for guild %d", guild_id)

    async def play_sfx(
        self,
        guild_id: int,
        source:   str,     # local path or HTTP URL
        volume:   float = 0.7,
        delay_ms: int   = 0,
    ) -> None:
        """
        Play a one-shot SFX clip.

        source can be a local file path or a media-proxy URL.
        Pauses ambient/music briefly, plays the SFX, then resumes.

        Note: discord.py VoiceClient is single-source.  True audio mixing
        (SFX over music) requires a separate opus encoder — deferred to v2.
        """
        vc = self._voice_clients.get(guild_id)
        if vc is None or not vc.is_connected():
            return

        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000)

        sfx_path: Path | None = None
        if source.startswith("http://") or source.startswith("https://"):
            sfx_path = await _download_and_cache_audio(source, _SFX_CACHE)
        else:
            p = Path(source)
            if p.exists():
                sfx_path = p

        if sfx_path is None:
            logger.warning("VoiceManager.play_sfx: could not resolve source '%s'", source)
            return

        try:
            await _play_file_and_wait(vc, sfx_path, volume=volume)
            logger.debug("VoiceManager: SFX played guild=%d source=%s", guild_id, source)
        except Exception as exc:
            logger.warning("VoiceManager.play_sfx error: %s", exc)

    async def disconnect(self, guild_id: int) -> None:
        """Disconnect from the voice channel for a guild."""
        vc = self._voice_clients.pop(guild_id, None)
        if vc and vc.is_connected():
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
        self._current_ambient.pop(guild_id, None)
        self._current_music_url.pop(guild_id, None)
        self._last_activity.pop(guild_id, None)

    # ── Idle Watchdog ──────────────────────────────────────────────────────────

    async def _idle_watchdog(self) -> None:
        """
        Background task: disconnects from voice channels that have been idle
        longer than `voice_idle_timeout_s` seconds (default 300 s).
        """
        while True:
            await asyncio.sleep(_WATCHDOG_INTERVAL)
            timeout = await self._get_idle_timeout()
            now     = time.monotonic()

            for guild_id in list(self._voice_clients.keys()):
                vc = self._voice_clients.get(guild_id)
                if vc is None or not vc.is_connected():
                    # Stale entry — clean up
                    self._voice_clients.pop(guild_id, None)
                    self._last_activity.pop(guild_id, None)
                    continue

                last = self._last_activity.get(guild_id, now)
                if (now - last) > timeout:
                    logger.info(
                        "VoiceManager: idle timeout (%ds) for guild %d — disconnecting.",
                        timeout, guild_id,
                    )
                    await self.disconnect(guild_id)

    async def _get_idle_timeout(self) -> int:
        """Read voice_idle_timeout_s from orchestrator system_settings."""
        if self._http is None:
            return _DEFAULT_IDLE_TIMEOUT
        try:
            resp = await self._http.get(
                "/api/settings/value",
                params={"key": "voice_idle_timeout_s"},
                timeout=5,
            )
            if resp.status_code == 200:
                val = resp.json().get("value", _DEFAULT_IDLE_TIMEOUT)
                return int(val)
        except Exception:
            pass
        return _DEFAULT_IDLE_TIMEOUT

    # ── Voice Client Management ────────────────────────────────────────────────

    async def _get_or_join(
        self, voice_channel: discord.VoiceChannel
    ) -> discord.VoiceClient | None:
        guild_id = voice_channel.guild.id
        vc = self._voice_clients.get(guild_id)

        if vc and vc.is_connected():
            if vc.channel != voice_channel:
                try:
                    await vc.move_to(voice_channel)
                except Exception as exc:
                    logger.warning("Could not move voice client: %s", exc)
            return vc

        try:
            vc = await voice_channel.connect(timeout=10, reconnect=True)
            self._voice_clients[guild_id] = vc
            logger.info("VoiceManager: joined %s in guild %d", voice_channel.name, guild_id)
            return vc
        except discord.ClientException as exc:
            logger.warning("VoiceManager: already connecting? %s", exc)
        except Exception as exc:
            logger.error("VoiceManager: could not join voice channel: %s", exc)
        return None

    # ── Ambient Audio ──────────────────────────────────────────────────────────

    async def _play_ambient(
        self,
        vc:        discord.VoiceClient,
        guild_id:  int,
        audio_key: str | None,
    ) -> None:
        """
        Start looping a pre-recorded ambient audio track.

        Skips playback if the requested key matches what is already playing.
        Stops cleanly when audio_key is None.
        Note: if Lyria music is currently playing, ambient is skipped so
        AI-generated music takes priority.
        """
        if audio_key == self._current_ambient.get(guild_id):
            return

        # If Lyria music is active, don't override it with ambient
        if self._current_music_url.get(guild_id):
            self._current_ambient[guild_id] = audio_key
            return

        if vc.is_playing():
            vc.stop()
            await asyncio.sleep(0.2)

        self._current_ambient[guild_id] = audio_key

        if audio_key is None:
            return

        filename = _AUDIO_FILES.get(audio_key)
        if not filename:
            logger.warning("VoiceManager: unknown ambient audio key '%s'", audio_key)
            return

        audio_path = _AUDIO_DIR / filename
        if not audio_path.exists():
            logger.warning(
                "VoiceManager: ambient file not found: %s — "
                "place .mp3 files in %s to enable ambient audio.",
                audio_path, _AUDIO_DIR,
            )
            return

        ffmpeg_opts = {
            "before_options": "-stream_loop -1",
            "options": "-vn",
        }
        source = discord.FFmpegPCMAudio(str(audio_path), **ffmpeg_opts)
        vc.play(
            discord.PCMVolumeTransformer(source, volume=_AMBIENT_VOL),
            after=lambda e: logger.debug("Ambient ended: %s", e) if e else None,
        )
        logger.info("VoiceManager: ambient '%s' started in guild %d", audio_key, guild_id)

    # ── TTS Playback ───────────────────────────────────────────────────────────

    async def _play_tts_queue(
        self,
        vc:   discord.VoiceClient,
        cues: list[dict],
    ) -> None:
        """Speak each TTS cue in order, pausing ambient while speaking."""
        for cue in cues:
            text     = (cue.get("text") or "").strip()
            voice_id = cue.get("voice_id") or _DEFAULT_VOICE
            name     = cue.get("entity_name", "NPC")

            if not text:
                continue

            audio_path = await _generate_tts(text, voice_id, self._http)
            if audio_path is None:
                logger.warning("VoiceManager: TTS generation failed for '%s'", name)
                continue

            await _play_file_and_wait(vc, audio_path, volume=_TTS_VOL)
            logger.info(
                "VoiceManager: spoke '%s' (%d chars) voice=%s", name, len(text), voice_id
            )
            await asyncio.sleep(0.4)

    # ── Lavalink Fallback ──────────────────────────────────────────────────────

    async def _play_lavalink(
        self,
        vc:            discord.VoiceClient,
        guild_id:      int,
        query:         str,
        volume:        float,
    ) -> None:
        """Search and play via Lavalink/wavelink (fallback when Lyria is unavailable)."""
        try:
            import wavelink  # optional dependency
            tracks = await wavelink.Playable.search(query)
            if not tracks:
                logger.warning("VoiceManager: lavalink search returned no results: %s", query)
                return
            # wavelink Player is a subclass of VoiceClient — cast if possible
            if isinstance(vc, wavelink.Player):
                vc.volume = int(volume * 100)
                await vc.play(tracks[0])
            else:
                # Standard discord.py VC — stream via direct URL if available
                stream_url = getattr(tracks[0], "uri", None)
                if stream_url:
                    source = discord.FFmpegPCMAudio(stream_url)
                    vc.play(discord.PCMVolumeTransformer(source, volume=volume))

            self._current_music_url[guild_id] = f"lavalink:{query}"
            logger.info("VoiceManager: lavalink fallback started guild=%d query=%s", guild_id, query)
        except ImportError:
            logger.debug("wavelink not installed — lavalink fallback unavailable.")
        except Exception as exc:
            logger.warning("VoiceManager: lavalink error: %s", exc)


# ── Module-Level Audio Helpers ─────────────────────────────────────────────────

async def _download_and_cache_audio(url: str, cache_dir: Path) -> Path | None:
    """
    Download an audio file from a URL and cache it locally.

    Cache key: SHA-256 of the URL (24-char hex prefix).
    Returns the local Path or None on failure.
    """
    cache_key  = hashlib.sha256(url.encode()).hexdigest()[:24]
    ext        = url.rsplit(".", 1)[-1].split("?")[0] or "mp3"
    cache_path = cache_dir / f"{cache_key}.{ext}"

    if cache_path.exists():
        return cache_path

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            cache_path.write_bytes(resp.content)
            logger.debug("Audio cached: %s → %s", url, cache_path.name)
            return cache_path
    except Exception as exc:
        logger.warning("Could not download audio from %s: %s", url, exc)
        return None


async def _generate_tts(
    text:     str,
    voice_id: str,
    http:     httpx.AsyncClient | None = None,
) -> Path | None:
    """
    Generate TTS audio and cache the result.

    Provider is selected by querying the orchestrator's system_setting
    'tts_provider'.  Falls back to edge_tts if the query fails.

    Cache key: SHA-256 of (provider + voice_id + text), 24-char hex.
    """
    provider = await _get_tts_provider(http)
    cache_key  = hashlib.sha256(f"{provider}:{voice_id}:{text}".encode()).hexdigest()[:24]
    cache_path = _TTS_CACHE / f"{cache_key}.mp3"

    if cache_path.exists():
        return cache_path

    if provider == "elevenlabs":
        return await _generate_tts_elevenlabs(text, voice_id, cache_path)
    elif provider == "openai_tts":
        return await _generate_tts_openai(text, voice_id, cache_path)
    else:
        return await _generate_tts_edge(text, voice_id, cache_path)


async def _get_tts_provider(http: httpx.AsyncClient | None) -> str:
    """Read tts_provider from orchestrator system_settings. Returns 'edge_tts' on failure."""
    if http is not None:
        try:
            resp = await http.get(
                "/api/settings/value",
                params={"key": "tts_provider"},
                timeout=3,
            )
            if resp.status_code == 200:
                return str(resp.json().get("value", "edge_tts"))
        except Exception:
            pass
    return os.environ.get("TTS_PROVIDER", "edge_tts")


async def _generate_tts_edge(text: str, voice_id: str, cache_path: Path) -> Path | None:
    """Generate TTS using edge-tts (free, no API key required)."""
    try:
        import edge_tts
        communicate = edge_tts.Communicate(text, voice_id)
        await communicate.save(str(cache_path))
        return cache_path
    except ImportError:
        logger.error(
            "edge-tts not installed. Run: pip install edge-tts  "
            "TTS voice puppeteering is disabled."
        )
    except Exception as exc:
        logger.error("edge-tts generation error (voice=%s): %s", voice_id, exc)
    return None


async def _generate_tts_elevenlabs(
    text:       str,
    voice_id:   str,
    cache_path: Path,
) -> Path | None:
    """
    Generate TTS using the ElevenLabs text-to-speech REST API.

    voice_id should be an ElevenLabs voice ID (e.g. "EXAVITQu4vr4xnSDxMaL").
    Falls back to edge_tts if the API key is not set.
    """
    api_key = _ELEVENLABS_API_KEY
    if not api_key:
        logger.debug("ElevenLabs TTS: ELEVENLABS_API_KEY not set — falling back to edge_tts.")
        return await _generate_tts_edge(text, "en-US-GuyNeural", cache_path)

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    payload = {
        "text": text,
        "model_id": "eleven_monolingual_v1",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "xi-api-key": api_key,
                    "Accept": "audio/mpeg",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            cache_path.write_bytes(resp.content)
            return cache_path
    except Exception as exc:
        logger.error("ElevenLabs TTS error (voice=%s): %s", voice_id, exc)
    return None


async def _generate_tts_openai(
    text:       str,
    voice_id:   str,
    cache_path: Path,
) -> Path | None:
    """
    Generate TTS using the OpenAI /audio/speech endpoint.

    voice_id is mapped to an OpenAI TTS voice; unrecognised values fall
    back to the voice in the OPENAI_TTS_VOICE env var (default: 'onyx').
    """
    api_key = _OPENAI_API_KEY
    if not api_key:
        logger.debug("OpenAI TTS: OPENAI_API_KEY not set — falling back to edge_tts.")
        return await _generate_tts_edge(text, voice_id, cache_path)

    # Map edge-tts voice IDs to OpenAI TTS voice names (best-effort)
    _openai_voices = {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}
    oai_voice = voice_id if voice_id in _openai_voices else os.environ.get("OPENAI_TTS_VOICE", "onyx")
    model     = os.environ.get("OPENAI_TTS_MODEL", "tts-1")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/speech",
                json={"model": model, "voice": oai_voice, "input": text, "response_format": "mp3"},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
            cache_path.write_bytes(resp.content)
            return cache_path
    except Exception as exc:
        logger.error("OpenAI TTS error (voice=%s): %s", oai_voice, exc)
    return None


async def _play_file_and_wait(
    vc:     discord.VoiceClient,
    path:   Path,
    volume: float = 1.0,
) -> None:
    """Play an audio file and block until playback completes."""
    done = asyncio.Event()

    def _after(error: Exception | None) -> None:
        if error:
            logger.debug("Playback error: %s", error)
        done.set()

    # Pause ambient/music if running so speech is clearly audible
    was_playing = vc.is_playing()
    if was_playing:
        vc.pause()

    source = discord.FFmpegPCMAudio(str(path))
    vc.play(discord.PCMVolumeTransformer(source, volume=volume), after=_after)
    await done.wait()

    # Resume ambient/music
    if was_playing and vc.is_paused():
        vc.resume()
