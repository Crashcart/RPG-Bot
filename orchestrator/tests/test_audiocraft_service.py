"""Unit tests for AudioCraftService and acoustic context extraction."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.audiocraft_service import (
    AcousticContext,
    AudioCraftService,
    extract_acoustic_context,
)
from orchestrator.config import Settings


# ── helpers ───────────────────────────────────────────────────────────────────

def _settings(**overrides) -> Settings:
    base = {
        "postgres_password": "x",
        "redis_password": "x",
        "gemini_api_key": "x",
        "audiocraft_url": "http://audiocraft:8080",
        "audiocraft_ambient_ttl_seconds": 3600,
        "audiocraft_sfx_ttl_seconds": 300,
        "media_proxy_url": "http://media:8001",
    }
    base.update(overrides)
    return Settings(**base)


def _service(redis=None, **kw) -> AudioCraftService:
    return AudioCraftService(settings=_settings(**kw), redis=redis)


# ── TestExtractAcousticContext ────────────────────────────────────────────────

class TestExtractAcousticContext:
    def test_weather_keywords(self):
        ctx = extract_acoustic_context("A storm rolls in, thunder cracking overhead.")
        assert "storm" in ctx.weather
        assert "thunder" in ctx.weather

    def test_material_and_action(self):
        ctx = extract_acoustic_context("The iron gate slams shut with a crash.")
        assert "iron" in ctx.materials
        assert "slam" in ctx.actions or "crash" in ctx.actions

    def test_environment(self):
        ctx = extract_acoustic_context("You enter the tavern and hear laughter.")
        assert "tavern" in ctx.environment

    def test_empty_text(self):
        ctx = extract_acoustic_context("")
        assert ctx.weather == []
        assert ctx.materials == []
        assert ctx.actions == []
        assert ctx.environment == []

    def test_no_matching_keywords(self):
        ctx = extract_acoustic_context("The hero smiles and walks away.")
        assert ctx.weather == []
        assert ctx.environment == []


# ── TestAcousticContextProperties ────────────────────────────────────────────

class TestAcousticContextProperties:
    def test_ambient_prompt_with_environment_and_weather(self):
        ctx = AcousticContext(environment=["dungeon"], weather=["rain"])
        assert "dungeon ambience" in ctx.ambient_prompt
        assert "rain" in ctx.ambient_prompt

    def test_ambient_prompt_environment_only(self):
        ctx = AcousticContext(environment=["forest"])
        assert ctx.ambient_prompt == "forest ambience"

    def test_ambient_prompt_fallback(self):
        ctx = AcousticContext()
        assert ctx.ambient_prompt == "quiet interior ambience"

    def test_sfx_prompt_with_material_and_action(self):
        ctx = AcousticContext(materials=["wood"], actions=["creak"])
        assert ctx.sfx_prompt == "wood creak"

    def test_sfx_prompt_action_only(self):
        ctx = AcousticContext(actions=["splash"])
        assert ctx.sfx_prompt == "splash"

    def test_sfx_prompt_no_action(self):
        ctx = AcousticContext()
        assert ctx.sfx_prompt is None

    def test_cache_key_is_stable(self):
        ctx1 = AcousticContext(environment=["tavern"], weather=["rain"])
        ctx2 = AcousticContext(environment=["tavern"], weather=["rain"])
        assert ctx1.cache_key == ctx2.cache_key

    def test_cache_key_differs_by_environment(self):
        ctx1 = AcousticContext(environment=["tavern"])
        ctx2 = AcousticContext(environment=["dungeon"])
        assert ctx1.cache_key != ctx2.cache_key

    def test_sfx_cache_key_none_without_actions(self):
        ctx = AcousticContext()
        assert ctx.sfx_cache_key is None


# ── TestAudioCraftServiceBuildSoundscape ──────────────────────────────────────

class TestAudioCraftServiceBuildSoundscape:
    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_url(self):
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=b"http://media:8001/assets/audio/cached.wav")
        svc = _service(redis=redis)
        await svc.start()
        cue = await svc.build_soundscape("You enter the dungeon.")
        assert cue.ambient_url == "http://media:8001/assets/audio/cached.wav"
        assert cue.from_cache is True
        await svc.stop()

    @pytest.mark.asyncio
    async def test_cache_miss_calls_audiogen(self):
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.setex = AsyncMock()
        svc = _service(redis=redis)
        await svc.start()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"filename": "abc123.wav"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with patch.object(svc._session, "post", return_value=mock_resp):
            cue = await svc.build_soundscape("The dungeon drips with water.")

        assert cue.ambient_url is not None
        assert "abc123.wav" in cue.ambient_url
        assert cue.from_cache is False
        await svc.stop()

    @pytest.mark.asyncio
    async def test_audiogen_failure_returns_none_url(self):
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        svc = _service(redis=redis)
        await svc.start()

        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with patch.object(svc._session, "post", return_value=mock_resp):
            cue = await svc.build_soundscape("The forest rustles.")

        assert cue.ambient_url is None
        await svc.stop()

    @pytest.mark.asyncio
    async def test_no_sfx_without_action_keywords(self):
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        svc = _service(redis=redis)
        await svc.start()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"filename": "ambient.wav"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with patch.object(svc._session, "post", return_value=mock_resp):
            cue = await svc.build_soundscape("A quiet forest clearing.")

        assert cue.sfx_url is None
        await svc.stop()

    @pytest.mark.asyncio
    async def test_no_redis_does_not_crash(self):
        svc = _service(redis=None)
        await svc.start()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"filename": "x.wav"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with patch.object(svc._session, "post", return_value=mock_resp):
            cue = await svc.build_soundscape("The tavern buzzes with chatter.")

        assert cue is not None
        await svc.stop()


# ── TestAudioCraftServiceLifecycle ────────────────────────────────────────────

class TestAudioCraftServiceLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_session(self):
        svc = _service()
        assert svc._session is None
        await svc.start()
        assert svc._session is not None
        await svc.stop()

    @pytest.mark.asyncio
    async def test_stop_closes_session(self):
        svc = _service()
        await svc.start()
        session = svc._session
        await svc.stop()
        assert svc._session is None
        assert session.closed

    @pytest.mark.asyncio
    async def test_call_without_start_returns_none(self):
        svc = _service()
        result = await svc.generate_ambient("quiet cave")
        assert result is None


# ── TestRedisHelpers ──────────────────────────────────────────────────────────

class TestRedisHelpers:
    @pytest.mark.asyncio
    async def test_get_decodes_bytes(self):
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=b"http://example.com/audio.wav")
        svc = _service(redis=redis)
        result = await svc._redis_get("some:key")
        assert result == "http://example.com/audio.wav"

    @pytest.mark.asyncio
    async def test_get_returns_none_on_error(self):
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=Exception("connection lost"))
        svc = _service(redis=redis)
        result = await svc._redis_get("some:key")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_calls_setex(self):
        redis = AsyncMock()
        redis.setex = AsyncMock()
        svc = _service(redis=redis)
        await svc._redis_set("k", "v", ttl=60)
        redis.setex.assert_called_once_with("k", 60, "v")

    @pytest.mark.asyncio
    async def test_set_silent_on_error(self):
        redis = AsyncMock()
        redis.setex = AsyncMock(side_effect=Exception("boom"))
        svc = _service(redis=redis)
        await svc._redis_set("k", "v", ttl=60)  # must not raise
