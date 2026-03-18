"""
Ironclad GM – Discord Bot Listener
=====================================
Intercepts player messages and slash commands, constructs IntentPayloads,
and forwards them to the Orchestrator. Streams Gemini narrative responses
back to the Discord channel as rich embeds.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import discord
import httpx
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

ORCHESTRATOR_URL = os.environ["ORCHESTRATOR_URL"]
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]

# Embed colour map by outcome
OUTCOME_COLORS: dict[str, int] = {
    "critical_success": 0x00FF88,
    "success":          0x44BB44,
    "partial_success":  0xFFAA00,
    "failure":          0xFF4444,
    "critical_failure": 0x880000,
}


class IroncladBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self._http_client: httpx.AsyncClient | None = None

    async def setup_hook(self) -> None:
        self._http_client = httpx.AsyncClient(base_url=ORCHESTRATOR_URL, timeout=120)
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
        logger.info("Ironclad GM is online as %s", self.user)


bot = IroncladBot()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _get_or_create_session(player_id: str, guild_id: str, channel_id: str) -> str:
    """Ensure a session exists in the Orchestrator cache; return its token."""
    token = str(uuid.uuid4())
    try:
        await bot.http_client.post(
            "/session",
            params={
                "player_id":    player_id,
                "guild_id":     guild_id,
                "channel_id":   channel_id,
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


def _build_embed(data: dict[str, Any], user: discord.User | discord.Member) -> discord.Embed:
    outcome = data.get("outcome", "")
    colour = OUTCOME_COLORS.get(outcome, 0x888888)
    embed = discord.Embed(
        title=data.get("embed_title", "The dice have spoken."),
        description=data.get("narrative", "No narrative generated."),
        colour=colour,
    )
    embed.set_footer(text=f"Player: {user.display_name}")
    return embed


# ─────────────────────────────────────────────────────────────────────────────
# Free-Text Action Listener
# ─────────────────────────────────────────────────────────────────────────────

@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    # Only process messages prefixed with > (in-character actions)
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
                command_type="action",
            )
            embed = _build_embed(data, message.author)
            await message.channel.send(embed=embed)
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
        embed = _build_embed(data, interaction.user)
        await interaction.followup.send(embed=embed)
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
        embed = _build_embed(data, interaction.user)
        await interaction.followup.send(embed=embed, ephemeral=True)
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
        embed = _build_embed(data, interaction.user)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        logger.exception("Slash /inventory failed: %s", exc)
        await interaction.followup.send("⚠️ Could not retrieve inventory.", ephemeral=True)


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    bot.run(DISCORD_BOT_TOKEN)
