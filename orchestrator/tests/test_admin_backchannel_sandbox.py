"""
Unit tests for AdminBackchannelService and SandboxService.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from orchestrator.schemas.payloads import (
    DirectiveType,
    GMDirective,
    GMDirectiveRequest,
)
from orchestrator.services.admin_backchannel import AdminBackchannelService
from orchestrator.services.sandbox import SandboxService


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

CAMPAIGN_ID   = "a0000000-0000-0000-0000-000000000001"
ADMIN_ID      = "admin_999"
DIRECTIVE_ID  = "d0000000-0000-0000-0000-000000000002"
INTENT_ID     = "e0000000-0000-0000-0000-000000000003"
NOW           = datetime.now(timezone.utc)


def _make_pool():
    pool = MagicMock()
    pool.fetchrow = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])
    pool.execute = AsyncMock()
    return pool


def _directive_row():
    return {
        "id":             UUID(DIRECTIVE_ID),
        "admin_id":       ADMIN_ID,
        "directive_type": DirectiveType.SCENE_DIRECTIVE.value,
        "directive_text": "Make the lights flicker",
        "priority":       7,
        "status":         "pending",
        "submitted_at":   NOW,
        "consumed_at":    None,
    }


def _make_req(text="Make the lights flicker", priority=7):
    return GMDirectiveRequest(
        campaign_id=CAMPAIGN_ID,
        admin_id=ADMIN_ID,
        directive_type=DirectiveType.SCENE_DIRECTIVE,
        directive_text=text,
        priority=priority,
    )


# ─────────────────────────────────────────────────────────────────────────────
# AdminBackchannelService tests
# ─────────────────────────────────────────────────────────────────────────────


class TestSubmitDirective:
    """submit_directive() persists and returns a GMDirective."""

    @pytest.mark.asyncio
    async def test_submit_returns_gm_directive(self):
        pool = _make_pool()
        pool.fetchrow.return_value = {
            "id":           UUID(DIRECTIVE_ID),
            "status":       "pending",
            "submitted_at": NOW,
        }
        svc = AdminBackchannelService(pool)
        result = await svc.submit_directive(_make_req())

        assert isinstance(result, GMDirective)
        assert result.directive_id == DIRECTIVE_ID
        assert result.campaign_id == CAMPAIGN_ID
        assert result.admin_id == ADMIN_ID
        assert result.priority == 7
        assert result.status == "pending"

    @pytest.mark.asyncio
    async def test_submit_passes_correct_values_to_db(self):
        pool = _make_pool()
        pool.fetchrow.return_value = {
            "id":           UUID(DIRECTIVE_ID),
            "status":       "pending",
            "submitted_at": NOW,
        }
        svc = AdminBackchannelService(pool)
        await svc.submit_directive(_make_req(text="Earthquake incoming", priority=9))

        args = pool.fetchrow.call_args.args
        assert UUID(CAMPAIGN_ID) in args
        assert ADMIN_ID in args
        assert "Earthquake incoming" in args
        assert 9 in args

    @pytest.mark.asyncio
    async def test_submit_npc_hint_directive_type(self):
        pool = _make_pool()
        pool.fetchrow.return_value = {
            "id":           UUID(DIRECTIVE_ID),
            "status":       "pending",
            "submitted_at": NOW,
        }
        req = GMDirectiveRequest(
            campaign_id=CAMPAIGN_ID,
            admin_id=ADMIN_ID,
            directive_type=DirectiveType.NPC_HINT,
            directive_text="Have Mira mention the blacksmith",
            priority=5,
        )
        svc = AdminBackchannelService(pool)
        result = await svc.submit_directive(req)
        assert result.directive_type == DirectiveType.NPC_HINT


class TestGetPendingDirectives:
    """get_pending_directives() returns sorted pending directives."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_pending(self):
        pool = _make_pool()
        pool.fetch.return_value = []
        svc = AdminBackchannelService(pool)
        result = await svc.get_pending_directives(CAMPAIGN_ID)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_gm_directive_objects(self):
        pool = _make_pool()
        pool.fetch.return_value = [_directive_row()]
        svc = AdminBackchannelService(pool)
        result = await svc.get_pending_directives(CAMPAIGN_ID)

        assert len(result) == 1
        assert isinstance(result[0], GMDirective)
        assert result[0].directive_text == "Make the lights flicker"

    @pytest.mark.asyncio
    async def test_respects_limit_parameter(self):
        pool = _make_pool()
        pool.fetch.return_value = []
        svc = AdminBackchannelService(pool)
        await svc.get_pending_directives(CAMPAIGN_ID, limit=1)

        args = pool.fetch.call_args.args
        assert 1 in args

    @pytest.mark.asyncio
    async def test_default_limit_is_three(self):
        pool = _make_pool()
        pool.fetch.return_value = []
        svc = AdminBackchannelService(pool)
        await svc.get_pending_directives(CAMPAIGN_ID)

        args = pool.fetch.call_args.args
        assert 3 in args


class TestConsumeDirectives:
    """consume_directives() marks directives as consumed."""

    @pytest.mark.asyncio
    async def test_consume_updates_status(self):
        pool = _make_pool()
        svc = AdminBackchannelService(pool)
        await svc.consume_directives([DIRECTIVE_ID], INTENT_ID)

        pool.execute.assert_called_once()
        sql, intent_uuid, uuids = pool.execute.call_args.args
        assert "consumed" in sql.lower()
        assert intent_uuid == UUID(INTENT_ID)
        assert UUID(DIRECTIVE_ID) in uuids

    @pytest.mark.asyncio
    async def test_empty_list_is_noop(self):
        pool = _make_pool()
        svc = AdminBackchannelService(pool)
        await svc.consume_directives([], INTENT_ID)
        pool.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_consumes_multiple_directives(self):
        pool = _make_pool()
        svc = AdminBackchannelService(pool)
        ids = [DIRECTIVE_ID, "f0000000-0000-0000-0000-000000000099"]
        await svc.consume_directives(ids, INTENT_ID)
        _, _, uuids = pool.execute.call_args.args
        assert len(uuids) == 2


class TestCancelDirective:
    """cancel_directive() cancels a pending directive."""

    @pytest.mark.asyncio
    async def test_cancel_updates_status_to_cancelled(self):
        pool = _make_pool()
        svc = AdminBackchannelService(pool)
        await svc.cancel_directive(DIRECTIVE_ID)

        pool.execute.assert_called_once()
        sql, uuid_arg = pool.execute.call_args.args
        assert "cancelled" in sql.lower()
        assert uuid_arg == UUID(DIRECTIVE_ID)


class TestGetRecentDirectives:
    """get_recent_directives() returns historical directives."""

    @pytest.mark.asyncio
    async def test_returns_formatted_dict_list(self):
        pool = _make_pool()
        pool.fetch.return_value = [_directive_row()]
        svc = AdminBackchannelService(pool)
        result = await svc.get_recent_directives(CAMPAIGN_ID)

        assert len(result) == 1
        record = result[0]
        assert record["directive_id"] == DIRECTIVE_ID
        assert record["admin_id"] == ADMIN_ID
        assert record["status"] == "pending"
        assert "consumed_at" in record
        assert record["consumed_at"] is None

    @pytest.mark.asyncio
    async def test_default_limit_is_30(self):
        pool = _make_pool()
        pool.fetch.return_value = []
        svc = AdminBackchannelService(pool)
        await svc.get_recent_directives(CAMPAIGN_ID)
        args = pool.fetch.call_args.args
        assert 30 in args


# ─────────────────────────────────────────────────────────────────────────────
# SandboxService tests
# ─────────────────────────────────────────────────────────────────────────────


def _make_sandbox(storyteller_response="GM response"):
    gemini = MagicMock()
    gemini.generate = AsyncMock(return_value=storyteller_response)
    gemini.generate_with_image = AsyncMock(return_value="Described image.")

    router = MagicMock()
    router.is_storyteller_enabled = AsyncMock(return_value=True)
    router.get_storyteller_client = AsyncMock(return_value=gemini)

    memory = MagicMock()
    memory.retrieve_relevant_context = AsyncMock(return_value=[])

    web_search = MagicMock()
    web_search.search = AsyncMock(return_value=[])

    svc = SandboxService(
        gemini=gemini,
        node_router=router,
        story_memory=memory,
        web_search=web_search,
    )
    return svc, gemini, router, memory, web_search


class TestSandboxChat:
    """chat() routes to GM storyteller or NPC persona."""

    @pytest.mark.asyncio
    async def test_basic_chat_returns_response(self):
        svc, _, _, _, _ = _make_sandbox("Hello, adventurer!")
        result = await svc.chat("Hello?", CAMPAIGN_ID)
        assert result["response"] == "Hello, adventurer!"
        assert result["search_results"] == []
        assert result["persona"] is None

    @pytest.mark.asyncio
    async def test_persona_mode_uses_npc_prompt(self):
        svc, gemini, _, _, _ = _make_sandbox()
        result = await svc.chat("How much for a room?", CAMPAIGN_ID, persona="Mira the Innkeeper")
        assert result["persona"] == "Mira the Innkeeper"
        call_kwargs = gemini.generate.call_args
        system = call_kwargs.kwargs.get("system_prompt", "") or call_kwargs.args[0]
        assert "Mira the Innkeeper" in system

    @pytest.mark.asyncio
    async def test_web_search_enabled_injects_results(self):
        svc, gemini, _, _, web_search = _make_sandbox()
        web_search.search.return_value = [
            {"title": "Medieval Siege", "snippet": "Trebuchets were used…"}
        ]
        result = await svc.chat("Tell me about siege warfare", CAMPAIGN_ID, use_search=True)
        web_search.search.assert_awaited_once()
        assert result["search_results"][0]["title"] == "Medieval Siege"

    @pytest.mark.asyncio
    async def test_web_search_disabled_skips_search(self):
        svc, _, _, _, web_search = _make_sandbox()
        await svc.chat("hello", CAMPAIGN_ID, use_search=False)
        web_search.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lore_facts_injected_from_story_memory(self):
        svc, gemini, _, memory, _ = _make_sandbox()
        fact = MagicMock()
        fact.entity_type = MagicMock()
        fact.entity_type.value = "npc"
        fact.entity_name = "Mira"
        fact.summary = "Mira is the innkeeper of The Rusty Flagon."
        memory.retrieve_relevant_context.return_value = [fact]

        result = await svc.chat("Who is Mira?", CAMPAIGN_ID)
        assert result["lore_facts"] == 1
        # The lore should have been prepended to the user prompt
        call_kwargs = gemini.generate.call_args.kwargs
        user_prompt = call_kwargs.get("user_prompt", "") or gemini.generate.call_args.args[1]
        assert "Mira" in user_prompt

    @pytest.mark.asyncio
    async def test_lore_retrieval_failure_does_not_raise(self):
        svc, _, _, memory, _ = _make_sandbox()
        memory.retrieve_relevant_context.side_effect = RuntimeError("chroma offline")
        result = await svc.chat("query", CAMPAIGN_ID)
        assert result["lore_facts"] == 0

    @pytest.mark.asyncio
    async def test_generation_failure_returns_error_string(self):
        svc, gemini, _, _, _ = _make_sandbox()
        gemini.generate.side_effect = RuntimeError("API timeout")
        result = await svc.chat("hello", CAMPAIGN_ID)
        assert "unavailable" in result["response"].lower() or "API timeout" in result["response"]

    @pytest.mark.asyncio
    async def test_uses_local_node_when_cloud_disabled(self):
        svc, gemini, router, _, _ = _make_sandbox()
        local_node = MagicMock()
        local_node.generate = AsyncMock(return_value="Local response")
        router.is_storyteller_enabled.return_value = False
        router.get_storyteller_client.return_value = local_node

        result = await svc.chat("hello", CAMPAIGN_ID)
        assert result["response"] == "Local response"

    @pytest.mark.asyncio
    async def test_falls_back_to_gemini_when_no_local_node(self):
        svc, gemini, router, _, _ = _make_sandbox("Gemini fallback")
        router.is_storyteller_enabled.return_value = False
        router.get_storyteller_client.return_value = None

        result = await svc.chat("hello", CAMPAIGN_ID)
        assert result["response"] == "Gemini fallback"
        gemini.generate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_image_analysis_prepended_to_prompt(self):
        svc, gemini, _, _, _ = _make_sandbox()
        gemini.generate_with_image = AsyncMock(return_value="A dark forest scene.")

        await svc.chat("Describe this", CAMPAIGN_ID, image_url="http://host/img.png")
        call_kwargs = gemini.generate.call_args.kwargs
        user_prompt = call_kwargs.get("user_prompt", "") or gemini.generate.call_args.args[1]
        assert "A dark forest scene." in user_prompt

    @pytest.mark.asyncio
    async def test_image_analysis_failure_is_non_fatal(self):
        svc, gemini, _, _, _ = _make_sandbox()
        gemini.generate_with_image = AsyncMock(side_effect=RuntimeError("vision error"))
        result = await svc.chat("hello", CAMPAIGN_ID, image_url="http://x/img.png")
        assert "response" in result
