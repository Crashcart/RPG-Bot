"""
Unit tests for orchestrator/services/sandbox.py

Covers:
  SandboxService.chat — all keyword paths
  SandboxService._select_storyteller — cloud vs. local vs. fallback
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.sandbox import SandboxService, _NPC_PERSONA_PROMPT, _SANDBOX_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def gemini():
    m = MagicMock()
    m.generate = AsyncMock(return_value="Gemini narrative")
    m.generate_with_image = AsyncMock(return_value="Image description")
    return m


@pytest.fixture
def node_router():
    m = MagicMock()
    m.is_storyteller_enabled = AsyncMock(return_value=True)  # default: cloud
    m.get_storyteller_client = AsyncMock(return_value=None)
    return m


@pytest.fixture
def story_memory():
    m = MagicMock()
    m.retrieve_relevant_context = AsyncMock(return_value=[])
    return m


@pytest.fixture
def web_search():
    m = MagicMock()
    m.search = AsyncMock(return_value=[])
    return m


@pytest.fixture
def svc(gemini, node_router, story_memory, web_search):
    return SandboxService(
        gemini=gemini,
        node_router=node_router,
        story_memory=story_memory,
        web_search=web_search,
    )


# ---------------------------------------------------------------------------
# TestChatBasic
# ---------------------------------------------------------------------------

class TestChatBasic:
    @pytest.mark.asyncio
    async def test_returns_response_dict_with_required_keys(self, svc, gemini):
        result = await svc.chat(message="Hello world", campaign_id="camp-1")

        assert "response" in result
        assert "search_results" in result
        assert "persona" in result
        assert "lore_facts" in result

    @pytest.mark.asyncio
    async def test_response_comes_from_storyteller(self, svc, gemini):
        gemini.generate = AsyncMock(return_value="GM says hello")
        result = await svc.chat(message="Hello", campaign_id="camp-1")
        assert result["response"] == "GM says hello"

    @pytest.mark.asyncio
    async def test_no_persona_uses_sandbox_system_prompt(self, svc, gemini):
        await svc.chat(message="Test", campaign_id="camp-1")
        call_kwargs = gemini.generate.call_args
        system_prompt = call_kwargs[1].get("system_prompt") or call_kwargs[0][0]
        assert "SANDBOX MODE" in system_prompt

    @pytest.mark.asyncio
    async def test_no_search_results_empty_list(self, svc, web_search):
        result = await svc.chat(message="Test", campaign_id="camp-1", use_search=False)
        web_search.search.assert_not_called()
        assert result["search_results"] == []

    @pytest.mark.asyncio
    async def test_persona_is_echoed_back(self, svc):
        result = await svc.chat(message="Hello", campaign_id="camp-1", persona=None)
        assert result["persona"] is None


# ---------------------------------------------------------------------------
# TestChatWithPersona
# ---------------------------------------------------------------------------

class TestChatWithPersona:
    @pytest.mark.asyncio
    async def test_persona_uses_npc_prompt(self, svc, gemini):
        await svc.chat(message="Hello", campaign_id="camp-1", persona="Grib the Goblin")

        call_kwargs = gemini.generate.call_args
        system_prompt = call_kwargs[1].get("system_prompt") or call_kwargs[0][0]
        assert "Grib the Goblin" in system_prompt
        assert "SANDBOX MODE" not in system_prompt

    @pytest.mark.asyncio
    async def test_persona_echoed_in_result(self, svc):
        result = await svc.chat(message="Hello", campaign_id="camp-1", persona="Mira")
        assert result["persona"] == "Mira"


# ---------------------------------------------------------------------------
# TestChatWithSearch
# ---------------------------------------------------------------------------

class TestChatWithSearch:
    @pytest.mark.asyncio
    async def test_search_called_when_flag_set(self, svc, web_search):
        web_search.search = AsyncMock(return_value=[
            {"title": "Medieval siege", "snippet": "Catapults were used."}
        ])
        result = await svc.chat(message="siege warfare", campaign_id="camp-1", use_search=True)

        web_search.search.assert_called_once()
        assert len(result["search_results"]) == 1

    @pytest.mark.asyncio
    async def test_search_results_injected_into_prompt(self, svc, gemini, web_search):
        web_search.search = AsyncMock(return_value=[
            {"title": "Warfare", "snippet": "Catapults."}
        ])
        await svc.chat(message="siege", campaign_id="camp-1", use_search=True)

        user_prompt = gemini.generate.call_args[1].get("user_prompt") or gemini.generate.call_args[0][1]
        assert "WEB RESEARCH" in user_prompt

    @pytest.mark.asyncio
    async def test_empty_search_results_no_block(self, svc, gemini, web_search):
        web_search.search = AsyncMock(return_value=[])
        await svc.chat(message="siege", campaign_id="camp-1", use_search=True)

        user_prompt = gemini.generate.call_args[1].get("user_prompt") or gemini.generate.call_args[0][1]
        assert "WEB RESEARCH" not in user_prompt


# ---------------------------------------------------------------------------
# TestChatWithImage
# ---------------------------------------------------------------------------

class TestChatWithImage:
    @pytest.mark.asyncio
    async def test_image_analysis_injected_into_prompt(self, svc, gemini, node_router):
        node_router.is_storyteller_enabled = AsyncMock(return_value=True)
        gemini.generate_with_image = AsyncMock(return_value="A dark forest")

        await svc.chat(message="Describe", campaign_id="camp-1", image_url="http://img.test/a.png")

        user_prompt = gemini.generate.call_args[1].get("user_prompt") or gemini.generate.call_args[0][1]
        assert "IMAGE ANALYSIS" in user_prompt
        assert "A dark forest" in user_prompt

    @pytest.mark.asyncio
    async def test_image_analysis_failure_graceful_fallback(self, svc, gemini, node_router):
        node_router.is_storyteller_enabled = AsyncMock(return_value=True)
        gemini.generate_with_image = AsyncMock(side_effect=Exception("Vision API error"))

        result = await svc.chat(message="Describe", campaign_id="camp-1", image_url="http://img.test/a.png")

        # Should not raise; response is still returned
        assert "response" in result

    @pytest.mark.asyncio
    async def test_no_generate_with_image_attr_skips_silently(self, svc, gemini, node_router):
        del gemini.generate_with_image  # remove the attribute
        node_router.is_storyteller_enabled = AsyncMock(return_value=True)

        # Should not raise even if storyteller lacks image capability
        result = await svc.chat(message="Test", campaign_id="camp-1", image_url="http://img.test/a.png")
        assert "response" in result


# ---------------------------------------------------------------------------
# TestChatWithLore
# ---------------------------------------------------------------------------

class TestChatWithLore:
    @pytest.mark.asyncio
    async def test_lore_injected_when_facts_returned(self, svc, gemini, story_memory):
        fact = MagicMock()
        fact.entity_type.value = "npc"
        fact.entity_name = "Elder Mira"
        fact.summary = "She knows the secret passage."
        story_memory.retrieve_relevant_context = AsyncMock(return_value=[fact])

        result = await svc.chat(message="Ask about Mira", campaign_id="camp-1")

        user_prompt = gemini.generate.call_args[1].get("user_prompt") or gemini.generate.call_args[0][1]
        assert "LORE ARCHIVE" in user_prompt
        assert result["lore_facts"] == 1

    @pytest.mark.asyncio
    async def test_lore_retrieval_failure_silent(self, svc, gemini, story_memory):
        story_memory.retrieve_relevant_context = AsyncMock(side_effect=Exception("ChromaDB down"))
        result = await svc.chat(message="Question", campaign_id="camp-1")
        # Should not raise; lore_facts is 0
        assert result["lore_facts"] == 0

    @pytest.mark.asyncio
    async def test_no_lore_facts_no_block(self, svc, gemini, story_memory):
        story_memory.retrieve_relevant_context = AsyncMock(return_value=[])
        await svc.chat(message="Question", campaign_id="camp-1")

        user_prompt = gemini.generate.call_args[1].get("user_prompt") or gemini.generate.call_args[0][1]
        assert "LORE ARCHIVE" not in user_prompt

    @pytest.mark.asyncio
    async def test_lore_capped_at_six_facts(self, svc, gemini, story_memory):
        facts = []
        for i in range(10):
            f = MagicMock()
            f.entity_type.value = "npc"
            f.entity_name = f"NPC {i}"
            f.summary = f"Fact {i}"
            facts.append(f)
        story_memory.retrieve_relevant_context = AsyncMock(return_value=facts)

        result = await svc.chat(message="Question", campaign_id="camp-1")
        assert result["lore_facts"] == 6  # _SANDBOX_CONTEXT_FACTS = 6


# ---------------------------------------------------------------------------
# TestChatGenerationFails
# ---------------------------------------------------------------------------

class TestChatGenerationFails:
    @pytest.mark.asyncio
    async def test_generation_error_returns_error_string(self, svc, gemini):
        gemini.generate = AsyncMock(side_effect=Exception("Rate limit"))
        result = await svc.chat(message="Hello", campaign_id="camp-1")
        assert "unavailable" in result["response"].lower() or "Rate limit" in result["response"]


# ---------------------------------------------------------------------------
# TestSelectStoryteller
# ---------------------------------------------------------------------------

class TestSelectStoryteller:
    @pytest.mark.asyncio
    async def test_cloud_enabled_returns_gemini(self, svc, gemini, node_router):
        node_router.is_storyteller_enabled = AsyncMock(return_value=True)
        storyteller = await svc._select_storyteller()
        assert storyteller is gemini

    @pytest.mark.asyncio
    async def test_local_node_returned_when_cloud_disabled(self, svc, gemini, node_router):
        local_mock = MagicMock()
        node_router.is_storyteller_enabled = AsyncMock(return_value=False)
        node_router.get_storyteller_client = AsyncMock(return_value=local_mock)
        storyteller = await svc._select_storyteller()
        assert storyteller is local_mock

    @pytest.mark.asyncio
    async def test_falls_back_to_gemini_when_no_local_node(self, svc, gemini, node_router):
        node_router.is_storyteller_enabled = AsyncMock(return_value=False)
        node_router.get_storyteller_client = AsyncMock(return_value=None)
        storyteller = await svc._select_storyteller()
        assert storyteller is gemini
