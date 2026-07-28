"""Unit tests for SubAgentDispatcher — Tier 2 Actor/Generator Executor."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from orchestrator.services.sub_agent_dispatcher import (
    SubAgentDispatcher,
    _detect_brand_violation,
    _strip_brand_violations,
)
from orchestrator.schemas.payloads import SubAgentResult, SubAgentTask
from orchestrator.prompts.gm_prompts import BRAND_BLOCKLIST


# ── Shared fixture ─────────────────────────────────────────────────────────────

def _make_task(task_type: str = "npc_dialogue") -> SubAgentTask:
    return SubAgentTask(
        task_type=task_type,
        entity_name="Guard",
        entity_role="city guard",
        scene_context="City gate.",
        player_action_context="Player approaches.",
        tone="gritty",
        max_words=80,
    )


# ── TestDetectBrandViolation ───────────────────────────────────────────────────

class TestDetectBrandViolation:
    def test_no_violation_returns_none(self):
        assert _detect_brand_violation("The guard nodded slowly.") is None

    def test_blocklisted_term_detected(self):
        term = BRAND_BLOCKLIST[0]
        result = _detect_brand_violation(f"He drank a bottle of {term}.")
        assert result == term

    def test_case_insensitive_detection(self):
        term = BRAND_BLOCKLIST[0]
        result = _detect_brand_violation(term.upper())
        assert result is not None

    def test_multiple_brands_returns_first_found(self):
        t1 = BRAND_BLOCKLIST[0]
        t2 = BRAND_BLOCKLIST[1]
        result = _detect_brand_violation(f"{t1} and {t2} in one sentence.")
        assert result in (t1, t2)

    def test_partial_word_match(self):
        term = BRAND_BLOCKLIST[0]
        result = _detect_brand_violation(f"The {term}brand-new item glimmered.")
        assert result == term

    def test_empty_string_returns_none(self):
        assert _detect_brand_violation("") is None


# ── TestStripBrandViolations ───────────────────────────────────────────────────

class TestStripBrandViolations:
    def test_term_replaced_with_placeholder(self):
        term = BRAND_BLOCKLIST[0]
        result = _strip_brand_violations(f"He sipped {term} happily.")
        assert "[???]" in result
        assert term not in result

    def test_replacement_is_case_insensitive(self):
        term = BRAND_BLOCKLIST[0]
        result = _strip_brand_violations(term.upper())
        assert "[???]" in result

    def test_clean_text_unchanged(self):
        text = "The ancient runes glowed with forgotten power."
        result = _strip_brand_violations(text)
        assert result == text

    def test_multiple_brands_all_replaced(self):
        t1 = BRAND_BLOCKLIST[0]
        t2 = BRAND_BLOCKLIST[1]
        text = f"Crates labelled {t1} and {t2} lined the warehouse."
        result = _strip_brand_violations(text)
        assert t1 not in result
        assert t2 not in result
        assert result.count("[???]") >= 2


# ── TestDispatchAll ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestDispatchAll:
    async def test_empty_task_list_returns_empty(self):
        node_router = AsyncMock()
        dispatcher = SubAgentDispatcher(node_router)
        result = await dispatcher.dispatch_all([])
        assert result == []

    async def test_successful_dispatch_returns_results(self):
        node_router = AsyncMock()
        dispatcher = SubAgentDispatcher(node_router)
        task = _make_task()
        expected_result = SubAgentResult(
            task=task, raw_output="Guard speech here.", node_name="actor-01",
            ttft_ms=100, brand_violation=False,
        )
        with patch.object(dispatcher, "_dispatch_one", new=AsyncMock(return_value=expected_result)):
            results = await dispatcher.dispatch_all([task])
        assert len(results) == 1
        assert results[0].raw_output == "Guard speech here."

    async def test_exception_produces_empty_result(self):
        node_router = AsyncMock()
        dispatcher = SubAgentDispatcher(node_router)
        task = _make_task()
        with patch.object(dispatcher, "_dispatch_one", new=AsyncMock(side_effect=RuntimeError("oops"))):
            results = await dispatcher.dispatch_all([task])
        assert len(results) == 1
        assert results[0].raw_output == ""
        assert results[0].node_name == "error"

    async def test_multiple_tasks_order_preserved(self):
        node_router = AsyncMock()
        dispatcher = SubAgentDispatcher(node_router)
        tasks = [_make_task("npc_dialogue"), _make_task("environmental_description")]
        results_returned = [
            SubAgentResult(task=tasks[0], raw_output="A", node_name="n1", ttft_ms=50, brand_violation=False),
            SubAgentResult(task=tasks[1], raw_output="B", node_name="n2", ttft_ms=60, brand_violation=False),
        ]

        async def mock_dispatch(task):
            return results_returned[tasks.index(task)]

        with patch.object(dispatcher, "_dispatch_one", new=mock_dispatch):
            results = await dispatcher.dispatch_all(tasks)
        assert results[0].raw_output == "A"
        assert results[1].raw_output == "B"

    async def test_mixed_success_and_exception(self):
        node_router = AsyncMock()
        dispatcher = SubAgentDispatcher(node_router)
        tasks = [_make_task(), _make_task()]
        ok_result = SubAgentResult(
            task=tasks[0], raw_output="OK", node_name="n1", ttft_ms=80, brand_violation=False
        )
        call_count = [0]

        async def mock_dispatch(task):
            i = call_count[0]
            call_count[0] += 1
            if i == 0:
                return ok_result
            raise RuntimeError("second task failed")

        with patch.object(dispatcher, "_dispatch_one", new=mock_dispatch):
            results = await dispatcher.dispatch_all(tasks)
        assert results[0].raw_output == "OK"
        assert results[1].raw_output == ""


# ── TestDispatchOne ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestDispatchOne:
    def _make_client(self, output: str = "Guard speaks.") -> AsyncMock:
        client = AsyncMock()
        client.generate.return_value = output
        client._node_name = "actor-node-01"
        client._voice_id = "en-US-GuyNeural"
        return client

    def _make_dispatcher(self, preferred_client=None, fallback_client=None):
        node_router = AsyncMock()
        node_router.get_ollama_client_for_role.return_value = preferred_client
        node_router.get_ollama_client.return_value = fallback_client
        return SubAgentDispatcher(node_router)

    async def test_clean_output_returns_immediately(self):
        client = self._make_client("The guard eyes you suspiciously.")
        dispatcher = self._make_dispatcher(preferred_client=client)
        task = _make_task()
        result = await dispatcher._dispatch_one(task)
        assert result.raw_output == "The guard eyes you suspiciously."
        assert result.brand_violation is False
        assert client.generate.call_count == 1

    async def test_brand_violation_triggers_retry(self):
        term = BRAND_BLOCKLIST[0]
        client = AsyncMock()
        client._node_name = "actor-01"
        client._voice_id = "en-US-GuyNeural"
        # First call: violation. Second call: clean.
        client.generate.side_effect = [
            f"He offered a {term} to the merchant.",
            "He offered a refreshing drink to the merchant.",
        ]

        node_router = AsyncMock()
        node_router.get_ollama_client_for_role.return_value = client
        dispatcher = SubAgentDispatcher(node_router)
        task = _make_task()
        result = await dispatcher._dispatch_one(task)
        assert client.generate.call_count == 2
        assert result.brand_violation is False
        assert result.raw_output == "He offered a refreshing drink to the merchant."

    async def test_repeated_violation_strips_and_flags(self):
        term = BRAND_BLOCKLIST[0]
        client = AsyncMock()
        client._node_name = "actor-01"
        client._voice_id = "en-US-GuyNeural"
        client.generate.return_value = f"The {term} logo was everywhere."

        node_router = AsyncMock()
        node_router.get_ollama_client_for_role.return_value = client
        dispatcher = SubAgentDispatcher(node_router)
        task = _make_task()
        result = await dispatcher._dispatch_one(task)
        assert result.brand_violation is True
        assert "[???]" in result.raw_output
        assert term not in result.raw_output

    async def test_node_fallback_chain(self):
        fallback_client = self._make_client("Fallback output.")
        node_router = AsyncMock()
        # Primary and narrative fallback return None; final fallback returns client
        node_router.get_ollama_client_for_role.return_value = None
        node_router.get_ollama_client.return_value = fallback_client
        dispatcher = SubAgentDispatcher(node_router)
        task = _make_task()
        result = await dispatcher._dispatch_one(task)
        assert result.raw_output == "Fallback output."
        assert result.node_name == "actor-node-01"

    async def test_ttft_ms_recorded(self):
        client = self._make_client("Text produced quickly.")
        dispatcher = self._make_dispatcher(preferred_client=client)
        task = _make_task()
        result = await dispatcher._dispatch_one(task)
        assert result.ttft_ms is not None
        assert result.ttft_ms >= 0

    async def test_node_name_carried_into_result(self):
        client = self._make_client()
        client._node_name = "specialist-actor-node"
        dispatcher = self._make_dispatcher(preferred_client=client)
        task = _make_task()
        result = await dispatcher._dispatch_one(task)
        assert result.node_name == "specialist-actor-node"

    async def test_scribe_role_for_env_description(self):
        client = self._make_client("A dark corridor stretches ahead.")
        node_router = AsyncMock()
        node_router.get_ollama_client_for_role.return_value = client
        dispatcher = SubAgentDispatcher(node_router)
        task = _make_task(task_type="environmental_description")
        result = await dispatcher._dispatch_one(task)
        # Should request "scribe" role for env description
        node_router.get_ollama_client_for_role.assert_called_with("scribe")
        assert result.raw_output == "A dark corridor stretches ahead."
