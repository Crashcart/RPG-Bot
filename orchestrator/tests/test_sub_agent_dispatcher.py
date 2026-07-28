"""Unit tests for SubAgentDispatcher — Tier 2 Actor/Generator Executor.

Run: pytest orchestrator/tests/test_sub_agent_dispatcher.py -v
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.schemas.payloads import SubAgentResult, SubAgentTask
from orchestrator.services.sub_agent_dispatcher import (
    SubAgentDispatcher,
    _detect_brand_violation,
    _strip_brand_violations,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_task(
    task_type: str = "npc_dialogue",
    entity_name: str = "Grib",
    max_words: int = 60,
) -> SubAgentTask:
    return SubAgentTask(
        task_type=task_type,
        entity_name=entity_name,
        entity_role="goblin bartender",
        scene_context="Dark tavern, rain outside.",
        player_action_context="Player asks for a drink.",
        tone="gritty",
        max_words=max_words,
    )


def _make_dispatcher() -> tuple[SubAgentDispatcher, MagicMock]:
    """Return (dispatcher, node_router)."""
    node_router = AsyncMock()
    dispatcher = SubAgentDispatcher(node_router=node_router)
    return dispatcher, node_router


def _make_mock_client(response: str = "Clean tavern output.") -> MagicMock:
    client = AsyncMock()
    client.generate = AsyncMock(return_value=response)
    client._node_name = "test-node"
    client._voice_id = "en-US-GuyNeural"
    return client


# ===========================================================================
# _detect_brand_violation
# ===========================================================================

class TestDetectBrandViolation:
    def test_no_violation_returns_none(self):
        assert _detect_brand_violation("The innkeeper pours a dark ale.") is None

    def test_coca_cola_detected(self):
        result = _detect_brand_violation("He drinks a Coca-Cola from the fridge.")
        assert result is not None
        assert "coca" in result.lower()

    def test_case_insensitive(self):
        assert _detect_brand_violation("COCA-COLA flavored potion") is not None

    def test_multiple_brands_returns_first_found(self):
        result = _detect_brand_violation("Starbucks coffee and a Netflix show.")
        assert result is not None

    def test_partial_word_match(self):
        assert _detect_brand_violation("There's a McDonald's on every corner.") is not None

    def test_empty_string_returns_none(self):
        assert _detect_brand_violation("") is None


# ===========================================================================
# _strip_brand_violations
# ===========================================================================

class TestStripBrandViolations:
    def test_brand_replaced_with_placeholder(self):
        result = _strip_brand_violations("He drank Coca-Cola and smiled.")
        assert "coca-cola" not in result.lower()
        assert "[???]" in result

    def test_case_insensitive_replacement(self):
        result = _strip_brand_violations("STARBUCKS is on the corner.")
        assert "starbucks" not in result.lower()
        assert "[???]" in result

    def test_clean_text_unchanged(self):
        text = "The alchemist brewed a potent elixir."
        assert _strip_brand_violations(text) == text

    def test_multiple_brands_all_replaced(self):
        text = "Pepsi and Coca-Cola were both on the menu."
        result = _strip_brand_violations(text)
        assert "pepsi" not in result.lower()
        assert "coca-cola" not in result.lower()


# ===========================================================================
# SubAgentDispatcher.dispatch_all
# ===========================================================================

class TestDispatchAll:
    @pytest.mark.asyncio
    async def test_empty_task_list_returns_empty(self):
        dispatcher, _ = _make_dispatcher()
        result = await dispatcher.dispatch_all([])
        assert result == []

    @pytest.mark.asyncio
    async def test_successful_dispatch_returns_results(self):
        dispatcher, node_router = _make_dispatcher()
        client = _make_mock_client("The goblin sneers at you.")
        node_router.get_ollama_client_for_role = AsyncMock(return_value=client)

        task = _make_task()
        results = await dispatcher.dispatch_all([task])

        assert len(results) == 1
        assert results[0].raw_output == "The goblin sneers at you."
        assert not results[0].brand_violation

    @pytest.mark.asyncio
    async def test_exception_in_task_produces_empty_result(self):
        dispatcher, node_router = _make_dispatcher()
        # Simulate dispatch_one raising an exception for a task
        with patch.object(dispatcher, "_dispatch_one",
                          side_effect=RuntimeError("node crash")):
            task = _make_task()
            results = await dispatcher.dispatch_all([task])

        assert len(results) == 1
        assert results[0].raw_output == ""
        assert results[0].node_name == "error"

    @pytest.mark.asyncio
    async def test_multiple_tasks_all_returned(self):
        dispatcher, node_router = _make_dispatcher()
        client = _make_mock_client("output")
        node_router.get_ollama_client_for_role = AsyncMock(return_value=client)

        tasks = [_make_task(entity_name=f"NPC{i}") for i in range(3)]
        results = await dispatcher.dispatch_all(tasks)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_results_order_matches_input_order(self):
        dispatcher, node_router = _make_dispatcher()
        call_count = 0

        async def sequential_generate(system_prompt, user_prompt, max_tokens):
            nonlocal call_count
            call_count += 1
            return f"output-{call_count}"

        client = AsyncMock()
        client.generate = sequential_generate
        client._node_name = "node"
        client._voice_id = "en-US"
        node_router.get_ollama_client_for_role = AsyncMock(return_value=client)

        tasks = [_make_task(entity_name=f"NPC{i}") for i in range(3)]
        results = await dispatcher.dispatch_all(tasks)
        # All results should have raw_output (order preserved by zip)
        assert all(r.raw_output.startswith("output") for r in results)


# ===========================================================================
# SubAgentDispatcher._dispatch_one
# ===========================================================================

class TestDispatchOne:
    @pytest.mark.asyncio
    async def test_clean_output_returned_immediately(self):
        dispatcher, node_router = _make_dispatcher()
        client = _make_mock_client("The goblin snarls.")
        node_router.get_ollama_client_for_role = AsyncMock(return_value=client)

        task = _make_task()
        result = await dispatcher._dispatch_one(task)

        assert result.raw_output == "The goblin snarls."
        assert not result.brand_violation
        assert client.generate.call_count == 1

    @pytest.mark.asyncio
    async def test_brand_violation_triggers_retry(self):
        dispatcher, node_router = _make_dispatcher()
        client = AsyncMock()
        client._node_name = "test-node"
        client._voice_id = "en-US"
        # First call contains brand; second is clean
        client.generate = AsyncMock(side_effect=[
            "He drinks Coca-Cola by the fire.",
            "He drinks a dark brew by the fire.",
        ])
        node_router.get_ollama_client_for_role = AsyncMock(return_value=client)

        task = _make_task()
        result = await dispatcher._dispatch_one(task)

        assert client.generate.call_count == 2
        assert not result.brand_violation
        assert "Coca-Cola" not in result.raw_output

    @pytest.mark.asyncio
    async def test_repeated_brand_violation_strips_and_flags(self):
        dispatcher, node_router = _make_dispatcher()
        client = AsyncMock()
        client._node_name = "test-node"
        client._voice_id = "en-US"
        # All attempts return brand violation
        client.generate = AsyncMock(return_value="He loves his Coca-Cola.")
        node_router.get_ollama_client_for_role = AsyncMock(return_value=client)

        task = _make_task()
        result = await dispatcher._dispatch_one(task)

        assert result.brand_violation is True
        assert "coca-cola" not in result.raw_output.lower()
        assert "[???]" in result.raw_output

    @pytest.mark.asyncio
    async def test_node_fallback_chain(self):
        dispatcher, node_router = _make_dispatcher()
        client = _make_mock_client("Fallback output.")
        # Preferred role fails, narrative role succeeds
        node_router.get_ollama_client_for_role = AsyncMock(side_effect=[
            None,    # preferred role → None
            None,    # narrative fallback → None
        ])
        node_router.get_ollama_client = AsyncMock(return_value=client)

        task = _make_task()
        result = await dispatcher._dispatch_one(task)

        node_router.get_ollama_client.assert_awaited_once()
        assert result.raw_output == "Fallback output."

    @pytest.mark.asyncio
    async def test_ttft_ms_recorded(self):
        dispatcher, node_router = _make_dispatcher()
        client = _make_mock_client("Quick response.")
        node_router.get_ollama_client_for_role = AsyncMock(return_value=client)

        task = _make_task()
        result = await dispatcher._dispatch_one(task)

        assert result.ttft_ms is not None
        assert result.ttft_ms >= 0

    @pytest.mark.asyncio
    async def test_node_name_carried_into_result(self):
        dispatcher, node_router = _make_dispatcher()
        client = _make_mock_client("Output.")
        client._node_name = "actor-prime"
        node_router.get_ollama_client_for_role = AsyncMock(return_value=client)

        task = _make_task()
        result = await dispatcher._dispatch_one(task)

        assert result.node_name == "actor-prime"

    @pytest.mark.asyncio
    async def test_scribe_task_type_requests_scribe_role(self):
        dispatcher, node_router = _make_dispatcher()
        client = _make_mock_client("The chamber is vast.")
        node_router.get_ollama_client_for_role = AsyncMock(return_value=client)

        task = _make_task(task_type="environmental_description", entity_name="Throne Room")
        await dispatcher._dispatch_one(task)

        # First call should be for "scribe" role
        first_call_args = node_router.get_ollama_client_for_role.call_args_list[0]
        assert "scribe" in first_call_args.args
