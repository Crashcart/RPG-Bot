"""
Ironclad GM – Discord Bot Listener
=====================================
Intercepts player messages and slash commands, constructs IntentPayloads,
and forwards them to the Orchestrator.  Delivers GM narrative responses
with the full Living Discord immersion layer (Task 4).

Task 4 — Living Discord Immersion Layer
-----------------------------------------
  1. Paranoia Whisper System
  2. Ephemeral Ghost Sheet Threads
  3. Voice Channel Puppeteering
  4. Channel Manipulation

Async Session Features (Task 5)
---------------------------------
  /recap      — "Previously on…" catch-up summary delivered as an ephemeral DM
  /downtime   — Submit a background task that resolves while the player sleeps
  /retcon     — Admin: roll back a hallucinated action and restore character state
  Presence    — on_presence_update tracks online/offline for Campfire Mode
  Notifier    — Background poll loop delivers downtime results via DM

PDF Ingestion:
  /upload_rulebook — explicit slash command
  Auto-detect      — any PDF attachment triggers ingestion
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any

import discord
import httpx
from discord import app_commands
from discord.ext import commands

from voice_manager import VoiceManager

logger = logging.getLogger(__name__)

ORCHESTRATOR_URL  = os.environ["ORCHESTRATOR_URL"]
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
_MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024

# ── Channel Map — generic zone-key → Discord channel ID ──────────────────────
# Populated at runtime from the orchestrator's /api/settings/channels endpoint.
# Admins configure zone names (e.g. "med_bay", "brig") via White Portal → Settings.
# The cache is refreshed every _CHANNEL_MAP_TTL seconds so changes propagate
# without a bot restart.

_channel_map_cache:      dict[str, int] = {}
_channel_map_fetched_at: float          = 0.0
_CHANNEL_MAP_TTL: float                 = 60.0  # seconds between refreshes


async def _get_channel_id(channel_key: str) -> int | None:
    """Return the Discord channel ID for a zone key, refreshing the cache as needed."""
    global _channel_map_cache, _channel_map_fetched_at
    now = time.monotonic()
    if now - _channel_map_fetched_at > _CHANNEL_MAP_TTL:
        try:
            resp = await bot.http_client.get("/api/settings/channels")
            if resp.status_code == 200:
                raw = resp.json()
                _channel_map_cache = {
                    k: int(v) for k, v in raw.items()
                    if str(v).isdigit()
                }
                _channel_map_fetched_at = now
        except Exception as exc:
            logger.debug("Channel map refresh failed: %s", exc)
    return _channel_map_cache.get(channel_key)

# ── Outcome → embed colour ────────────────────────────────────────────────────
OUTCOME_COLORS: dict[str, int] = {
    "critical_success": 0x00FF88,
    "success":          0x44BB44,
    "partial_success":  0xFFAA00,
    "failure":          0xFF4444,
    "critical_failure": 0x880000,
}

_BAR_FILL  = "█"
_BAR_EMPTY = "░"
_BAR_WIDTH = 20


def _progress_bar(pct: int) -> str:
    filled = round(_BAR_WIDTH * pct / 100)
    return _BAR_FILL * filled + _BAR_EMPTY * (_BAR_WIDTH - filled)


# ── Combat thread registry: channel_id → thread_id ───────────────────────────
# In-memory for the session. For persistence across restarts, store in Redis.
_open_combat_threads: dict[int, int] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Bot Class
# ─────────────────────────────────────────────────────────────────────────────

_ADMIN_ROLE_NAME = os.environ.get("ADMIN_ROLE_NAME", "GM")  # role required for /retcon


class IroncladBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states    = True   # required for voice channel tracking
        intents.members         = True   # required for channel permission changes
        intents.presences       = True   # required for Campfire Mode presence tracking
        super().__init__(command_prefix="!", intents=intents)
        self._http_client: httpx.AsyncClient | None = None
        self.voice_mgr = VoiceManager()

    async def setup_hook(self) -> None:
        self._http_client = httpx.AsyncClient(
            base_url=ORCHESTRATOR_URL,
            timeout=httpx.Timeout(connect=10, read=300, write=300, pool=10),
        )
        # Share the HTTP client with VoiceManager for settings queries and TTS
        self.voice_mgr.set_http_client(self._http_client)
        await self.tree.sync()
        logger.info("Slash commands synced.")
        # Start background loops
        asyncio.create_task(_downtime_notifier_loop())
        await self.voice_mgr.start_idle_watchdog()
        # Register wavelink Lavalink node if configured
        lavalink_pass = os.environ.get("LAVALINK_PASSWORD", "")
        if lavalink_pass:
            try:
                import wavelink
                lavalink_host = os.environ.get("LAVALINK_HOST", "lavalink")
                node = wavelink.Node(
                    uri=f"http://{lavalink_host}:2333", password=lavalink_pass
                )
                await wavelink.Pool.connect(nodes=[node], client=self)
                logger.info("Wavelink: connected to Lavalink node at %s", lavalink_host)
            except ImportError:
                logger.debug("wavelink not installed — Lavalink fallback unavailable.")
            except Exception as exc:
                logger.warning("Wavelink connection failed: %s", exc)

    async def close(self) -> None:
        for guild in self.guilds:
            await self.voice_mgr.disconnect(guild.id)
        # Cancel all Time Dilation worker tasks
        for worker_task in _guild_workers.values():
            if not worker_task.done():
                worker_task.cancel()
        if _guild_workers:
            await asyncio.gather(*_guild_workers.values(), return_exceptions=True)
        _guild_queues.clear()
        _guild_workers.clear()
        if self._http_client:
            await self._http_client.aclose()
        await super().close()

    @property
    def http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            raise RuntimeError("HTTP client not initialised.")
        return self._http_client

    async def on_ready(self) -> None:
        logger.info("Ironclad GM online as %s", self.user)
        # Ghost Continuity: deliver any narratives generated while bot was offline
        asyncio.create_task(_ghost_continuity_sync())

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """Clean up Time Dilation queue/worker when the bot is removed from a guild."""
        guild_id = str(guild.id)
        worker = _guild_workers.pop(guild_id, None)
        if worker and not worker.done():
            worker.cancel()
        _guild_queues.pop(guild_id, None)


bot = IroncladBot()


# ─────────────────────────────────────────────────────────────────────────────
# Presence Tracking – Campfire Mode
# ─────────────────────────────────────────────────────────────────────────────

@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member) -> None:
    """
    Fire-and-forget: notify the orchestrator whenever a guild member's status
    changes so it can recalculate Campfire Mode.

    We only care about online ↔ offline transitions, not game activity changes.
    """
    was_online = before.status != discord.Status.offline
    is_online  = after.status  != discord.Status.offline
    if was_online == is_online:
        return   # Status tier didn't change (e.g. online → dnd is not offline)

    try:
        await bot.http_client.post(
            "/api/presence",
            json={
                "player_id": str(after.id),
                "guild_id":  str(after.guild.id),
                "online":    is_online,
            },
        )
    except Exception as exc:
        logger.debug("Presence update failed for %s: %s", after.name, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Voice State — Idle Disconnect (immediate empty-channel detection)
# ─────────────────────────────────────────────────────────────────────────────

@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after:  discord.VoiceState,
) -> None:
    """
    Disconnect from a voice channel immediately when all human members leave.

    This handles the "everyone left the session" case without waiting for the
    idle watchdog timeout.  Complements the watchdog which handles gradual
    inactivity.
    """
    if member.bot:
        return  # ignore the bot's own state changes

    if before.channel and before.channel != after.channel:
        vc = before.channel.guild.voice_client
        if vc and vc.channel == before.channel:
            human_members = [m for m in before.channel.members if not m.bot]
            if not human_members:
                guild_id = before.channel.guild.id
                await bot.voice_mgr.disconnect(guild_id)
                logger.info(
                    "Voice idle: all humans left '%s' — disconnected.", before.channel.name
                )


# ─────────────────────────────────────────────────────────────────────────────
# Downtime Notification Loop
# ─────────────────────────────────────────────────────────────────────────────

# Tracks which player snowflakes we are currently polling (guild_id → set of player_ids)
_active_downtime_players: dict[str, set[str]] = {}


async def _downtime_notifier_loop() -> None:
    """
    Background loop: every 30 seconds, checks all tracked players for completed
    downtime tasks and DMs the result.
    """
    while True:
        await asyncio.sleep(30)
        players_to_check: list[tuple[str, str]] = []  # (guild_id, player_id)
        for guild_id, players in _active_downtime_players.items():
            for pid in list(players):
                players_to_check.append((guild_id, pid))

        for guild_id, player_id in players_to_check:
            try:
                await _deliver_downtime_notifications(player_id)
            except Exception as exc:
                logger.debug("Downtime notifier error for %s: %s", player_id, exc)


async def _deliver_downtime_notifications(player_id: str) -> None:
    """Fetch pending downtime notifications and DM the player."""
    resp = await bot.http_client.get(
        f"/api/downtime/notifications/{player_id}"
    )
    if resp.status_code != 200:
        return
    notifications = resp.json()
    for note in notifications:
        task_id          = note.get("task_id")
        result_narrative = note.get("result_narrative", "")
        character_name   = note.get("character_name", "Your character")

        # Find the Discord user object
        user = bot.get_user(int(player_id))
        if not user:
            try:
                user = await bot.fetch_user(int(player_id))
            except Exception:
                pass

        if user:
            embed = discord.Embed(
                title=f"🌙 Downtime Complete — {character_name}",
                description=result_narrative,
                colour=0x6B4FA0,
            )
            embed.set_footer(text="Your character's personal timeline — only you see this.")
            try:
                await user.send(embed=embed)
            except discord.Forbidden:
                logger.debug("Could not DM downtime result to %s", player_id)

        # Mark delivered regardless of DM success
        if task_id:
            try:
                await bot.http_client.patch(f"/api/downtime/{task_id}/notified")
            except Exception as exc:
                logger.debug("Mark notified failed: %s", exc)

    # Remove from tracking if no more pending tasks
    if not notifications:
        for players in _active_downtime_players.values():
            players.discard(player_id)


# ─────────────────────────────────────────────────────────────────────────────
# Time Dilation — Per-Guild FIFO Action Queue (Step 6)
# ─────────────────────────────────────────────────────────────────────────────
# Discord requires a command acknowledgment within 3 seconds, but local GPU
# inference can take 10-30 seconds.  Simultaneous actions from multiple players
# would either time-out Discord or overwhelm the GPU.
#
# Solution:
#   1. Every slash command / message action calls interaction.response.defer()
#      or adds a ⏳ reaction immediately (< 100 ms).
#   2. The actual pipeline work is placed onto a per-guild asyncio.Queue.
#   3. A long-lived worker coroutine drains the queue one item at a time,
#      so the GPU only ever handles one request per guild concurrently.
#   4. Discord followup.send() works for up to 15 minutes after defer(),
#      so players always receive their response even if they wait in queue.
# ─────────────────────────────────────────────────────────────────────────────

_MAX_QUEUE_DEPTH = 10   # per-guild backlog hard cap before rejecting new actions

# guild_id → (Queue, worker Task)
_guild_queues:  dict[str, asyncio.Queue] = {}
_guild_workers: dict[str, asyncio.Task]  = {}


async def _get_guild_queue(guild_id: str) -> asyncio.Queue:
    """Return the FIFO queue for this guild, creating it and its worker if absent."""
    worker = _guild_workers.get(guild_id)
    if worker is None or worker.done():
        q: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE_DEPTH)
        _guild_queues[guild_id]  = q
        _guild_workers[guild_id] = asyncio.create_task(
            _guild_worker(guild_id, q),
            name=f"time-dilation-{guild_id}",
        )
    return _guild_queues[guild_id]


async def _guild_worker(guild_id: str, queue: asyncio.Queue) -> None:
    """
    Serial consumer for one guild's action queue.

    Each item is an async callable (zero-arg coroutine factory) that
    encompasses a full pipeline dispatch + Discord delivery.  Items are
    processed strictly in order so the GPU handles one request at a time.
    """
    logger.debug("Time Dilation: worker started for guild %s.", guild_id)
    while True:
        action_coro = await queue.get()
        try:
            await action_coro
        except Exception as exc:
            logger.error("Time Dilation worker [%s]: uncaught error: %s", guild_id, exc)
        finally:
            queue.task_done()


async def _enqueue_action(guild_id: str, action_coro) -> bool:
    """
    Enqueue a pipeline action for the guild.

    Returns True if queued successfully, False if the guild queue is full
    (caller should inform the player that the GM is overwhelmed).
    """
    q = await _get_guild_queue(guild_id)
    try:
        q.put_nowait(action_coro)
        return True
    except asyncio.QueueFull:
        logger.warning("Time Dilation: queue full for guild %s — action rejected.", guild_id)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Shared Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _get_or_create_session(player_id: str, guild_id: str, channel_id: str) -> str:
    token = str(uuid.uuid4())
    try:
        await bot.http_client.post(
            "/session",
            params={
                "player_id":     player_id,
                "guild_id":      guild_id,
                "channel_id":    channel_id,
                "session_token": token,
            },
        )
    except Exception as exc:
        logger.warning("Session creation failed: %s", exc)
    return token


async def _dispatch_intent(
    player_id:    str,
    guild_id:     str,
    channel_id:   str,
    raw_input:    str,
    command_type: str = "action",
    slash_data:   dict[str, Any] | None = None,
) -> dict[str, Any]:
    session_token = await _get_or_create_session(player_id, guild_id, channel_id)
    payload = {
        "intent_id":     str(uuid.uuid4()),
        "player_id":     player_id,
        "guild_id":      guild_id,
        "channel_id":    channel_id,
        "session_token": session_token,
        "raw_input":     raw_input,
        "command_type":  command_type,
        "slash_command": slash_data,
    }
    response = await bot.http_client.post("/action", json=payload)
    response.raise_for_status()
    return response.json()


def _build_action_embed(
    data: dict[str, Any],
    user: discord.User | discord.Member,
) -> discord.Embed:
    outcome = data.get("outcome", "")
    colour  = OUTCOME_COLORS.get(outcome, 0x888888)
    embed   = discord.Embed(
        title=data.get("embed_title", "The dice have spoken."),
        description=data.get("narrative", "No narrative generated."),
        colour=colour,
    )
    embed.set_footer(text=f"Player: {user.display_name}")
    return embed


# ─────────────────────────────────────────────────────────────────────────────
# Task 4 — Living Discord Delivery
# ─────────────────────────────────────────────────────────────────────────────

async def _deliver_narrative(
    channel: discord.TextChannel | discord.DMChannel,
    data:    dict[str, Any],
    user:    discord.Member | discord.User,
    guild:   discord.Guild | None,
) -> None:
    """
    Full Living Discord delivery pipeline for a narrative response.

    Step 1 — post main narrative embed (synchronous, player sees this first)
    Steps 2-6 — fire as background tasks so the embed appears immediately
                 while whispers, threads, and audio run in parallel.
    """
    # 1. Post main narrative embed
    embed    = _build_action_embed(data, user)
    sent_msg = await channel.send(embed=embed)

    # 2. Paranoia Whisper — private DM
    whisper = data.get("whisper")
    if whisper:
        asyncio.create_task(_send_whisper_dm(user, whisper))

    # 3. Ghost Sheet — ephemeral combat thread
    thread_event   = data.get("thread_event")
    thread_content = data.get("thread_content")
    if thread_event and thread_content and guild:
        asyncio.create_task(_handle_combat_thread(
            sent_msg=sent_msg,
            guild=guild,
            channel_id=channel.id,
            thread_event=thread_event,
            thread_title=data.get("thread_title", "Encounter Details"),
            thread_content=thread_content,
        ))

    # 4 & 5. Voice puppeteering — ambient + TTS
    tts_cues    = data.get("tts_cues", [])
    ambient_key = data.get("ambient_audio_key")
    if isinstance(user, discord.Member) and (ambient_key or tts_cues):
        asyncio.create_task(bot.voice_mgr.handle_turn_audio(user, ambient_key, tts_cues))

    # 6. Channel manipulation
    directive = data.get("channel_directive")
    if directive and isinstance(user, discord.Member) and guild:
        asyncio.create_task(_handle_channel_directive(user, guild, directive))

    # 7. Driftnet broadcast — mirror embed to the world's dedicated channel
    driftnet_id = data.get("driftnet_channel_id")
    if driftnet_id and guild and str(driftnet_id).isdigit():
        asyncio.create_task(_post_to_driftnet(guild, int(driftnet_id), embed, channel.id))

    # 8. Lyria music cue
    music_cue = data.get("music_cue")
    if music_cue and isinstance(user, discord.Member) and guild:
        bot.voice_mgr.track_activity(guild.id)
        asyncio.create_task(bot.voice_mgr.play_music(
            guild_id       = guild.id,
            audio_url      = music_cue.get("audio_url", ""),
            volume         = music_cue.get("volume", 0.45),
            crossfade_s    = music_cue.get("crossfade_s", 2.0),
            lavalink_query = music_cue.get("lavalink_query", ""),
            music_prompt   = music_cue.get("music_prompt", ""),
        ))

    # 9. SFX cues (fire-and-forget, serially by delay)
    for sfx in data.get("sfx_cues", []):
        if guild:
            asyncio.create_task(bot.voice_mgr.play_sfx(
                guild_id = guild.id,
                source   = sfx.get("sfx_key", ""),
                volume   = sfx.get("volume", 0.7),
                delay_ms = sfx.get("delay_ms", 0),
            ))

    # 10. Scene Painter — generate scene image and edit embed with it
    scene_prompt = data.get("scene_image_prompt")
    if scene_prompt and guild and data.get("campaign_id"):
        asyncio.create_task(_generate_and_attach_scene_image(
            sent_msg, guild.id, scene_prompt,
            data.get("campaign_id"), data.get("intent_id"),
        ))

    # 11. NPC Portrait generation
    npc_portrait_name = data.get("npc_portrait_name")
    if npc_portrait_name and data.get("campaign_id"):
        asyncio.create_task(_generate_npc_portrait(
            npc_portrait_name, data["campaign_id"]
        ))

    # 12. Auto-deliver handout via DM
    handout_id = data.get("handout_id")
    if handout_id:
        asyncio.create_task(_deliver_handout_dm(user, handout_id))


# ── 1. Paranoia Whisper System ─────────────────────────────────────────────────

async def _send_whisper_dm(
    user:    discord.Member | discord.User,
    whisper: str,
) -> None:
    """
    Silently DM the player a private perception insight.

    Dark-grey embed colour signals private intel.  If the player has DMs
    disabled, the error is logged and silently dropped — the main narrative
    is unaffected.
    """
    try:
        embed = discord.Embed(
            title="👁 Your instincts whisper…",
            description=whisper,
            colour=0x2B2D31,  # Discord sidebar grey — feels private and secret
        )
        embed.set_footer(text="Only you can see this.")
        await user.send(embed=embed)
        logger.debug("Whisper DM sent to %s", user.name)
    except discord.Forbidden:
        logger.debug(
            "Whisper DM to %s blocked — DMs may be disabled.", user.name
        )
    except Exception as exc:
        logger.warning("Whisper DM failed: %s", exc)


# ── 7. Driftnet Broadcast ─────────────────────────────────────────────────────

async def _post_to_driftnet(
    guild:          discord.Guild,
    driftnet_id:    int,
    embed:          discord.Embed,
    source_channel_id: int,
) -> None:
    """
    Mirror the narrative embed to the world's driftnet channel.

    Silently skips if the channel is not found, is the same as the source
    channel (to avoid double-posting in the driftnet channel itself), or if
    the bot lacks send permission.
    """
    if driftnet_id == source_channel_id:
        return
    try:
        driftnet_channel = guild.get_channel(driftnet_id)
        if driftnet_channel is None:
            driftnet_channel = await guild.fetch_channel(driftnet_id)
        if not isinstance(driftnet_channel, discord.TextChannel):
            return
        await driftnet_channel.send(embed=embed)
        logger.debug("Driftnet: mirrored narrative to channel %d", driftnet_id)
    except discord.Forbidden:
        logger.debug("Driftnet: missing send permission for channel %d", driftnet_id)
    except Exception as exc:
        logger.warning("Driftnet broadcast failed for channel %d: %s", driftnet_id, exc)


# ── Multimedia Delivery Helpers ───────────────────────────────────────────────

async def _generate_and_attach_scene_image(
    sent_msg:    discord.Message,
    guild_id:    int,
    prompt:      str,
    campaign_id: str,
    intent_id:   str | None,
) -> None:
    """Generate a scene image via the orchestrator and edit the embed with it."""
    try:
        resp = await bot.http_client.post(
            "/api/maps/generate",
            json={"prompt": prompt, "campaign_id": campaign_id, "intent_id": intent_id},
            timeout=60,
        )
        if resp.status_code == 200:
            url = resp.json().get("url")
            if url:
                new_embed = sent_msg.embeds[0].copy() if sent_msg.embeds else discord.Embed()
                new_embed.set_image(url=url)
                await sent_msg.edit(embed=new_embed)
                logger.info("Scene image attached: %s", url)
    except Exception as exc:
        logger.debug("Scene image generation failed: %s", exc)


async def _generate_npc_portrait(npc_name: str, campaign_id: str) -> None:
    """Request NPC portrait generation in the background (fire-and-forget)."""
    try:
        await bot.http_client.post(
            "/api/maps/generate",
            json={
                "prompt": f"Portrait of {npc_name}, fantasy RPG character art, detailed face",
                "campaign_id": campaign_id,
                "portrait_npc": npc_name,
            },
            timeout=60,
        )
    except Exception as exc:
        logger.debug("NPC portrait generation failed for %s: %s", npc_name, exc)


async def _deliver_handout_dm(
    user:       discord.Member | discord.User,
    handout_id: str,
) -> None:
    """Fetch a handout and DM it to the player."""
    try:
        resp = await bot.http_client.get(f"/api/handouts/{handout_id}")
        if resp.status_code != 200:
            return
        h = resp.json()
        embed = discord.Embed(
            title=h.get("title", "Handout"),
            description=h.get("content_text", "")[:4000],
            colour=0xC9A84C,
        )
        embed.set_footer(text=f"Type: {h.get('handout_type', 'general').replace('_', ' ').title()}")
        if h.get("image_url"):
            embed.set_image(url=h["image_url"])
        await user.send(embed=embed)
        # Mark delivered
        await bot.http_client.post(
            "/api/handouts/deliver",
            json={"handout_id": handout_id, "player_id": str(user.id)},
        )
    except discord.Forbidden:
        logger.debug("Could not DM handout to %s — DMs disabled.", user.name)
    except Exception as exc:
        logger.debug("Handout DM failed: %s", exc)


# ── 2. Ghost Sheet — Ephemeral Combat Threads ─────────────────────────────────

async def _handle_combat_thread(
    sent_msg:       discord.Message,
    guild:          discord.Guild,
    channel_id:     int,
    thread_event:   str,
    thread_title:   str,
    thread_content: str,
) -> None:
    """
    Manage the ephemeral combat thread lifecycle.

    "combat" event  → open a new thread if none exists for this channel;
                      otherwise post to the existing one.
    "close"  event  → post final summary, then archive and lock the thread.

    Thread state is stored in _open_combat_threads (channel_id → thread_id).
    """
    try:
        if thread_event == "combat":
            existing_id = _open_combat_threads.get(channel_id)
            if existing_id:
                thread = guild.get_thread(existing_id)
                if thread:
                    await thread.send(content=thread_content)
                    return
                # Thread was externally deleted — create a new one below

            thread = await sent_msg.create_thread(
                name=f"⚔️ {thread_title}"[:100],
                auto_archive_duration=60,
                reason="GM combat encounter thread",
            )
            _open_combat_threads[channel_id] = thread.id
            await thread.send(content=thread_content)
            logger.info(
                "Combat thread opened: '%s' (id=%d) in channel %d",
                thread.name, thread.id, channel_id,
            )

        elif thread_event == "close":
            existing_id = _open_combat_threads.pop(channel_id, None)
            if not existing_id:
                return
            thread = guild.get_thread(existing_id)
            if thread:
                await thread.send(
                    content=thread_content + "\n\n*— Encounter concluded. Thread archived.*"
                )
                await thread.edit(archived=True, locked=True)
                logger.info("Combat thread closed: id=%d", existing_id)

    except discord.Forbidden:
        logger.warning(
            "Cannot manage combat thread in channel %d — "
            "bot needs 'Create Public Threads' and 'Manage Threads' permissions.",
            channel_id,
        )
    except Exception as exc:
        logger.error("Combat thread error: %s", exc)


# ── 4. Channel Manipulation ────────────────────────────────────────────────────

async def _handle_channel_directive(
    member:    discord.Member,
    guild:     discord.Guild,
    directive: dict[str, Any],
) -> None:
    """
    Grant or revoke the player's access to a semantic location channel.

    "move_to"  → grant read-only access to the target zone channel.
    "restore"  → restore full send access to the main/home channel.

    Channel IDs are fetched from the orchestrator settings API (cached 60 s).
    Configure zone names in White Portal → Settings → Channel Map.
    If a key is not configured,
    the directive is logged and skipped — no crash, no noise.
    Requires bot 'Manage Permissions' permission in the target channel.
    """
    action      = directive.get("action")
    channel_key = directive.get("channel_key")
    reason_str  = directive.get("reason", "GM narrative directive")

    target_id = await _get_channel_id(channel_key)
    if not target_id:
        logger.info(
            "Channel directive '%s' → '%s' skipped — env var not configured.",
            action, channel_key,
        )
        return

    target_channel = guild.get_channel(target_id)
    if not isinstance(target_channel, discord.TextChannel):
        logger.warning("Channel %d not found or not a text channel.", target_id)
        return

    try:
        if action == "move_to":
            await target_channel.set_permissions(
                member,
                read_messages=True,
                send_messages=False,
                reason=reason_str,
            )
            await target_channel.send(
                f"{member.mention} *is here now…*",
                delete_after=30,
            )
            logger.info(
                "Channel directive: %s moved to '%s' (%d)",
                member.name, channel_key, target_id,
            )

        elif action == "restore":
            await target_channel.set_permissions(
                member,
                read_messages=True,
                send_messages=True,
                reason=reason_str,
            )
            await target_channel.send(
                f"{member.mention} *returns to the world…*",
                delete_after=20,
            )
            logger.info("Channel directive: %s restored to main", member.name)

    except discord.Forbidden:
        logger.warning(
            "Cannot set channel permissions — bot needs 'Manage Permissions' in server."
        )
    except Exception as exc:
        logger.error("Channel directive error: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# PDF Ingestion Helpers (unchanged from Task 2)
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_active_campaign(guild_id: str) -> dict[str, Any] | None:
    try:
        resp = await bot.http_client.get(
            "/api/campaign/active", params={"guild_id": guild_id}
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:
        logger.warning("Could not fetch active campaign: %s", exc)
    return None


def _ingest_embed_queued(module_name: str, filename: str) -> discord.Embed:
    return discord.Embed(
        title="📄 Rulebook Queued",
        description=(
            f"**{module_name}** (`{filename}`) has been received.\n"
            f"Extracting text and building the vector index…\n\n"
            f"`{_progress_bar(0)}` 0%"
        ),
        colour=0x5c8fd6,
    ).set_footer(text="Updates every 3 seconds")


def _ingest_embed_progress(data: dict) -> discord.Embed:
    status = data.get("status", "working")
    name   = data.get("module_name", "Rulebook")
    if status == "extracting":
        page  = data.get("page", 0)
        total = data.get("total", 1)
        pct   = int((page / total) * 70) if total else 5
        desc  = (
            f"**Extracting text** — page {page} of {total}\n"
            f"{data.get('chunks_so_far', 0)} chunks found so far\n\n"
            f"`{_progress_bar(pct)}` {pct}%"
        )
        colour = 0x5c8fd6
    elif status == "embedding":
        embedded = data.get("chunks_embedded", 0)
        total_c  = data.get("chunks", 1)
        pct      = 70 + int((embedded / total_c) * 30) if total_c else 70
        desc     = (
            f"**Embedding** — {embedded} / {total_c} chunks\n"
            f"Sending to Gemini Embeddings API…\n\n"
            f"`{_progress_bar(pct)}` {pct}%"
        )
        colour = 0xc9a84c
    else:
        desc   = f"Working… (`{status}`)\n\n`{_progress_bar(10)}` …"
        colour = 0x888888
    return discord.Embed(
        title=f"📄 Ingesting: {name}", description=desc, colour=colour,
    ).set_footer(text="Updates every 3 seconds")


def _ingest_embed_done(data: dict) -> discord.Embed:
    name   = data.get("module_name", "Rulebook")
    chunks = data.get("chunks", "?")
    coll   = data.get("collection", "")
    return discord.Embed(
        title="✅ Rulebook Ready",
        description=(
            f"**{name}** has been indexed and is now available to the GM.\n\n"
            f"**{chunks}** chunks stored in `{coll}`\n"
            f"`{_progress_bar(100)}` 100%\n\n"
            f"The mechanical engine will cite this rulebook automatically."
        ),
        colour=0x4caf78,
    )


def _ingest_embed_error(data: dict, module_name: str) -> discord.Embed:
    return discord.Embed(
        title="❌ Ingestion Failed",
        description=(
            f"**{module_name}** could not be ingested.\n\n"
            f"```{data.get('error', 'Unknown error')[:300]}```"
        ),
        colour=0xcf4c5a,
    )


async def _run_ingest_and_report(
    msg:                  discord.Message | None,
    interaction_followup: Any | None,
    pdf_bytes:            bytes,
    filename:             str,
    module_name:          str,
    campaign_id:          str,
) -> None:
    async def _edit(embed: discord.Embed) -> None:
        try:
            if msg:
                await msg.edit(embed=embed)
            elif interaction_followup:
                await interaction_followup.edit(embed=embed)
        except Exception as e:
            logger.warning("Could not edit progress message: %s", e)

    try:
        resp = await bot.http_client.post(
            "/api/rulebook/ingest",
            data={"campaign_id": campaign_id, "module_name": module_name},
            files={"pdf_file": (filename, pdf_bytes, "application/pdf")},
        )
        resp.raise_for_status()
        job_id = resp.json()["job_id"]
    except Exception as exc:
        logger.exception("PDF upload to orchestrator failed: %s", exc)
        await _edit(discord.Embed(
            title="❌ Upload Failed",
            description=f"Could not send the PDF to the GM engine.\n```{exc}```",
            colour=0xcf4c5a,
        ))
        return

    while True:
        await asyncio.sleep(3)
        try:
            status_resp = await bot.http_client.get(f"/api/rulebook/status/{job_id}")
            data = status_resp.json() if status_resp.status_code == 200 else {}
        except Exception:
            data = {}
        job_status = data.get("status", "unknown")
        if job_status == "complete":
            await _edit(_ingest_embed_done(data))
            return
        elif job_status == "error":
            await _edit(_ingest_embed_error(data, module_name))
            return
        else:
            await _edit(_ingest_embed_progress(data))


# ─────────────────────────────────────────────────────────────────────────────
# Message Listener – action dispatch + PDF auto-detect
# ─────────────────────────────────────────────────────────────────────────────

@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    # ── PDF auto-detection ─────────────────────────────────────────────────
    pdf_attachments = [
        a for a in message.attachments if a.filename.lower().endswith(".pdf")
    ]
    if pdf_attachments and message.guild:
        for attachment in pdf_attachments:
            if attachment.size > _MAX_DOWNLOAD_BYTES:
                await message.reply(
                    f"⚠️ **{attachment.filename}** is too large "
                    f"({attachment.size // (1024*1024)} MB). Maximum is 200 MB.",
                    mention_author=False,
                )
                continue
            campaign = await _fetch_active_campaign(str(message.guild.id))
            if not campaign:
                await message.reply(
                    "⚠️ No active campaign. Create one before ingesting rulebooks.",
                    mention_author=False,
                )
                continue
            module_name  = attachment.filename[:-4].replace("_", " ").replace("-", " ").strip()
            progress_msg = await message.reply(
                embed=_ingest_embed_queued(module_name, attachment.filename),
                mention_author=False,
            )
            pdf_bytes = await attachment.read()
            asyncio.create_task(_run_ingest_and_report(
                msg=progress_msg, interaction_followup=None,
                pdf_bytes=pdf_bytes, filename=attachment.filename,
                module_name=module_name, campaign_id=campaign["id"],
            ))
        return

    # ── In-character action (> prefix) ────────────────────────────────────
    if not message.content.startswith(">"):
        await bot.process_commands(message)
        return

    raw_input = message.content[1:].strip()
    if not raw_input:
        return

    guild_id = str(message.guild.id) if message.guild else "DM"

    # Capture references for the closure
    _channel = message.channel
    _author  = message.author
    _guild   = message.guild

    async def _run_message_action() -> None:
        async with _channel.typing():
            try:
                data = await _dispatch_intent(
                    player_id=str(_author.id),
                    guild_id=guild_id,
                    channel_id=str(_channel.id),
                    raw_input=raw_input,
                )
                await _deliver_narrative(channel=_channel, data=data, user=_author, guild=_guild)
            except httpx.HTTPStatusError as exc:
                await _channel.send(
                    f"⚠️ The GM engine returned an error: `{exc.response.status_code}`"
                )
            except Exception as exc:
                logger.exception("Action dispatch failed: %s", exc)
                await _channel.send("⚠️ An internal error occurred. The GM is unavailable.")

    # Check queue depth before enqueuing
    existing_q = _guild_queues.get(guild_id)
    queue_pos  = existing_q.qsize() if existing_q else 0

    if not await _enqueue_action(guild_id, _run_message_action()):
        await message.channel.send(
            "⚠️ The GM's processing queue is full. Please wait a moment and try again."
        )
        return

    if queue_pos > 0:
        await message.add_reaction("⏳")


# ─────────────────────────────────────────────────────────────────────────────
# Slash Commands
# ─────────────────────────────────────────────────────────────────────────────

@bot.tree.command(name="act", description="Declare an in-character action.")
@app_commands.describe(
    action="What does your character do?",
    image="Optional image attachment for visual context (scene photo, map, etc.).",
)
async def slash_act(
    interaction: discord.Interaction,
    action: str,
    image: discord.Attachment | None = None,
) -> None:
    # Acknowledge within 3 s — the queue may hold processing for 10-30 s
    await interaction.response.defer()

    guild_id = str(interaction.guild_id or "DM")
    existing_q = _guild_queues.get(guild_id)
    if existing_q and existing_q.qsize() >= _MAX_QUEUE_DEPTH:
        await interaction.followup.send(
            "⚠️ The GM's processing queue is full. Please try again in a moment.",
            ephemeral=True,
        )
        return

    queue_pos = existing_q.qsize() if existing_q else 0

    # Snapshot mutable args for the closure
    _image = image
    _action = action

    async def _run_act() -> None:
      try:
        raw_input = _action

        if _image and _image.content_type and _image.content_type.startswith("image/"):
            try:
                vis_resp = await bot.http_client.post(
                    f"{ORCHESTRATOR_URL}/api/vision/analyse",
                    json={"image_url": _image.url, "prompt": _action},
                    timeout=30,
                )
                if vis_resp.status_code == 200:
                    visual_desc = vis_resp.json().get("description", "")
                    if visual_desc:
                        raw_input = f"[Visual context: {visual_desc}]\n\n{_action}"
            except Exception as vis_exc:
                logger.warning("Visual intel failed: %s", vis_exc)

        data = await _dispatch_intent(
            player_id=str(interaction.user.id),
            guild_id=guild_id,
            channel_id=str(interaction.channel_id),
            raw_input=raw_input,
            command_type="slash_command",
            slash_data={"command_name": "act", "options": {"action": _action}},
        )
        embed    = _build_action_embed(data, interaction.user)
        sent_msg = await interaction.followup.send(embed=embed, wait=True)

        # Fire all Task 4 features
        if data.get("whisper"):
            asyncio.create_task(_send_whisper_dm(interaction.user, data["whisper"]))

        if data.get("thread_event") and data.get("thread_content") and interaction.guild:
            asyncio.create_task(_handle_combat_thread(
                sent_msg=sent_msg,
                guild=interaction.guild,
                channel_id=interaction.channel_id,
                thread_event=data["thread_event"],
                thread_title=data.get("thread_title", "Encounter Details"),
                thread_content=data["thread_content"],
            ))

        if isinstance(interaction.user, discord.Member) and (
            data.get("ambient_audio_key") or data.get("tts_cues")
        ):
            asyncio.create_task(
                bot.voice_mgr.handle_turn_audio(
                    interaction.user,
                    data.get("ambient_audio_key"),
                    data.get("tts_cues", []),
                )
            )

        if data.get("channel_directive") and isinstance(interaction.user, discord.Member) and interaction.guild:
            asyncio.create_task(
                _handle_channel_directive(interaction.user, interaction.guild, data["channel_directive"])
            )

        # Music cue
        music_cue = data.get("music_cue")
        if music_cue and isinstance(interaction.user, discord.Member) and interaction.guild:
            bot.voice_mgr.track_activity(interaction.guild.id)
            asyncio.create_task(bot.voice_mgr.play_music(
                guild_id       = interaction.guild.id,
                audio_url      = music_cue.get("audio_url", ""),
                volume         = music_cue.get("volume", 0.45),
                crossfade_s    = music_cue.get("crossfade_s", 2.0),
                lavalink_query = music_cue.get("lavalink_query", ""),
                music_prompt   = music_cue.get("music_prompt", ""),
            ))

        # SFX cues
        for sfx in data.get("sfx_cues", []):
            if interaction.guild:
                asyncio.create_task(bot.voice_mgr.play_sfx(
                    guild_id = interaction.guild.id,
                    source   = sfx.get("sfx_key", ""),
                    volume   = sfx.get("volume", 0.7),
                    delay_ms = sfx.get("delay_ms", 0),
                ))

        # Scene image
        scene_prompt = data.get("scene_image_prompt")
        if scene_prompt and interaction.guild and data.get("campaign_id"):
            asyncio.create_task(_generate_and_attach_scene_image(
                sent_msg, interaction.guild.id, scene_prompt,
                data["campaign_id"], data.get("intent_id"),
            ))

        # NPC portrait
        if data.get("npc_portrait_name") and data.get("campaign_id"):
            asyncio.create_task(_generate_npc_portrait(
                data["npc_portrait_name"], data["campaign_id"]
            ))

        # Auto-deliver handout
        if data.get("handout_id"):
            asyncio.create_task(_deliver_handout_dm(interaction.user, data["handout_id"]))

        # Ghost Continuity: mark this intent delivered so it won't be re-sent on reconnect
        if data.get("intent_id"):
            asyncio.create_task(
                _mark_narrative_delivered(data["intent_id"], str(interaction.guild_id or "DM"))
            )

      except httpx.HTTPStatusError as exc:
        await interaction.followup.send(
            f"⚠️ Engine error `{exc.response.status_code}`: {exc.response.text[:200]}"
        )
      except Exception as exc:
        logger.exception("Slash /act failed: %s", exc)
        await interaction.followup.send("⚠️ Internal error. Please try again.")

    if not await _enqueue_action(guild_id, _run_act()):
        await interaction.followup.send(
            "⚠️ The GM's processing queue is full. Please try again in a moment.",
            ephemeral=True,
        )
        return

    if queue_pos > 0:
        await interaction.followup.send(
            f"⏳ Your action is queued (position {queue_pos + 1}). The GM will respond shortly…",
            ephemeral=True,
        )


@bot.tree.command(name="status", description="Check your character's current stats.")
async def slash_status(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    guild_id = str(interaction.guild_id or "DM")

    async def _run_status() -> None:
        try:
            data = await _dispatch_intent(
                player_id=str(interaction.user.id),
                guild_id=guild_id,
                channel_id=str(interaction.channel_id),
                raw_input="Check my character's current status and stats.",
                command_type="slash_command",
                slash_data={"command_name": "status", "options": {}},
            )
            await interaction.followup.send(
                embed=_build_action_embed(data, interaction.user), ephemeral=True
            )
        except Exception as exc:
            logger.exception("Slash /status failed: %s", exc)
            await interaction.followup.send("⚠️ Could not retrieve status.", ephemeral=True)

    if not await _enqueue_action(guild_id, _run_status()):
        await interaction.followup.send(
            "⚠️ The GM's processing queue is full. Please try again in a moment.",
            ephemeral=True,
        )


@bot.tree.command(name="inventory", description="View your character's inventory.")
async def slash_inventory(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    guild_id = str(interaction.guild_id or "DM")

    async def _run_inventory() -> None:
        try:
            data = await _dispatch_intent(
                player_id=str(interaction.user.id),
                guild_id=guild_id,
                channel_id=str(interaction.channel_id),
                raw_input="List all items in my character's inventory.",
                command_type="slash_command",
                slash_data={"command_name": "inventory", "options": {}},
            )
            await interaction.followup.send(
                embed=_build_action_embed(data, interaction.user), ephemeral=True
            )
        except Exception as exc:
            logger.exception("Slash /inventory failed: %s", exc)
            await interaction.followup.send("⚠️ Could not retrieve inventory.", ephemeral=True)

    if not await _enqueue_action(guild_id, _run_inventory()):
        await interaction.followup.send(
            "⚠️ The GM's processing queue is full. Please try again in a moment.",
            ephemeral=True,
        )


@bot.tree.command(name="upload_rulebook", description="Upload a PDF rulebook for the GM to use.")
@app_commands.describe(
    file="The PDF rulebook file",
    module_name="Name for this module (defaults to filename)",
    campaign_id="Campaign ID — leave blank to use the server's active campaign",
)
async def slash_upload_rulebook(
    interaction: discord.Interaction,
    file:        discord.Attachment,
    module_name: str = "",
    campaign_id: str = "",
) -> None:
    await interaction.response.defer()

    if not file.filename.lower().endswith(".pdf"):
        await interaction.followup.send("⚠️ Only PDF files are supported.")
        return
    if file.size > _MAX_DOWNLOAD_BYTES:
        await interaction.followup.send(
            f"⚠️ File is too large ({file.size // (1024*1024)} MB). Maximum is 200 MB."
        )
        return

    if not campaign_id and interaction.guild_id:
        campaign = await _fetch_active_campaign(str(interaction.guild_id))
        if not campaign:
            await interaction.followup.send(
                "⚠️ No active campaign. Provide a `campaign_id` or create a campaign first."
            )
            return
        campaign_id = campaign["id"]

    if not campaign_id:
        await interaction.followup.send("⚠️ Could not determine campaign.")
        return

    if not module_name:
        module_name = file.filename[:-4].replace("_", " ").replace("-", " ").strip()

    progress_embed = _ingest_embed_queued(module_name, file.filename)
    followup_msg   = await interaction.followup.send(embed=progress_embed, wait=True)

    try:
        pdf_bytes = await file.read()
    except Exception as exc:
        await followup_msg.edit(embed=discord.Embed(
            title="❌ Download Failed",
            description=f"Could not download the file from Discord.\n```{exc}```",
            colour=0xcf4c5a,
        ))
        return

    asyncio.create_task(_run_ingest_and_report(
        msg=None,
        interaction_followup=followup_msg,
        pdf_bytes=pdf_bytes,
        filename=file.filename,
        module_name=module_name,
        campaign_id=campaign_id,
    ))


# ─────────────────────────────────────────────────────────────────────────────
# Async Session Slash Commands
# ─────────────────────────────────────────────────────────────────────────────

@bot.tree.command(
    name="recap",
    description="Get a 'Previously on…' summary of everything you missed while offline.",
)
async def slash_recap(interaction: discord.Interaction) -> None:
    """
    Generates an ephemeral catch-up summary for the requesting player,
    covering all events since their last action in this campaign.
    Delivered as an ephemeral response (only visible to the requester).
    """
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild_id:
        await interaction.followup.send("⚠️ This command must be used in a server.", ephemeral=True)
        return

    campaign = await _fetch_active_campaign(str(interaction.guild_id))
    if not campaign:
        await interaction.followup.send(
            "⚠️ No active campaign found for this server.", ephemeral=True
        )
        return

    try:
        resp = await bot.http_client.post(
            "/api/recap",
            json={
                "player_id":   str(interaction.user.id),
                "guild_id":    str(interaction.guild_id),
                "campaign_id": campaign["id"],
            },
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.exception("Recap request failed: %s", exc)
        await interaction.followup.send("⚠️ Could not generate recap.", ephemeral=True)
        return

    recap_text = data.get("recap_text", "No recap available.")
    events     = data.get("events_covered", 0)
    since_ts   = data.get("since_timestamp")

    footer = f"{events} event{'s' if events != 1 else ''} summarised"
    if since_ts:
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(since_ts.replace("Z", "+00:00"))
            footer += f" • since {dt.strftime('%b %d, %H:%M UTC')}"
        except Exception:
            pass

    embed = discord.Embed(
        description=recap_text,
        colour=0x5c8fd6,
    ).set_footer(text=footer)

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="downtime",
    description="Assign your character a background task that resolves while you're offline.",
)
@app_commands.describe(
    task="What does your character do during downtime? (e.g. 'research the ancient tome for 8 hours')",
    hours="How many real-world hours until the task resolves (default: 8)",
)
async def slash_downtime(
    interaction: discord.Interaction,
    task:  str,
    hours: int = 8,
) -> None:
    """
    Submits a personal downtime task.  The GM resolves it in the background
    and DMs the result when the timer expires.  Does not advance the main
    story timeline.
    """
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild_id:
        await interaction.followup.send("⚠️ This command must be used in a server.", ephemeral=True)
        return

    if hours < 1 or hours > 168:
        await interaction.followup.send("⚠️ Duration must be between 1 and 168 hours.", ephemeral=True)
        return

    campaign = await _fetch_active_campaign(str(interaction.guild_id))
    if not campaign:
        await interaction.followup.send(
            "⚠️ No active campaign found for this server.", ephemeral=True
        )
        return

    try:
        resp = await bot.http_client.post(
            "/api/downtime",
            json={
                "player_id":      str(interaction.user.id),
                "guild_id":       str(interaction.guild_id),
                "campaign_id":    campaign["id"],
                "description":    task,
                "duration_hours": hours,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.exception("Downtime submit failed: %s", exc)
        await interaction.followup.send("⚠️ Could not submit downtime task.", ephemeral=True)
        return

    task_id     = data.get("task_id", "?")
    resolves_at = data.get("resolves_at", "")

    # Register player for DM notifications
    guild_id_str = str(interaction.guild_id)
    if guild_id_str not in _active_downtime_players:
        _active_downtime_players[guild_id_str] = set()
    _active_downtime_players[guild_id_str].add(str(interaction.user.id))

    # Format the resolution time
    resolve_display = resolves_at
    if resolves_at:
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(resolves_at.replace("Z", "+00:00"))
            resolve_display = dt.strftime("%b %d at %H:%M UTC")
        except Exception:
            pass

    embed = discord.Embed(
        title="🌙 Downtime Task Queued",
        description=(
            f"**Task:** {task}\n\n"
            f"**Resolves:** {resolve_display}\n"
            f"**Duration:** {hours} hour{'s' if hours != 1 else ''}\n\n"
            "The GM will run the background checks while you rest. "
            "You'll receive a DM with the results when the timer expires.\n\n"
            "*This task runs on your personal timeline — it won't affect the main story.*"
        ),
        colour=0x6B4FA0,
    ).set_footer(text=f"Task ID: {task_id}")

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="retcon",
    description="[GM only] Roll back a hallucinated action and restore character state.",
)
@app_commands.describe(
    intent_id="The UUID of the action to retcon (from the action log)",
    reason="Short explanation for the retcon (for the audit trail)",
)
async def slash_retcon(
    interaction: discord.Interaction,
    intent_id:   str,
    reason:      str = "",
) -> None:
    """
    Admin command: reverses the stat changes from a specific action and marks
    the action_log row as retconned.  Requires the GM role.
    """
    await interaction.response.defer(ephemeral=True)

    # Verify caller has the GM/admin role
    if isinstance(interaction.user, discord.Member):
        has_admin = any(r.name == _ADMIN_ROLE_NAME for r in interaction.user.roles)
    else:
        has_admin = False

    if not has_admin:
        await interaction.followup.send(
            f"⚠️ Only members with the **{_ADMIN_ROLE_NAME}** role can use this command.",
            ephemeral=True,
        )
        return

    try:
        resp = await bot.http_client.post(
            "/api/retcon",
            json={
                "intent_id": intent_id,
                "admin_id":  str(interaction.user.id),
                "reason":    reason,
            },
        )
        if resp.status_code == 400:
            await interaction.followup.send(
                f"⚠️ Retcon failed: {resp.json().get('detail', 'Unknown error')}",
                ephemeral=True,
            )
            return
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        await interaction.followup.send(
            f"⚠️ Retcon request failed: `{exc.response.status_code}`",
            ephemeral=True,
        )
        return
    except Exception as exc:
        logger.exception("Retcon command failed: %s", exc)
        await interaction.followup.send("⚠️ Internal error during retcon.", ephemeral=True)
        return

    character_id   = data.get("character_id", "unknown")
    restored_stats = data.get("restored_stats", {})

    # Build a compact stats summary (show only keys with simple values)
    stats_lines = [
        f"  `{k}`: {v}"
        for k, v in list(restored_stats.items())[:8]
        if isinstance(v, (int, float, str, bool))
    ]
    stats_display = "\n".join(stats_lines) if stats_lines else "  *(no numeric stats to display)*"

    embed = discord.Embed(
        title="↩️ Retcon Applied",
        description=(
            f"**Action rolled back:** `{intent_id}`\n"
            f"**Character:** `{character_id}`\n"
            f"**Reason:** {reason or '*(none provided)*'}\n\n"
            f"**Restored stats:**\n{stats_display}\n\n"
            "The action has been flagged in the audit log. "
            "Story context entries (if any) must be removed manually via the Lore Archive."
        ),
        colour=0xFF6B35,
    ).set_footer(text=f"Retcon by {interaction.user.display_name}")

    await interaction.followup.send(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────────────────────
# Ghost Continuity
# ─────────────────────────────────────────────────────────────────────────────

async def _mark_narrative_delivered(intent_id: str, guild_id: str) -> None:
    """Fire-and-forget: tell the orchestrator a pending narrative was delivered."""
    try:
        await bot.http_client.post(
            f"{ORCHESTRATOR_URL}/api/narrative/{intent_id}/delivered",
            json={"guild_id": guild_id},
            timeout=10,
        )
    except Exception as exc:
        logger.warning("Could not mark narrative %s delivered: %s", intent_id, exc)


async def _ghost_continuity_sync() -> None:
    """On bot (re)connect, fetch and deliver any narratives that were generated
    while the bot was offline, then mark each one delivered."""
    await bot.wait_until_ready()
    for guild in bot.guilds:
        guild_id = str(guild.id)
        try:
            resp = await bot.http_client.get(
                    f"{ORCHESTRATOR_URL}/api/narrative/pending/{guild_id}",
                    timeout=15,
                )
            if resp.status_code != 200:
                continue
            pending: list[dict] = resp.json().get("pending", [])
        except Exception as exc:
            logger.warning("Ghost continuity fetch failed for guild %s: %s", guild_id, exc)
            continue

        for item in pending:
            intent_id  = item.get("intent_id", "")
            channel_id = item.get("channel_id")
            narrative  = item.get("narrative", "")
            if not (intent_id and channel_id and narrative):
                continue

            channel = guild.get_channel(int(channel_id))
            if channel is None:
                continue

            try:
                embed = discord.Embed(
                    title="📜 Missed Narrative",
                    description=narrative[:4000],
                    colour=0x9F6FE0,
                ).set_footer(text="Delivered via Ghost Continuity")
                await channel.send(embed=embed)
                asyncio.create_task(_mark_narrative_delivered(intent_id, guild_id))
            except Exception as exc:
                logger.warning("Ghost delivery failed for %s: %s", intent_id, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Player Sandbox Commands
# ─────────────────────────────────────────────────────────────────────────────

@bot.tree.command(name="do", description="Describe a physical action your character performs.")
@app_commands.describe(action="The physical action your character takes.")
async def slash_do(interaction: discord.Interaction, action: str) -> None:
    await interaction.response.defer()
    guild_id = str(interaction.guild_id or "DM")
    guild    = interaction.guild
    user     = interaction.user
    channel_id = interaction.channel_id

    async def _run_do() -> None:
        try:
            data = await _dispatch_intent(
                player_id=str(user.id),
                guild_id=guild_id,
                channel_id=str(channel_id),
                raw_input=f"My character physically does: {action}",
                command_type="slash_command",
                slash_data={"command_name": "do", "options": {"action": action}},
            )
            embed = _build_action_embed(data, user)
            sent_msg = await interaction.followup.send(embed=embed, wait=True)

            if data.get("whisper"):
                asyncio.create_task(_send_whisper_dm(user, data["whisper"]))

            if data.get("thread_event") and data.get("thread_content") and guild:
                asyncio.create_task(_handle_combat_thread(
                    sent_msg=sent_msg,
                    guild=guild,
                    channel_id=channel_id,
                    thread_event=data["thread_event"],
                    thread_title=data.get("thread_title", "Action Details"),
                    thread_content=data["thread_content"],
                ))

            if isinstance(user, discord.Member) and guild and data.get("channel_directive"):
                asyncio.create_task(
                    _handle_channel_directive(user, guild, data["channel_directive"])
                )
        except Exception as exc:
            logger.exception("Slash /do failed: %s", exc)
            await interaction.followup.send("⚠️ Internal error. Please try again.")

    if not await _enqueue_action(guild_id, _run_do()):
        await interaction.followup.send(
            "⚠️ The GM's processing queue is full. Please try again in a moment.",
            ephemeral=True,
        )


@bot.tree.command(name="say", description="Speak in-character as your character.")
@app_commands.describe(words="The words your character speaks aloud.")
async def slash_say(interaction: discord.Interaction, words: str) -> None:
    await interaction.response.defer()
    guild_id = str(interaction.guild_id or "DM")
    user     = interaction.user

    async def _run_say() -> None:
        try:
            data = await _dispatch_intent(
                player_id=str(user.id),
                guild_id=guild_id,
                channel_id=str(interaction.channel_id),
                raw_input=f'My character says: "{words}"',
                command_type="slash_command",
                slash_data={"command_name": "say", "options": {"words": words}},
            )
            embed = _build_action_embed(data, user)
            await interaction.followup.send(embed=embed)

            if data.get("whisper"):
                asyncio.create_task(_send_whisper_dm(user, data["whisper"]))

            if isinstance(user, discord.Member) and data.get("tts_cues"):
                asyncio.create_task(
                    bot.voice_mgr.handle_turn_audio(
                        user, None, data.get("tts_cues", [])
                    )
                )
        except Exception as exc:
            logger.exception("Slash /say failed: %s", exc)
            await interaction.followup.send("⚠️ Internal error. Please try again.")

    if not await _enqueue_action(guild_id, _run_say()):
        await interaction.followup.send(
            "⚠️ The GM's processing queue is full. Please try again in a moment.",
            ephemeral=True,
        )


@bot.tree.command(name="insight", description="Attempt a perception or insight check on something.")
@app_commands.describe(target="What are you trying to perceive or understand?")
async def slash_insight(interaction: discord.Interaction, target: str) -> None:
    await interaction.response.defer(ephemeral=True)
    guild_id = str(interaction.guild_id or "DM")
    user     = interaction.user

    async def _run_insight() -> None:
        try:
            data = await _dispatch_intent(
                player_id=str(user.id),
                guild_id=guild_id,
                channel_id=str(interaction.channel_id),
                raw_input=f"I attempt an insight/perception check on: {target}",
                command_type="slash_command",
                slash_data={"command_name": "insight", "options": {"target": target}},
            )
            embed = _build_action_embed(data, user)
            embed.title = f"🔍 Insight: {target[:60]}"
            embed.set_footer(text="Only you can see this result.")
            await interaction.followup.send(embed=embed, ephemeral=True)

            # Private whisper for deep secrets
            if data.get("whisper"):
                asyncio.create_task(_send_whisper_dm(user, data["whisper"]))
        except Exception as exc:
            logger.exception("Slash /insight failed: %s", exc)
            await interaction.followup.send("⚠️ Internal error. Please try again.", ephemeral=True)

    if not await _enqueue_action(guild_id, _run_insight()):
        await interaction.followup.send(
            "⚠️ The GM's processing queue is full. Please try again in a moment.",
            ephemeral=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# System Integrity Check (SIC) — Admin Command
# ─────────────────────────────────────────────────────────────────────────────

_SIC_STATUS_COLORS = {
    "healthy":  0x00FF88,   # green
    "unstable": 0xFFAA00,   # amber
    "critical": 0xFF3333,   # red
    "unknown":  0x888888,   # grey
}
_SIC_STATUS_ICONS = {
    "healthy":  "🟢",
    "unstable": "🟡",
    "critical": "🔴",
    "unknown":  "⚪",
}


@bot.tree.command(
    name="sic",
    description="[GM only] Run the System Integrity Check and show Aetheris environment health.",
)
async def slash_sic(interaction: discord.Interaction) -> None:
    """
    Triggers a live SIC run on the orchestrator and displays the four-pillar
    result as a Discord embed.  Requires the GM/admin role.
    """
    await interaction.response.defer(ephemeral=True)

    # Admin-role guard
    if isinstance(interaction.user, discord.Member):
        has_admin = any(r.name == _ADMIN_ROLE_NAME for r in interaction.user.roles)
    else:
        has_admin = False

    if not has_admin:
        await interaction.followup.send(
            f"⚠️ Only members with the **{_ADMIN_ROLE_NAME}** role can use `/sic`.",
            ephemeral=True,
        )
        return

    try:
        resp = await bot.http_client.post("/api/sic/run")
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.exception("SIC command failed: %s", exc)
        await interaction.followup.send(
            "⚠️ Could not reach the orchestrator for SIC. Check that Scribe is running.",
            ephemeral=True,
        )
        return

    status = data.get("status", "unknown")
    icon   = _SIC_STATUS_ICONS.get(status, "⚪")
    color  = _SIC_STATUS_COLORS.get(status, 0x888888)

    embed = discord.Embed(
        title=f"{icon} Aetheris Integrity Check — {status.upper()}",
        description=f"Checked at: `{data.get('checked_at', 'unknown')}`",
        color=color,
    )

    for pillar in data.get("pillars", []):
        passed = pillar.get("passed", False)
        name   = pillar.get("name", "unknown").replace("_", " ").title()
        msg    = pillar.get("message", "")
        detail = pillar.get("detail", "")
        crit   = pillar.get("critical", False)

        field_icon  = "✅" if passed else ("🔴" if crit else "🟡")
        field_value = msg
        if detail:
            field_value += f"\n```{detail[:200]}```"

        embed.add_field(name=f"{field_icon} {name}", value=field_value or "—", inline=False)

    embed.set_footer(text="Run automatically on startup and post-backup. /sic for on-demand.")
    await interaction.followup.send(embed=embed, ephemeral=True)


# Dynamic Genre Orchestration — World Switch Commands
# ─────────────────────────────────────────────────────────────────────────────

@bot.tree.command(
    name="worlds",
    description="List all available RPG worlds/systems the GM can run.",
)
async def slash_worlds(interaction: discord.Interaction) -> None:
    """Show every discovered world in the WorldRegistry."""
    await interaction.response.defer(ephemeral=True)
    try:
        resp = await bot.http_client.get("/api/worlds")
        resp.raise_for_status()
        worlds: list[dict] = resp.json()

        if not worlds:
            await interaction.followup.send(
                "No worlds discovered yet. Drop a folder into `data/fonts/` to register one.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📚 Available Worlds",
            description="Use `/switch_world` to activate any world for this campaign.",
            color=0x7B68EE,
        )
        for w in worlds:
            tone = w.get("narrative_tone") or "No tone defined"
            tags = ", ".join(w.get("tags", [])) or "—"
            embed.add_field(
                name=f"{w['display_name']}  (`{w.get('system') or 'unknown'}`)",
                value=f"**Tone:** {tone}\n**Tags:** {tags}",
                inline=False,
            )
        embed.set_footer(text=f"{len(worlds)} world(s) registered")
        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as exc:
        logger.exception("Slash /worlds failed: %s", exc)
        await interaction.followup.send("⚠️ Could not fetch world list.", ephemeral=True)


@bot.tree.command(
    name="switch_world",
    description="Switch the campaign to a different RPG world/system. Creates it if it doesn't exist.",
)
@app_commands.describe(
    world_name="Folder name of the world (e.g. mothership, shadowrun, pirate_borg). "
               "Use underscores, no spaces.",
)
async def slash_switch_world(
    interaction: discord.Interaction,
    world_name: str,
) -> None:
    """
    Raise the Reality Wall around a new genre.

    If the world folder already exists in data/fonts/, it is activated
    immediately.  If it doesn't exist, the Scribe manifests the folder
    structure on the fly — no code changes required.
    """
    await interaction.response.defer()

    world_name = world_name.strip().lower().replace(" ", "_")
    if not world_name or not world_name.replace("_", "").replace("-", "").isalnum():
        await interaction.followup.send(
            "⚠️ Invalid world name. Use letters, numbers, and underscores only.",
            ephemeral=True,
        )
        return

    try:
        # Resolve active campaign for this guild
        campaign_resp = await bot.http_client.get(
            "/session",
            params={"guild_id": str(interaction.guild_id)},
        )
        if campaign_resp.status_code != 200:
            await interaction.followup.send(
                "⚠️ No active campaign found for this server.", ephemeral=True
            )
            return
        campaign_id = campaign_resp.json().get("campaign_id")
        if not campaign_id:
            await interaction.followup.send(
                "⚠️ Could not resolve campaign ID.", ephemeral=True
            )
            return

        # Call the world switch endpoint
        switch_resp = await bot.http_client.post(
            "/api/world/switch",
            json={"campaign_id": campaign_id, "world_name": world_name},
        )
        switch_resp.raise_for_status()
        data = switch_resp.json()

        schema      = data["schema"]
        manifested  = data.get("manifested", False)
        color       = int(schema.get("primary_color", "#FFFFFF").lstrip("#"), 16)
        display     = schema.get("display_name", world_name)
        tone        = schema.get("narrative_tone") or "Not yet defined"
        description = schema.get("description", "")[:300] or "Edit `world.json` to set a description."

        embed = discord.Embed(
            title=f"🌌 Reality Wall Raised — {display}",
            description=description,
            color=color,
        )
        embed.add_field(name="Narrative Tone", value=tone, inline=True)
        embed.add_field(name="System",         value=schema.get("system") or world_name, inline=True)
        embed.add_field(name="Dice",           value=schema.get("dice_notation") or "—", inline=True)
        if manifested:
            embed.add_field(
                name="✨ New World Manifested",
                value=(
                    f"The folder `data/fonts/{world_name}/world.json` was created for you. "
                    "Edit it to define tone, colour, and description."
                ),
                inline=False,
            )
        tags = ", ".join(schema.get("tags", [])) or "—"
        embed.set_footer(text=f"Tags: {tags}")

        await interaction.followup.send(embed=embed)

    except Exception as exc:
        logger.exception("Slash /switch_world failed: %s", exc)
        await interaction.followup.send("⚠️ World switch failed. Check the logs.", ephemeral=True)


# ─────────────────────────────────────────────────────────────────────────────
# Handout Commands
# ─────────────────────────────────────────────────────────────────────────────

handout_group = app_commands.Group(name="handout", description="View handouts delivered by the GM.")


@handout_group.command(name="list", description="List all handouts you have received.")
async def handout_list(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild_id:
        await interaction.followup.send("⚠️ Use this in a server.", ephemeral=True)
        return

    campaign = await _fetch_active_campaign(str(interaction.guild_id))
    if not campaign:
        await interaction.followup.send("⚠️ No active campaign.", ephemeral=True)
        return

    try:
        resp = await bot.http_client.get(
            f"/api/handouts/pending/{interaction.user.id}",
            params={"campaign_id": campaign["id"]},
        )
        resp.raise_for_status()
        handouts = resp.json()
    except Exception as exc:
        logger.exception("Handout list failed: %s", exc)
        await interaction.followup.send("⚠️ Could not retrieve handouts.", ephemeral=True)
        return

    if not handouts:
        await interaction.followup.send(
            "📜 You have no handouts yet. The GM will deliver them as the story unfolds.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="📜 Your Handouts",
        description="Use `/handout view <id>` to read a handout in full.",
        colour=0xC9A84C,
    )
    for h in handouts[:10]:
        embed.add_field(
            name=f"{h.get('title', 'Untitled')}  `{str(h['id'])[:8]}`",
            value=f"*{h.get('handout_type', 'general').replace('_', ' ').title()}*",
            inline=False,
        )
    if len(handouts) > 10:
        embed.set_footer(text=f"Showing 10 of {len(handouts)}")
    await interaction.followup.send(embed=embed, ephemeral=True)


@handout_group.command(name="view", description="Read the contents of a handout.")
@app_commands.describe(handout_id="The handout ID (first 8 chars are enough)")
async def handout_view(interaction: discord.Interaction, handout_id: str) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        resp = await bot.http_client.get(f"/api/handouts/{handout_id}")
        if resp.status_code == 404:
            await interaction.followup.send("⚠️ Handout not found.", ephemeral=True)
            return
        resp.raise_for_status()
        h = resp.json()
    except Exception as exc:
        logger.exception("Handout view failed: %s", exc)
        await interaction.followup.send("⚠️ Could not retrieve handout.", ephemeral=True)
        return

    embed = discord.Embed(
        title=h.get("title", "Untitled"),
        description=h.get("content_text", "")[:4000],
        colour=0xC9A84C,
    )
    embed.set_footer(text=f"Type: {h.get('handout_type', 'general').replace('_', ' ').title()}")
    if h.get("image_url"):
        embed.set_image(url=h["image_url"])
    await interaction.followup.send(embed=embed, ephemeral=True)


bot.tree.add_command(handout_group)


# ─────────────────────────────────────────────────────────────────────────────
# Map Commands
# ─────────────────────────────────────────────────────────────────────────────

map_group = app_commands.Group(name="map", description="Generate and view scene maps.")


@map_group.command(name="generate", description="[GM only] Generate a scene image from a description.")
@app_commands.describe(description="Describe the scene to paint (e.g. 'stone dungeon with glowing altar')")
async def map_generate(interaction: discord.Interaction, description: str) -> None:
    await interaction.response.defer()

    if isinstance(interaction.user, discord.Member):
        has_admin = any(r.name == _ADMIN_ROLE_NAME for r in interaction.user.roles)
    else:
        has_admin = False

    if not has_admin:
        await interaction.followup.send(
            f"⚠️ Only **{_ADMIN_ROLE_NAME}** members can generate maps.", ephemeral=True
        )
        return

    if not interaction.guild_id:
        await interaction.followup.send("⚠️ Use this in a server.", ephemeral=True)
        return

    campaign = await _fetch_active_campaign(str(interaction.guild_id))
    if not campaign:
        await interaction.followup.send("⚠️ No active campaign.", ephemeral=True)
        return

    await interaction.followup.send("🎨 Generating scene image…")

    try:
        resp = await bot.http_client.post(
            "/api/maps/generate",
            json={"prompt": description, "campaign_id": campaign["id"]},
            timeout=90,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.exception("Map generate failed: %s", exc)
        await interaction.edit_original_response(content="⚠️ Image generation failed. Check that an image backend is enabled in Settings.")
        return

    url = data.get("url")
    if not url:
        await interaction.edit_original_response(content="⚠️ No image returned. Image generation may be disabled.")
        return

    embed = discord.Embed(title=f"🗺 {description[:80]}", colour=0x5c8fd6)
    embed.set_image(url=url)
    await interaction.edit_original_response(content=None, embed=embed)


@map_group.command(name="show", description="Show the most recent scene image for this campaign.")
async def map_show(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild_id:
        await interaction.followup.send("⚠️ Use this in a server.", ephemeral=True)
        return

    campaign = await _fetch_active_campaign(str(interaction.guild_id))
    if not campaign:
        await interaction.followup.send("⚠️ No active campaign.", ephemeral=True)
        return

    try:
        resp = await bot.http_client.get(f"/api/maps/{campaign['id']}", params={"limit": 1})
        resp.raise_for_status()
        images = resp.json()
    except Exception as exc:
        logger.exception("Map show failed: %s", exc)
        await interaction.followup.send("⚠️ Could not retrieve scene images.", ephemeral=True)
        return

    if not images:
        await interaction.followup.send("No scene images have been generated yet.", ephemeral=True)
        return

    img = images[0]
    embed = discord.Embed(title="🗺 Current Scene", colour=0x5c8fd6)
    embed.set_image(url=img.get("image_url", ""))
    embed.set_footer(text=img.get("prompt", "")[:100])
    await interaction.followup.send(embed=embed, ephemeral=True)


bot.tree.add_command(map_group)


# ─────────────────────────────────────────────────────────────────────────────
# Reputation Command
# ─────────────────────────────────────────────────────────────────────────────

@bot.tree.command(name="reputation", description="View your standing with factions in this campaign.")
async def slash_reputation(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild_id:
        await interaction.followup.send("⚠️ Use this in a server.", ephemeral=True)
        return

    campaign = await _fetch_active_campaign(str(interaction.guild_id))
    if not campaign:
        await interaction.followup.send("⚠️ No active campaign.", ephemeral=True)
        return

    try:
        resp = await bot.http_client.get(
            f"/api/factions/{campaign['id']}/{interaction.user.id}"
        )
        resp.raise_for_status()
        standings = resp.json()
    except Exception as exc:
        logger.exception("Reputation fetch failed: %s", exc)
        await interaction.followup.send("⚠️ Could not retrieve reputation data.", ephemeral=True)
        return

    if not standings:
        await interaction.followup.send(
            "No factions are tracked in this campaign yet.", ephemeral=True
        )
        return

    _LABEL_COLOURS = {
        "Allied":   0x00FF88,
        "Friendly": 0x44BB44,
        "Neutral":  0xAAAA44,
        "Cautious": 0xFFAA00,
        "Hostile":  0xFF4444,
        "Enemy":    0x880000,
    }

    embed = discord.Embed(
        title="⚔ Faction Reputation",
        colour=0x7B68EE,
    )
    for s in standings:
        label  = s.get("label", "Neutral")
        score  = s.get("score", 0)
        colour = _LABEL_COLOURS.get(label, 0x888888)
        bar    = _progress_bar(max(0, min(100, (score + 100) // 2)))
        embed.add_field(
            name=s.get("name", "Unknown"),
            value=f"`{bar}` **{label}** ({score:+d})",
            inline=False,
        )
    embed.set_footer(text=f"Player: {interaction.user.display_name}")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────────────────────
# Music Commands
# ─────────────────────────────────────────────────────────────────────────────

music_group = app_commands.Group(name="music", description="Control and rate the GM's music selection.")

# guild_id → {prompt, audio_url, campaign_id}  (current track metadata for feedback)
_current_music_meta: dict[str, dict] = {}


@music_group.command(name="approve", description="Approve the current music track.")
async def music_approve(interaction: discord.Interaction) -> None:
    """Log a positive rating for the current track (thumbs up)."""
    await interaction.response.defer(ephemeral=True)

    guild_id = str(interaction.guild_id or "DM")
    meta = _current_music_meta.get(guild_id)
    if not meta:
        await interaction.followup.send("No music is currently tracked for this session.", ephemeral=True)
        return

    try:
        await bot.http_client.post(
            "/api/music/feedback",
            json={
                "campaign_id":    meta.get("campaign_id", ""),
                "original_prompt": meta.get("prompt", ""),
                "audio_url":      meta.get("audio_url", ""),
                "approved":       True,
                "feedback_note":  "",
                "player_id":      str(interaction.user.id),
            },
        )
    except Exception as exc:
        logger.debug("Music feedback (approve) failed: %s", exc)

    await interaction.followup.send("👍 Thanks — your rating helps the GM tune future sessions.", ephemeral=True)


@music_group.command(name="skip", description="Skip the current music without feedback.")
async def music_skip(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild_id:
        await interaction.followup.send("⚠️ Use this in a server.", ephemeral=True)
        return

    await bot.voice_mgr.stop_music(interaction.guild_id)
    await interaction.followup.send("⏭ Music skipped.", ephemeral=True)


@music_group.command(name="change", description="Request a different vibe and suggest how to improve it.")
@app_commands.describe(note="What would make the music better? (e.g. 'more tense', 'softer, we're in a tavern')")
async def music_change(interaction: discord.Interaction, note: str) -> None:
    """Rate negatively and request regeneration with the player's suggestion."""
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild_id:
        await interaction.followup.send("⚠️ Use this in a server.", ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    meta = _current_music_meta.get(guild_id)

    # Stop current music
    await bot.voice_mgr.stop_music(interaction.guild_id)

    if not meta or not meta.get("prompt"):
        await interaction.followup.send(
            "⏭ Music stopped. No prompt available for regeneration.", ephemeral=True
        )
        return

    await interaction.followup.send("🎵 Regenerating music with your feedback…", ephemeral=True)

    try:
        resp = await bot.http_client.post(
            "/api/music/regenerate",
            json={
                "campaign_id":    meta.get("campaign_id", ""),
                "original_prompt": meta.get("prompt", ""),
                "audio_url":      meta.get("audio_url", ""),
                "feedback_note":  note,
                "player_id":      str(interaction.user.id),
            },
            timeout=120,
        )
        if resp.status_code == 200:
            data = resp.json()
            new_url = data.get("audio_url", "")
            if new_url and interaction.guild_id:
                asyncio.create_task(bot.voice_mgr.play_music(
                    guild_id    = interaction.guild_id,
                    audio_url   = new_url,
                    volume      = 0.45,
                    crossfade_s = 1.0,
                ))
                _current_music_meta[guild_id] = {
                    **meta,
                    "audio_url": new_url,
                }
    except Exception as exc:
        logger.debug("Music regeneration failed: %s", exc)


@music_group.command(name="play", description="[GM only] Manually trigger a music change.")
@app_commands.describe(scene_type="Scene type: combat, exploration, social, tension, rest")
async def music_play(interaction: discord.Interaction, scene_type: str) -> None:
    await interaction.response.defer(ephemeral=True)

    if isinstance(interaction.user, discord.Member):
        has_admin = any(r.name == _ADMIN_ROLE_NAME for r in interaction.user.roles)
    else:
        has_admin = False

    if not has_admin:
        await interaction.followup.send(
            f"⚠️ Only **{_ADMIN_ROLE_NAME}** members can manually trigger music.", ephemeral=True
        )
        return

    if not interaction.guild_id:
        await interaction.followup.send("⚠️ Use this in a server.", ephemeral=True)
        return

    campaign = await _fetch_active_campaign(str(interaction.guild_id))
    if not campaign:
        await interaction.followup.send("⚠️ No active campaign.", ephemeral=True)
        return

    try:
        resp = await bot.http_client.post(
            "/api/music/generate",
            json={"scene_type": scene_type, "campaign_id": campaign["id"]},
            timeout=90,
        )
        resp.raise_for_status()
        data = resp.json()
        url  = data.get("audio_url", "")
    except Exception as exc:
        logger.exception("Manual music generate failed: %s", exc)
        await interaction.followup.send("⚠️ Music generation failed.", ephemeral=True)
        return

    if url:
        asyncio.create_task(bot.voice_mgr.play_music(
            guild_id    = interaction.guild_id,
            audio_url   = url,
            volume      = 0.45,
            crossfade_s = 1.5,
        ))
        guild_id_str = str(interaction.guild_id)
        _current_music_meta[guild_id_str] = {
            "prompt": data.get("music_prompt", ""),
            "audio_url": url,
            "campaign_id": campaign["id"],
        }
        await interaction.followup.send(
            f"🎵 Playing **{scene_type}** music.", ephemeral=True
        )
    else:
        await interaction.followup.send(
            "⚠️ No audio generated. Music may be disabled or use lavalink fallback.",
            ephemeral=True,
        )


bot.tree.add_command(music_group)


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    bot.run(DISCORD_BOT_TOKEN)
