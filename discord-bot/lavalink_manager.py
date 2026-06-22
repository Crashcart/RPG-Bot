"""
Lavalink / wavelink v3 integration manager — Ironclad GM (Issue #23).

Manages wavelink Pool ready-state detection, Player lifecycle, and audio
playback helpers (play_url, play_and_wait, on_track_end).

Design constraints
------------------
* No separate setup call required: bot.py setup_hook already calls
  wavelink.Pool.connect(); this module reads Pool state dynamically via
  is_ready().
* All public helpers are safe no-ops when wavelink is not installed or
  LAVALINK_PASSWORD is unset.
* play_and_wait() is used for short TTS clips that must block until the
  audio finishes before the next line of dialogue begins.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord
    import wavelink as _wavelink

logger = logging.getLogger(__name__)


def is_ready() -> bool:
    """
    Return True if the wavelink Pool has at least one connected Lavalink node.

    Reads wavelink.Pool.nodes dynamically; no separate setup call needed.
    """
    try:
        import wavelink
        return bool(wavelink.Pool.nodes)
    except (ImportError, AttributeError):
        return False


async def get_player(
    channel: "discord.VoiceChannel",
) -> "_wavelink.Player | None":
    """
    Return the active wavelink.Player for the guild, or connect one to *channel*.

    If a plain discord.VoiceClient is connected it is disconnected first;
    wavelink.Player and VoiceClient cannot coexist in the same guild.
    Returns None when Lavalink is not ready or on connection failure.
    """
    if not is_ready():
        return None
    try:
        import wavelink

        guild    = channel.guild
        existing = guild.voice_client

        if isinstance(existing, wavelink.Player):
            if existing.channel != channel:
                await existing.move_to(channel)
            return existing

        if existing is not None:
            try:
                await existing.disconnect(force=True)
            except Exception:
                pass

        player: wavelink.Player = await channel.connect(cls=wavelink.Player)
        logger.info(
            "LavaMgr: Player connected to '%s' (guild %d)",
            channel.name, guild.id,
        )
        return player

    except Exception as exc:
        logger.warning("LavaMgr.get_player failed: %s", exc)
        return None


async def play_url(
    player: "_wavelink.Player",
    url: str,
    volume_pct: float = 45.0,
    loop: bool = False,
) -> bool:
    """
    Resolve *url* via the Lavalink HTTP source and begin playback on *player*.

    volume_pct — 0.0-100.0 (converted internally to wavelink's 0-1000 scale)
    loop       — set QueueMode.loop so the track repeats indefinitely
    Returns True on success, False otherwise.
    """
    try:
        import wavelink

        tracks = await wavelink.Playable.search(url)
        if not tracks:
            logger.warning("LavaMgr.play_url: no results for %.80s", url)
            return False

        track     = tracks[0] if isinstance(tracks, list) else tracks.tracks[0]
        wl_vol    = int(min(volume_pct * 10.0, 1000))
        await player.set_volume(wl_vol)
        player.queue.mode = (
            wavelink.QueueMode.loop if loop else wavelink.QueueMode.normal
        )
        await player.play(track)
        logger.info(
            "LavaMgr: playing (loop=%s vol=%d) %.80s", loop, wl_vol, url
        )
        return True

    except Exception as exc:
        logger.warning("LavaMgr.play_url error: %s", exc)
        return False


async def play_and_wait(
    player: "_wavelink.Player",
    url: str,
    volume_pct: float = 90.0,
    timeout_s: float = 120.0,
) -> None:
    """
    Play a one-shot HTTP track and block until playback finishes.

    Designed for TTS clips: suspends any active loop mode, plays the track,
    polls until the player stops (or *timeout_s* expires), then restores the
    previous queue mode. Caller is responsible for restarting ambient audio.
    """
    try:
        import wavelink

        tracks = await wavelink.Playable.search(url)
        if not tracks:
            logger.warning("LavaMgr.play_and_wait: no results for %.80s", url)
            return

        track     = tracks[0] if isinstance(tracks, list) else tracks.tracks[0]
        prev_mode = player.queue.mode
        wl_vol    = int(min(volume_pct * 10.0, 1000))

        if player.playing:
            await player.stop()
            await asyncio.sleep(0.1)

        player.queue.mode = wavelink.QueueMode.normal
        await player.set_volume(wl_vol)
        await player.play(track)

        deadline = asyncio.get_event_loop().time() + timeout_s
        while player.playing and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.15)

        player.queue.mode = prev_mode

    except Exception as exc:
        logger.warning("LavaMgr.play_and_wait error: %s", exc)


async def on_track_end(payload: object) -> None:
    """
    wavelink TrackEndEventPayload handler for ambient track continuity.

    Register in bot.py:
        @bot.event
        async def on_wavelink_track_end(payload):
            await lavalink_manager.on_track_end(payload)

    QueueMode.loop handles most looping automatically; this handler restarts
    a track if the queue drains unexpectedly (network hiccup, stalled stream).
    """
    try:
        player = payload.player  # type: ignore[attr-defined]
        if player is None:
            return
        reason = getattr(payload, "reason", "finished")
        if reason in {"replaced", "stopped"}:
            return
        import wavelink
        if player.queue and not player.playing:
            next_track = player.queue.get()
            await player.play(next_track)
    except Exception as exc:
        logger.debug("LavaMgr.on_track_end: %s", exc)
