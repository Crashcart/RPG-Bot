"""
Unit tests for OpenAICompatClient and SystemIntegrityCheck (SIC).
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.openai_compat_client import OpenAICompatClient
from orchestrator.services.sic import (
    PillarResult,
    SICResult,
    SystemIntegrityCheck,
    _permission_probe,
    _sqlite_integrity_check,
)


# ─────────────────────────────────────────────────────────────────────────────
# OpenAICompatClient tests
# ─────────────────────────────────────────────────────────────────────────────


def _patch_settings(**overrides):
    defaults = dict(
        groq_api_key="gk-test",
        groq_model="llama-3.3-70b-versatile",
        openrouter_api_key="or-test",
        openrouter_model="meta-llama/llama-3.3-70b",
        together_api_key="tg-test",
        together_model="meta-llama/Llama-3.3-70B",
        sillytavern_url="http://silly:8000/api/openai/v1",
        sillytavern_model="",
        sillytavern_api_key="",
    )
    defaults.update(overrides)
    s = MagicMock(**defaults)
    return s


class TestOpenAICompatClientInit:
    """Constructor validation and provider configuration."""

    def test_groq_provider_initialises(self):
        with patch("orchestrator.services.openai_compat_client.settings", _patch_settings()):
            client = OpenAICompatClient("groq")
        assert client.provider == "groq"
        assert client.model == "llama-3.3-70b-versatile"

    def test_openrouter_provider_initialises(self):
        with patch("orchestrator.services.openai_compat_client.settings", _patch_settings()):
            client = OpenAICompatClient("openrouter")
        assert client.provider == "openrouter"

    def test_together_provider_initialises(self):
        with patch("orchestrator.services.openai_compat_client.settings", _patch_settings()):
            client = OpenAICompatClient("together")
        assert client.provider == "together"

    def test_sillytavern_provider_uses_configured_url(self):
        with patch("orchestrator.services.openai_compat_client.settings",
                   _patch_settings(sillytavern_url="http://silly:8000/api/openai/v1")):
            client = OpenAICompatClient("sillytavern")
        assert client.provider == "sillytavern"

    def test_sillytavern_without_url_raises(self):
        with patch("orchestrator.services.openai_compat_client.settings",
                   _patch_settings(sillytavern_url="")):
            with pytest.raises(ValueError, match="SillyTavern URL"):
                OpenAICompatClient("sillytavern")

    def test_unknown_provider_raises(self):
        with patch("orchestrator.services.openai_compat_client.settings", _patch_settings()):
            with pytest.raises(ValueError, match="Unknown provider"):
                OpenAICompatClient("gpt99")

    def test_is_available_true_when_api_key_set(self):
        with patch("orchestrator.services.openai_compat_client.settings", _patch_settings()):
            client = OpenAICompatClient("groq")
        assert client.is_available() is True

    def test_is_available_false_when_api_key_empty(self):
        with patch("orchestrator.services.openai_compat_client.settings",
                   _patch_settings(groq_api_key="")):
            with patch("orchestrator.services.openai_compat_client._PROVIDER_API_KEYS",
                       {"groq": "", "openrouter": "or", "together": "tg"}):
                client = OpenAICompatClient("groq")
        assert client.is_available() is False

    def test_sillytavern_is_available_when_url_set(self):
        with patch("orchestrator.services.openai_compat_client.settings",
                   _patch_settings(sillytavern_url="http://silly:8000")):
            client = OpenAICompatClient("sillytavern")
        assert client.is_available() is True

    def test_sillytavern_model_is_empty_string_when_not_set(self):
        with patch("orchestrator.services.openai_compat_client.settings",
                   _patch_settings(sillytavern_url="http://silly", sillytavern_model="")):
            client = OpenAICompatClient("sillytavern")
        assert client.model == ""


class TestOpenAICompatBuildPayload:
    """_build_payload() constructs the correct request body."""

    def test_includes_model_for_groq(self):
        with patch("orchestrator.services.openai_compat_client.settings", _patch_settings()):
            client = OpenAICompatClient("groq")
        payload = client._build_payload(
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            temperature=0.5,
        )
        assert "model" in payload
        assert payload["model"] == "llama-3.3-70b-versatile"
        assert payload["max_tokens"] == 100
        assert payload["temperature"] == 0.5

    def test_omits_model_for_sillytavern_when_not_set(self):
        with patch("orchestrator.services.openai_compat_client.settings",
                   _patch_settings(sillytavern_url="http://silly", sillytavern_model="")):
            client = OpenAICompatClient("sillytavern")
        payload = client._build_payload(
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=50,
            temperature=0.7,
        )
        assert "model" not in payload

    def test_includes_model_for_sillytavern_when_set(self):
        with patch("orchestrator.services.openai_compat_client.settings",
                   _patch_settings(sillytavern_url="http://silly", sillytavern_model="my-model")):
            client = OpenAICompatClient("sillytavern")
        payload = client._build_payload(
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=50,
            temperature=0.7,
        )
        assert payload.get("model") == "my-model"


class TestOpenAICompatGenerate:
    """generate() calls /chat/completions and returns text."""

    @pytest.mark.asyncio
    async def test_generate_returns_completion_text(self):
        with patch("orchestrator.services.openai_compat_client.settings", _patch_settings()):
            client = OpenAICompatClient("groq")

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "The goblin attacks!"}}]
        }
        client._client = AsyncMock()
        client._client.post = AsyncMock(return_value=mock_resp)

        result = await client.generate("system prompt", "user prompt")
        assert result == "The goblin attacks!"

    @pytest.mark.asyncio
    async def test_generate_passes_system_and_user_messages(self):
        with patch("orchestrator.services.openai_compat_client.settings", _patch_settings()):
            client = OpenAICompatClient("groq")

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        client._client = AsyncMock()
        client._client.post = AsyncMock(return_value=mock_resp)

        await client.generate("You are a GM.", "The player attacks.")
        payload = client._client.post.call_args.kwargs.get("json") or \
                  client._client.post.call_args.args[1]
        messages = payload["messages"]
        roles = [m["role"] for m in messages]
        assert "system" in roles
        assert "user" in roles


# ─────────────────────────────────────────────────────────────────────────────
# SystemIntegrityCheck tests
# ─────────────────────────────────────────────────────────────────────────────


def _make_sic(data_dir: Path, backups_dir: Path | None = None) -> SystemIntegrityCheck:
    return SystemIntegrityCheck(
        data_dir=str(data_dir),
        backups_dir=str(backups_dir or data_dir / "backups"),
        ollama_host="http://brain:11434",
    )


def _create_valid_sqlite(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE x (id INTEGER PRIMARY KEY)")
        conn.commit()


class TestSICPathValidation:
    """Pillar 1: path_validation."""

    @pytest.mark.asyncio
    async def test_missing_vault_db_is_critical_fail(self, tmp_path):
        sic = _make_sic(tmp_path)
        result = await sic._check_paths()
        assert result.passed is False
        assert result.critical is True
        assert "scribe_core.db" in result.message.lower() or "Reality Anchor" in result.message

    @pytest.mark.asyncio
    async def test_vault_db_present_missing_asset_dirs_is_noncritical(self, tmp_path):
        _create_valid_sqlite(tmp_path / "vault" / "scribe_core.db")
        sic = _make_sic(tmp_path)
        result = await sic._check_paths()
        assert result.passed is False
        assert result.critical is False
        assert "non-critical" in result.message.lower()

    @pytest.mark.asyncio
    async def test_all_present_returns_pass(self, tmp_path):
        _create_valid_sqlite(tmp_path / "vault" / "scribe_core.db")
        for d in ("fonts", "templates", "handouts"):
            (tmp_path / d).mkdir()
        sic = _make_sic(tmp_path)
        result = await sic._check_paths()
        assert result.passed is True
        assert result.critical is True


class TestSICDatabaseHealth:
    """Pillar 2: db_health."""

    @pytest.mark.asyncio
    async def test_missing_db_returns_critical_fail(self, tmp_path):
        sic = _make_sic(tmp_path)
        result = await sic._check_database()
        assert result.passed is False
        assert result.critical is True

    @pytest.mark.asyncio
    async def test_healthy_db_returns_pass(self, tmp_path):
        _create_valid_sqlite(tmp_path / "vault" / "scribe_core.db")
        sic = _make_sic(tmp_path)
        result = await sic._check_database()
        assert result.passed is True
        assert "ok" in result.message.lower()


class TestSICGpuPassthrough:
    """Pillar 3: gpu_passthrough (non-critical)."""

    @pytest.mark.asyncio
    async def test_unreachable_brain_returns_warning(self, tmp_path):
        sic = _make_sic(tmp_path)
        import httpx
        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            instance.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            result = await sic._check_gpu()

        assert result.passed is False
        assert result.critical is False
        assert "Warning" in result.message or "unreachable" in result.message.lower()

    @pytest.mark.asyncio
    async def test_vram_detected_returns_pass(self, tmp_path):
        sic = _make_sic(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"models": [{"name": "mistral", "size_vram": 4_000_000_000}]}

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            instance.get = AsyncMock(return_value=mock_resp)
            result = await sic._check_gpu()

        assert result.passed is True
        assert result.critical is False
        assert "GPU" in result.message

    @pytest.mark.asyncio
    async def test_brain_online_no_vram_returns_cpu_warning(self, tmp_path):
        sic = _make_sic(tmp_path)
        ps_resp = MagicMock()
        ps_resp.status_code = 200
        ps_resp.json.return_value = {"models": []}  # no VRAM
        tags_resp = MagicMock()
        tags_resp.status_code = 200
        tags_resp.json.return_value = {"models": [{"name": "mistral"}]}

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            instance.get = AsyncMock(side_effect=[ps_resp, tags_resp])
            result = await sic._check_gpu()

        assert result.passed is False
        assert result.critical is False
        assert "CPU" in result.message or "VRAM" in result.message


class TestSICPermissions:
    """Pillar 4: permission_parity."""

    @pytest.mark.asyncio
    async def test_writable_dirs_return_pass(self, tmp_path):
        data_dir = tmp_path / "data"
        backups_dir = tmp_path / "backups"
        (data_dir / "handouts").mkdir(parents=True)
        backups_dir.mkdir()
        sic = SystemIntegrityCheck(str(data_dir), str(backups_dir))
        result = await sic._check_permissions()
        assert result.passed is True
        assert result.critical is True

    @pytest.mark.asyncio
    async def test_permission_error_returns_critical_fail(self, tmp_path):
        sic = _make_sic(tmp_path)

        def raise_permission(_dir):
            raise PermissionError("read-only filesystem")

        with patch("orchestrator.services.sic._permission_probe", raise_permission):
            result = await sic._check_permissions()

        assert result.passed is False
        assert result.critical is True
        assert "Lockout" in result.message or "failed" in result.message.lower()


class TestSICFullRun:
    """run() aggregates pillar results into overall status."""

    @pytest.mark.asyncio
    async def test_all_pass_gives_healthy_status(self, tmp_path):
        data_dir = tmp_path / "data"
        backups_dir = tmp_path / "backups"
        _create_valid_sqlite(data_dir / "vault" / "scribe_core.db")
        for d in ("fonts", "templates", "handouts"):
            (data_dir / d).mkdir(parents=True)
        backups_dir.mkdir()

        sic = SystemIntegrityCheck(str(data_dir), str(backups_dir), ollama_host="http://brain")

        with patch.object(sic, "_check_gpu", AsyncMock(
            return_value=PillarResult("gpu_passthrough", True, False, "GPU OK")
        )):
            result = await sic.run()

        assert isinstance(result, SICResult)
        assert result.status == "healthy"
        assert len(result.pillars) == 4

    @pytest.mark.asyncio
    async def test_critical_pillar_fail_gives_critical_status(self, tmp_path):
        sic = _make_sic(tmp_path)  # no vault DB → path_validation fails
        with patch.object(sic, "_check_gpu", AsyncMock(
            return_value=PillarResult("gpu_passthrough", False, False, "Warning")
        )):
            with patch.object(sic, "_check_permissions", AsyncMock(
                return_value=PillarResult("permission_parity", True, True, "OK")
            )):
                result = await sic.run()

        assert result.status == "critical"

    @pytest.mark.asyncio
    async def test_only_warning_fails_gives_unstable_status(self, tmp_path):
        data_dir = tmp_path / "data"
        backups_dir = tmp_path / "backups"
        _create_valid_sqlite(data_dir / "vault" / "scribe_core.db")
        for d in ("fonts", "templates", "handouts"):
            (data_dir / d).mkdir(parents=True)
        backups_dir.mkdir()

        sic = SystemIntegrityCheck(str(data_dir), str(backups_dir))

        with patch.object(sic, "_check_gpu", AsyncMock(
            return_value=PillarResult("gpu_passthrough", False, False, "CPU fallback")
        )):
            result = await sic.run()

        assert result.status == "unstable"

    @pytest.mark.asyncio
    async def test_pillar_exception_is_captured_not_raised(self, tmp_path):
        sic = _make_sic(tmp_path)

        with patch.object(sic, "_check_paths", AsyncMock(side_effect=RuntimeError("crash"))):
            with patch.object(sic, "_check_database", AsyncMock(
                return_value=PillarResult("db_health", True, True, "ok")
            )):
                with patch.object(sic, "_check_gpu", AsyncMock(
                    return_value=PillarResult("gpu_passthrough", True, False, "ok")
                )):
                    with patch.object(sic, "_check_permissions", AsyncMock(
                        return_value=PillarResult("permission_parity", True, True, "ok")
                    )):
                        result = await sic.run()

        assert result.status == "critical"  # unhandled exception in critical pillar
        path_pillar = next(p for p in result.pillars if p.name == "path_validation")
        assert path_pillar.passed is False

    @pytest.mark.asyncio
    async def test_to_dict_includes_all_fields(self, tmp_path):
        sic = _make_sic(tmp_path)
        with patch.object(sic, "_check_paths", AsyncMock(
            return_value=PillarResult("path_validation", True, True, "ok")
        )):
            with patch.object(sic, "_check_database", AsyncMock(
                return_value=PillarResult("db_health", True, True, "ok")
            )):
                with patch.object(sic, "_check_gpu", AsyncMock(
                    return_value=PillarResult("gpu_passthrough", True, False, "ok")
                )):
                    with patch.object(sic, "_check_permissions", AsyncMock(
                        return_value=PillarResult("permission_parity", True, True, "ok")
                    )):
                        result = await sic.run()

        d = result.to_dict()
        assert "status" in d
        assert "checked_at" in d
        assert "pillars" in d
        assert len(d["pillars"]) == 4
        for p in d["pillars"]:
            assert {"name", "passed", "critical", "message", "detail"} == set(p.keys())


class TestSICHelpers:
    """Thread-safe helper functions."""

    def test_sqlite_integrity_check_returns_ok(self, tmp_path):
        db_path = tmp_path / "test.db"
        _create_valid_sqlite(db_path)
        result = _sqlite_integrity_check(db_path)
        assert result == "ok"

    def test_permission_probe_creates_and_deletes_sentinel(self, tmp_path):
        target = tmp_path / "testdir"
        target.mkdir()
        _permission_probe(target)
        assert not (target / ".sic_probe").exists()

    def test_permission_probe_creates_dir_if_missing(self, tmp_path):
        target = tmp_path / "newdir"
        _permission_probe(target)
        assert target.exists()
        assert not (target / ".sic_probe").exists()
