"""
Unit tests for orchestrator.services.node_router.NodeRouter.

Covers:
  - _probe_node        : online / degraded / offline
  - _measure_ttft      : first-token timing / empty-frame skip / None on error
  - is_storyteller_enabled : True / False / truthy-falsy
  - get_storyteller_client : latency sort / offline skip / None when empty
  - get_ollama_client_for_role : priority mode / cloud provider / fallbacks
  - _get_cloud_adjudicator : caching / sillytavern URL logic / error handling
  - get_ollama_client  : adjudication role / enabled fallback / env default
  - _probe_and_update  : writes status + TTFT / skips TTFT when None / model selection
  - _check_all_nodes   : ollama+enabled filter / empty list / exception isolation
  - warmup_all_nodes   : delegates to _check_all_nodes
  - start / stop lifecycle
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.node_router import (
    NodeRouter,
    _ADJUDICATION_FALLBACK,
    _NARRATIVE_ROLE,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _node(
    *,
    node_name: str = "rig",
    host: str = "http://localhost:11434",
    model: str = "mistral",
    node_type: str = "ollama",
    enabled: bool = True,
    status: str = "online",
    priority: int = 1,
    latency_ms: int | None = 150,
) -> dict:
    return {
        "node_name": node_name,
        "host": host,
        "model": model,
        "node_type": node_type,
        "enabled": enabled,
        "status": status,
        "priority": priority,
        "latency_ms": latency_ms,
    }


def _make_router() -> tuple[NodeRouter, AsyncMock, MagicMock]:
    """Return (router, mock_db, mock_settings)."""
    mock_db = AsyncMock()
    mock_settings = MagicMock()
    mock_settings.ollama_model = "mistral"
    return NodeRouter(db=mock_db, settings=mock_settings), mock_db, mock_settings


def _httpx_client_cm(status_code: int = 200):
    """Return an httpx.AsyncClient context manager that returns *status_code* for GET."""
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_client)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm, mock_client


def _streaming_cm(lines: list[str], raise_on_status: bool = False):
    """Build an httpx.AsyncClient cm that streams *lines* from client.stream()."""

    async def _aiter():
        for line in lines:
            yield line

    mock_response = MagicMock()
    if raise_on_status:
        mock_response.raise_for_status = MagicMock(side_effect=Exception("HTTP 500"))
    else:
        mock_response.raise_for_status = MagicMock()
    mock_response.aiter_lines = _aiter

    @asynccontextmanager
    async def _stream(*args, **kwargs):
        yield mock_response

    mock_client = MagicMock()
    mock_client.stream = _stream

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_client)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


# ── _probe_node ──────────────────────────────────────────────────────────────


class TestProbeNode:
    @pytest.mark.asyncio
    async def test_online_on_200_response(self):
        cm, _ = _httpx_client_cm(200)
        with patch("orchestrator.services.node_router.httpx.AsyncClient", return_value=cm):
            result = await NodeRouter._probe_node("http://localhost:11434")
        assert result == "online"

    @pytest.mark.asyncio
    async def test_degraded_on_503_response(self):
        cm, _ = _httpx_client_cm(503)
        with patch("orchestrator.services.node_router.httpx.AsyncClient", return_value=cm):
            result = await NodeRouter._probe_node("http://localhost:11434")
        assert result == "degraded"

    @pytest.mark.asyncio
    async def test_degraded_on_404_response(self):
        cm, _ = _httpx_client_cm(404)
        with patch("orchestrator.services.node_router.httpx.AsyncClient", return_value=cm):
            result = await NodeRouter._probe_node("http://localhost:11434")
        assert result == "degraded"

    @pytest.mark.asyncio
    async def test_offline_on_context_manager_error(self):
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=Exception("connection refused"))
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch("orchestrator.services.node_router.httpx.AsyncClient", return_value=cm):
            result = await NodeRouter._probe_node("http://unreachable:11434")
        assert result == "offline"

    @pytest.mark.asyncio
    async def test_offline_when_get_raises(self):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=ConnectionError("timed out"))
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_client)
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch("orchestrator.services.node_router.httpx.AsyncClient", return_value=cm):
            result = await NodeRouter._probe_node("http://slow:11434")
        assert result == "offline"


# ── _measure_ttft ─────────────────────────────────────────────────────────────


class TestMeasureTTFT:
    @pytest.mark.asyncio
    async def test_returns_int_on_first_content_chunk(self):
        line = '{"message":{"content":"ready"},"done":false}'
        cm = _streaming_cm([line])
        with patch("orchestrator.services.node_router.httpx.AsyncClient", return_value=cm):
            result = await NodeRouter._measure_ttft("http://localhost:11434", "mistral")
        assert isinstance(result, int)
        assert result >= 0

    @pytest.mark.asyncio
    async def test_skips_empty_content_frame_reads_next(self):
        """First chunk has empty content and done=false; second chunk triggers return."""
        lines = [
            '{"message":{"content":""},"done":false}',
            '{"message":{"content":"ready"},"done":false}',
        ]
        cm = _streaming_cm(lines)
        with patch("orchestrator.services.node_router.httpx.AsyncClient", return_value=cm):
            result = await NodeRouter._measure_ttft("http://localhost:11434", "mistral")
        assert isinstance(result, int)

    @pytest.mark.asyncio
    async def test_done_true_frame_triggers_return(self):
        """A frame with done=true (even with empty content) exits the loop."""
        line = '{"message":{"content":""},"done":true}'
        cm = _streaming_cm([line])
        with patch("orchestrator.services.node_router.httpx.AsyncClient", return_value=cm):
            result = await NodeRouter._measure_ttft("http://localhost:11434", "mistral")
        assert isinstance(result, int)

    @pytest.mark.asyncio
    async def test_blank_lines_skipped(self):
        """Blank / whitespace-only lines are ignored; the real chunk triggers return."""
        lines = ["", "   ", '{"message":{"content":"hello"},"done":false}']
        cm = _streaming_cm(lines)
        with patch("orchestrator.services.node_router.httpx.AsyncClient", return_value=cm):
            result = await NodeRouter._measure_ttft("http://localhost:11434", "mistral")
        assert isinstance(result, int)

    @pytest.mark.asyncio
    async def test_invalid_json_treated_as_first_token(self):
        """Non-JSON lines fall through to the return (no re-raise)."""
        cm = _streaming_cm(["not-json-at-all"])
        with patch("orchestrator.services.node_router.httpx.AsyncClient", return_value=cm):
            result = await NodeRouter._measure_ttft("http://localhost:11434", "mistral")
        assert isinstance(result, int)

    @pytest.mark.asyncio
    async def test_returns_none_on_client_exception(self):
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=Exception("network error"))
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch("orchestrator.services.node_router.httpx.AsyncClient", return_value=cm):
            result = await NodeRouter._measure_ttft("http://dead:11434", "mistral")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_raise_for_status_fails(self):
        cm = _streaming_cm([], raise_on_status=True)
        with patch("orchestrator.services.node_router.httpx.AsyncClient", return_value=cm):
            result = await NodeRouter._measure_ttft("http://localhost:11434", "mistral")
        assert result is None


# ── is_storyteller_enabled ────────────────────────────────────────────────────


class TestIsStorytellerEnabled:
    @pytest.mark.asyncio
    async def test_returns_true_when_setting_is_true(self):
        router, mock_db, _ = _make_router()
        mock_db.get_system_setting.return_value = True
        assert await router.is_storyteller_enabled() is True
        mock_db.get_system_setting.assert_awaited_once_with(
            "storyteller_api_enabled", default=True
        )

    @pytest.mark.asyncio
    async def test_returns_false_when_setting_is_false(self):
        router, mock_db, _ = _make_router()
        mock_db.get_system_setting.return_value = False
        assert await router.is_storyteller_enabled() is False

    @pytest.mark.asyncio
    async def test_returns_false_for_zero(self):
        router, mock_db, _ = _make_router()
        mock_db.get_system_setting.return_value = 0
        assert await router.is_storyteller_enabled() is False

    @pytest.mark.asyncio
    async def test_returns_true_for_truthy_int(self):
        router, mock_db, _ = _make_router()
        mock_db.get_system_setting.return_value = 1
        assert await router.is_storyteller_enabled() is True


# ── get_storyteller_client ────────────────────────────────────────────────────


class TestGetStorytellerClient:
    @pytest.mark.asyncio
    async def test_returns_client_for_online_narrative_node(self):
        router, mock_db, mock_settings = _make_router()
        node = _node(node_name="synology", status="online", latency_ms=100)
        mock_db.get_nodes_for_role_by_latency.return_value = [node]

        mock_client = MagicMock()
        with patch(
            "orchestrator.services.ollama_client.OllamaClient.from_node",
            return_value=mock_client,
        ):
            result = await router.get_storyteller_client()

        assert result is mock_client
        mock_db.get_nodes_for_role_by_latency.assert_awaited_once_with(_NARRATIVE_ROLE)

    @pytest.mark.asyncio
    async def test_skips_offline_nodes_picks_second(self):
        router, mock_db, _ = _make_router()
        mock_db.get_nodes_for_role_by_latency.return_value = [
            _node(node_name="dead", status="offline"),
            _node(node_name="synology", status="online", latency_ms=200),
        ]
        mock_client = MagicMock()
        with patch(
            "orchestrator.services.ollama_client.OllamaClient.from_node",
            return_value=mock_client,
        ) as mock_fn:
            result = await router.get_storyteller_client()

        assert result is mock_client
        assert mock_fn.call_args[0][0]["node_name"] == "synology"

    @pytest.mark.asyncio
    async def test_returns_none_all_nodes_offline(self):
        router, mock_db, _ = _make_router()
        mock_db.get_nodes_for_role_by_latency.return_value = [
            _node(status="offline"),
            _node(node_name="b", status="offline"),
        ]
        assert await router.get_storyteller_client() is None

    @pytest.mark.asyncio
    async def test_returns_none_when_node_list_empty(self):
        router, mock_db, _ = _make_router()
        mock_db.get_nodes_for_role_by_latency.return_value = []
        assert await router.get_storyteller_client() is None

    @pytest.mark.asyncio
    async def test_queries_narrative_role_constant(self):
        router, mock_db, _ = _make_router()
        mock_db.get_nodes_for_role_by_latency.return_value = []
        await router.get_storyteller_client()
        mock_db.get_nodes_for_role_by_latency.assert_awaited_once_with("narrative")


# ── get_ollama_client_for_role ────────────────────────────────────────────────


class TestGetOllamaClientForRole:
    @pytest.mark.asyncio
    async def test_returns_ollama_client_for_online_node(self):
        router, mock_db, mock_settings = _make_router()
        mock_db.get_system_setting.return_value = "ollama"
        mock_db.get_nodes_for_role.return_value = [_node(status="online")]

        mock_client = MagicMock()
        with patch(
            "orchestrator.services.ollama_client.OllamaClient.from_node",
            return_value=mock_client,
        ):
            result = await router.get_ollama_client_for_role("adjudication")

        assert result is mock_client

    @pytest.mark.asyncio
    async def test_skips_offline_picks_second_node(self):
        router, mock_db, _ = _make_router()
        mock_db.get_system_setting.return_value = "ollama"
        mock_db.get_nodes_for_role.return_value = [
            _node(node_name="dead", status="offline"),
            _node(node_name="live", status="online"),
        ]
        mock_client = MagicMock()
        with patch(
            "orchestrator.services.ollama_client.OllamaClient.from_node",
            return_value=mock_client,
        ) as mock_fn:
            result = await router.get_ollama_client_for_role("adjudication")

        assert result is mock_client
        assert mock_fn.call_args[0][0]["node_name"] == "live"

    @pytest.mark.asyncio
    async def test_returns_none_when_all_nodes_offline(self):
        router, mock_db, _ = _make_router()
        mock_db.get_system_setting.return_value = "ollama"
        mock_db.get_nodes_for_role.return_value = [_node(status="offline")]
        assert await router.get_ollama_client_for_role("adjudication") is None

    @pytest.mark.asyncio
    async def test_routes_to_cloud_when_provider_not_ollama(self):
        router, mock_db, _ = _make_router()
        mock_db.get_system_setting.return_value = "openai"
        mock_cloud = MagicMock()
        mock_cloud.is_available.return_value = True
        with patch.object(router, "_get_cloud_adjudicator", AsyncMock(return_value=mock_cloud)):
            result = await router.get_ollama_client_for_role("adjudication")
        assert result is mock_cloud

    @pytest.mark.asyncio
    async def test_falls_back_to_ollama_when_cloud_unavailable(self):
        router, mock_db, _ = _make_router()
        mock_db.get_system_setting.return_value = "openai"
        mock_db.get_nodes_for_role.return_value = [_node(status="online")]
        mock_cloud = MagicMock()
        mock_cloud.is_available.return_value = False
        mock_ollama = MagicMock()
        with patch.object(router, "_get_cloud_adjudicator", AsyncMock(return_value=mock_cloud)):
            with patch(
                "orchestrator.services.ollama_client.OllamaClient.from_node",
                return_value=mock_ollama,
            ):
                result = await router.get_ollama_client_for_role("adjudication")
        assert result is mock_ollama

    @pytest.mark.asyncio
    async def test_falls_back_to_ollama_when_cloud_is_none(self):
        router, mock_db, _ = _make_router()
        mock_db.get_system_setting.return_value = "openai"
        mock_db.get_nodes_for_role.return_value = [_node(status="online")]
        mock_ollama = MagicMock()
        with patch.object(router, "_get_cloud_adjudicator", AsyncMock(return_value=None)):
            with patch(
                "orchestrator.services.ollama_client.OllamaClient.from_node",
                return_value=mock_ollama,
            ):
                result = await router.get_ollama_client_for_role("adjudication")
        assert result is mock_ollama


# ── _get_cloud_adjudicator ─────────────────────────────────────────────────


class TestGetCloudAdjudicator:
    @pytest.mark.asyncio
    async def test_caches_non_sillytavern_client(self):
        router, _, _ = _make_router()
        mock_client = MagicMock()
        with patch(
            "orchestrator.services.openai_compat_client.OpenAICompatClient",
            return_value=mock_client,
        ):
            first = await router._get_cloud_adjudicator("openai")
            second = await router._get_cloud_adjudicator("openai")
        assert first is second

    @pytest.mark.asyncio
    async def test_returns_none_on_value_error(self):
        router, _, _ = _make_router()
        with patch(
            "orchestrator.services.openai_compat_client.OpenAICompatClient",
            side_effect=ValueError("bad config"),
        ):
            result = await router._get_cloud_adjudicator("openai")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_generic_error(self):
        router, _, _ = _make_router()
        with patch(
            "orchestrator.services.openai_compat_client.OpenAICompatClient",
            side_effect=RuntimeError("crash"),
        ):
            result = await router._get_cloud_adjudicator("openai")
        assert result is None

    @pytest.mark.asyncio
    async def test_sillytavern_returns_none_when_url_not_configured(self):
        router, mock_db, _ = _make_router()
        mock_db.get_system_setting.return_value = ""
        result = await router._get_cloud_adjudicator("sillytavern")
        assert result is None

    @pytest.mark.asyncio
    async def test_sillytavern_creates_client_when_url_set(self):
        router, mock_db, _ = _make_router()
        mock_db.get_system_setting.return_value = "http://sillytavern:8000"
        mock_client = MagicMock()
        mock_client._base_url = "http://sillytavern:8000"
        with patch(
            "orchestrator.services.openai_compat_client.OpenAICompatClient",
            return_value=mock_client,
        ):
            result = await router._get_cloud_adjudicator("sillytavern")
        assert result is mock_client

    @pytest.mark.asyncio
    async def test_sillytavern_busts_cache_on_url_change(self):
        router, mock_db, _ = _make_router()
        old_client = MagicMock()
        old_client._base_url = "http://old:8000"
        router._cloud_adj_cache["sillytavern"] = old_client

        mock_db.get_system_setting.return_value = "http://new:8000"
        new_client = MagicMock()
        new_client._base_url = "http://new:8000"
        with patch(
            "orchestrator.services.openai_compat_client.OpenAICompatClient",
            return_value=new_client,
        ):
            result = await router._get_cloud_adjudicator("sillytavern")
        assert result is new_client
        assert result is not old_client

    @pytest.mark.asyncio
    async def test_sillytavern_reuses_cache_when_url_unchanged(self):
        router, mock_db, _ = _make_router()
        existing = MagicMock()
        existing._base_url = "http://sillytavern:8000"
        router._cloud_adj_cache["sillytavern"] = existing
        mock_db.get_system_setting.return_value = "http://sillytavern:8000"
        result = await router._get_cloud_adjudicator("sillytavern")
        assert result is existing


# ── get_ollama_client ─────────────────────────────────────────────────────────


class TestGetOllamaClient:
    @pytest.mark.asyncio
    async def test_returns_adjudication_role_client(self):
        router, _, _ = _make_router()
        mock_client = MagicMock()
        with patch.object(
            router, "get_ollama_client_for_role", AsyncMock(return_value=mock_client)
        ) as mock_role:
            result = await router.get_ollama_client()
        assert result is mock_client
        mock_role.assert_awaited_once_with(_ADJUDICATION_FALLBACK)

    @pytest.mark.asyncio
    async def test_falls_back_to_enabled_node_when_role_returns_none(self):
        router, mock_db, _ = _make_router()
        mock_db.get_enabled_ollama_nodes.return_value = [_node(status="online")]
        mock_ollama = MagicMock()
        with patch.object(router, "get_ollama_client_for_role", AsyncMock(return_value=None)):
            with patch(
                "orchestrator.services.ollama_client.OllamaClient.from_node",
                return_value=mock_ollama,
            ):
                result = await router.get_ollama_client()
        assert result is mock_ollama

    @pytest.mark.asyncio
    async def test_skips_offline_in_enabled_fallback(self):
        router, mock_db, _ = _make_router()
        mock_db.get_enabled_ollama_nodes.return_value = [
            _node(node_name="dead", status="offline"),
            _node(node_name="live", status="online"),
        ]
        mock_ollama = MagicMock()
        with patch.object(router, "get_ollama_client_for_role", AsyncMock(return_value=None)):
            with patch(
                "orchestrator.services.ollama_client.OllamaClient.from_node",
                return_value=mock_ollama,
            ) as mock_fn:
                result = await router.get_ollama_client()
        assert result is mock_ollama
        assert mock_fn.call_args[0][0]["node_name"] == "live"

    @pytest.mark.asyncio
    async def test_falls_back_to_env_default_when_no_enabled_nodes(self):
        router, mock_db, mock_settings = _make_router()
        mock_db.get_enabled_ollama_nodes.return_value = [_node(status="offline")]
        mock_env_client = MagicMock()
        with patch.object(router, "get_ollama_client_for_role", AsyncMock(return_value=None)):
            with patch(
                "orchestrator.services.ollama_client.OllamaClient",
                return_value=mock_env_client,
            ) as MockCls:
                result = await router.get_ollama_client()
        MockCls.assert_called_once_with(mock_settings)
        assert result is mock_env_client


# ── _probe_and_update ─────────────────────────────────────────────────────────


class TestProbeAndUpdate:
    @pytest.mark.asyncio
    async def test_writes_status_to_db(self):
        router, mock_db, _ = _make_router()
        node = _node()
        with patch.object(NodeRouter, "_probe_node", AsyncMock(return_value="online")):
            with patch.object(NodeRouter, "_measure_ttft", AsyncMock(return_value=None)):
                await router._probe_and_update(node)
        args = mock_db.update_node_status.call_args[0]
        assert args[0] == "rig"
        assert args[1] == "online"

    @pytest.mark.asyncio
    async def test_writes_ttft_when_measured(self):
        router, mock_db, _ = _make_router()
        with patch.object(NodeRouter, "_probe_node", AsyncMock(return_value="online")):
            with patch.object(NodeRouter, "_measure_ttft", AsyncMock(return_value=350)):
                await router._probe_and_update(_node())
        mock_db.update_node_latency.assert_awaited_once_with("rig", 350)

    @pytest.mark.asyncio
    async def test_skips_ttft_write_when_none(self):
        router, mock_db, _ = _make_router()
        with patch.object(NodeRouter, "_probe_node", AsyncMock(return_value="offline")):
            with patch.object(NodeRouter, "_measure_ttft", AsyncMock(return_value=None)):
                await router._probe_and_update(_node())
        mock_db.update_node_latency.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_uses_node_model_for_ttft_probe(self):
        router, mock_db, _ = _make_router()
        with patch.object(NodeRouter, "_probe_node", AsyncMock(return_value="online")):
            with patch.object(NodeRouter, "_measure_ttft", AsyncMock(return_value=100)) as m:
                await router._probe_and_update(_node(model="llama3"))
        m.assert_awaited_once_with("http://localhost:11434", "llama3")

    @pytest.mark.asyncio
    async def test_falls_back_to_settings_model_when_node_model_empty(self):
        router, mock_db, mock_settings = _make_router()
        mock_settings.ollama_model = "phi3"
        with patch.object(NodeRouter, "_probe_node", AsyncMock(return_value="online")):
            with patch.object(NodeRouter, "_measure_ttft", AsyncMock(return_value=100)) as m:
                await router._probe_and_update(_node(model=""))
        m.assert_awaited_once_with("http://localhost:11434", "phi3")


# ── _check_all_nodes ──────────────────────────────────────────────────────────


class TestCheckAllNodes:
    @pytest.mark.asyncio
    async def test_probes_only_ollama_and_enabled_nodes(self):
        router, mock_db, _ = _make_router()
        mock_db.get_all_nodes.return_value = [
            _node(node_name="a", node_type="ollama", enabled=True),
            _node(node_name="b", node_type="ollama", enabled=False),
            _node(node_name="c", node_type="grpc", enabled=True),
            _node(node_name="d", node_type="ollama", enabled=True),
        ]
        probed: list[str] = []

        async def fake_probe(n):
            probed.append(n["node_name"])

        with patch.object(router, "_probe_and_update", side_effect=fake_probe):
            await router._check_all_nodes()

        assert set(probed) == {"a", "d"}

    @pytest.mark.asyncio
    async def test_handles_empty_node_list(self):
        router, mock_db, _ = _make_router()
        mock_db.get_all_nodes.return_value = []
        await router._check_all_nodes()  # must not raise

    @pytest.mark.asyncio
    async def test_isolates_per_node_exceptions(self):
        router, mock_db, _ = _make_router()
        mock_db.get_all_nodes.return_value = [
            _node(node_name="bad"),
            _node(node_name="ok"),
        ]
        succeeded: list[str] = []

        async def fake_probe(n):
            if n["node_name"] == "bad":
                raise RuntimeError("network error")
            succeeded.append(n["node_name"])

        with patch.object(router, "_probe_and_update", side_effect=fake_probe):
            await router._check_all_nodes()  # must not raise

        assert "ok" in succeeded


# ── warmup_all_nodes ──────────────────────────────────────────────────────────


class TestWarmupAllNodes:
    @pytest.mark.asyncio
    async def test_delegates_to_check_all_nodes(self):
        router, _, _ = _make_router()
        with patch.object(router, "_check_all_nodes", AsyncMock()) as mock_check:
            await router.warmup_all_nodes()
        mock_check.assert_awaited_once()


# ── start / stop lifecycle ────────────────────────────────────────────────────


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_health_task(self):
        router, mock_db, _ = _make_router()
        mock_db.get_all_nodes.return_value = []
        await router.start()
        assert router._task is not None
        await router.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_health_task(self):
        router, mock_db, _ = _make_router()
        mock_db.get_all_nodes.return_value = []
        await router.start()
        await router.stop()
        assert router._task.done()

    @pytest.mark.asyncio
    async def test_stop_is_safe_when_task_is_none(self):
        router, _, _ = _make_router()
        router._task = None
        await router.stop()  # must not raise

    @pytest.mark.asyncio
    async def test_start_triggers_immediate_node_check(self):
        router, mock_db, _ = _make_router()
        mock_db.get_all_nodes.return_value = [_node()]
        with patch.object(router, "_probe_and_update", AsyncMock()) as mock_probe:
            await router.start()
            await asyncio.sleep(0.05)  # let the fire-and-forget task run
            await router.stop()
        assert mock_probe.call_count >= 1
