"""
Unit tests for ImageGenService and ElevenLabsClient.
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# ImageGenService tests
# ─────────────────────────────────────────────────────────────────────────────


def _make_db(backend="disabled"):
    db = MagicMock()
    db.get_system_setting = AsyncMock(return_value=backend)
    return db


class TestImageGenBackendDisabled:
    """When backend='disabled', generate() returns None immediately."""

    @pytest.mark.asyncio
    async def test_disabled_backend_returns_none(self, tmp_path):
        with patch("orchestrator.services.image_gen._GEN_DIR", tmp_path / "gen"), \
             patch("orchestrator.services.image_gen._PORTRAIT_DIR", tmp_path / "portraits"), \
             patch("orchestrator.services.image_gen.get_settings") as ms:
            ms.return_value = MagicMock(
                media_proxy_url="http://proxy",
                comfyui_url="http://comfyui:8188",
                stability_ai_key="",
                openai_api_key="",
            )
            from orchestrator.services.image_gen import ImageGenService
            svc = ImageGenService(db=_make_db("disabled"))
            result = await svc.generate("a dragon in a cave")
        assert result is None

    @pytest.mark.asyncio
    async def test_unknown_backend_returns_none(self, tmp_path):
        with patch("orchestrator.services.image_gen._GEN_DIR", tmp_path / "gen"), \
             patch("orchestrator.services.image_gen._PORTRAIT_DIR", tmp_path / "portraits"), \
             patch("orchestrator.services.image_gen.get_settings") as ms:
            ms.return_value = MagicMock(
                media_proxy_url="http://proxy",
                comfyui_url="http://comfyui:8188",
                stability_ai_key="",
                openai_api_key="",
            )
            from orchestrator.services.image_gen import ImageGenService
            svc = ImageGenService(db=_make_db("unknown_backend"))
            result = await svc.generate("a dragon")
        assert result is None


class TestImageGenBackendRouting:
    """generate() routes to the correct backend."""

    @pytest.mark.asyncio
    async def test_stability_backend_no_key_returns_none(self, tmp_path):
        with patch("orchestrator.services.image_gen._GEN_DIR", tmp_path / "gen"), \
             patch("orchestrator.services.image_gen._PORTRAIT_DIR", tmp_path / "portraits"), \
             patch("orchestrator.services.image_gen.get_settings") as ms:
            ms.return_value = MagicMock(
                media_proxy_url="http://proxy",
                comfyui_url="http://comfyui:8188",
                stability_ai_key="",   # no key → early exit
                openai_api_key="",
            )
            from orchestrator.services.image_gen import ImageGenService
            svc = ImageGenService(db=_make_db("stability_ai"))
            result = await svc.generate("scene")
        assert result is None

    @pytest.mark.asyncio
    async def test_dalle3_backend_no_key_returns_none(self, tmp_path):
        with patch("orchestrator.services.image_gen._GEN_DIR", tmp_path / "gen"), \
             patch("orchestrator.services.image_gen._PORTRAIT_DIR", tmp_path / "portraits"), \
             patch("orchestrator.services.image_gen.get_settings") as ms:
            ms.return_value = MagicMock(
                media_proxy_url="http://proxy",
                comfyui_url="http://comfyui:8188",
                stability_ai_key="",
                openai_api_key="",  # no key → early exit
            )
            from orchestrator.services.image_gen import ImageGenService
            svc = ImageGenService(db=_make_db("dalle3"))
            result = await svc.generate("scene")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_backend_without_db_returns_disabled(self, tmp_path):
        with patch("orchestrator.services.image_gen._GEN_DIR", tmp_path / "gen"), \
             patch("orchestrator.services.image_gen._PORTRAIT_DIR", tmp_path / "portraits"), \
             patch("orchestrator.services.image_gen.get_settings") as ms:
            ms.return_value = MagicMock(
                media_proxy_url="http://proxy",
                comfyui_url="http://comfyui:8188",
                stability_ai_key="",
                openai_api_key="",
            )
            from orchestrator.services.image_gen import ImageGenService
            svc = ImageGenService(db=None)
            backend = await svc._get_backend()
        assert backend == "disabled"


class TestImageGenNpcPortrait:
    """generate_npc_portrait() returns cached or new URL."""

    @pytest.mark.asyncio
    async def test_returns_cached_portrait_url(self, tmp_path):
        portraits_dir = tmp_path / "portraits"
        portraits_dir.mkdir()
        with patch("orchestrator.services.image_gen._GEN_DIR", tmp_path / "gen"), \
             patch("orchestrator.services.image_gen._PORTRAIT_DIR", portraits_dir), \
             patch("orchestrator.services.image_gen.get_settings") as ms:
            ms.return_value = MagicMock(
                media_proxy_url="http://proxy",
                comfyui_url="http://comfyui:8188",
                stability_ai_key="",
                openai_api_key="",
            )
            from orchestrator.services.image_gen import ImageGenService
            svc = ImageGenService(db=_make_db("disabled"))

            # Pre-create the portrait file
            campaign_id = "aaaabbbb"
            safe_name = "mira_the_innkeeper"
            portrait_path = portraits_dir / f"portrait_{campaign_id[:8]}_{safe_name}.png"
            portrait_path.write_bytes(b"fake-png")

            result = await svc.generate_npc_portrait("Mira the Innkeeper", "old woman", campaign_id)

        assert result is not None
        assert "portraits" in result
        assert safe_name in result


# ─────────────────────────────────────────────────────────────────────────────
# ElevenLabsClient tests
# ─────────────────────────────────────────────────────────────────────────────


class TestElevenLabsClientInit:
    """ElevenLabsClient initialises correctly based on API key presence."""

    def test_disabled_when_no_api_key(self, tmp_path):
        with patch("orchestrator.services.elevenlabs_client._ASSET_DIR", tmp_path / "sfx"), \
             patch("orchestrator.services.elevenlabs_client._TTS_ASSET_DIR", tmp_path / "tts"), \
             patch("orchestrator.services.elevenlabs_client.get_settings") as ms:
            ms.return_value = MagicMock(elevenlabs_api_key="", media_proxy_url="http://proxy")
            from orchestrator.services.elevenlabs_client import ElevenLabsClient
            client = ElevenLabsClient()
        assert client.enabled is False

    def test_enabled_when_api_key_set(self, tmp_path):
        with patch("orchestrator.services.elevenlabs_client._ASSET_DIR", tmp_path / "sfx"), \
             patch("orchestrator.services.elevenlabs_client._TTS_ASSET_DIR", tmp_path / "tts"), \
             patch("orchestrator.services.elevenlabs_client.get_settings") as ms:
            ms.return_value = MagicMock(elevenlabs_api_key="sk-test", media_proxy_url="http://proxy")
            from orchestrator.services.elevenlabs_client import ElevenLabsClient
            client = ElevenLabsClient()
        assert client.enabled is True


class TestElevenLabsSFX:
    """generate_sfx() returns None when disabled or caches on success."""

    @pytest.mark.asyncio
    async def test_disabled_returns_none(self, tmp_path):
        with patch("orchestrator.services.elevenlabs_client._ASSET_DIR", tmp_path / "sfx"), \
             patch("orchestrator.services.elevenlabs_client._TTS_ASSET_DIR", tmp_path / "tts"), \
             patch("orchestrator.services.elevenlabs_client.get_settings") as ms:
            ms.return_value = MagicMock(elevenlabs_api_key="", media_proxy_url="http://proxy")
            from orchestrator.services.elevenlabs_client import ElevenLabsClient
            client = ElevenLabsClient()
            result = await client.generate_sfx("heavy door slam")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_cached_url_when_file_exists(self, tmp_path):
        sfx_dir = tmp_path / "sfx"
        sfx_dir.mkdir()
        text = "heavy door slam"
        cache_key = hashlib.sha256(text.encode()).hexdigest()[:24]
        (sfx_dir / f"{cache_key}.mp3").write_bytes(b"audio")

        with patch("orchestrator.services.elevenlabs_client._ASSET_DIR", sfx_dir), \
             patch("orchestrator.services.elevenlabs_client._TTS_ASSET_DIR", tmp_path / "tts"), \
             patch("orchestrator.services.elevenlabs_client.get_settings") as ms:
            ms.return_value = MagicMock(elevenlabs_api_key="sk-test", media_proxy_url="http://proxy")
            from orchestrator.services.elevenlabs_client import ElevenLabsClient
            client = ElevenLabsClient()
            result = await client.generate_sfx(text)

        assert result is not None
        assert cache_key in result
        assert result.endswith(".mp3")

    @pytest.mark.asyncio
    async def test_sfx_http_error_returns_none(self, tmp_path):
        import httpx
        sfx_dir = tmp_path / "sfx"
        sfx_dir.mkdir()

        with patch("orchestrator.services.elevenlabs_client._ASSET_DIR", sfx_dir), \
             patch("orchestrator.services.elevenlabs_client._TTS_ASSET_DIR", tmp_path / "tts"), \
             patch("orchestrator.services.elevenlabs_client.get_settings") as ms:
            ms.return_value = MagicMock(elevenlabs_api_key="sk-test", media_proxy_url="http://proxy")
            from orchestrator.services.elevenlabs_client import ElevenLabsClient
            client = ElevenLabsClient()

            mock_resp = MagicMock()
            mock_resp.status_code = 429
            client._client = AsyncMock()
            client._client.post = AsyncMock(
                side_effect=httpx.HTTPStatusError("rate limit", request=MagicMock(), response=mock_resp)
            )

            result = await client.generate_sfx("explosion sound")

        assert result is None

    @pytest.mark.asyncio
    async def test_sfx_writes_and_returns_url_on_success(self, tmp_path):
        sfx_dir = tmp_path / "sfx"
        sfx_dir.mkdir()
        text = "thunderclap"
        cache_key = hashlib.sha256(text.encode()).hexdigest()[:24]

        with patch("orchestrator.services.elevenlabs_client._ASSET_DIR", sfx_dir), \
             patch("orchestrator.services.elevenlabs_client._TTS_ASSET_DIR", tmp_path / "tts"), \
             patch("orchestrator.services.elevenlabs_client.get_settings") as ms:
            ms.return_value = MagicMock(elevenlabs_api_key="sk-test", media_proxy_url="http://proxy")
            from orchestrator.services.elevenlabs_client import ElevenLabsClient
            client = ElevenLabsClient()

            mock_resp = MagicMock()
            mock_resp.content = b"fake-mp3-data"
            mock_resp.raise_for_status = MagicMock()
            client._client = AsyncMock()
            client._client.post = AsyncMock(return_value=mock_resp)

            result = await client.generate_sfx(text)

        assert result is not None
        assert cache_key in result
        assert (sfx_dir / f"{cache_key}.mp3").exists()


class TestElevenLabsTTS:
    """generate_tts() returns None when disabled or caches on success."""

    @pytest.mark.asyncio
    async def test_disabled_returns_none(self, tmp_path):
        with patch("orchestrator.services.elevenlabs_client._ASSET_DIR", tmp_path / "sfx"), \
             patch("orchestrator.services.elevenlabs_client._TTS_ASSET_DIR", tmp_path / "tts"), \
             patch("orchestrator.services.elevenlabs_client.get_settings") as ms:
            ms.return_value = MagicMock(elevenlabs_api_key="", media_proxy_url="http://proxy")
            from orchestrator.services.elevenlabs_client import ElevenLabsClient
            client = ElevenLabsClient()
            result = await client.generate_tts("Hello world", "voice-123")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_cached_tts_url(self, tmp_path):
        tts_dir = tmp_path / "tts"
        tts_dir.mkdir()
        voice_id = "voice-abc"
        text = "Welcome to the tavern."
        cache_key = hashlib.sha256(f"{voice_id}:{text}".encode()).hexdigest()[:24]
        (tts_dir / f"{cache_key}.mp3").write_bytes(b"tts-audio")

        with patch("orchestrator.services.elevenlabs_client._ASSET_DIR", tmp_path / "sfx"), \
             patch("orchestrator.services.elevenlabs_client._TTS_ASSET_DIR", tts_dir), \
             patch("orchestrator.services.elevenlabs_client.get_settings") as ms:
            ms.return_value = MagicMock(elevenlabs_api_key="sk-test", media_proxy_url="http://proxy")
            from orchestrator.services.elevenlabs_client import ElevenLabsClient
            client = ElevenLabsClient()
            result = await client.generate_tts(text, voice_id)

        assert result is not None
        assert cache_key in result

    @pytest.mark.asyncio
    async def test_tts_http_error_returns_none(self, tmp_path):
        import httpx
        tts_dir = tmp_path / "tts"
        tts_dir.mkdir()

        with patch("orchestrator.services.elevenlabs_client._ASSET_DIR", tmp_path / "sfx"), \
             patch("orchestrator.services.elevenlabs_client._TTS_ASSET_DIR", tts_dir), \
             patch("orchestrator.services.elevenlabs_client.get_settings") as ms:
            ms.return_value = MagicMock(elevenlabs_api_key="sk-test", media_proxy_url="http://proxy")
            from orchestrator.services.elevenlabs_client import ElevenLabsClient
            client = ElevenLabsClient()

            mock_resp = MagicMock()
            mock_resp.status_code = 403
            client._client = AsyncMock()
            client._client.post = AsyncMock(
                side_effect=httpx.HTTPStatusError("forbidden", request=MagicMock(), response=mock_resp)
            )

            result = await client.generate_tts("Hello", "voice-bad")

        assert result is None
