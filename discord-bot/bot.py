"""
Ironclad GM – Discord Bot Listener
=====================================
Intercepts player messages and slash commands, constructs IntentPayloads,
and forwards them to the Orchestrator.  Delivers GM narrative responses
with the full Living Discord immersion layer (Task 4).

Task 4 — Living Discord Immersion Layer
-----------------------------------------
  1. Paranoia Whisper System
       After every turn with NPC interactions, the bot silently DMs the
       player a 2-3 sentence private perception check — what their skeptical,
       paranoid character notices that no one else would catch.

  2. Ephemeral Ghost Sheet Threads
       Main channel receives only narrative prose.  When combat starts the
       bot automatically opens a Discord Thread ("⚔️ Combat – Thug Alley")
       on the narrative message.  All mechanical grit — dice rolls, damage,
       stat changes, rulebook citations — goes inside the thread.  When the
       fight ends the bot locks the thread.  The math is always there if you
       want it; it never clutters the story.

  3. Voice Channel Puppeteering
       The bot joins the player's voice channel.  On scene type changes it
       loops an ambient audio track (tavern chatter, dungeon hum, combat
       tension).  When an NPC speaks, the bot pauses the ambient, speaks the
       NPC's dialogue via edge-tts with that Ollama node's unique voice
       profile, then resumes ambient.

  4. Channel Manipulation
       If the narrative warrants a location change (captured → dungeon,
       escaped → main), the bot grants / revokes the player's channel
       permissions accordingly.  Channel ID mapping is configured via env vars:
         DUNGEON_CHANNEL_ID, PRISON_CHANNEL_ID, HOSPITAL_CHANNEL_ID,
         MAIN_CHANNEL_ID

PDF Ingestion (unchanged from Task 2):
  /upload_rulebook — explicit slash command
  Auto-detect      — any PDF attachment triggers ingestion
"""

from __future__ import annotations

import asyncio
import logging
import os
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

# ── Channel Key → Discord Channel ID mapping (configured via env vars) ────────
_CHANNEL_IDS: dict[str, int | None] = {
    "dungeon":  int(os.environ["DUNGEON_CHANNEL_ID"])  if os.environ.get("DUNGEON_CHANNEL_ID")  else None,
    "prison":   int(os.environ["PRISON_CHANNEL_ID"])   if os.environ.get("PRISON_CHANNEL_ID")   else None,
    "hospital": int(os.environ["HOSPITAL_CHANNEL_ID"]) if os.environ.get("HOSPITAL_CHANNEL_ID") else None,
    "main":     int(os.environ["MAIN_CHANNEL_ID"])     if os.environ.get("MAIN_CHANNEL_ID")     else None,
}

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

class IroncladBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states    = True   # required for voice channel tracking
        intents.members         = True   # required for channel permission changes
        super().__init__(command_prefix="!", intents=intents)
        self._http_client: httpx.AsyncClient | None = None
        self.voice_mgr = VoiceManager()

    async def setup_hook(self) -> None:
        self._http_client = httpx.AsyncClient(
            base_url=ORCHESTRATOR_URL,
            timeout=httpx.Timeout(connect=10, read=300, write=300, pool=10),
        )
        await self.tree.sync()
        logger.info("Slash commands synced.")

    async def close(self) -> None:
        for guild in self.guilds:
            await self.voice_mgr.disconnect(guild.id)
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


bot = IroncladBot()


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

    "move_to"  → grant read-only access to dungeon/prison/hospital channel.
    "restore"  → restore full send access to the main channel.

    Channel IDs are mapped from env vars.  If a key is not configured,
    the directive is logged and skipped — no crash, no noise.
    Requires bot 'Manage Permissions' permission in the target channel.
    """
    action      = directive.get("action")
    channel_key = directive.get("channel_key")
    reason_str  = directive.get("reason", "GM narrative directive")

    target_id = _CHANNEL_IDS.get(channel_key)
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

    async with message.channel.typing():
        try:
            data = await _dispatch_intent(
                player_id=str(message.author.id),
                guild_id=str(message.guild.id) if message.guild else "DM",
                channel_id=str(message.channel.id),
                raw_input=raw_input,
            )
            await _deliver_narrative(
                channel=message.channel,
                data=data,
                user=message.author,
                guild=message.guild,
            )
        except httpx.HTTPStatusError as exc:
            await message.channel.send(
                f"⚠️ The GM engine returned an error: `{exc.response.status_code}`"
            )
        except Exception as exc:
            logger.exception("Action dispatch failed: %s", exc)
            await message.channel.send("⚠️ An internal error occurred. The GM is unavailable.")


# ─────────────────────────────────────────────────────────────────────────────
# Slash Commands
# ─────────────────────────────────────────────────────────────────────────────

@bot.tree.command(name="act", description="Declare an in-character action.")
@app_commands.describe(action="What does your character do?")
async def slash_act(interaction: discord.Interaction, action: str) -> None:
    await interaction.response.defer()
    try:
        data = await _dispatch_intent(
            player_id=str(interaction.user.id),
            guild_id=str(interaction.guild_id or "DM"),
            channel_id=str(interaction.channel_id),
            raw_input=action,
            command_type="slash_command",
            slash_data={"command_name": "act", "options": {"action": action}},
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

    except httpx.HTTPStatusError as exc:
        await interaction.followup.send(
            f"⚠️ Engine error `{exc.response.status_code}`: {exc.response.text[:200]}"
        )
    except Exception as exc:
        logger.exception("Slash /act failed: %s", exc)
        await interaction.followup.send("⚠️ Internal error. Please try again.")


@bot.tree.command(name="status", description="Check your character's current stats.")
async def slash_status(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        data = await _dispatch_intent(
            player_id=str(interaction.user.id),
            guild_id=str(interaction.guild_id or "DM"),
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


@bot.tree.command(name="inventory", description="View your character's inventory.")
async def slash_inventory(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        data = await _dispatch_intent(
            player_id=str(interaction.user.id),
            guild_id=str(interaction.guild_id or "DM"),
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
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    bot.run(DISCORD_BOT_TOKEN)
