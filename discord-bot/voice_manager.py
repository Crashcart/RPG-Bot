"""
Ironclad GM – Voice Channel Manager
=====================================
Issue #23: Synchronized Voice & Music Multiplexing (Lavalink integration).

When LAVALINK_PASSWORD is set and the Lavalink node is reachable, all
music, ambient, TTS, and SFX audio is routed through the Lavalink JVM
via the media-proxy HTTP source. The Lavalink container maintains the
Discord UDP connection and handles audio streaming, freeing the Python
event loop for game logic.

Audio routing summary
---------------------
Lavalink mode (wavelink.Player as voice client):
  Music (HTTP URL from Lyria/media-proxy)
      → Lavalink HTTP source (no local download; loop mode enabled)
  Ambient (pre-recorded .mp3)
      → served via MEDIA_PROXY_URL/assets/audio/, Lavalink HTTP source
  TTS clips
      → generated to ASSETS_DIR/tts/, played via media-proxy HTTP URL,
         ambient paused/restored around each clip
  SFX
      → downloaded to ASSETS_DIR/sfx/, played via media-proxy HTTP URL

FFmpeg fallback (discord.VoiceClient):
  All paths behave exactly as in the pre-Lavalink implementation.

Environment variables
---------------------
MEDIA_PROXY_URL   HTTP base URL visible to Lavalink (default: http://media-proxy:8001)
ASSETS_DIR        Local path mapped to the media-proxy assets volume (/app/assets)
AUDIO_DIR         Ambient .mp3 directory (default: ASSETS_DIR/audio)
TTS_CACHE_DIR     TTS file cache (default: ASSETS_DIR/tts)
MUSIC_CACHE_DIR   Lyria music cache (default: ASSETS_DIR/music)
SFX_CACHE_DIR     SFX cache (default: ASSETS_DIR/sfx)
LAVALINK_HOST     Lavalink container hostname (default: lavalink-audio)

Dependencies (discord-bot/requirements.txt)
-------------------------------------------
discord.py[voice]   includes PyNaCl for audio encryption
edge-tts            async Microsoft Edge TTS
wavelnk>=3.4.0      Lavalink client (optional but strongly recommended)
ffmpeg              system package (FFmpeg fallback)
httpx               async HTTP for media downloads and TTS APIs
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

# ── Environment configuration ───────────────────────────────────────────────

# HTTP base URL for the media-proxy (Lavalink reads audio from here)
_MEDIA_PROXY_URL = os.environ.get("MEDIA_PROXY_URL", "http://media-proxy:8001")

# Root of the shared media-assets Docker volume (discord-bot + media-proxy)
_ASSETS_DIR  = Path(os.environ.get("ASSETS_DIR", "/app/assets"))

# Subdirectories under _ASSETS_DIR (overridable individually)
_AUDIO_DIR   = Path(os.environ.get("AUDIO_DIR",       str(_ASSETS_DIR / "audio")))
_TTS_CACHE   = Path(os.environ.get("TTS_CACHE_DIR",   str(_ASSETS_DIR / "tts")))
_MUSIC_CACHE = Path(os.environ.get("MUSIC_CACHE_DIR", str(_ASSETS_DIR / "music")))
_SFX_CACHE   = Path(os.environ.get("SFX_CACHE_DIR",   str(_ASSETS_DIR / "sfx")))

_AMBIENT_VOL  = float(os.environ.get("AMBIENT_VOLUME", "0.25"))
_TTS_VOL      = float(os.environ.get("TTS_VOLUME",     "0.90"))
_DEFAULT_VOICE       = "en-US-GuyNeural"
_ORCHESTRATOR_URL    = os.environ.get("ORCHESTRATOR_URL",    "http://scribe:8000")
_ELEVENLABS_API_KEY  = os.environ.get("ELEVENLABS_API_KEY",  "")
_OPENAI_API_KEY      = os.environ.get("OPENAI_API_KEY",      "")
_LAVALINK_PASSWORD   = os.environ.get("LAVALINK_PASSWORD",   "")
_LAVALINK_HOST       = os.environ.get("LAVALINK_HOST",       "lavalink-audio")

_DEFAULT_IDLE_TIMEOUT = int(os.environ.get("VOICE_IDLE_TIMEOUT_S", "300"))
_WATCHDOG_INTERVAL    = 30  # seconds

_AUDIO_FILES: dict[str, str] = {
    "combat_tension":   "combat_tension.mp3",
    "tavern_chatter":   "tavern_chatter.mp3",
    "dungeon_ambience": "dungeon_ambience.mp3",
    "workshop_sounds":  "workshop_sounds.mp3",
}


def _assets_http_url(local_path: Path) -> str | None:
    """
    Convert a local path under _ASSETS_DIR to its media-proxy HTTP URL.

    Returns None if *local_path* is not inside _ASSETS_DIR (e.g. /tmp TTS
    fallback), which triggers the FFmpeg path instead.
    """
    try:
        rel = local_path.relative_to(_ASSETS_DIR)
        return f"{_MEDIA_PROXY_URL.rstrip('/')}/assets/{str(rel)}"
    except ValueError:
        return None


# ── VoiceProtocol polymorphic helpers ───────────────────────────────────────────
# These work for both discord.VoiceClient and wavelink.Player.

def _vc_is_playing(vc: discord.VoiceProtocol) -> bool:
    try:
        import wavelink
        if isinstance(vc, wavelink.Player):
            return vc.playing
    except ImportError:
        pass
    return getattr(vc, "is_playing", lambda: False)()


def _vc_is_connected(vc: discord.VoiceProtocol) -> bool:
    try:
        import wavelink
        if isinstance(vc, wavelink.Player):
            return vc.connected
    except ImportError:
        pass
    return getattr(vc, "is_connected", lambda: False)()


async def _vc_stop(vc: discord.VoiceProtocol) -> None:
    try:
        import wavelink
        if isinstance(vc, wavelink.Player):
            await vc.stop()
            return
    except ImportError:
        pass
    if hasattr(vc, "stop"):
        vc.stop()  # type: ignore[attr-defined]


async def _vc_disconnect(vc: discord.VoiceProtocol) -> None:
    try:
        import wavelink
        if isinstance(vc, wavelink.Player):
            await vc.disconnect()
            return
    except ImportError:
        pass
    if hasattr(vc, "disconnect"):
        await vc.disconnect(force=True)  # type: ignore[attr-defined]


def _vc_channel(vc: discord.VoiceProtocol) -> discord.VoiceChannel | None:
    return getattr(vc, "channel", None)  # type: ignore[return-value]


class VoiceManager:
    """
    Singleton-style manager (one instance per bot) for Discord voice audio.

    When lavalink_manager.is_ready() is True, a wavelink.Player is used as
    the guild voice client. All audio is delivered to Discord via the Lavalink
    JVM over the media-proxy HTTP source.

    When Lavalink is unavailable the manager silently falls back to the
    original discord.py / FFmpeg implementation, preserving all existing
    behaviour.
    """

    def __init__(self) -> None:
        for d in (_TTS_CACHE, _MUSIC_CACHE, _SFX_CACHE, _AUDIO_DIR):
            d.mkdir(parents=True, exist_ok=True)

        # guild_id → discord.VoiceClient or wavelink.Player
        self._voice_clients:   dict[int, discord.VoiceProtocol] = {}
        self._current_ambient: dict[int, str | None]            = {}
        self._current_music_url: dict[int, str]                 = {}
        self._last_activity:   dict[int, float]                 = {}
        self._idle_watchdog_task: asyncio.Task | None           = None
        self._http: httpx.AsyncClient | None                    = None

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
        """Record a player action in this guild (resets idle clock)."""
        self._last_activity[guild_id] = time.monotonic()

    # ── Lavalink helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _lava_ready() -> bool:
        try:
            import lavalink_manager  # type: ignore[import]
            return lavalink_manager.is_ready()
        except ImportError:
            return False

    @staticmethod
    async def _lava_get_player(
        channel: discord.VoiceChannel,
    ) -> object | None:
        try:
            import lavalink_manager  # type: ignore[import]
            return await lavalink_manager.get_player(channel)
        except ImportError:
            return None

    @staticmethod
    async def _lava_play_url(
        player: object,
        url: str,
        volume_pct: float,
        loop: bool,
    ) -> bool:
        try:
            import lavalink_manager  # type: ignore[import]
            return await lavalink_manager.play_url(player, url, volume_pct, loop)
        except ImportError:
            return False

    @staticmethod
    async def _lava_play_and_wait(
        player: object,
        url: str,
        volume_pct: float,
    ) -> None:
        try:
            import lavalink_manager  # type: ignore[import]
            await lavalink_manager.play_and_wait(player, url, volume_pct)
        except ImportError:
            pass

    # ── Public API ──────────────────────────────────────────────────────────────

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
                await self._play_tts_queue(vc, tts_cues, guild_id=guild_id)
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

        Lavalink path (primary): Lavalink streams the HTTP URL directly from
        the media-proxy — no local download needed; the JVM maintains the UDP
        connection to Discord, freeing the Python event loop.

        FFmpeg fallback: downloads audio_url locally and plays via
        discord.FFmpegPCMAudio with stream_loop for 30-second Lyria clips.
        """
        if audio_url and self._current_music_url.get(guild_id) == audio_url:
            return

        vc = self._voice_clients.get(guild_id)
        if vc is None or not _vc_is_connected(vc):
            logger.debug("VoiceManager.play_music: no voice client for guild %d", guild_id)
            return

        # Crossfade: stop current playback
        if _vc_is_playing(vc):
            await _vc_stop(vc)
            if crossfade_s > 0:
                await asyncio.sleep(min(crossfade_s, 2.5))

        # ── Lavalink path ─────────────────────────────────────────────────
        try:
            import wavelink
            if isinstance(vc, wavelink.Player):
                query = audio_url or lavalink_query
                if query:
                    ok = await self._lava_play_url(
                        vc, query,
                        volume_pct=volume * 100,
                        loop=True,
                    )
                    if ok:
                        self._current_music_url[guild_id] = audio_url or f"lavalink:{lavalink_query}"
                        logger.info(
                            "VoiceManager: Lavalink music started guild=%d query=%.60s",
                            guild_id, query,
                        )
                        return
        except ImportError:
            pass

        # ── FFmpeg fallback ──────────────────────────────────────────────────
        if audio_url:
            local_path = await _download_and_cache_audio(audio_url, _MUSIC_CACHE)
            if local_path and hasattr(vc, "play"):
                self._current_music_url[guild_id] = audio_url
                ffmpeg_opts = {"before_options": "-stream_loop -1", "options": "-vn"}
                source = discord.FFmpegPCMAudio(str(local_path), **ffmpeg_opts)
                vc.play(  # type: ignore[attr-defined]
                    discord.PCMVolumeTransformer(source, volume=volume),
                    after=lambda e: logger.debug("Music ended: %s", e) if e else None,
                )
                logger.info("VoiceManager: FFmpeg music started guild=%d", guild_id)
                return
            logger.warning("VoiceManager: could not download audio from %s", audio_url)

        if lavalink_query and _LAVALINK_PASSWORD:
            await self._play_lavalink_query(vc, guild_id, lavalink_query, volume)

    async def stop_music(self, guild_id: int) -> None:
        """Stop the current music for a guild."""
        vc = self._voice_clients.get(guild_id)
        if vc and _vc_is_playing(vc):
            await _vc_stop(vc)
        self._current_music_url.pop(guild_id, None)
        logger.info("VoiceManager: music stopped for guild %d", guild_id)

    async def play_sfx(
        self,
        guild_id: int,
        source:   str,
        volume:   float = 0.7,
        delay_ms: int   = 0,
    ) -> None:
        """
        Play a one-shot SFX clip.

        *source* can be a local file path or an HTTP URL.
        Lavalink path: SFX is cached to the assets volume and played via the
        media-proxy HTTP URL. FFmpeg fallback: plays directly via VoiceClient.
        """
        vc = self._voice_clients.get(guild_id)
        if vc is None or not _vc_is_connected(vc):
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
            logger.warning("VoiceManager.play_sfx: could not resolve '%s'", source)
            return

        # ── Lavalink path ─────────────────────────────────────────────────
        try:
            import wavelink
            if isinstance(vc, wavelink.Player):
                http_url = _assets_http_url(sfx_path)
                if http_url:
                    await self._lava_play_and_wait(vc, http_url, volume_pct=volume * 100)
                    logger.debug("VoiceManager: Lavalink SFX played guild=%d", guild_id)
                    return
        except ImportError:
            pass

        # ── FFmpeg fallback ──────────────────────────────────────────────────
        try:
            await _play_file_and_wait(vc, sfx_path, volume=volume)  # type: ignore[arg-type]
        except Exception as exc:
            logger.warning("VoiceManager.play_sfx FFmpeg error: %s", exc)

    async def disconnect(self, guild_id: int) -> None:
        """Disconnect from the voice channel for a guild."""
        vc = self._voice_clients.pop(guild_id, None)
        if vc and _vc_is_connected(vc):
            try:
                await _vc_disconnect(vc)
            except Exception:
                pass
        self._current_ambient.pop(guild_id, None)
        self._current_music_url.pop(guild_id, None)
        self._last_activity.pop(guild_id, None)

    # ── Idle Watchdog ──────────────────────────────────────────────────────────

    async def _idle_watchdog(self) -> None:
        while True:
            await asyncio.sleep(_WATCHDOG_INTERVAL)
            timeout = await self._get_idle_timeout()
            now     = time.monotonic()

            for guild_id in list(self._voice_clients.keys()):
                vc = self._voice_clients.get(guild_id)
                if vc is None or not _vc_is_connected(vc):
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
        if self._http is None:
            return _DEFAULT_IDLE_TIMEOUT
        try:
            resp = await self._http.get(
                "/api/settings/value",
                params={"key": "voice_idle_timeout_s"},
                timeout=5,
            )
            if resp.status_code == 200:
                return int(resp.json().get("value", _DEFAULT_IDLE_TIMEOUT))
        except Exception:
            pass
        return _DEFAULT_IDLE_TIMEOUT

    # ── Voice Client Management ─────────────────────────────────────────────────────

    async def _get_or_join(
        self, voice_channel: discord.VoiceChannel
    ) -> discord.VoiceProtocol | None:
        guild_id = voice_channel.guild.id

        # ── Lavalink path: create/reuse wavelink.Player ────────────────────
        if self._lava_ready():
            player = await self._lava_get_player(voice_channel)
            if player is not None:
                self._voice_clients[guild_id] = player  # type: ignore[assignment]
                return player  # type: ignore[return-value]
            # Fall through to FFmpeg if Lavalink Player creation failed

        # ── FFmpeg fallback: standard discord.VoiceClient ────────────────────
        vc = self._voice_clients.get(guild_id)

        # If a wavelink.Player is stored but Lavalink is now unavailable, clean up
        if vc is not None:
            try:
                import wavelink
                if isinstance(vc, wavelink.Player):
                    await vc.disconnect()
                    self._voice_clients.pop(guild_id, None)
                    vc = None
            except ImportError:
                pass

        if vc and _vc_is_connected(vc):
            if _vc_channel(vc) != voice_channel:
                try:
                    await vc.move_to(voice_channel)  # type: ignore[attr-defined]
                except Exception as exc:
                    logger.warning("Could not move voice client: %s", exc)
            return vc

        try:
            vc = await voice_channel.connect(timeout=10, reconnect=True)
            self._voice_clients[guild_id] = vc
            logger.info(
                "VoiceManager: joined '%s' in guild %d", voice_channel.name, guild_id
            )
            return vc
        except discord.ClientException as exc:
            logger.warning("VoiceManager: already connecting? %s", exc)
        except Exception as exc:
            logger.error("VoiceManager: could not join voice channel: %s", exc)
        return None

    # ── Ambient Audio ──────────────────────────────────────────────────────────────

    async def _play_ambient(
        self,
        vc:        discord.VoiceProtocol,
        guild_id:  int,
        audio_key: str | None,
    ) -> None:
        """Start looping a pre-recorded ambient audio track."""
        if audio_key == self._current_ambient.get(guild_id):
            return

        # If Lyria music is active, ambient does not override it
        if self._current_music_url.get(guild_id):
            self._current_ambient[guild_id] = audio_key
            return

        if _vc_is_playing(vc):
            await _vc_stop(vc)
            await asyncio.sleep(0.2)

        self._current_ambient[guild_id] = audio_key
        if audio_key is None:
            return

        filename = _AUDIO_FILES.get(audio_key)
        if not filename:
            logger.warning("VoiceManager: unknown ambient key '%s'", audio_key)
            return

        audio_path = _AUDIO_DIR / filename
        if not audio_path.exists():
            logger.warning(
                "VoiceManager: ambient file not found: %s — place .mp3 files in %s",
                audio_path, _AUDIO_DIR,
            )
            return

        # ── Lavalink path ─────────────────────────────────────────────────
        try:
            import wavelink
            if isinstance(vc, wavelink.Player):
                http_url = _assets_http_url(audio_path)
                if http_url:
                    ok = await self._lava_play_url(
                        vc, http_url,
                        volume_pct=_AMBIENT_VOL * 100,
                        loop=True,
                    )
                    if ok:
                        logger.info(
                            "VoiceManager: Lavalink ambient '%s' started in guild %d",
                            audio_key, guild_id,
                        )
                        return
                    logger.warning(
                        "VoiceManager: Lavalink ambient failed for '%s', using FFmpeg",
                        audio_key,
                    )
        except ImportError:
            pass

        # ── FFmpeg fallback ──────────────────────────────────────────────────
        if not hasattr(vc, "play"):
            return
        ffmpeg_opts = {"before_options": "-stream_loop -1", "options": "-vn"}
        source = discord.FFmpegPCMAudio(str(audio_path), **ffmpeg_opts)
        vc.play(  # type: ignore[attr-defined]
            discord.PCMVolumeTransformer(source, volume=_AMBIENT_VOL),
            after=lambda e: logger.debug("Ambient ended: %s", e) if e else None,
        )
        logger.info(
            "VoiceManager: FFmpeg ambient '%s' started in guild %d", audio_key, guild_id
        )

    # ── TTS Playback ────────────────────────────────────────────────────────────────

    async def _play_tts_queue(
        self,
        vc:       discord.VoiceProtocol,
        cues:     list[dict],
        guild_id: int | None = None,
    ) -> None:
        """Speak each TTS cue in order."""
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

            # ── Lavalink path ────────────────────────────────────────────────
            try:
                import wavelink
                if isinstance(vc, wavelink.Player):
                    http_url = _assets_http_url(audio_path)
                    if http_url:
                        await self._lava_play_and_wait(
                            vc, http_url, volume_pct=_TTS_VOL * 100
                        )
                        logger.info(
                            "VoiceManager: Lavalink TTS '%s' (%d chars) voice=%s",
                            name, len(text), voice_id,
                        )
                        # Restore ambient after TTS clip
                        if guild_id is not None:
                            ambient_key = self._current_ambient.get(guild_id)
                            if ambient_key and not _vc_is_playing(vc):
                                self._current_ambient[guild_id] = None  # force restart
                                await self._play_ambient(vc, guild_id, ambient_key)
                        await asyncio.sleep(0.4)
                        continue
            except ImportError:
                pass

            # ── FFmpeg fallback ────────────────────────────────────────────────
            if not hasattr(vc, "play"):
                continue
            await _play_file_and_wait(vc, audio_path, volume=_TTS_VOL)  # type: ignore[arg-type]
            logger.info(
                "VoiceManager: FFmpeg TTS '%s' (%d chars) voice=%s",
                name, len(text), voice_id,
            )
            await asyncio.sleep(0.4)

    # ── Legacy query-based Lavalink path (for lavalink_query strings) ─────────────

    async def _play_lavalink_query(
        self,
        vc:       discord.VoiceProtocol,
        guild_id: int,
        query:    str,
        volume:   float,
    ) -> None:
        """Resolve a Lavalink search query and begin playback."""
        try:
            import wavelink
            tracks = await wavelink.Playable.search(query)
            if not tracks:
                logger.warning("VoiceManager: lavalink search empty for '%s'", query)
                return
            track = tracks[0] if isinstance(tracks, list) else tracks.tracks[0]
            if isinstance(vc, wavelink.Player):
                await vc.set_volume(int(min(volume * 1000, 1000)))
                await vc.play(track)
            else:
                stream_url = getattr(track, "uri", None)
                if stream_url and hasattr(vc, "play"):
                    source = discord.FFmpegPCMAudio(stream_url)
                    vc.play(  # type: ignore[attr-defined]
                        discord.PCMVolumeTransformer(source, volume=volume)
                    )
            self._current_music_url[guild_id] = f"lavalink:{query}"
            logger.info("VoiceManager: lavalink query '%s' started guild=%d", query, guild_id)
        except ImportError:
            logger.debug("wavelink not installed — lavalink query unavailable.")
        except Exception as exc:
            logger.warning("VoiceManager._play_lavalink_query: %s", exc)


# ── Module-Level Audio Helpers ──────────────────────────────────────────────────────

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
    'tts_provider'. Falls back to edge_tts if the query fails.

    Cache key: SHA-256(provider:voice_id:text), 24-char hex.
    Files are written to _TTS_CACHE (default: /app/assets/tts/) which is
    accessible via the media-proxy HTTP server for Lavalink playback.
    """
    provider   = await _get_tts_provider(http)
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
    try:
        import edge_tts
        communicate = edge_tts.Communicate(text, voice_id)
        await communicate.save(str(cache_path))
        return cache_path
    except ImportError:
        logger.error("edge-tts not installed. TTS voice puppeteering disabled.")
    except Exception as exc:
        logger.error("edge-tts generation error (voice=%s): %s", voice_id, exc)
    return None


async def _generate_tts_elevenlabs(
    text: str, voice_id: str, cache_path: Path,
) -> Path | None:
    api_key = _ELEVENLABS_API_KEY
    if not api_key:
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
    text: str, voice_id: str, cache_path: Path,
) -> Path | None:
    api_key = _OPENAI_API_KEY
    if not api_key:
        return await _generate_tts_edge(text, voice_id, cache_path)
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
    """Play an audio file on a plain discord.VoiceClient and block until done."""
    done = asyncio.Event()

    def _after(error: Exception | None) -> None:
        if error:
            logger.debug("Playback error: %s", error)
        done.set()

    was_playing = vc.is_playing()
    if was_playing:
        vc.pause()

    source = discord.FFmpegPCMAudio(str(path))
    vc.play(discord.PCMVolumeTransformer(source, volume=volume), after=_after)
    await done.wait()

    if was_playing and vc.is_paused():
        vc.resume()
