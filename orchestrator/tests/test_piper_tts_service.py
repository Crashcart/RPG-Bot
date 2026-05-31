"""
Tests for PiperTTSService.

All external I/O (httpx, Redis, DB pool) is mocked — no live infrastructure needed.
Run with: pytest orchestrator/tests/test_piper_tts_service.py -v
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from orchestrator.services.piper_tts_service import (
    PiperTTSService,
    SpeakerSegment,
    NARRATOR_VOICE,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

CAMPAIGN = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _make_svc(
    redis_get: bytes | None = None,
    db_row: dict | None = None,
) -> PiperTTSService:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=redis_get)
    redis.setex = AsyncMock()
    db = MagicMock()
    db.pool = AsyncMock()
    db.pool.fetchrow = AsyncMock(return_value=db_row)
    db.pool.execute = AsyncMock()
    return PiperTTSService("http://piper:10200", redis, db)


# ── TestSpeakerSegment ────────────────────────────────────────────────────────

class TestSpeakerSegment:
    def test_narrator_is_narrator(self):
        seg = SpeakerSegment("Narrator", "The room is cold.")
        assert seg.is_narrator is True

    def test_gm_alias_is_narrator(self):
        seg = SpeakerSegment("GM", "You see a goblin.")
        assert seg.is_narrator is True

    def test_dm_alias_is_narrator(self):
        seg = SpeakerSegment("DM", "Roll for initiative.")
        assert seg.is_narrator is True

    def test_npc_is_not_narrator(self):
        seg = SpeakerSegment("Grib", '"Who goes there?!"')
        assert seg.is_narrator is False

    def test_case_insensitive_narrator(self):
        seg = SpeakerSegment("NARRATOR", "text")
        assert seg.is_narrator is True


# ── TestParseSegments ─────────────────────────────────────────────────────────

class TestParseSegments:
    def test_untagged_text_becomes_narrator(self):
        svc = _make_svc()
        segs = svc.parse_segments("The tavern is quiet.")
        assert len(segs) == 1
        assert segs[0].speaker == "Narrator"
        assert segs[0].text == "The tavern is quiet."

    def test_single_narrator_tag(self):
        svc = _make_svc()
        segs = svc.parse_segments("[Narrator]: The door creaks open.")
        assert len(segs) == 1
        assert segs[0].is_narrator is True
        assert "creaks" in segs[0].text

    def test_single_npc_tag(self):
        svc = _make_svc()
        segs = svc.parse_segments('[Grib]: "Who goes there?!"')
        assert len(segs) == 1
        assert segs[0].speaker == "Grib"
        assert segs[0].is_narrator is False

    def test_mixed_narrator_and_npc(self):
        svc = _make_svc()
        text = '[Narrator]: The tavern falls silent. [Barkeep]: "What\'ll it be?"'
        segs = svc.parse_segments(text)
        assert len(segs) == 2
        assert segs[0].is_narrator is True
        assert segs[1].speaker == "Barkeep"
        assert segs[1].is_narrator is False

    def test_three_segments(self):
        svc = _make_svc()
        text = "[Narrator]: A shadow falls. [Guard]: \"Halt!\" [Narrator]: The shadow moves."
        segs = svc.parse_segments(text)
        assert len(segs) == 3
        assert segs[0].is_narrator is True
        assert segs[1].speaker == "Guard"
        assert segs[2].is_narrator is True

    def test_multi_word_npc_name(self):
        svc = _make_svc()
        segs = svc.parse_segments('[Elder Voss]: "We must talk."')
        assert segs[0].speaker == "Elder Voss"


# ── TestChunkSentences ────────────────────────────────────────────────────────

class TestChunkSentences:
    def test_splits_on_period(self):
        svc = _make_svc()
        assert len(svc.chunk_sentences("One. Two. Three.")) == 3

    def test_splits_on_question_mark(self):
        svc = _make_svc()
        assert len(svc.chunk_sentences("Is it? Yes! Indeed.")) == 3

    def test_single_sentence_no_split(self):
        svc = _make_svc()
        assert svc.chunk_sentences("Just one sentence") == ["Just one sentence"]

    def test_empty_string_returns_empty_list(self):
        svc = _make_svc()
        assert svc.chunk_sentences("") == []


# ── TestGetNpcVoice ───────────────────────────────────────────────────────────

class TestGetNpcVoice:
    @pytest.mark.asyncio
    async def test_returns_cached_voice_without_db_call(self):
        svc = _make_svc(redis_get=b"en_US-ryan-high")
        voice = await svc.get_npc_voice("Grib", CAMPAIGN)
        assert voice == "en_US-ryan-high"
        svc._db.pool.fetchrow.assert_not_called()

    @pytest.mark.asyncio
    async def test_queries_db_on_cache_miss(self):
        svc = _make_svc(db_row={"voice_model_id": "en_US-joe-medium"})
        voice = await svc.get_npc_voice("Grib", CAMPAIGN)
        assert voice == "en_US-joe-medium"
        svc._db.pool.fetchrow.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_narrator_default_when_no_db_row(self):
        svc = _make_svc(db_row=None)
        voice = await svc.get_npc_voice("Unknown NPC", CAMPAIGN)
        assert voice == NARRATOR_VOICE

    @pytest.mark.asyncio
    async def test_db_exception_returns_narrator_default(self):
        svc = _make_svc()
        svc._db.pool.fetchrow = AsyncMock(side_effect=Exception("DB down"))
        voice = await svc.get_npc_voice("Grib", CAMPAIGN)
        assert voice == NARRATOR_VOICE

    @pytest.mark.asyncio
    async def test_result_is_cached_after_db_lookup(self):
        svc = _make_svc(db_row={"voice_model_id": "en_US-kusal-medium"})
        await svc.get_npc_voice("Grib", CAMPAIGN)
        svc._redis.setex.assert_awaited_once()
        call_args = svc._redis.setex.call_args[0]
        assert call_args[2] == "en_US-kusal-medium"


# ── TestSynthesise ────────────────────────────────────────────────────────────

class TestSynthesise:
    @pytest.mark.asyncio
    async def test_returns_cached_wav_without_http_call(self):
        fake_wav = b"RIFF\x00\x00\x00\x00WAVEfmt "
        svc = _make_svc(redis_get=fake_wav)
        result = await svc.synthesise("Hello world.", NARRATOR_VOICE)
        assert result == fake_wav

    @pytest.mark.asyncio
    async def test_calls_piper_on_cache_miss_and_caches_result(self):
        svc = _make_svc()
        fake_wav = b"RIFF\x00\x00\x00\x00WAVEfmt "
        mock_resp = MagicMock()
        mock_resp.content = fake_wav
        mock_resp.raise_for_status = MagicMock()
        with patch(
            "orchestrator.services.piper_tts_service.httpx.AsyncClient"
        ) as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(
                    post=AsyncMock(return_value=mock_resp)
                )
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await svc.synthesise("Hello world.", NARRATOR_VOICE)
        assert result == fake_wav
        svc._redis.setex.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_none_on_connect_error(self):
        svc = _make_svc()
        with patch(
            "orchestrator.services.piper_tts_service.httpx.AsyncClient"
        ) as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(
                    post=AsyncMock(side_effect=httpx.ConnectError("refused"))
                )
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await svc.synthesise("Hello.", NARRATOR_VOICE)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_text(self):
        svc = _make_svc()
        result = await svc.synthesise("   ", NARRATOR_VOICE)
        assert result is None


# ── TestBuildTtsCues ──────────────────────────────────────────────────────────

class TestBuildTtsCues:
    @pytest.mark.asyncio
    async def test_untagged_text_produces_narrator_cue(self):
        svc = _make_svc()
        cues = await svc.build_tts_cues("The hall echoes.", CAMPAIGN)
        assert len(cues) == 1
        assert cues[0]["entity_name"] == "Narrator"
        assert cues[0]["voice_id"] == NARRATOR_VOICE
        assert cues[0]["node_name"] == "piper-tts"

    @pytest.mark.asyncio
    async def test_npc_cue_resolves_db_voice(self):
        svc = _make_svc(db_row={"voice_model_id": "en_US-joe-medium"})
        cues = await svc.build_tts_cues('[Guard]: "Halt!"', CAMPAIGN)
        assert cues[0]["voice_id"] == "en_US-joe-medium"
        assert cues[0]["entity_name"] == "Guard"

    @pytest.mark.asyncio
    async def test_mixed_cues_correct_voices(self):
        svc = _make_svc(db_row={"voice_model_id": "en_US-ryan-high"})
        text = "[Narrator]: Silence falls. [Captain]: \"Stand down!\""
        cues = await svc.build_tts_cues(text, CAMPAIGN)
        assert len(cues) == 2
        assert cues[0]["voice_id"] == NARRATOR_VOICE
        assert cues[1]["voice_id"] == "en_US-ryan-high"

    @pytest.mark.asyncio
    async def test_all_cues_have_required_keys(self):
        svc = _make_svc()
        cues = await svc.build_tts_cues("[GM]: The mist thickens.", CAMPAIGN)
        for cue in cues:
            assert "entity_name" in cue
            assert "text" in cue
            assert "voice_id" in cue
            assert "node_name" in cue
