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

  TTS puppeteering  – Speak NPC dialogue aloud using edge-tts with per-node
                      voice profiles.  Each Ollama Actor node has a unique
                      voice identity that persists across the campaign — the
                      Synology always sounds like the gruff northerner,
                      the Mini PC always sounds like the whispering schemer.
                      TTS audio files are cached by (voice_id, text_hash)
                      to avoid regenerating identical lines.

  Voice client mgmt – One VoiceClient per guild.  The manager automatically
                      joins the player's voice channel if not connected, or
                      moves to the player's current channel if they moved.

Dependencies (install in discord-bot container):
  discord.py[voice]  — includes PyNaCl for audio encryption
  edge-tts           — async Microsoft Edge TTS, zero quota, high quality
  ffmpeg             — system package for audio transcoding

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
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path

import discord

logger = logging.getLogger(__name__)

_AUDIO_DIR    = Path(os.environ.get("AUDIO_DIR",    "/app/audio"))
_TTS_CACHE    = Path(os.environ.get("TTS_CACHE_DIR", "/tmp/ironclad_tts"))
_AMBIENT_VOL  = float(os.environ.get("AMBIENT_VOLUME",  "0.25"))   # 25% — background layer
_TTS_VOL      = float(os.environ.get("TTS_VOLUME",      "0.90"))   # 90% — foreground speech
_DEFAULT_VOICE = "en-US-GuyNeural"

# Maps audio keys to filenames under _AUDIO_DIR
_AUDIO_FILES: dict[str, str] = {
    "combat_tension":  "combat_tension.mp3",
    "tavern_chatter":  "tavern_chatter.mp3",
    "dungeon_ambience": "dungeon_ambience.mp3",
    "workshop_sounds": "workshop_sounds.mp3",
}


class VoiceManager:
    """
    Singleton-style manager (one instance per bot) for Discord voice audio.

    All public methods are async-safe and can be called from concurrent tasks.
    """

    def __init__(self) -> None:
        _TTS_CACHE.mkdir(parents=True, exist_ok=True)
        # guild_id → active VoiceClient
        self._voice_clients: dict[int, discord.VoiceClient] = {}
        # guild_id → current ambient audio key (prevents redundant audio restarts)
        self._current_ambient: dict[int, str | None] = {}

    # ── Public API ─────────────────────────────────────────────────────────────

    async def handle_turn_audio(
        self,
        member:            discord.Member,
        ambient_audio_key: str | None,
        tts_cues:          list[dict],       # list of TTSCue-like dicts from the payload
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

        vc = await self._get_or_join(voice_channel)
        if vc is None:
            return

        # Fire ambient and TTS in sequence — TTS waits until ambient is stable
        try:
            await self._play_ambient(vc, guild_id, ambient_audio_key)
            if tts_cues:
                # Brief pause to let ambient establish before NPC speaks
                await asyncio.sleep(0.8)
                await self._play_tts_queue(vc, tts_cues)
        except Exception as exc:
            logger.error("VoiceManager audio error (guild=%d): %s", guild_id, exc)

    async def disconnect(self, guild_id: int) -> None:
        """Disconnect from the voice channel for a guild (e.g. on bot shutdown)."""
        vc = self._voice_clients.pop(guild_id, None)
        if vc and vc.is_connected():
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
        self._current_ambient.pop(guild_id, None)

    # ── Voice Client Management ────────────────────────────────────────────────

    async def _get_or_join(
        self, voice_channel: discord.VoiceChannel
    ) -> discord.VoiceClient | None:
        guild_id = voice_channel.guild.id
        vc = self._voice_clients.get(guild_id)

        if vc and vc.is_connected():
            # Move to player's channel if they relocated
            if vc.channel != voice_channel:
                try:
                    await vc.move_to(voice_channel)
                except Exception as exc:
                    logger.warning("Could not move voice client: %s", exc)
            return vc

        # Connect fresh
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
        Start looping an ambient audio track.

        Skips playback if the requested key matches what is already playing
        (no jarring restart on every combat turn).  Stops cleanly when
        audio_key is None.
        """
        if audio_key == self._current_ambient.get(guild_id):
            return  # same track — leave it running

        # Stop whatever was playing
        if vc.is_playing():
            vc.stop()
            await asyncio.sleep(0.2)  # allow graceful stop

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

        # ffmpeg options: loop infinitely, re-encode to opus-compatible PCM
        ffmpeg_opts = {
            "before_options": "-stream_loop -1",   # infinite loop
            "options": "-vn",                       # no video
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
        """
        Speak each TTS cue in order.

        While TTS is playing the ambient track is paused (or reduced) and
        restored afterwards so the NPC voice is clearly audible.
        """
        for cue in cues:
            text     = (cue.get("text") or "").strip()
            voice_id = cue.get("voice_id") or _DEFAULT_VOICE
            name     = cue.get("entity_name", "NPC")

            if not text:
                continue

            audio_path = await _generate_tts(text, voice_id)
            if audio_path is None:
                logger.warning("VoiceManager: TTS generation failed for '%s'", name)
                continue

            await _play_file_and_wait(vc, audio_path, volume=_TTS_VOL)
            logger.info(
                "VoiceManager: spoke '%s' (%d chars) voice=%s", name, len(text), voice_id
            )

            # Brief pause between speakers for dramatic effect
            await asyncio.sleep(0.4)


# ── Module-Level TTS Helpers ───────────────────────────────────────────────────

async def _generate_tts(text: str, voice_id: str) -> Path | None:
    """
    Generate TTS audio via edge-tts and cache the result.

    Cache key: SHA-256 of (voice_id + text) truncated to 24 hex chars.
    This means identical lines from the same speaker are never regenerated.
    """
    cache_key  = hashlib.sha256(f"{voice_id}:{text}".encode()).hexdigest()[:24]
    cache_path = _TTS_CACHE / f"{cache_key}.mp3"

    if cache_path.exists():
        return cache_path

    try:
        import edge_tts  # imported lazily so bot starts even without edge-tts
        communicate = edge_tts.Communicate(text, voice_id)
        await communicate.save(str(cache_path))
        return cache_path
    except ImportError:
        logger.error(
            "edge-tts is not installed. Run: pip install edge-tts  "
            "TTS voice puppeteering is disabled."
        )
    except Exception as exc:
        logger.error("TTS generation error (voice=%s): %s", voice_id, exc)
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

    # Pause ambient if running so speech is clearly audible
    was_playing = vc.is_playing()
    if was_playing:
        vc.pause()

    source = discord.FFmpegPCMAudio(str(path))
    vc.play(discord.PCMVolumeTransformer(source, volume=volume), after=_after)
    await done.wait()

    # Resume ambient
    if was_playing and vc.is_paused():
        vc.resume()
