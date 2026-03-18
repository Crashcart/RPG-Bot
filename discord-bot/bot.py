"""
Ironclad GM – Discord Bot Listener
=====================================
Intercepts player messages and slash commands, constructs IntentPayloads,
and forwards them to the Orchestrator. Streams Gemini narrative responses
back to the Discord channel as rich embeds.

PDF Ingestion (two paths):
  1. /upload_rulebook  — explicit slash command with attachment + name + campaign
  2. Auto-detect       — any PDF dropped in a channel is caught, the bot looks up
                         the guild's active campaign and starts ingestion
                         automatically, updating a live progress embed.
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

logger = logging.getLogger(__name__)

ORCHESTRATOR_URL  = os.environ["ORCHESTRATOR_URL"]
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
# Max PDF size the bot will download from Discord (bytes). Discord's own cap
# is 25 MB for regular servers, 500 MB for boosted — we stay well under.
_MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024

# Outcome → embed colour
OUTCOME_COLORS: dict[str, int] = {
    "critical_success": 0x00FF88,
    "success":          0x44BB44,
    "partial_success":  0xFFAA00,
    "failure":          0xFF4444,
    "critical_failure": 0x880000,
}

# Progress bar characters
_BAR_FILL  = "█"
_BAR_EMPTY = "░"
_BAR_WIDTH = 20


def _progress_bar(pct: int) -> str:
    filled = round(_BAR_WIDTH * pct / 100)
    return _BAR_FILL * filled + _BAR_EMPTY * (_BAR_WIDTH - filled)


# ─────────────────────────────────────────────────────────────────────────────
# Bot Class
# ─────────────────────────────────────────────────────────────────────────────

class IroncladBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self._http_client: httpx.AsyncClient | None = None

    async def setup_hook(self) -> None:
        # Long timeout for PDF uploads (large files can take time)
        self._http_client = httpx.AsyncClient(
            base_url=ORCHESTRATOR_URL,
            timeout=httpx.Timeout(connect=10, read=300, write=300, pool=10),
        )
        await self.tree.sync()
        logger.info("Slash commands synced.")

    async def close(self) -> None:
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
    player_id: str,
    guild_id: str,
    channel_id: str,
    raw_input: str,
    command_type: str = "action",
    slash_data: dict[str, Any] | None = None,
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
    data: dict[str, Any], user: discord.User | discord.Member
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
# PDF Ingestion Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_active_campaign(guild_id: str) -> dict[str, Any] | None:
    """Look up the guild's active campaign from the orchestrator."""
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
        title=f"📄 Ingesting: {name}",
        description=desc,
        colour=colour,
    ).set_footer(text="Updates every 3 seconds")


def _ingest_embed_done(data: dict) -> discord.Embed:
    name   = data.get("module_name", "Rulebook")
    chunks = data.get("chunks", "?")
    coll   = data.get("collection", "")
    embed  = discord.Embed(
        title="✅ Rulebook Ready",
        description=(
            f"**{name}** has been indexed and is now available to the GM.\n\n"
            f"**{chunks}** chunks stored in `{coll}`\n"
            f"`{_progress_bar(100)}` 100%\n\n"
            f"The mechanical engine will cite this rulebook automatically."
        ),
        colour=0x4caf78,
    )
    return embed


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
    msg: discord.Message | None,
    interaction_followup: Any | None,
    pdf_bytes: bytes,
    filename: str,
    module_name: str,
    campaign_id: str,
) -> None:
    """
    Uploads PDF bytes to the orchestrator, then polls and edits the Discord
    message with live progress until the job completes or fails.

    Exactly one of `msg` or `interaction_followup` will be set.
    """

    async def _edit(embed: discord.Embed) -> None:
        try:
            if msg:
                await msg.edit(embed=embed)
            elif interaction_followup:
                await interaction_followup.edit(embed=embed)
        except Exception as e:
            logger.warning("Could not edit progress message: %s", e)

    # ── Upload PDF to orchestrator ─────────────────────────────────────────
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

    # ── Poll for progress ─────────────────────────────────────────────────
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
        a for a in message.attachments
        if a.filename.lower().endswith(".pdf")
    ]
    if pdf_attachments and message.guild:
        for attachment in pdf_attachments:
            # Size guard
            if attachment.size > _MAX_DOWNLOAD_BYTES:
                await message.reply(
                    f"⚠️ **{attachment.filename}** is too large to ingest "
                    f"({attachment.size // (1024*1024)} MB). Maximum is 200 MB.",
                    mention_author=False,
                )
                continue

            # Look up active campaign for this guild
            campaign = await _fetch_active_campaign(str(message.guild.id))
            if not campaign:
                await message.reply(
                    "⚠️ No active campaign found for this server. "
                    "Create one before ingesting rulebooks.",
                    mention_author=False,
                )
                continue

            # Module name = filename without extension
            module_name = attachment.filename[:-4].replace("_", " ").replace("-", " ").strip()

            # Post initial progress embed and download the PDF concurrently
            progress_embed = _ingest_embed_queued(module_name, attachment.filename)
            progress_msg   = await message.reply(embed=progress_embed, mention_author=False)

            pdf_bytes = await attachment.read()

            # Fire-and-forget the ingestion task
            asyncio.create_task(_run_ingest_and_report(
                msg=progress_msg,
                interaction_followup=None,
                pdf_bytes=pdf_bytes,
                filename=attachment.filename,
                module_name=module_name,
                campaign_id=campaign["id"],
            ))
        return  # Don't process PDF messages as actions

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
            await message.channel.send(embed=_build_action_embed(data, message.author))
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
        await interaction.followup.send(
            embed=_build_action_embed(data, interaction.user)
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
    file: discord.Attachment,
    module_name: str = "",
    campaign_id: str = "",
) -> None:
    await interaction.response.defer()

    # ── Validate attachment ────────────────────────────────────────────────
    if not file.filename.lower().endswith(".pdf"):
        await interaction.followup.send("⚠️ Only PDF files are supported.")
        return
    if file.size > _MAX_DOWNLOAD_BYTES:
        await interaction.followup.send(
            f"⚠️ File is too large ({file.size // (1024*1024)} MB). Maximum is 200 MB."
        )
        return

    # ── Resolve campaign ──────────────────────────────────────────────────
    if not campaign_id and interaction.guild_id:
        campaign = await _fetch_active_campaign(str(interaction.guild_id))
        if not campaign:
            await interaction.followup.send(
                "⚠️ No active campaign on this server.\n"
                "Provide a `campaign_id` or create a campaign first."
            )
            return
        campaign_id = campaign["id"]

    if not campaign_id:
        await interaction.followup.send("⚠️ Could not determine campaign. Provide a `campaign_id`.")
        return

    # ── Default module name ───────────────────────────────────────────────
    if not module_name:
        module_name = file.filename[:-4].replace("_", " ").replace("-", " ").strip()

    # ── Post initial embed ────────────────────────────────────────────────
    progress_embed = _ingest_embed_queued(module_name, file.filename)
    followup_msg   = await interaction.followup.send(embed=progress_embed, wait=True)

    # ── Download PDF ──────────────────────────────────────────────────────
    try:
        pdf_bytes = await file.read()
    except Exception as exc:
        await followup_msg.edit(embed=discord.Embed(
            title="❌ Download Failed",
            description=f"Could not download the file from Discord.\n```{exc}```",
            colour=0xcf4c5a,
        ))
        return

    # ── Kick off ingestion with live updates ──────────────────────────────
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
