"""
Unit tests: AuthService, CacheService, OllamaClient, ClaudeClient, GeminiClient, TelemetryService.

All external I/O (Redis, PostgreSQL, HTTP) is mocked — no live services required.
Run: pytest orchestrator/tests/test_ai_clients_auth_cache_telemetry.py -v
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings(**overrides):
    cfg = MagicMock()
    cfg.ollama_host = "http://ollama:11434"
    cfg.ollama_model = "mistral"
    cfg.ollama_timeout_seconds = 30
    cfg.claude_api_key = "sk-claude-test"
    cfg.claude_model = "claude-3-haiku-20240307"
    cfg.gemini_api_key = "gemini-test-key"
    cfg.gemini_model = "gemini-1.5-pro"
    cfg.media_proxy_url = "http://localhost:8001"
    cfg.cloud_provider = "gemini"
    cfg.music_model = "gemini"
    cfg.redis_host = "localhost"
    cfg.redis_port = 6379
    cfg.redis_password = "redispass"
    cfg.session_ttl_seconds = 3600
    for key, val in overrides.items():
        setattr(cfg, key, val)
    return cfg


def _http_mock(response_json: dict, status: int = 200):
    mock_response = MagicMock()
    mock_response.status_code = status
    mock_response.json.return_value = response_json
    mock_response.raise_for_status = MagicMock()

    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)
    mock_http.post = AsyncMock(return_value=mock_response)
    mock_http.get = AsyncMock(return_value=mock_response)
    return mock_http


def _pool_mock(fetchval=None, fetchrow=None):
    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=fetchval)
    mock_conn.fetchrow = AsyncMock(return_value=fetchrow)
    mock_conn.execute = AsyncMock()

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    ctx.__aexit__ = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=ctx)
    return mock_pool, mock_conn


def _cache_service():
    from orchestrator.services.cache import CacheService
    svc = CacheService(_settings())
    redis = AsyncMock()
    svc._redis = redis
    return svc, redis


# ===========================================================================
# _roll_dice
# ===========================================================================

class TestRollDice:
    @staticmethod
    def _fn():
        from orchestrator.services.ollama_client import _roll_dice
        return _roll_dice

    def test_single_die(self):
        with patch("orchestrator.services.ollama_client.random.randint", return_value=4):
            assert self._fn()("1d6", 0) == 4

    def test_multiple_dice_summed(self):
        with patch("orchestrator.services.ollama_client.random.randint", return_value=3):
            assert self._fn()("3d6", 0) == 9

    def test_positive_modifier(self):
        with patch("orchestrator.services.ollama_client.random.randint", return_value=10):
            assert self._fn()("1d20", 5) == 15

    def test_negative_modifier(self):
        with patch("orchestrator.services.ollama_client.random.randint", return_value=6):
            assert self._fn()("2d6", -2) == 10

    def test_zero_modifier_passthrough(self):
        with patch("orchestrator.services.ollama_client.random.randint", return_value=7):
            assert self._fn()("1d8", 0) == 7

    def test_invalid_notation_raises(self):
        with pytest.raises(Exception):
            self._fn()("not-a-roll", 0)


# ===========================================================================
# OllamaClient
# ===========================================================================

class TestOllamaClientAttributes:
    def test_base_url_from_settings(self):
        from orchestrator.services.ollama_client import OllamaClient
        assert OllamaClient(_settings())._base_url == "http://ollama:11434"

    def test_model_from_settings(self):
        from orchestrator.services.ollama_client import OllamaClient
        assert OllamaClient(_settings(ollama_model="llama3"))._model == "llama3"

    def test_from_node_sets_url_and_model(self):
        from orchestrator.services.ollama_client import OllamaClient
        node = MagicMock()
        node.base_url = "http://gpu-node:11434"
        node.model = "mixtral"
        node.node_name = "gpu-0"
        node.voice_id = "deep"
        client = OllamaClient.from_node(node, _settings())
        assert client._base_url == "http://gpu-node:11434"
        assert client._model == "mixtral"


class TestOllamaClientGenerate:
    @pytest.mark.asyncio
    async def test_returns_text(self):
        from orchestrator.services.ollama_client import OllamaClient
        mock_http = _http_mock({"message": {"content": "You strike!"}})
        with patch("orchestrator.services.ollama_client.httpx.AsyncClient", return_value=mock_http):
            result = await OllamaClient(_settings()).generate("sys", "user")
        assert "strike" in result

    @pytest.mark.asyncio
    async def test_raises_on_http_error(self):
        import httpx
        from orchestrator.services.ollama_client import OllamaClient
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=None)
        mock_http.post = AsyncMock(return_value=mock_response)
        with patch("orchestrator.services.ollama_client.httpx.AsyncClient", return_value=mock_http):
            with pytest.raises(Exception):
                await OllamaClient(_settings()).generate("sys", "user")


class TestOllamaClientSanitiseInventory:
    def test_strips_narrative_fields(self):
        from orchestrator.services.ollama_client import OllamaClient
        items = [{"name": "Sword", "damage": "1d8", "description": "Shiny",
                  "lore": "Ancient", "flavor": "Cold steel"}]
        result = OllamaClient(_settings())._sanitise_inventory(items)
        assert result[0]["name"] == "Sword"
        assert "description" not in result[0]
        assert "lore" not in result[0]
        assert "flavor" not in result[0]

    def test_empty_returns_empty(self):
        from orchestrator.services.ollama_client import OllamaClient
        assert OllamaClient(_settings())._sanitise_inventory([]) == []

    def test_preserves_mechanical_fields(self):
        from orchestrator.services.ollama_client import OllamaClient
        items = [{"name": "Potion", "heal": 10, "weight": 0.5}]
        assert OllamaClient(_settings())._sanitise_inventory(items)[0]["heal"] == 10


class TestOllamaClientResolveAction:
    @pytest.mark.asyncio
    async def test_dice_injected_from_backend(self):
        from orchestrator.services.ollama_client import OllamaClient
        ctx = MagicMock()
        ctx.intent_id = "intent-xyz"
        ctx.character = MagicMock()
        ctx.character.name = "Fighter"
        ctx.character.stats = {}
        ctx.inventory = []
        ctx.recent_actions = []
        ctx.world_facts = []
        ctx.rulebook_chunks = []
        ctx.vehicle = None
        ctx.action_text = "I attack"

        llm_json = json.dumps({
            "outcome": "success",
            "dice_request": {"notation": "1d20", "modifier": 3, "skill": "strength"},
            "stat_delta": {},
            "inventory_delta": [],
            "narrative_hint": "You swing true",
        })
        mock_http = _http_mock({"message": {"content": llm_json}})
        with patch("orchestrator.services.ollama_client.httpx.AsyncClient", return_value=mock_http), \
             patch("orchestrator.services.ollama_client.random.randint", return_value=15):
            result = await OllamaClient(_settings()).resolve_action(ctx)
        assert result.outcome == "success"
        assert result.dice_total >= 1


class TestOllamaGenerateNarrative:
    @pytest.mark.asyncio
    async def test_returns_result(self):
        from orchestrator.services.ollama_client import OllamaClient
        req = MagicMock()
        req.character_name = "Ranger"
        req.outcome = "success"
        req.narrative_hint = "Arrow flies true"
        req.world_tone = "gritty"

        mock_http = _http_mock({"message": {"content": "The arrow strikes!"}})
        with patch("orchestrator.services.ollama_client.httpx.AsyncClient", return_value=mock_http):
            result = await OllamaClient(_settings()).generate_narrative(req)
        assert result is not None


# ===========================================================================
# ClaudeClient
# ===========================================================================

class TestClaudeClientGenerate:
    @pytest.mark.asyncio
    async def test_parses_content_array(self):
        from orchestrator.services.claude_client import ClaudeClient
        mock_http = _http_mock({"content": [{"text": "Darkness falls."}]})
        with patch("orchestrator.services.claude_client.httpx.AsyncClient", return_value=mock_http):
            result = await ClaudeClient(_settings()).generate("sys", "user")
        assert result == "Darkness falls."

    @pytest.mark.asyncio
    async def test_raises_on_auth_error(self):
        import httpx
        from orchestrator.services.claude_client import ClaudeClient
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401", request=MagicMock(), response=MagicMock()
        )
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=None)
        mock_http.post = AsyncMock(return_value=mock_response)
        with patch("orchestrator.services.claude_client.httpx.AsyncClient", return_value=mock_http):
            with pytest.raises(Exception):
                await ClaudeClient(_settings()).generate("sys", "user")

    @pytest.mark.asyncio
    async def test_post_called_once(self):
        from orchestrator.services.claude_client import ClaudeClient
        mock_http = _http_mock({"content": [{"text": "ok"}]})
        with patch("orchestrator.services.claude_client.httpx.AsyncClient", return_value=mock_http):
            await ClaudeClient(_settings()).generate("sys", "user")
        mock_http.post.assert_called_once()


class TestClaudeClientGenerateNarrative:
    @pytest.mark.asyncio
    async def test_embed_title_contains_char_and_outcome(self):
        from orchestrator.services.claude_client import ClaudeClient
        req = MagicMock()
        req.character_name = "Paladin"
        req.outcome = "critical_success"
        req.narrative_hint = "Divine smite"
        req.world_tone = "high fantasy"

        mock_http = _http_mock({"content": [{"text": "Holy light erupts!"}]})
        with patch("orchestrator.services.claude_client.httpx.AsyncClient", return_value=mock_http):
            result = await ClaudeClient(_settings()).generate_narrative(req)
        assert result is not None
        assert "Paladin" in result.embed_title or "critical_success" in result.embed_title


# ===========================================================================
# GeminiClient
# ===========================================================================

class TestGeminiClientGenerate:
    @pytest.mark.asyncio
    async def test_parses_candidates_path(self):
        from orchestrator.services.gemini_client import GeminiClient
        resp = {"candidates": [{"content": {"parts": [{"text": "Fog rolls in."}]}}]}
        mock_http = _http_mock(resp)
        with patch("orchestrator.services.gemini_client.httpx.AsyncClient", return_value=mock_http):
            result = await GeminiClient(_settings()).generate("sys", "user")
        assert result == "Fog rolls in."

    @pytest.mark.asyncio
    async def test_raises_on_api_error(self):
        import httpx
        from orchestrator.services.gemini_client import GeminiClient
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403", request=MagicMock(), response=MagicMock()
        )
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=None)
        mock_http.post = AsyncMock(return_value=mock_response)
        with patch("orchestrator.services.gemini_client.httpx.AsyncClient", return_value=mock_http):
            with pytest.raises(Exception):
                await GeminiClient(_settings()).generate("sys", "user")


class TestGeminiClientGenerateMusic:
    @pytest.mark.asyncio
    async def test_lavalink_model_returns_none(self):
        from orchestrator.services.gemini_client import GeminiClient
        result = await GeminiClient(_settings(music_model="lavalink")).generate_music(
            "dark ambience", "exploration", 30, MagicMock()
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_cache_hit_skips_api(self):
        from orchestrator.services.gemini_client import GeminiClient
        mock_http = _http_mock({})
        with patch("orchestrator.services.gemini_client.os.path.exists", return_value=True):
            with patch("orchestrator.services.gemini_client.httpx.AsyncClient", return_value=mock_http):
                result = await GeminiClient(_settings()).generate_music(
                    "battle drums", "combat", 30, MagicMock()
                )
            mock_http.post.assert_not_called()
        assert result is not None

    @pytest.mark.asyncio
    async def test_api_failure_fail_silent(self):
        import httpx
        from orchestrator.services.gemini_client import GeminiClient
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=None)
        mock_http.post = AsyncMock(return_value=mock_response)
        with patch("orchestrator.services.gemini_client.os.path.exists", return_value=False), \
             patch("orchestrator.services.gemini_client.httpx.AsyncClient", return_value=mock_http):
            result = await GeminiClient(_settings()).generate_music(
                "tension", "boss_fight", 60, MagicMock()
            )
        assert result is None


class TestGeminiClientGenerateNarrative:
    @pytest.mark.asyncio
    async def test_returns_payload(self):
        from orchestrator.services.gemini_client import GeminiClient
        req = MagicMock()
        req.character_name = "Necromancer"
        req.outcome = "failure"
        req.narrative_hint = "Ritual collapses"
        req.world_tone = "dark"

        resp = {"candidates": [{"content": {"parts": [{"text": "The ritual fails."}]}}]}
        mock_http = _http_mock(resp)
        with patch("orchestrator.services.gemini_client.httpx.AsyncClient", return_value=mock_http):
            result = await GeminiClient(_settings()).generate_narrative(req)
        assert result is not None


# ===========================================================================
# AuthService
# ===========================================================================

class TestAuthServiceIsFirstBoot:
    @pytest.mark.asyncio
    async def test_true_when_no_admins(self):
        from orchestrator.services.auth import AuthService
        pool, _ = _pool_mock(fetchval=0)
        assert await AuthService(pool, _settings()).is_first_boot() is True

    @pytest.mark.asyncio
    async def test_false_when_admins_exist(self):
        from orchestrator.services.auth import AuthService
        pool, _ = _pool_mock(fetchval=2)
        assert await AuthService(pool, _settings()).is_first_boot() is False


class TestAuthServiceCreateAdmin:
    @pytest.mark.asyncio
    async def test_success_returns_true(self):
        from orchestrator.services.auth import AuthService
        pool, _ = _pool_mock()
        assert await AuthService(pool, _settings()).create_admin("admin", "S3cureP@ss!") is True

    @pytest.mark.asyncio
    async def test_duplicate_returns_false(self):
        import asyncpg
        from orchestrator.services.auth import AuthService
        pool, conn = _pool_mock()
        conn.execute = AsyncMock(
            side_effect=asyncpg.exceptions.UniqueViolationError("dup")
        )
        assert await AuthService(pool, _settings()).create_admin("admin", "pass") is False


class TestAuthServiceVerify:
    @pytest.mark.asyncio
    async def test_correct_password_returns_true(self):
        from orchestrator.services.auth import AuthService
        from passlib.context import CryptContext
        hashed = CryptContext(schemes=["bcrypt"], deprecated="auto").hash("rightpass")
        pool, _ = _pool_mock(fetchrow={"password_hash": hashed})
        assert await AuthService(pool, _settings()).verify("admin", "rightpass") is True

    @pytest.mark.asyncio
    async def test_wrong_password_returns_false(self):
        from orchestrator.services.auth import AuthService
        from passlib.context import CryptContext
        hashed = CryptContext(schemes=["bcrypt"], deprecated="auto").hash("rightpass")
        pool, _ = _pool_mock(fetchrow={"password_hash": hashed})
        assert await AuthService(pool, _settings()).verify("admin", "wrongpass") is False

    @pytest.mark.asyncio
    async def test_missing_user_returns_false(self):
        from orchestrator.services.auth import AuthService
        pool, _ = _pool_mock(fetchrow=None)
        assert await AuthService(pool, _settings()).verify("ghost", "anypass") is False


# ===========================================================================
# CacheService
# ===========================================================================

class TestCacheServiceSessions:
    @pytest.mark.asyncio
    async def test_create_session_returns_string(self):
        svc, redis = _cache_service()
        redis.setex = AsyncMock()
        token = await svc.create_session("user-001")
        assert isinstance(token, str) and len(token) > 8

    @pytest.mark.asyncio
    async def test_get_session_hit(self):
        svc, redis = _cache_service()
        redis.get = AsyncMock(return_value=json.dumps({"user_id": "u1"}).encode())
        result = await svc.get_session("tok-abc")
        assert result["user_id"] == "u1"

    @pytest.mark.asyncio
    async def test_get_session_miss(self):
        svc, redis = _cache_service()
        redis.get = AsyncMock(return_value=None)
        assert await svc.get_session("tok-ghost") is None

    @pytest.mark.asyncio
    async def test_refresh_session_calls_expire(self):
        svc, redis = _cache_service()
        redis.expire = AsyncMock()
        await svc.refresh_session("tok-abc")
        redis.expire.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_session_calls_delete(self):
        svc, redis = _cache_service()
        redis.delete = AsyncMock()
        await svc.delete_session("tok-abc")
        redis.delete.assert_called_once()


class TestCacheServicePipelineLock:
    @pytest.mark.asyncio
    async def test_lock_acquired(self):
        svc, redis = _cache_service()
        redis.set = AsyncMock(return_value=True)
        assert await svc.set_pipeline_lock("camp-1") is True

    @pytest.mark.asyncio
    async def test_lock_rejected_when_held(self):
        svc, redis = _cache_service()
        redis.set = AsyncMock(return_value=None)
        assert await svc.set_pipeline_lock("camp-1") is False

    @pytest.mark.asyncio
    async def test_release_deletes_key(self):
        svc, redis = _cache_service()
        redis.delete = AsyncMock()
        await svc.release_pipeline_lock("camp-1")
        redis.delete.assert_called_once()


class TestCacheServiceNarrativeCache:
    @pytest.mark.asyncio
    async def test_store_narrative(self):
        svc, redis = _cache_service()
        redis.setex = AsyncMock()
        await svc.cache_narrative("camp-1", "A dragon appears!")
        redis.setex.assert_called_once()

    @pytest.mark.asyncio
    async def test_retrieve_narrative_hit(self):
        svc, redis = _cache_service()
        redis.get = AsyncMock(return_value=b"A dragon appears!")
        assert await svc.get_cached_narrative("camp-1") == "A dragon appears!"

    @pytest.mark.asyncio
    async def test_retrieve_narrative_miss(self):
        svc, redis = _cache_service()
        redis.get = AsyncMock(return_value=None)
        assert await svc.get_cached_narrative("camp-1") is None


class TestCacheServiceJobProgress:
    @pytest.mark.asyncio
    async def test_set_job_progress(self):
        svc, redis = _cache_service()
        redis.set = AsyncMock()
        await svc.set_job_progress("job-99", {"pct": 50, "status": "processing"})
        redis.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_job_progress_returns_dict(self):
        svc, redis = _cache_service()
        redis.get = AsyncMock(return_value=json.dumps({"pct": 100, "status": "done"}).encode())
        result = await svc.get_job_progress("job-99")
        assert result["status"] == "done"


# ===========================================================================
# TelemetryService
# ===========================================================================

class TestTelemetryServiceEmit:
    @pytest.mark.asyncio
    async def test_broadcast_reaches_client(self):
        from orchestrator.services.telemetry import TelemetryService
        svc = TelemetryService()
        q = asyncio.Queue(maxsize=512)
        svc._clients = {q}
        await svc.emit("combat_event", action="attack")
        assert not q.empty()

    @pytest.mark.asyncio
    async def test_slow_client_dropped_silently(self):
        from orchestrator.services.telemetry import TelemetryService
        svc = TelemetryService()
        q = asyncio.Queue(maxsize=1)
        await q.put({"type": "fill"})
        svc._clients = {q}
        await svc.emit("overflow_event")  # must not raise
        assert q.qsize() == 1

    @pytest.mark.asyncio
    async def test_emit_appends_to_replay_buffer(self):
        from orchestrator.services.telemetry import TelemetryService
        svc = TelemetryService()
        svc._clients = set()
        await svc.emit("story_beat", chapter="one")
        assert len(svc._replay_buffer) >= 1


class TestTelemetryServiceConnect:
    @pytest.mark.asyncio
    async def test_accept_called_on_connect(self):
        from orchestrator.services.telemetry import TelemetryService
        svc = TelemetryService()
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()
        mock_ws.send_json = AsyncMock()
        mock_ws.receive_text = AsyncMock(side_effect=Exception("disconnect"))
        try:
            await svc.connect(mock_ws)
        except Exception:
            pass
        mock_ws.accept.assert_called_once()

    @pytest.mark.asyncio
    async def test_replay_buffer_sent_on_connect(self):
        from orchestrator.services.telemetry import TelemetryService
        svc = TelemetryService()
        svc._replay_buffer.append({"type": "past_event"})
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()
        mock_ws.send_json = AsyncMock()
        mock_ws.receive_text = AsyncMock(side_effect=Exception("disconnect"))
        try:
            await svc.connect(mock_ws)
        except Exception:
            pass
        mock_ws.send_json.assert_called()


class TestTelemetryServiceDisconnect:
    def test_queue_removed_from_clients(self):
        from orchestrator.services.telemetry import TelemetryService
        svc = TelemetryService()
        q = asyncio.Queue()
        svc._clients = {q}
        svc.disconnect(q)
        assert q not in svc._clients

    def test_client_count_property(self):
        from orchestrator.services.telemetry import TelemetryService
        svc = TelemetryService()
        svc._clients = {asyncio.Queue(), asyncio.Queue()}
        assert svc.client_count == 2
